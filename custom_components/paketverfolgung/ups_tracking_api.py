"""Client for UPS's account-less tracking endpoint (ups.com/track).

No account and no API key. Two calls per lookup:

1. A GET to the API host seeds the session cookies, including a CSRF cookie.
2. A POST to ``Track/GetStatus`` echoes that cookie as a header and returns
   the tracking payload.

The endpoint sits behind Akamai's bot guard. It never answers a request whose
header set contradicts the claimed browser, so the Chrome-consistent headers
below belong together and are a maintenance tripwire: if lookups start to hang,
bump the Chrome version in the User-Agent and the client hints together.

UPS also grants an unvalidated session only a handful of lookups and then goes
silent for hours, hence :class:`UpsBudget`. The approach and the budget
numbers come from the MIT-licensed ha-ups project
(github.com/ha-parcel-integrations/ha-ups).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout, CookieJar

from .const import (
    CARRIER_UPS,
    DEFAULT_STATUS,
    GROUP_DELIVERED,
    GROUP_OUT_FOR_DELIVERY,
    GROUP_REGISTERED,
    GROUP_TRANSIT,
    GROUP_UNKNOWN,
    UPS_COOKIE_XSRF,
    UPS_HEADER_XSRF,
    UPS_LOCALE,
    UPS_TRACKING_API_URL,
    UPS_TRACKING_PAGE_URL,
)
from .tracking_util import as_bool, pick, text

_LOGGER = logging.getLogger(__name__)

# The guard's failure mode is silence, not an error status, so every request
# needs its own timeout.
_TIMEOUT = ClientTimeout(total=20)

_CHROME_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)
_GUARD_HEADERS = {
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "de-DE,de;q=0.9",
    "sec-ch-ua": '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "User-Agent": _CHROME_USER_AGENT,
}
_NAVIGATION_HEADERS = {
    **_GUARD_HEADERS,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
}
_FETCH_HEADERS = {
    **_GUARD_HEADERS,
    "Accept": "application/json, text/plain, */*",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "Origin": "https://www.ups.com",
    "Referer": "https://www.ups.com/",
}


class UpsTrackingApiError(Exception):
    """A UPS lookup failed. ``transient`` = the request never came back."""

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


class UpsBudget:
    """A token bucket for UPS lookups that survives restarts.

    UPS counts requests rather than a rate, so spacing them out buys nothing:
    only the number matters. Wall-clock time (not monotonic) keeps the balance
    correct across a restart - a budget that reset on boot would protect
    against nothing.
    """

    def __init__(
        self,
        capacity: int,
        refill_seconds: float,
        tokens: float | None = None,
        updated: float | None = None,
    ) -> None:
        self.capacity = capacity
        self.refill_seconds = refill_seconds
        # A fresh install starts full: the address hasn't been spent against.
        self.tokens = float(capacity if tokens is None else min(tokens, capacity))
        self.updated = time.time() if updated is None else updated

    def _accrue(self, now: float) -> None:
        elapsed = now - self.updated
        if elapsed < 0:
            self.updated = now  # clock went backwards: re-anchor, bank nothing
            return
        if elapsed < self.refill_seconds:
            return
        earned = int(elapsed // self.refill_seconds)
        self.tokens = min(float(self.capacity), self.tokens + earned)
        if self.tokens >= self.capacity:
            self.updated = now  # full: an idle week must not bank credit
        else:
            self.updated += earned * self.refill_seconds

    def available(self, now: float | None = None) -> int:
        self._accrue(time.time() if now is None else now)
        return int(self.tokens)

    def try_spend(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        if self.available(now) < 1:
            return False
        self.tokens -= 1
        # Restart the refill clock at the request: the measured beat is the
        # gap between two answered requests, not since a token was earned.
        self.updated = now
        return True

    def to_dict(self) -> dict[str, float]:
        return {"tokens": self.tokens, "updated": self.updated}

    @classmethod
    def from_dict(
        cls, data: dict[str, Any] | None, capacity: int, refill_seconds: float
    ) -> "UpsBudget":
        if not isinstance(data, dict):
            return cls(capacity, refill_seconds)
        try:
            return cls(
                capacity,
                refill_seconds,
                tokens=float(data["tokens"]),
                updated=float(data["updated"]),
            )
        except (KeyError, TypeError, ValueError):
            return cls(capacity, refill_seconds)


class UpsTrackingApiClient:
    """One short-lived session: bootstrap once, then a POST per number."""

    def __init__(self) -> None:
        self._session = ClientSession(cookie_jar=CookieJar())

    async def close(self) -> None:
        await self._session.close()

    def _xsrf(self) -> str | None:
        for cookie in self._session.cookie_jar:
            if cookie.key == UPS_COOKIE_XSRF:
                return cookie.value
        return None

    async def bootstrap(self) -> None:
        """Seed the cookie jar. Not budgeted - it is a plain GET."""
        url = UPS_TRACKING_API_URL.format(locale=UPS_LOCALE)
        try:
            async with self._session.get(
                url, headers=_NAVIGATION_HEADERS, timeout=_TIMEOUT
            ) as resp:
                await resp.read()
        except asyncio.TimeoutError as err:
            raise UpsTrackingApiError("bootstrap timed out", transient=True) from err
        except (ClientError, OSError) as err:
            raise UpsTrackingApiError(f"bootstrap failed: {err}") from err
        if not self._xsrf():
            raise UpsTrackingApiError(
                "UPS gave no CSRF cookie (bot protection); not sending a request"
            )

    async def fetch(self, number: str) -> dict | None:
        """Normalized shipment, or ``None`` if UPS doesn't know the number."""
        xsrf = self._xsrf()
        if not xsrf:
            raise UpsTrackingApiError("no CSRF cookie - call bootstrap() first")
        body = {
            "Locale": UPS_LOCALE,
            "TrackingNumber": [number.strip().upper()],
            "isBarcodeScanned": False,
            "Requester": "quic",
            "ClientUrl": "https://www.ups.com/track",
            "returnToValue": "",
            "AssociatedBcdnNumber": None,
        }
        try:
            async with self._session.post(
                UPS_TRACKING_API_URL.format(locale=UPS_LOCALE),
                json=body,
                headers={**_FETCH_HEADERS, UPS_HEADER_XSRF: xsrf},
                timeout=_TIMEOUT,
            ) as resp:
                _LOGGER.debug("UPS track POST for %s -> status %s", number, resp.status)
                if resp.status != 200:
                    raise UpsTrackingApiError(f"UPS answered HTTP {resp.status}")
                payload = await resp.json(content_type=None)
        except asyncio.TimeoutError as err:
            raise UpsTrackingApiError(
                "request timed out (UPS stops answering once its per-connection "
                "allowance is used up)",
                transient=True,
            ) from err
        except (ClientError, OSError, ValueError) as err:
            raise UpsTrackingApiError(f"network error: {err}") from err

        details = payload.get("trackDetails") if isinstance(payload, dict) else None
        if not isinstance(details, list) or not details or not isinstance(details[0], dict):
            raise UpsTrackingApiError("response carried no trackDetails")
        detail = details[0]
        if str(detail.get("errorCode") or "") == "504":
            return None  # tracking number not found in UPS's database
        if detail.get("errorCode"):
            raise UpsTrackingApiError(
                f"UPS error {detail.get('errorCode')}: {detail.get('errorText')}"
            )
        return normalize_ups_shipment(number, detail)


