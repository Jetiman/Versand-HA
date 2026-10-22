"""Optional Picnic account login + delivery tracking.

Picnic (grocery delivery) has no public tracking page. Its apps talk to a JSON
API that the community has reverse engineered (``python-picnic-api2``, which
Home Assistant's own ``picnic`` integration uses). This module speaks the part
of it that is needed for "when does my delivery arrive":

1. ``POST /user/login`` with ``{"key": e-mail, "secret": md5(password),
   "client_id": 30100}``. The session token comes back in the ``x-picnic-auth``
   *response header* and has to be sent in the same header afterwards; Picnic
   may hand out a changed token on any later response, so the newest one wins
   (the coordinator persists it). The password is never stored - only the
   token.
2. If the account has two-factor authentication the login answers with
   ``second_factor_authentication_required``; then ``POST /user/2fa/generate``
   (``{"channel": "SMS"}``) and ``POST /user/2fa/verify`` (``{"otp": code}``)
   finish it.
3. ``POST /deliveries/summary`` (body ``[]``) lists the deliveries; every entry
   has ``delivery_id``, ``status`` (``CURRENT`` / ``COMPLETED`` /
   ``CANCELLED``), the booked ``slot`` (``window_start`` / ``window_end``,
   ISO-8601 with offset), an ``eta2`` window and - once delivered - a
   ``delivery_time``. ``GET /deliveries/{id}`` adds the order lines and
   ``GET /deliveries/{id}/position`` is only filled while the van is on its
   way (``eta_window`` and ``eta`` as epoch milliseconds).

Everything is best-effort against an undocumented API: the parsing below only
reads documented-by-observation keys and degrades to ``None`` for anything it
does not find, instead of raising. Failures surface as ``PicnicAuthError``
(sign in again) or ``PicnicAccountError`` (try again later), so the
coordinator can tell the two apart without crashing.

The module is free of Home Assistant imports so it can be tested on its own.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from aiohttp import ClientError, ClientSession

from .const import (
    CARRIER_PICNIC,
    GROUP_DELIVERED,
    GROUP_OUT_FOR_DELIVERY,
    GROUP_REGISTERED,
    GROUP_TRANSIT,
    PICNIC_AGENT,
    PICNIC_API_URL,
    PICNIC_API_VERSION,
    PICNIC_CLIENT_ID,
    PICNIC_DEFAULT_COUNTRY,
    PICNIC_DEVICE_ID,
    PICNIC_ETA_HOLD_MINUTES,
    PICNIC_ETA_ROUND_MINUTES,
    PICNIC_LIVE_INTERVAL,
    PICNIC_SLOT_INTERVAL,
    PICNIC_USER_AGENT,
)
from .tracking_util import pick

_LOGGER = logging.getLogger(__name__)

AUTH_HEADER = "x-picnic-auth"
_TIMEOUT = 20

# Error codes in Picnic's ``{"error": {"code": ...}}`` body that mean "the
# credentials / the session are not accepted".
_AUTH_CODES = {"AUTH_ERROR", "AUTH_INVALID_CRED", "AUTH_INVALID_CREDENTIALS"}


class PicnicAccountError(Exception):
    """Picnic could not be reached / answered with an unexpected error."""


class PicnicAuthError(PicnicAccountError):
    """Login rejected, or the session token is no longer accepted."""


class PicnicTwoFactorRequired(PicnicAccountError):
    """The login needs a two-factor code (SMS) to finish."""


class PicnicTwoFactorError(PicnicAccountError):
    """The two-factor code was rejected / could not be sent."""


def md5_secret(password: str) -> str:
    """The ``secret`` Picnic expects: the hex MD5 of the password."""
    return hashlib.md5(password.encode("utf-8")).hexdigest()  # noqa: S324 - API contract


def _error_code(data: Any) -> str | None:
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict) and err.get("code"):
            return str(err["code"])
    return None


class PicnicAccountClient:
    """Minimal async client for the Picnic customer API."""

    def __init__(
        self,
        session: ClientSession,
        country: str = PICNIC_DEFAULT_COUNTRY,
        token: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self._session = session
        self._country = (country or PICNIC_DEFAULT_COUNTRY).lower()
        self._token = token or None
        self._base = (
            base_url
            or PICNIC_API_URL.format(country=self._country, version=PICNIC_API_VERSION)
        ).rstrip("/")

    @property
    def token(self) -> str | None:
        """The newest session token (may change after any request)."""
        return self._token

    # -- plumbing --------------------------------------------------------------

    async def _request(
        self, method: str, path: str, *, body: Any = None
    ) -> tuple[int, Any, str | None]:
        headers = {
            "User-Agent": PICNIC_USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-picnic-agent": PICNIC_AGENT,
            "x-picnic-did": PICNIC_DEVICE_ID,
        }
        if self._token:
            headers[AUTH_HEADER] = self._token
        try:
            async with asyncio.timeout(_TIMEOUT):
                async with self._session.request(
                    method, self._base + path, json=body, headers=headers
                ) as resp:
                    fresh = resp.headers.get(AUTH_HEADER)
                    if fresh:
                        self._token = fresh
                    status = resp.status
                    raw = await resp.text()
        except (ClientError, TimeoutError) as err:
            raise PicnicAccountError(f"Picnic nicht erreichbar: {err}") from err
        data: Any = None
        if raw and raw.strip():
            try:
                data = json.loads(raw)
            except ValueError:
                data = None
        return status, data, _error_code(data)

    @staticmethod
    def _check(status: int, code: str | None, *, auth: bool = True) -> None:
        """Raise the right error for a failed response."""
        if auth and (code in _AUTH_CODES or status == 401):
            raise PicnicAuthError(f"Picnic: Anmeldung abgelehnt ({code or status})")
        if status >= 500 or status == 429:
            raise PicnicAccountError(f"Picnic: HTTP {status}")
        if status >= 400:
            raise PicnicAccountError(f"Picnic: HTTP {status} {code or ''}".strip())

    # -- login -----------------------------------------------------------------

    async def login(self, username: str, password: str) -> None:
        """Sign in. Raises ``PicnicTwoFactorRequired`` when a code is needed."""
        status, data, code = await self._request(
            "POST",
            "/user/login",
            body={
                "key": username,
                "secret": md5_secret(password),
                "client_id": PICNIC_CLIENT_ID,
            },
        )
        self._check(status, code)
        if code in _AUTH_CODES:
            raise PicnicAuthError(f"Picnic: Anmeldung abgelehnt ({code})")
        if isinstance(data, dict) and data.get("second_factor_authentication_required"):
            raise PicnicTwoFactorRequired("Picnic verlangt einen Bestätigungscode")
        if code:
            raise PicnicAccountError(f"Picnic: Anmeldung fehlgeschlagen ({code})")
        if not self._token:
            raise PicnicAuthError("Picnic hat kein Sitzungs-Token geliefert")

    async def generate_2fa_code(self, channel: str = "SMS") -> None:
        status, _data, code = await self._request(
            "POST", "/user/2fa/generate", body={"channel": channel}
        )
        self._check(status, code)
        if code:
            raise PicnicTwoFactorError(f"Picnic: Code konnte nicht gesendet werden ({code})")

    async def verify_2fa_code(self, otp: str) -> None:
        status, _data, code = await self._request(
            "POST", "/user/2fa/verify", body={"otp": str(otp).strip()}
        )
        if status >= 500 or status == 429:
            raise PicnicAccountError(f"Picnic: HTTP {status}")
        if status >= 400 or code:
            raise PicnicTwoFactorError(f"Picnic: Code abgelehnt ({code or status})")

    # -- data ------------------------------------------------------------------

    async def fetch_user(self) -> dict:
        status, data, code = await self._request("GET", "/user")
        self._check(status, code)
        return data if isinstance(data, dict) else {}

    async def fetch_deliveries(self) -> list[dict]:
        """All delivery summaries (newest first), open and completed."""
        status, data, code = await self._request(
            "POST", "/deliveries/summary", body=[]
        )
        self._check(status, code)
        if code:
            raise PicnicAccountError(f"Picnic: Lieferungen nicht abrufbar ({code})")
        if not isinstance(data, list):
            return []
        return [d for d in data if isinstance(d, dict)]

    async def fetch_delivery(self, delivery_id: str) -> dict | None:
        """One delivery with its order lines (``None`` if unavailable).

        Only enriches the summary, so any trouble except a rejected session
        just means "no details this time".
        """
        try:
            status, data, code = await self._request("GET", f"/deliveries/{delivery_id}")
            self._check(status, code)
        except PicnicAuthError:
            raise
        except PicnicAccountError as err:
            _LOGGER.debug("Picnic delivery %s unavailable: %s", delivery_id, err)
            return None
        return data if isinstance(data, dict) and not code else None

    async def fetch_position(self, delivery_id: str) -> dict:
        """Live van position/ETA - only filled while the delivery is on its way."""
        try:
            status, data, code = await self._request(
                "GET", f"/deliveries/{delivery_id}/position"
            )
            self._check(status, code)
        except PicnicAuthError:
            raise
        except PicnicAccountError as err:
            _LOGGER.debug("Picnic position %s unavailable: %s", delivery_id, err)
            return {}
        if code or not isinstance(data, dict):
            return {}
        return data


# -- parsing -------------------------------------------------------------------


def parse_ts(value: Any) -> datetime | None:
    """ISO-8601 string (with offset or ``Z``) or epoch seconds/ms -> aware UTC."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value) / 1000.0 if abs(float(value)) > 1e11 else float(value)
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if text.lstrip("-").isdigit():
        return parse_ts(int(text))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _window(obj: Any) -> tuple[datetime | None, datetime | None]:
    if not isinstance(obj, dict):
        return None, None
    start = parse_ts(pick(obj, "start", "window_start"))
    end = parse_ts(pick(obj, "end", "window_end"))
    if start and end and end < start:
        start, end = end, start
    return start, end


