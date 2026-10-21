"""Client for the myDPD *web account* (Germany).

DPD switched off its public tracking page (tracking.dpd.de) in April 2026 and
the Android app's SOAP API now demands a Google Play Integrity attestation
(``AppToken``) that nothing but the genuine app can produce. What still works
for a private user is the same thing the website does: sign in with the myDPD
account, open the parcel list and read it. This module does exactly that,
with the user's own credentials; it never solves or bypasses a CAPTCHA.

The account spans three hosts and lives on cookies, so the client needs its
own ``ClientSession`` (own cookie jar):

1. ``POST`` the login form on dpd.com -> redirect to portal.dpd.de with a
   one-time ``dpd_token``;
2. open the portal dashboard and "hop" (an ASP.NET WebForms postback) to the
   parcel list on my.dpd.de - without the hop the list renders as an empty
   inbox instead of asking for a login;
3. read the rows of the Empfang / Versand / Retoure tabs and, per row, the
   detail panel with DPD's five milestones.

If DPD asks for a CAPTCHA at login, no further login is attempted for a few
hours (it only gets worse when retried); the integration keeps its last data.

The page ids, the login form and the request chain were worked out by
https://github.com/ha-parcel-integrations/ha-dpd (MIT licence), whose German
transport is the reference this module follows.
"""
from __future__ import annotations

import asyncio
import html as html_lib
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

from aiohttp import ClientError, ClientSession, ClientTimeout

