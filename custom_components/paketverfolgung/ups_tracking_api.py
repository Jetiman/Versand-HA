"""Client for UPS's official Track API.

UPS has no anonymous tracking: ``ups.com/track`` is bot-walled (a plain
request just hangs) and the JSON endpoints return 401 without a token. So
this uses the official API with the user's own free developer app -
Client ID + Secret, exchanged for an OAuth ``client_credentials`` token
(~4 h, cached and auto-renewed) - then ``GET /api/track/v1/details/{id}``.
"""
from __future__ import annotations

import logging
import time
import uuid
from base64 import b64encode
from typing import Any

from aiohttp import BasicAuth, ClientError, ClientSession

from .const import (
    DEFAULT_STATUS,
    GROUP_DELIVERED,
    GROUP_OUT_FOR_DELIVERY,
    GROUP_REGISTERED,
    GROUP_TRANSIT,
    GROUP_UNKNOWN,
    UPS_OAUTH_URL,
    UPS_TRACK_URL,
    UPS_TRACKING_PAGE_URL,
)
from .tracking_util import pick, text

_LOGGER = logging.getLogger(__name__)

# UPS activity/status "type" -> lifecycle group. The granular statusCode
# (e.g. "003" origin scan) is not stable across shipment types, so the
# coarse type plus a German/English text heuristic below is more reliable.
_TYPE_GROUP = {
    "M": GROUP_REGISTERED,  # billing/label information received
    "MV": GROUP_TRANSIT,
    "P": GROUP_TRANSIT,  # picked up / origin scan
    "O": GROUP_OUT_FOR_DELIVERY,
    "I": GROUP_TRANSIT,  # in transit
    "D": GROUP_DELIVERED,
    "DO": GROUP_OUT_FOR_DELIVERY,
    "RS": GROUP_DELIVERED,  # returned to shipper
    "X": GROUP_TRANSIT,  # exception - keep visible, show the text
    "NA": GROUP_UNKNOWN,
}

_TEXT_GROUP = (
    ("zugestellt", GROUP_DELIVERED),
    ("delivered", GROUP_DELIVERED),
    ("out for delivery", GROUP_OUT_FOR_DELIVERY),
    ("wird heute zugestellt", GROUP_OUT_FOR_DELIVERY),
    ("in zustellung", GROUP_OUT_FOR_DELIVERY),
    ("on its way", GROUP_TRANSIT),
    ("in transit", GROUP_TRANSIT),
    ("unterwegs", GROUP_TRANSIT),
    ("departed", GROUP_TRANSIT),
    ("arrived", GROUP_TRANSIT),
    ("origin scan", GROUP_TRANSIT),
    ("picked up", GROUP_TRANSIT),
    ("abgeholt", GROUP_TRANSIT),
    ("label created", GROUP_REGISTERED),
    ("order processed", GROUP_REGISTERED),
    ("versandvorbereitung", GROUP_REGISTERED),
)


class UpsTrackingApiError(Exception):
    """Transport / lookup error talking to the UPS Track API."""


class UpsAuthError(UpsTrackingApiError):
    """The UPS Client ID / Secret was rejected."""


def _dt(date: str | None, tm: str | None) -> str:
    """UPS gives date "YYYYMMDD" and time "HHMMSS" -> ISO-ish local string."""
    date = (date or "").strip()
    tm = (tm or "").strip().ljust(6, "0")[:6]
    if len(date) != 8 or not date.isdigit():
        return ""
    iso = f"{date[:4]}-{date[4:6]}-{date[6:8]}"
    if tm.isdigit() and tm != "000000":
        iso += f"T{tm[:2]}:{tm[2:4]}:{tm[4:6]}"
    return iso


def _location(activity: dict) -> str:
    addr = pick(pick(activity, "location") or {}, "address") or {}
    parts = [
        text(pick(addr, "city")),
        text(pick(addr, "stateProvince")),
        text(pick(addr, "countryCode", "country")),
    ]
    return ", ".join(p for p in parts if p)