def _nearest(dt: datetime, minutes: int) -> datetime:
    step = minutes * 60
    return datetime.fromtimestamp(
        math.floor(dt.timestamp() / step + 0.5) * step, tz=timezone.utc
    )


def round_window(
    start: datetime, end: datetime, minutes: int = PICNIC_ETA_ROUND_MINUTES
) -> tuple[datetime, datetime]:
    """Round a window to a coarse grid, so a one-minute ETA wobble does not
    rewrite the calendar entry on every poll. The window never collapses."""
    a, b = _nearest(start, minutes), _nearest(end, minutes)
    if b <= a:
        b = a + timedelta(minutes=minutes)
    return a, b


def hold_window(
    start: datetime,
    end: datetime,
    previous_from: Any,
    previous_to: Any,
    minutes: int = PICNIC_ETA_HOLD_MINUTES,
) -> tuple[datetime, datetime]:
    """Keep the window shown so far unless the new one moved noticeably.

    Rounding to a grid (``round_window``) still lets an ETA that sits on a
    rounding edge flip between neighbouring steps from poll to poll. Anything
    that follows the window (calendar entry, "window changed" push, cards)
    would report every flip. So the previously shown window is kept as long as
    both ends moved by less than ``minutes``; a real shift passes through.
    """
    prev_start, prev_end = parse_ts(previous_from), parse_ts(previous_to)
    if prev_start is None or prev_end is None:
        return start, end
    limit = timedelta(minutes=minutes)
    if abs(start - prev_start) < limit and abs(end - prev_end) < limit:
        return prev_start, prev_end
    return start, end


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