# Milestone key (stable, locale-independent) -> lifecycle group.
_MILESTONE_GROUP = {
    "cms.stapp.orderReceived": GROUP_REGISTERED,
    "cms.stapp.readyForShpmt": GROUP_REGISTERED,
    "cms.stapp.returnLabelCreated": GROUP_REGISTERED,
    "cms.stapp.weHaveYourPkg": GROUP_TRANSIT,
    "cms.stapp.inTransit": GROUP_TRANSIT,
    "cms.stapp.shipped": GROUP_TRANSIT,
    "cms.stapp.collected": GROUP_TRANSIT,
    "cms.stapp.pickedUpByUPS": GROUP_TRANSIT,
    "cms.stapp.clearedCustoms": GROUP_TRANSIT,
    "cms.stapp.clearedImprtCustoms": GROUP_TRANSIT,
    "cms.stapp.tenderedToUPSDeliveryAgent": GROUP_TRANSIT,
    "cms.stapp.dropoffAccessPoint": GROUP_TRANSIT,
    "cms.stapp.outForDelivery": GROUP_OUT_FOR_DELIVERY,
    "cms.stapp.deliveredToUAP": GROUP_OUT_FOR_DELIVERY,  # ready at a pickup point
    "cms.stapp.delivered": GROUP_DELIVERED,
    "cms.stapp.pkgIsDel": GROUP_DELIVERED,
    "cms.stapp.shipmentIsDelivered": GROUP_DELIVERED,
    "cms.stapp.customerPickUp": GROUP_DELIVERED,
    "cms.stapp.rfidConfirmedPickUp": GROUP_DELIVERED,
}