class UpsTrackingApiClient:
    """Fetches a single package's status + history from UPS."""

    def __init__(
        self, session: ClientSession, client_id: str, client_secret: str
    ) -> None:
        self._session = session
        self._client_id = (client_id or "").strip()
        self._client_secret = (client_secret or "").strip()
        self._token: str | None = None
        self._token_expiry = 0.0

    @property
    def configured(self) -> bool:
        return bool(self._client_id and self._client_secret)

    async def _access_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        auth = BasicAuth(self._client_id, self._client_secret)
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "accept": "application/json",
        }
        try:
            async with self._session.post(
                UPS_OAUTH_URL,
                auth=auth,
                headers=headers,
                data={"grant_type": "client_credentials"},
                timeout=20,
            ) as resp:
                body = await resp.json(content_type=None)
                if resp.status in (400, 401, 403):
                    raise UpsAuthError(
                        f"UPS rejected the Client ID/Secret (HTTP {resp.status})"
                    )
                if resp.status != 200:
                    raise UpsTrackingApiError(
                        f"UPS OAuth returned status {resp.status}"
                    )
        except ClientError as err:
            raise UpsTrackingApiError(f"Network error on UPS OAuth: {err}") from err
        except ValueError as err:
            raise UpsTrackingApiError(f"UPS OAuth returned no JSON: {err}") from err

        token = str((body or {}).get("access_token") or "")
        if not token:
            raise UpsAuthError("UPS OAuth response had no access_token")
        try:
            ttl = float((body or {}).get("expires_in") or 3600)
        except (TypeError, ValueError):
            ttl = 3600.0
        self._token = token
        self._token_expiry = time.time() + ttl
        return token

    async def fetch(self, number: str) -> dict | None:
        """Return a normalized shipment dict, or None if UPS doesn't know it.

        Raises UpsAuthError on a credential problem, UpsTrackingApiError on
        transport problems, so the caller can keep the last-known state.
        """
        if not self.configured:
            return None
        token = await self._access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "accept": "application/json",
            "transId": uuid.uuid4().hex,
            "transactionSrc": "home-assistant-paketverfolgung",
        }
        url = UPS_TRACK_URL.format(id=number)
        try:
            async with self._session.get(
                url,
                headers=headers,
                params={"locale": "de_DE", "returnSignature": "false"},
                timeout=20,
            ) as resp:
                body = await resp.json(content_type=None)
                if resp.status == 401:
                    self._token = None
                    raise UpsAuthError("UPS token rejected")
                if resp.status == 404:
                    return None
                if resp.status != 200:
                    # A "number not found" comes back as 4xx with an errors
                    # array - treat that as "UPS doesn't know it", not a fault.
                    if _is_not_found(body):
                        return None
                    raise UpsTrackingApiError(
                        f"UPS tracking for {number} returned status {resp.status}"
                    )
        except ClientError as err:
            raise UpsTrackingApiError(
                f"Network error fetching UPS tracking for {number}: {err}"
            ) from err
        except ValueError as err:
            raise UpsTrackingApiError(
                f"UPS tracking for {number} returned no JSON: {err}"
            ) from err

        if _is_not_found(body):
            return None
        _LOGGER.debug("UPS response for %s: %s", number, body)
        return _parse(number, body)


def _is_not_found(body: Any) -> bool:
    errors = pick(pick(body, "response") or {}, "errors") or []
    codes = {str(pick(e, "code") or "") for e in errors if isinstance(e, dict)}
    # TW1001 = "Tracking Information Not Found"; 151044 = no data yet.
    return bool(codes & {"TW1001", "151044", "9151044"})


def _parse(number: str, body: Any) -> dict | None:
    shipments = pick(pick(body, "trackResponse") or {}, "shipment") or []
    packages: list[dict] = []
    for shipment in shipments if isinstance(shipments, list) else [shipments]:
        pkg = pick(shipment, "package") or []
        packages.extend(pkg if isinstance(pkg, list) else [pkg])
    package = next((p for p in packages if isinstance(p, dict)), None)
    if not package:
        return None

    activities = pick(package, "activity") or []
    if not isinstance(activities, list):
        activities = [activities]

    events: list[dict] = []
    for act in activities:
        if not isinstance(act, dict):
            continue
        status = pick(act, "status") or {}
        label = text(pick(status, "description")) or text(pick(act, "description"))
        loc = _location(act)
        when = _dt(text(pick(act, "date")), text(pick(act, "time")))
        if not label and not when:
            continue
        events.append(
            {
                "datum": when,
                "status": label or DEFAULT_STATUS,
                "location": loc or None,
            }
        )
    # UPS usually returns activities newest-first; sort defensively anyway
    # (entries with no parseable date keep their original relative order).
    events.sort(key=lambda e: e.get("datum") or "", reverse=True)

    cur_status = pick(package, "currentStatus") or (
        (pick(activities[0], "status") or {})
        if activities and isinstance(activities[0], dict)
        else {}
    )
    status_type = str(text(pick(cur_status, "type")) or "").upper()
    status_text = text(pick(cur_status, "description")) or (
        events[0]["status"] if events else ""
    )

    group = _TYPE_GROUP.get(status_type, GROUP_UNKNOWN)
    if group == GROUP_UNKNOWN:
        haystack = status_text.lower()
        for needle, mapped in _TEXT_GROUP:
            if needle in haystack:
                group = mapped
                break
        else:
            group = GROUP_TRANSIT if events else GROUP_REGISTERED
    delivered = group == GROUP_DELIVERED or status_type == "D"

    if not status_text and not events:
        return None

    return {
        "id": number,
        "carrier": "ups",
        "name": f"UPS {number}",
        "status": status_text or DEFAULT_STATUS,
        "group": GROUP_DELIVERED if delivered else group,
        "direction": "receive",
        "delivery_from": None,
        "delivery_to": None,
        "tracking_url": UPS_TRACKING_PAGE_URL.format(id=number),
        "events": events,
        "delivered": delivered,
        "protected": False,
    }