_WEEKDAYS = ("Mo.", "Di.", "Mi.", "Do.", "Fr.", "Sa.", "So.")


def _fmt_day(local: datetime) -> str:
    return f"{_WEEKDAYS[local.weekday()]} {local.strftime('%d.%m.')}"


def _fmt_range(a: datetime, b: datetime) -> str:
    return f"{a.strftime('%H:%M')}–{b.strftime('%H:%M')}"


def _cents(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _eur(cents: int) -> str:
    return f"{cents / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") + " €"


def _orders(obj: Any) -> list[dict]:
    orders = obj.get("orders") if isinstance(obj, dict) else None
    return [o for o in orders if isinstance(o, dict)] if isinstance(orders, list) else []


def total_cents(summary: dict) -> int | None:
    """What the delivery cost (cents), summed over its orders."""
    total, found = 0, False
    for order in _orders(summary):
        value = _cents(order.get("checkout_total_price"))
        if value is None:
            value = _cents(order.get("total_price"))
        if value is not None:
            total += value
            found = True
    return total if found else None


def article_count(detail: dict | None) -> int | None:
    """Number of articles in a delivery, from its order lines (best effort).

    Order lines carry their articles in ``items``; an article's amount sits in
    a ``QUANTITY`` decorator. Anything unexpected counts as one article, and a
    delivery without lines gives ``None`` (name stays generic).
    """
    total = 0
    for order in _orders(detail or {}):
        for line in order.get("items") or []:
            if not isinstance(line, dict):
                continue
            articles = line.get("items")
            if not isinstance(articles, list) or not articles:
                total += 1
                continue
            for article in articles:
                qty = 1
                for deco in (article or {}).get("decorators") or []:
                    if isinstance(deco, dict) and deco.get("type") == "QUANTITY":
                        try:
                            qty = max(1, int(deco.get("quantity") or 1))
                        except (TypeError, ValueError):
                            qty = 1
                total += qty
    return total or None


