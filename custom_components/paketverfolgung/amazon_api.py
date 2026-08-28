"""Amazon.de login and shipment scraping for Paketverfolgung."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from html import unescape
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

from aiohttp import ClientError, ClientSession, CookieJar
from bs4 import BeautifulSoup
from yarl import URL

_LOGGER = logging.getLogger(__name__)

AMAZON_BASE = "https://www.amazon.de"
AMAZON_ORDERS_URL = "https://www.amazon.de/gp/css/order-history?ref_=nav_orders_first"
AMAZON_SIGNIN_URL = (
    "https://www.amazon.de/ap/signin?_encoding=UTF8&accountStatusPolicy=P1"
    "&openid.assoc_handle=deflex"
    "&openid.claimed_id=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select"
    "&openid.identity=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select"
    "&openid.mode=checkid_setup&openid.ns=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0"
    "&openid.return_to=https%3A%2F%2Fwww.amazon.de%2Fgp%2Fcss%2Forder-history"
    "&pageId=webcs-yourorder&showRmrMe=1"
)
AMAZON_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/139.0.0.0 Safari/537.36"
)


class AmazonApiError(Exception):
    """Base Amazon provider error."""


class AmazonAuthError(AmazonApiError):
    """Amazon authentication failed or expired."""


class AmazonCaptchaError(AmazonAuthError):
    """Amazon requires a captcha/manual browser login."""


@dataclass
class AmazonOtpChallenge:
    """Pending Amazon OTP challenge."""

    url: str
    form: dict[str, str]
    cookies: dict[str, str]
    mode: str = "otp"


@dataclass
class AmazonLoginResult:
    """Result of one Amazon login attempt."""

    cookies: dict[str, str] | None = None
    otp: AmazonOtpChallenge | None = None

    @property
    def authenticated(self) -> bool:
        return bool(self.cookies) and self.otp is None


def _headers(referer: str | None = None) -> dict[str, str]:
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "accept-language": "de-DE,de;q=0.9,en;q=0.7",
        "cache-control": "no-cache",
        "user-agent": AMAZON_USER_AGENT,
    }
    if referer:
        headers["referer"] = referer
        headers["origin"] = AMAZON_BASE
    return headers


def _form_data(html: str) -> tuple[dict[str, str], str | None]:
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form", attrs={"name": "signIn"}) or soup.find("form")
    if not form:
        return {}, None
    data: dict[str, str] = {}
    for input_tag in form.find_all("input"):
        name = input_tag.get("name")
        if name:
            data[name] = input_tag.get("value", "")
    action = form.get("action")
    return data, urljoin(AMAZON_BASE, action) if action else None


def _looks_authenticated(html: str, final_url: str = "") -> bool:
    return (
        "js-yo-main-content" in html
        or "order-card js-order-card" in html
        or "/gp/css/order-history" in final_url
        or "/your-orders/" in final_url
    ) and "auth-workflow" not in html


def _looks_captcha(html: str) -> bool:
    lowered = html.lower()
    return (
        "captcha-placeholder" in lowered
        or "cvf_captcha" in lowered
        or "/errors/validatecaptcha" in lowered
        or "löse das rätsel" in lowered
    )


def _otp_challenge(
    html: str, final_url: str, cookies: dict[str, str]
) -> AmazonOtpChallenge | None:
    if "auth-mfa-otpcode" in html:
        form, action = _form_data(html)
        form.pop("undefined", None)
        form.setdefault("deviceId", "")
        form["rememberDevice"] = "true"
        return AmazonOtpChallenge(
            action or f"{AMAZON_BASE}/ap/signin", form, cookies, "mfa"
        )

    markers = (
        "transactionapproval",
        "Enter verification code",
        "Bestätigungscode eingeben",
        "verification-code-form",
        "auth-select-device-form",
    )
    if any(marker in html for marker in markers):
        form, action = _form_data(html)
        form.pop("undefined", None)
        form["action"] = "code"
        for key in ("resendContactType", "timerMessage", "timerComplete"):
            form.pop(key, None)
        return AmazonOtpChallenge(
            action or f"{AMAZON_BASE}/ap/cvf/verify", form, cookies, "cvf"
        )
    return None


def _is_tracking_href(href: str) -> bool:
    """Return True for known Amazon package-tracking URLs."""
    lowered = href.lower()
    markers = (
        "progress-tracker",
        "track-package",
        "trackpackage",
        "ship-track",
        "shipment-tracking",
        "tracking/package",
        "package-tracking",
    )
    if any(marker in lowered for marker in markers):
        return True
    return (
        "your-orders" in lowered
        and ("shipmentid=" in lowered or "packageid=" in lowered)
    )


def _query_value(url: str, *keys: str) -> str:
    parsed = parse_qs(urlparse(url).query)
    for key in keys:
        value = parsed.get(key)
        if value:
            return value[0]
    lowered = {key.lower(): value for key, value in parsed.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value:
            return value[0]
    return ""


def _normalize_carrier(raw: str, page_text: str) -> str:
    """Return a short carrier name without tracking-page action text."""
    candidates = [raw, page_text]
    for text in candidates:
        if not text:
            continue
        match = re.search(
            r"Versendet mit\s+(.+?)(?=\s+Trackingnummer\b|\s+Alle Aktualisierungen\b|\s+Lieferanweisungen\b|\s+Versandadresse\b|\s+Bestellinfo\b|$)",
            text,
            flags=re.I,
        )
        if match:
            return " ".join(match.group(1).split()).strip(" :-")

    cleaned = " ".join((raw or "").split()).strip()
    cleaned = re.split(
        r"\s+(?:Trackingnummer|Alle Aktualisierungen|Lieferanweisungen|Versandadresse|Bestellinfo)\b",
        cleaned,
        maxsplit=1,
        flags=re.I,
    )[0].strip(" :-")
    return cleaned


class AmazonApiClient:
    """Small isolated Amazon.de web client with its own cookie jar."""

    def __init__(self, cookies: dict[str, str] | None = None) -> None:
        self._jar = CookieJar(unsafe=True)
        if cookies:
            self._jar.update_cookies(cookies, response_url=URL(AMAZON_BASE))
        self._session = ClientSession(cookie_jar=self._jar)

    async def close(self) -> None:
        await self._session.close()

    def export_cookies(self) -> dict[str, str]:
        return {
            name: morsel.value
            for name, morsel in self._jar.filter_cookies(URL(AMAZON_BASE)).items()
        }

    async def login(self, username: str, password: str) -> AmazonLoginResult:
        """Perform Amazon login. Password is never returned or persisted."""
        try:
            async with self._session.get(
                AMAZON_SIGNIN_URL, headers=_headers(), timeout=25
            ) as response:
                html = await response.text()
                final_url = str(response.url)

            if _looks_authenticated(html, final_url):
                return AmazonLoginResult(cookies=self.export_cookies())
            if _looks_captcha(html):
                raise AmazonCaptchaError("Amazon captcha/manual login required")

            form, post_url = _form_data(html)
            if not form:
                raise AmazonAuthError("Amazon login form not found")

            has_password = (
                BeautifulSoup(html, "html.parser").find(
                    "input", {"type": "password"}
                )
                is not None
            )
            if (
                form.get("appAction") == "SIGNIN_CLAIM_COLLECT"
                or "FullPageUnifiedClaimCollect" in html
                or not has_password
            ):
                for key in (
                    "webAuthnGetArbForAutofill",
                    "webAuthnGetParametersForAutofill",
                    "webAuthnChallengeIdForAutofill",
                    "ue_back",
                ):
                    form.pop(key, None)
                form["email"] = username
                async with self._session.post(
                    post_url or f"{AMAZON_BASE}/ap/signin",
                    data=form,
                    headers=_headers(post_url or AMAZON_SIGNIN_URL),
                    timeout=25,
                ) as response:
                    html = await response.text()
                    final_url = str(response.url)
                if _looks_captcha(html):
                    raise AmazonCaptchaError("Amazon captcha/manual login required")
                form, post_url = _form_data(html)

            if not form:
                raise AmazonAuthError("Amazon password form not found")
            form.pop("undefined", None)
            form.pop("=", None)
            form["email"] = form.get("email") or username
            form["password"] = password
            form["rememberMe"] = "true"

            async with self._session.post(
                post_url or f"{AMAZON_BASE}/ap/signin",
                data=form,
                headers=_headers(post_url or AMAZON_SIGNIN_URL),
                timeout=25,
            ) as response:
                html = await response.text()
                final_url = str(response.url)

            cookies = self.export_cookies()
            if _looks_authenticated(html, final_url):
                return AmazonLoginResult(cookies=cookies)
            if _looks_captcha(html):
                raise AmazonCaptchaError("Amazon captcha/manual login required")
            challenge = _otp_challenge(html, final_url, cookies)
            if challenge:
                return AmazonLoginResult(otp=challenge)
            raise AmazonAuthError(
                "Amazon rejected the login or returned an unsupported verification page"
            )
        except ClientError as err:
            raise AmazonAuthError(f"Network error during Amazon login: {err}") from err

    async def submit_otp(
        self, challenge: AmazonOtpChallenge, code: str
    ) -> dict[str, str]:
        self._jar.update_cookies(challenge.cookies, response_url=URL(AMAZON_BASE))
        form = dict(challenge.form)
        if challenge.mode == "mfa":
            form["otpCode"] = code
            form["rememberDevice"] = "true"
        else:
            form["code"] = code
            form["action"] = "code"
        try:
            async with self._session.post(
                challenge.url,
                data=form,
                headers=_headers(challenge.url),
                timeout=25,
            ) as response:
                html = await response.text()
                final_url = str(response.url)
            if not _looks_authenticated(html, final_url):
                if _looks_captcha(html):
                    raise AmazonCaptchaError("Amazon captcha/manual login required")
                raise AmazonAuthError("Amazon verification code was not accepted")
            return self.export_cookies()
        except ClientError as err:
            raise AmazonAuthError(
                f"Network error during Amazon verification: {err}"
            ) from err

    async def fetch_shipments(
        self,
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """Return currently trackable Amazon deliveries plus refreshed cookies."""
        try:
            async with self._session.get(
                AMAZON_ORDERS_URL, headers=_headers(), timeout=25
            ) as response:
                html = await response.text()
                final_url = str(response.url)
            if "auth-workflow" in html or "/ap/signin" in final_url:
                raise AmazonAuthError("Amazon session expired")

            soup = BeautifulSoup(html, "html.parser")
            order_cards = soup.select(
                ".order-card.js-order-card, .order-card, [data-order-id], .js-order-card"
            )
            order_links: list[tuple[str, str]] = []
            seen_urls: set[str] = set()

            for order in order_cards:
                desc_tag = (
                    order.select_one(".yohtmlc-product-title")
                    or order.select_one(".a-link-normal[href*='/dp/']")
                    or order.select_one(".a-fixed-right-grid-col .a-link-normal")
                )
                desc = " ".join(desc_tag.stripped_strings) if desc_tag else ""
                for link in order.select("a[href]"):
                    href = str(link.get("href") or "")
                    text = link.get_text(" ", strip=True).lower()
                    if not (
                        _is_tracking_href(href)
                        or "lieferung verfolgen" in text
                        or "sendung verfolgen" in text
                        or "paket verfolgen" in text
                    ):
                        continue
                    absolute = urljoin(AMAZON_BASE, href)
                    if absolute not in seen_urls:
                        seen_urls.add(absolute)
                        order_links.append((desc, absolute))

            for link in soup.select("a[href]"):
                href = str(link.get("href") or "")
                text = link.get_text(" ", strip=True).lower()
                if not (
                    _is_tracking_href(href)
                    or "lieferung verfolgen" in text
                    or "sendung verfolgen" in text
                    or "paket verfolgen" in text
                ):
                    continue
                absolute = urljoin(AMAZON_BASE, href)
                if absolute not in seen_urls:
                    seen_urls.add(absolute)
                    order_links.append(("", absolute))

            if not order_links:
                _LOGGER.warning(
                    "Amazon: order page loaded but no tracking links were found "
                    "(order_cards=%d, page_links=%d)",
                    len(order_cards),
                    len(soup.select("a[href]")),
                )

            shipments: list[dict[str, Any]] = []
            skipped_without_tracking = 0
            for desc, url in order_links:
                shipment = await self._fetch_tracking_page(url, desc)
                if shipment:
                    shipments.append(shipment)
                else:
                    skipped_without_tracking += 1

            _LOGGER.info(
                "Amazon: order_cards=%d tracking_links=%d shipments=%d skipped_without_tracking=%d",
                len(order_cards),
                len(order_links),
                len(shipments),
                skipped_without_tracking,
            )
            return shipments, self.export_cookies()
        except ClientError as err:
            raise AmazonApiError(
                f"Network error fetching Amazon orders: {err}"
            ) from err

    async def _fetch_tracking_page(
        self, url: str, desc: str
    ) -> dict[str, Any] | None:
        try:
            async with self._session.get(
                url, headers=_headers(AMAZON_ORDERS_URL), timeout=25
            ) as response:
                html = await response.text()
                final_url = str(response.url)
        except ClientError as err:
            raise AmazonApiError(
                f"Network error fetching Amazon tracking page: {err}"
            ) from err

        if "auth-workflow" in html or "/ap/signin" in final_url:
            raise AmazonAuthError("Amazon session expired while opening tracking page")

        soup = BeautifulSoup(html, "html.parser")
        page_text = " ".join(soup.stripped_strings)

        state: dict[str, Any] = {}
        for state_script in soup.select('script[data-a-state*="page-state"]'):
            if not state_script.string:
                continue
            try:
                candidate = json.loads(state_script.string)
            except (ValueError, TypeError):
                continue
            if isinstance(candidate, dict):
                state = candidate
                break

        status_tag = (
            soup.select_one(".pt-status-main-status")
            or soup.select_one(".milestone-primaryMessage.alpha")
            or soup.select_one(".milestone-primaryMessage")
            or soup.select_one("#primaryStatus")
            or soup.select_one("h1")
        )
        status = " ".join(status_tag.stripped_strings) if status_tag else ""
        promise = state.get("promise") or {}
        if not status:
            status = promise.get("promiseMessage") or ""
        if not status:
            shipping_info = soup.select_one(".js-shipment-info-container")
            status = " ".join(shipping_info.stripped_strings) if shipping_info else ""

        additions: list[str] = []
        for selector in (
            "#primaryStatus",
            "#secondaryStatus",
            ".pt-promise-details-slot",
            ".pt-status-secondary-status",
            ".pt-promise-main-slot",
        ):
            tag = soup.select_one(selector)
            text = " ".join(tag.stripped_strings) if tag else ""
            if text and text != status and text not in additions:
                additions.append(text)
        map_tracking = state.get("mapTracking") or {}
        callout = map_tracking.get("calloutMessage")
        if callout and callout not in additions:
            additions.append(callout)
        if additions:
            status = ". ".join([part for part in [status, *additions] if part])

        tracking_tag = (
            soup.select_one(".pt-delivery-card-trackingId")
            or soup.select_one("[class*='trackingId']")
            or soup.select_one("[class*='tracking-id']")
        )
        tracking_id = " ".join(tracking_tag.stripped_strings) if tracking_tag else ""
        tracking_id = re.sub(
            r"^Trackingnummer\s*:?\s*", "", tracking_id, flags=re.I
        ).strip()
        if not tracking_id:
            match = re.search(
                r"Trackingnummer\s*:?\s*([A-Z0-9][A-Z0-9-]{5,})",
                page_text,
                flags=re.I,
            )
            if match:
                tracking_id = match.group(1)

        # Do not create entities from Amazon's internal shipment/package IDs. Those
        # are order placeholders, not actual trackable parcels. We deliberately do
        # not require a DE prefix because other carriers may use different formats.
        if not tracking_id:
            return None

        order_id = _query_value(final_url, "orderId", "orderID", "order")
        shipment_id = tracking_id

        carrier_tag = (
            soup.select_one(".carrierRelatedInfo-mfn-providerTitle")
            or soup.select_one("[class*='providerTitle']")
        )
        carrier_raw = " ".join(carrier_tag.stripped_strings) if carrier_tag else ""
        carrier = _normalize_carrier(carrier_raw, page_text)

        short_status = (
            (state.get("detailedState") or {}).get("shortStatus")
            or state.get("shortStatus")
            or (state.get("progressTracker") or {}).get("shortStatus")
        )

        if not status:
            for tag in soup.select("h1, h2, h3, .a-size-large, .a-size-medium"):
                text = " ".join(tag.stripped_strings)
                if text and len(text) <= 160 and text.lower() not in {
                    "versandadresse",
                    "bestellinfo",
                }:
                    status = text
                    break
        if not status:
            _LOGGER.warning(
                "Amazon: shipment identifier found but no status could be parsed"
            )
            return None

        return {
            "id": shipment_id,
            "provider": "amazon",
            "name": desc or order_id or f"Amazon {shipment_id}",
            "status": unescape(status).strip(),
            "tracking_id": tracking_id,
            "order_id": order_id or None,
            "carrier": carrier or None,
            "tracking_url": final_url,
            "short_status": short_status,
        }