from .const import (
    DPD_INBOX_URL,
    DPD_LOGIN_URL,
    DPD_PORTAL_HOME_URL,
    DPD_WEB_USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = ClientTimeout(total=30)
_MAX_REDIRECTS = 8
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_CAPTCHA_BACKOFF_SECONDS = 6 * 60 * 60
_UNOPENED_BACKOFF_SECONDS = 60 * 60


class DpdApiError(Exception):
    """Generic error talking to the DPD web account."""


class DpdAuthError(DpdApiError):
    """DPD rejected the user name / password."""


class DpdCaptchaError(DpdApiError):
    """DPD asks for a CAPTCHA at login, which cannot be answered here."""


class DpdPageError(DpdApiError):
    """A page lacks something every page of its kind carries."""


# --------------------------------------------------------------------------
# Page reading - everything is read by server-control id, never by position.
# --------------------------------------------------------------------------

# my.dpd.de: a control that only exists for a signed-in user.
_LOGGED_IN_MARKER = 'id="modBurgerNavi_btnLogout"'
# portal.dpd.de: the dashboard control that performs the hop.
_PORTAL_HOST = "portal.dpd.de"
_PORTAL_LOGGED_IN_MARKER = "btnHomeMyParcel"
_HOP_TARGET = "ctl00$cphBody$ctl00$btnHomeMyParcel"

_PREFIX = "ContentPlaceHolder1_"
_ROW_PREFIX = f"{_PREFIX}repParcelList_"
_ROW_TARGET = "ctl00$ContentPlaceHolder1$repParcelList$ctl{index:02d}$btnSelectParcel"
# tab -> (postback target, direction of the parcels on it)
_TABS = {
    "incoming": ("ctl00$ContentPlaceHolder1$btnGetIncomingParcel", "receive"),
    "sending": ("ctl00$ContentPlaceHolder1$btnGetSendingParcel", "send"),
    "returning": ("ctl00$ContentPlaceHolder1$btnGetReturningParcel", "return"),
}
# The five milestones of the detail panel, in order.
SLOTS = ("Start", "OnTheRoad", "DeliveryDepot", "CarLoad", "Delivered")
# milestone -> StatusID understood by const.DPD_STATUS_GROUP
_SLOT_STATUS_ID = {
    "Start": "START",
    "OnTheRoad": "ON_THE_ROAD",
    "DeliveryDepot": "AT_DELIVERY_DEPOT",
    "CarLoad": "OUT_FOR_DELIVERY",
    "Delivered": "DELIVERED",
}
_ICON_DELIVERED = 6  # images/status_6.svg

_FORM_TAG = re.compile(r"<form\b[^>]*>", re.IGNORECASE)
_SITEKEY = re.compile(r'data-sitekey\s*=\s*"[^"]+"', re.IGNORECASE)
_RECAPTCHA_SCRIPT = re.compile(r"recaptcha/(?:api|enterprise)\.js", re.IGNORECASE)
_INLINE_MESSAGE = re.compile(
    r'<(\w+)\b[^>]*\bclass="[^"]*\bdgs-inline-message\b[^"]*"[^>]*>(.*?)</\1>',
    re.IGNORECASE | re.DOTALL,
)
_INPUT = re.compile(r"<input\b([^>]*)>", re.IGNORECASE | re.DOTALL)
_ATTR = re.compile(r"""([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_SKIPPED_INPUT_TYPES = frozenset(
    {"submit", "button", "image", "checkbox", "radio", "password"}
)
_REQUIRED_STATE = ("__VIEWSTATE", "__EVENTVALIDATION")
_OPENING_TAG = re.compile(r"<([A-Za-z][\w:-]*)\b([^>]*)>")
_VOID_TAGS = frozenset({"input", "img", "br", "hr", "meta", "link"})
_STRIP_TAGS = re.compile(r"<[^>]*>")
_WHITESPACE = re.compile(r"\s+")
_ICON = re.compile(r"status_(\d+)\.svg")
_ERROR_PAGE = re.compile(r"/error\.aspx|showerror", re.IGNORECASE)
_ERROR_TITLE = re.compile(r"<title>[^<]*runtime error", re.IGNORECASE)
_DAY_MONTH = re.compile(r"^(\d{1,2})\.(\d{1,2})\.$")
# A day shown without a year is a past event; more than this many days in the
# future it can only belong to the previous year.
_FUTURE_TOLERANCE_DAYS = 7


def _text(fragment: str) -> str:
    """Tag-free, unescaped, whitespace-collapsed text of an HTML fragment."""
    stripped = _STRIP_TAGS.sub(" ", fragment)
    return _WHITESPACE.sub(" ", html_lib.unescape(stripped)).strip()


def _attrs(raw: str) -> dict[str, str]:
    return {
        match.group(1).lower(): html_lib.unescape(
            match.group(2) if match.group(2) is not None else match.group(3)
        )
        for match in _ATTR.finditer(raw)
    }


@dataclass(frozen=True)
class _Control:
    attrs: dict[str, str]
    text: str


class _Page:
    """A fetched page, indexed by element id."""

    def __init__(self, html: str) -> None:
        self._controls: dict[str, _Control] = {}
        lowered = html.lower()
        for match in _OPENING_TAG.finditer(html):
            attrs = _attrs(match.group(2))
            element_id = attrs.get("id")
            if not element_id or element_id in self._controls:
                continue
            tag = match.group(1).lower()
            if tag in _VOID_TAGS:
                inner = ""
            else:
                end = lowered.find(f"</{tag}", match.end())
                inner = html[match.end() : end if end != -1 else match.end()]
            self._controls[element_id] = _Control(attrs, _text(inner))

    def control(self, element_id: str) -> _Control | None:
        return self._controls.get(element_id)

    def text(self, element_id: str) -> str | None:
        control = self._controls.get(element_id)
        return control.text if control else None

    def value(self, element_id: str) -> str | None:
        control = self._controls.get(element_id)
        return control.attrs.get("value", "") if control else None

    def counter(self, tab: str) -> int | None:
        """Parcel count shown on a tab, ``None`` when unreadable."""
        text = self.text(f"{_PREFIX}counterGet{tab.capitalize()}Parcel")
        return int(text) if text is not None and text.isdigit() else None


def is_logged_in(html: str) -> bool:
    """Whether a my.dpd.de page belongs to a signed-in session.

    Never decided from the parcel count: a signed-out page renders the same
    empty-inbox text as an empty account.
    """
    return _LOGGED_IN_MARKER in html


def is_portal_logged_in(url: str, html: str) -> bool:
    """Whether the page fetched from the portal belongs to a signed-in session.

    A signed-out portal redirects to the public login page, so the final host
    matters as much as the control.
    """
    return urlparse(url).hostname == _PORTAL_HOST and _PORTAL_LOGGED_IN_MARKER in html


def has_captcha(html: str) -> bool:
    """Whether a login page or login response asks for a CAPTCHA.

    The form-class marker is matched on the ``<form>`` tag only: the same
    string sits in an inline style rule of the pristine page.
    """
    for tag in _FORM_TAG.findall(html):
        if "german-sso-login-form" in tag and "dgs-form-has-captcha" in tag:
            return True
    return bool(_SITEKEY.search(html) or _RECAPTCHA_SCRIPT.search(html))


def login_rejected(html: str) -> bool:
    """Whether the login form came back with a non-empty error message."""
    if 'name="dpg_username"' not in html:
        return False
    return any(_text(inner) for _tag, inner in _INLINE_MESSAGE.findall(html))


def form_fields(html: str) -> dict[str, str]:
    """Every input of a page, to be echoed back by the next postback."""
    fields: dict[str, str] = {}
    for match in _INPUT.finditer(html):
        attrs = _attrs(match.group(1))
        name = attrs.get("name")
        if not name or attrs.get("type", "text").lower() in _SKIPPED_INPUT_TYPES:
            continue
        fields[name] = attrs.get("value", "")
    for required in _REQUIRED_STATE:
        if required not in fields:
            raise DpdPageError(f"DPD page has no {required}")
    return fields


def _icon(control: _Control | None) -> int | None:
    match = _ICON.search(control.attrs.get("src", "")) if control else None
    return int(match.group(1)) if match else None


def list_rows(page: _Page) -> list[dict[str, Any]]:
    """The parcel rows of the current tab (numbered from 0, no fixed cap)."""
    rows: list[dict[str, Any]] = []
    index = 0
    while (number := page.control(f"{_ROW_PREFIX}labParcelNo_{index}")) is not None:
        if number.text:
            rows.append(
                {
                    "index": index,
                    "barcode": number.text,
                    "label": page.text(f"{_ROW_PREFIX}labParcelName_{index}") or "",
                    "status_sentence": page.text(
                        f"{_ROW_PREFIX}labDeliveryStatus_{index}"
                    )
                    or "",
                    "status_icon": _icon(
                        page.control(f"{_ROW_PREFIX}imgDeliverStatus_{index}")
                    ),
                }
            )
        index += 1
    return rows


def read_detail(page: _Page) -> dict[str, Any] | None:
    """The selected parcel's detail panel, or ``None`` without one.

    The recipient postcode (``hidZipCode``) and all address blocks are
    deliberately not read.
    """
    identity = page.value(f"{_PREFIX}hidParcelNo")
    if identity is None:
        return None
    slots = [
        {
            "slot": slot,
            "text": page.text(f"{_PREFIX}labStatus{slot}") or "",
            "date": page.text(f"{_PREFIX}labStatus{slot}Date") or "",
        }
        for slot in SLOTS
    ]
    return {
        "identity": identity,
        "slots": slots,
        "status_icon": _icon(page.control(f"{_PREFIX}imgParcelStatus")),
    }


# --------------------------------------------------------------------------
# Turning rows + panels into the parcel dicts the coordinator uses
# --------------------------------------------------------------------------


def _local_today() -> date:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Europe/Berlin")).date()
    except Exception:  # noqa: BLE001 - no tz database: UTC is close enough
        return datetime.now(timezone.utc).date()


def infer_dates(slots: list[dict[str, str]], today: date) -> list[date | None]:
    """Give each milestone day (``DD.MM.``, no year) its year, in slot order.

    A day more than a week after today belongs to the previous year, and a
    later milestone never falls before an earlier one.
    """
    inferred: list[date | None] = []
    previous: date | None = None
    for slot in slots:
        match = _DAY_MONTH.match(slot["date"].strip())
        if match is None:
            inferred.append(None)
            continue
        day, month = int(match.group(1)), int(match.group(2))
        try:
            moment = date(today.year, month, day)
            if moment > today + timedelta(days=_FUTURE_TOLERANCE_DAYS):
                moment = date(today.year - 1, month, day)
            if previous is not None and moment < previous:
                moment = date(moment.year + 1, month, day)
        except ValueError:  # 29 February in a non-leap year
            inferred.append(None)
            continue
        inferred.append(moment)
        previous = moment
    return inferred


def _status_id_from_sentence(sentence: str) -> str | None:
    """Last-resort status when a parcel has no dated milestone."""
    text = sentence.casefold()
    if "nicht zugestellt" in text:
        return None
    if any(w in text for w in ("paketshop", "abhol", "packstation")):
        return "AT_PARCELSHOP"
    if any(w in text for w in ("in zustellung", "zustellfahrzeug", "heute zugestellt")):
        return "OUT_FOR_DELIVERY"
    if "zugestellt" in text or "geliefert" in text:
        return "DELIVERED"
    return None


def build_parcel(
    row: dict[str, Any],
    detail: dict[str, Any] | None,
    direction: str,
    today: date,
) -> dict[str, Any]:
    """One parcel dict: id, name, status, status_id, delivered, direction, events."""
    slots = detail["slots"] if detail else []
    events: list[dict[str, str]] = []
    furthest: str | None = None
    furthest_text = ""
    for slot, moment in zip(slots, infer_dates(slots, today)):
        if moment is None:
            continue
        furthest, furthest_text = slot["slot"], slot["text"]
        events.append(
            {"datum": moment.isoformat(), "status": slot["text"] or slot["slot"]}
        )
    events.reverse()  # newest first, like every other carrier

    icon = row["status_icon"]
    if icon is None and detail:
        icon = detail["status_icon"]
    if furthest:
        status_id = _SLOT_STATUS_ID[furthest]
    elif icon == _ICON_DELIVERED:
        status_id = "DELIVERED"
    else:
        status_id = _status_id_from_sentence(row["status_sentence"])
    return {
        "id": row["barcode"],
        "name": row["label"] or row["barcode"],
        "status": row["status_sentence"] or furthest_text,
        "status_id": status_id,
        "delivered": status_id == "DELIVERED",
        "direction": direction,
        "events": events,
    }


def _allowed_host(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return host == "dpd.com" or host.endswith((".dpd.com", ".dpd.de"))


# --------------------------------------------------------------------------
# The client
# --------------------------------------------------------------------------

# Back-offs describe the account's recent login history, so they outlive the
# client object (a reload builds a new one). Keyed by lower-cased user name.
_CAPTCHA_UNTIL: dict[str, float] = {}
_UNOPENED_UNTIL: dict[str, float] = {}


class DpdApiClient:
    """Reads the parcel list of one myDPD account.

    ``session`` must be a ``ClientSession`` with its **own cookie jar**
    (``async_create_clientsession``), never the shared Home Assistant one.
    """

    def __init__(self, session: ClientSession, username: str, password: str) -> None:
        self._session = session
        self._username = username
        self._password = password
        self._lock = asyncio.Lock()

    # -- transport ---------------------------------------------------------

    async def _fetch_page(
        self,
        method: str,
        url: str,
        *,
        data: dict[str, str] | None = None,
        referer: str | None = None,
    ) -> tuple[str, str]:
        """Request a page, following redirects by hand: ``(final_url, html)``."""
        headers = {
            "User-Agent": DPD_WEB_USER_AGENT,
            "Accept-Language": "de-DE,de;q=0.9",
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        }
        if referer:
            parsed = urlparse(referer)
            headers["Referer"] = referer
            headers["Origin"] = f"{parsed.scheme}://{parsed.netloc}"
        for _ in range(_MAX_REDIRECTS):
            if not _allowed_host(url):
                raise DpdApiError(f"DPD redirected to an unexpected host: {url}")
            if _ERROR_PAGE.search(url):
                raise DpdApiError("DPD answered with an error page")
            try:
                async with self._session.request(
                    method,
                    url,
                    data=data,
                    headers=headers,
                    allow_redirects=False,
                    timeout=REQUEST_TIMEOUT,
                ) as response:
                    status = response.status
                    if status in _REDIRECT_STATUSES:
                        location = response.headers.get("Location")
                        if not location:
                            raise DpdApiError(f"DPD redirect without target ({status})")
                        url = urljoin(url, location)
                        method, data = "GET", None
                        headers = {
                            k: v
                            for k, v in headers.items()
                            if k not in ("Referer", "Origin")
                        }
                        continue
                    if status != 200:
                        raise DpdApiError(f"DPD returned HTTP {status}")
                    if "html" not in (response.content_type or ""):
                        raise DpdApiError("DPD returned a non-HTML page")
                    body = await response.text(errors="replace")
            except (ClientError, TimeoutError) as err:
                raise DpdApiError(f"Network error contacting DPD: {err}") from err
            if _ERROR_TITLE.search(body):
                raise DpdApiError("DPD showed a runtime error page")
            return url, body
        raise DpdApiError("DPD redirected too often")

    async def _fetch(self, *args: Any, **kwargs: Any) -> str:
        return (await self._fetch_page(*args, **kwargs))[1]

    async def _postback(
        self, url: str, html: str, target: str, *, referer: str | None = None
    ) -> str:
        """Post one WebForms event carrying the form state of ``html``."""
        fields = form_fields(html)
        fields["__EVENTTARGET"] = target
        fields["__EVENTARGUMENT"] = ""
        return await self._fetch("POST", url, data=fields, referer=referer or url)

    # -- login ladder ------------------------------------------------------

    def _check_backoff(self) -> None:
        key, now = self._username.lower(), time.monotonic()
        if _CAPTCHA_UNTIL.get(key, 0.0) > now:
            raise DpdCaptchaError(
                "DPD asks for a CAPTCHA at login; waiting before retrying"
            )
        if _UNOPENED_UNTIL.get(key, 0.0) > now:
            raise DpdApiError(
                "DPD login did not open the parcel list; waiting before retrying"
            )

    async def _full_login(self, *, respect_backoff: bool) -> str:
        """Sign in and return the portal dashboard page."""
        if respect_backoff:
            self._check_backoff()
        try:
            async with self._session.request(
                "POST",
                DPD_LOGIN_URL,
                data={"dpg_username": self._username, "dpg_password": self._password},
                headers={
                    "User-Agent": DPD_WEB_USER_AGENT,
                    "Accept-Language": "de-DE,de;q=0.9",
                    "Origin": "https://www.dpd.com",
                    "Referer": DPD_LOGIN_URL,
                },
                allow_redirects=False,
                timeout=REQUEST_TIMEOUT,
            ) as response:
                status = response.status
                location = response.headers.get("Location", "")
                body = await response.text(errors="replace") if status == 200 else ""
        except (ClientError, TimeoutError) as err:
            raise DpdApiError(f"Network error contacting DPD: {err}") from err

        if status in _REDIRECT_STATUSES:
            target = urljoin(DPD_LOGIN_URL, location)
            if urlparse(target).hostname == _PORTAL_HOST and "dpd_token=" in target:
                return await self._fetch("GET", target)
            raise DpdApiError(f"DPD login redirected unexpectedly ({status})")
        if status != 200:
            raise DpdApiError(f"DPD login returned HTTP {status}")
        if has_captcha(body):
            _CAPTCHA_UNTIL[self._username.lower()] = (
                time.monotonic() + _CAPTCHA_BACKOFF_SECONDS
            )
            _LOGGER.warning(
                "DPD asks for a CAPTCHA at login, which Home Assistant cannot "
                "answer. Further sign-in attempts are paused for six hours; "
                "signing in once at dpd.com in a browser may clear the CAPTCHA."
            )
            raise DpdCaptchaError("DPD asks for a CAPTCHA at login")
        if login_rejected(body):
            raise DpdAuthError("DPD rejected the login")
        raise DpdApiError("DPD login answered with an unrecognised page")

    async def _hop(self, portal_html: str) -> str:
        """Open the parcel list from the portal dashboard."""
        return await self._postback(DPD_PORTAL_HOME_URL, portal_html, _HOP_TARGET)

    async def _login_and_open(self, *, respect_backoff: bool) -> str:
        portal = await self._full_login(respect_backoff=respect_backoff)
        html = await self._hop(portal)
        key = self._username.lower()
        if not is_logged_in(html):
            _UNOPENED_UNTIL[key] = time.monotonic() + _UNOPENED_BACKOFF_SECONDS
            raise DpdApiError("Signed in to DPD, but the parcel list did not open")
        _CAPTCHA_UNTIL.pop(key, None)
        _UNOPENED_UNTIL.pop(key, None)
        return html

    async def _reuse_session(self) -> str | None:
        """The parcel list if the cookies still open it, else ``None``.

        Only a clean signed-out answer returns ``None`` (and so leads to a
        login). Any error fails the poll instead: a login cannot fix those,
        and repeating logins is what can switch the CAPTCHA on.
        """
        html = await self._fetch("GET", DPD_INBOX_URL)
        if is_logged_in(html):
            return html
        portal_url, portal = await self._fetch_page("GET", DPD_PORTAL_HOME_URL)
        if not is_portal_logged_in(portal_url, portal):
            return None
        html = await self._hop(portal)
        return html if is_logged_in(html) else None

    async def _open_inbox(self) -> str:
        html = await self._reuse_session()
        if html is not None:
            return html
        return await self._login_and_open(respect_backoff=True)

    async def login(self, *, respect_backoff: bool = False) -> None:
        """Sign in once. The config flow always makes a real attempt;
        polls respect the CAPTCHA back-off."""
        async with self._lock:
            await self._login_and_open(respect_backoff=respect_backoff)

    # -- reading -----------------------------------------------------------

    async def fetch_parcels(self) -> list[dict[str, Any]]:
        """All parcels of the account (Empfang, Versand, Retoure)."""
        today = _local_today()
        async with self._lock:
            html = await self._open_inbox()
            counters = {tab: _Page(html).counter(tab) for tab in _TABS}
            parcels: list[dict[str, Any]] = []
            for tab, (target, direction) in _TABS.items():
                if tab != "incoming":
                    if not counters[tab]:
                        continue
                    html = await self._postback(DPD_INBOX_URL, html, target)
                    if not is_logged_in(html):
                        raise DpdApiError("DPD parcel list signed out unexpectedly")
                for row in list_rows(_Page(html)):
                    html, detail = await self._read_detail(html, row)
                    parcels.append(build_parcel(row, detail, direction, today))
            return parcels

    async def _read_detail(
        self, html: str, row: dict[str, Any]
    ) -> tuple[str, dict[str, Any] | None]:
        """Select a row and read its panel; return the page to chain from."""
        try:
            fresh = await self._postback(
                DPD_INBOX_URL, html, _ROW_TARGET.format(index=row["index"])
            )
        except DpdApiError as err:
            _LOGGER.debug("DPD parcel detail unavailable: %s", err)
            return html, None
        if not is_logged_in(fresh):
            return html, None
        panel = read_detail(_Page(fresh))
        if panel is None or panel["identity"] != row["barcode"]:
            _LOGGER.debug("DPD parcel detail did not match its row")
            return fresh, None
        return fresh, panel