def delivered_moment(summary: dict) -> datetime | None:
    """When the delivery happened (or, failing that, was due)."""
    start, _end = _window(summary.get("delivery_time"))
    if start:
        return start
    _s, slot_end = _window(summary.get("slot"))
    return slot_end or parse_ts(summary.get("creation_time"))


def is_delivered(summary: dict) -> bool:
    """``COMPLETED`` only appears after the invoice mail, so a recorded
    ``delivery_time`` counts as delivered, too."""
    if str(summary.get("status") or "").upper() == "COMPLETED":
        return True
    start, _end = _window(summary.get("delivery_time"))
    return start is not None


def in_history(summary: dict, now: datetime, history_days: int) -> bool:
    """Keep open deliveries, and delivered ones for ``history_days`` days."""
    status = str(summary.get("status") or "").upper()
    if status == "CANCELLED":
        return False
    if not is_delivered(summary):
        return True
    if history_days <= 0:
        return False
    moment = delivered_moment(summary)
    return moment is None or moment >= now - timedelta(days=history_days)


def _position_window(position: dict | None) -> tuple[datetime | None, datetime | None, bool]:
    """(start, end, live) from the van position: the ETA window, or the ETA
    point widened by a few minutes. ``live`` = the van is on its way."""
    if not isinstance(position, dict) or not position:
        return None, None, False
    start, end = _window(position.get("eta_window"))
    if start and end:
        return start, end, True
    eta = parse_ts(position.get("eta"))
    if eta:
        pad = timedelta(minutes=PICNIC_ETA_ROUND_MINUTES)
        return eta - pad, eta + pad, True
    return None, None, False