# packageStatusType -> group, used when no milestone decides.
_TYPE_GROUP = {
    "D": GROUP_DELIVERED,
    "RS": GROUP_DELIVERED,  # returned to shipper
    "O": GROUP_OUT_FOR_DELIVERY,
    "I": GROUP_TRANSIT,
    "P": GROUP_TRANSIT,
    "X": GROUP_TRANSIT,
    "M": GROUP_REGISTERED,
}


def _event(activity: dict) -> dict | None:
    scan = text(pick(activity, "activityScan"))
    if not scan:
        return None
    place = text(pick(activity, "location"))
    label = f"{scan} – {place}" if place else scan
    stamp = ""
    date = str(pick(activity, "date") or "")
    clock = str(pick(activity, "time") or "")
    try:
        day, month, year = date.split(".")
        hour, minute = clock.split(":")
        stamp = f"{year}-{int(month):02d}-{int(day):02d}T{int(hour):02d}:{int(minute):02d}:00"
        offset = str(pick(activity, "gmtOffset") or "")
        if len(offset) == 6 and offset[0] in "+-":
            stamp += offset
    except ValueError:
        stamp = f"{date} {clock}".strip()
    return {"datum": stamp, "status": label}


def normalize_ups_shipment(number: str, detail: dict) -> dict:
    """Turn a UPS ``trackDetails[0]`` into the shared shipment shape."""
    activities = [
        a for a in (pick(detail, "shipmentProgressActivities") or []) if isinstance(a, dict)
    ]
    events = [e for e in (_event(a) for a in activities) if e]

    status = text(pick(detail, "packageStatus")) or DEFAULT_STATUS
    status_type = str(pick(detail, "packageStatusType") or "").upper()

    milestone_group = GROUP_UNKNOWN
    for activity in activities:  # newest first
        milestone = pick(activity, "milestoneName")
        group = _MILESTONE_GROUP.get(str(pick(milestone, "nameKey") or ""))
        if group:
            milestone_group = group
            break

    delivered = as_bool(pick(detail, "isDelivered")) or status_type in ("D", "RS")
    if delivered:
        group = GROUP_DELIVERED
    elif status_type == "M":
        group = GROUP_REGISTERED
    elif milestone_group != GROUP_UNKNOWN:
        group = milestone_group
    else:
        group = _TYPE_GROUP.get(status_type, GROUP_UNKNOWN)

    return {
        "id": number,
        "carrier": CARRIER_UPS,
        "name": f"UPS {number}",
        "status": status,
        "group": group,
        "direction": None,
        "delivery_from": None,
        "delivery_to": None,
        "tracking_url": UPS_TRACKING_PAGE_URL.format(id=number.strip().upper()),
        "events": events,
        "delivered": group == GROUP_DELIVERED,
        "protected": False,
    }