def normalize_delivery(
    summary: dict,
    detail: dict | None,
    position: dict | None,
    now: datetime,
    previous: dict | None = None,
) -> dict | None:
    """Turn a Picnic delivery into the shared shipment shape.

    ``now`` must be timezone-aware and in the *local* zone (dates such as
    "today" are decided in it). Returns ``None`` for cancelled deliveries and
    for entries without an id.

    ``previous`` is the item built for the same delivery on the last poll. It
    steadies an open delivery's window (see ``hold_window``): small ETA
    wobbles keep the window that was shown before.

    Status texts are deliberately plain and stable: the delivery window and the
    live ETA live in ``delivery_from``/``delivery_to``, so a drifting ETA does
    not count as a status change (= no repeated push), and the wording avoids
    phrases the dashboard's calendar/notification templates read as "in
    Zustellung" ("Zustellung heute", "wird zugestellt", ...).
    """
    delivery_id = str(pick(summary, "delivery_id", "id") or "").strip()
    if not delivery_id:
        return None
    status_raw = str(summary.get("status") or "").upper()
    if status_raw == "CANCELLED":
        return None

    local = now.tzinfo
    slot_start, slot_end = _window(summary.get("slot"))
    eta2_start, eta2_end = _window(summary.get("eta2"))
    pos_start, pos_end, live = _position_window(position)
    created = parse_ts(summary.get("creation_time"))
    d_start, _d_end = _window(summary.get("delivery_time"))
    delivered = status_raw == "COMPLETED" or d_start is not None
    live = live and not delivered

    # Which window to announce: the van's ETA beats Picnic's day-of estimate
    # (eta2), which beats the booked slot.
    if delivered:
        w_start, w_end = slot_start, slot_end
    elif pos_start and pos_end:
        w_start, w_end = round_window(pos_start, pos_end)
    elif eta2_start and eta2_end:
        w_start, w_end = round_window(eta2_start, eta2_end)
    else:
        w_start, w_end = slot_start, slot_end

    if (
        not delivered
        and w_start
        and w_end
        and isinstance(previous, dict)
        and not previous.get("delivered")
    ):
        w_start, w_end = hold_window(
            w_start, w_end, previous.get("delivery_from"), previous.get("delivery_to")
        )

    slot_day = slot_start.astimezone(local).date() if slot_start else None
    today = now.date()
    if delivered:
        group, status = GROUP_DELIVERED, "Zugestellt"
    elif live:
        group, status = GROUP_OUT_FOR_DELIVERY, "In Zustellung"
    elif slot_day is not None and slot_day == today:
        group, status = GROUP_TRANSIT, "Lieferung heute geplant"
    elif slot_day is not None and slot_day < today:
        group, status = GROUP_TRANSIT, "Lieferung ausstehend"
    elif slot_day is not None:
        group = GROUP_REGISTERED
        status = f"Lieferung geplant am {_fmt_day(slot_start.astimezone(local))}"
    else:
        group, status = GROUP_REGISTERED, "Bestellt"

    # History, newest first (the shared timeline of the panel). The newest
    # *dated* event of a delivered parcel is read as the delivery moment, so
    # "Zugestellt" must come first and carry the real time.
    events: list[dict] = []
    if delivered:
        moment = d_start or slot_end or created
        if moment:
            events.append({"datum": _iso(moment), "status": "Zugestellt"})
    if live and w_start and w_end:
        events.append(
            {
                "datum": _iso(now),
                "status": "In Zustellung – Ankunft ca. "
                + _fmt_range(w_start.astimezone(local), w_end.astimezone(local))
                + " Uhr",
            }
        )
    if slot_start and slot_end:
        s_loc, e_loc = slot_start.astimezone(local), slot_end.astimezone(local)
        events.append(
            {
                "datum": _iso(created or slot_start),
                "status": f"Lieferfenster gebucht: {_fmt_day(s_loc)} {_fmt_range(s_loc, e_loc)} Uhr",
            }
        )
    if created:
        # The summary only has the goods value; the detail also has what was
        # actually charged (deposit, bags).
        price = total_cents(detail or {}) or total_cents(summary)
        events.append(
            {
                "datum": _iso(created),
                "status": "Bestellung aufgegeben" + (f" ({_eur(price)})" if price else ""),
            }
        )

    count = article_count(detail)
    name = "Einkauf" + (f" · {count} Artikel" if count else "")

    return {
        "id": delivery_id,
        "carrier": CARRIER_PICNIC,
        "delivery_carrier": None,
        "tracking_id": None,
        "name": name,
        "status": status,
        "group": group,
        "direction": "receive",
        "delivery_from": _iso(w_start),
        "delivery_to": _iso(w_end),
        "tracking_url": None,
        "events": events,
        "delivered": delivered,
        "protected": False,
        "order_id": None,
        "short_status": None,
        "stops_away": None,
        # Coordinator bookkeeping (not exposed as sensor attributes).
        "live": live,
        "slot_from": _iso(slot_start),
        "slot_to": _iso(slot_end),
    }


def choose_interval(
    items: list[dict], now: datetime, base: timedelta
) -> timedelta:
    """How long to wait before the next poll.

    The van is on its way (live ETA): every minute. A delivery whose slot is
    about to start, running or just over: every few minutes. Otherwise the
    configured interval. Never slower than ``base``.
    """
    near = False
    for item in items:
        if item.get("delivered"):
            continue
        if item.get("live"):
            return min(PICNIC_LIVE_INTERVAL, base)
        start, end = parse_ts(item.get("slot_from")), parse_ts(item.get("slot_to"))
        if start and end and start - timedelta(hours=1) <= now <= end + timedelta(hours=1):
            near = True
    return min(PICNIC_SLOT_INTERVAL, base) if near else base


def wants_position(summary: dict, now: datetime) -> bool:
    """Ask for the van position only on delivery day (it is empty otherwise)."""
    if is_delivered(summary):
        return False
    start, _end = _window(summary.get("slot"))
    if _window(summary.get("eta2"))[0]:
        return True
    return start is not None and start.astimezone(now.tzinfo).date() == now.date()
