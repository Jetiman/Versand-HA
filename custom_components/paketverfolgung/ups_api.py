"""UPS My Choice client for Paketverfolgung.

UPS has no anonymous tracking - the consumer site is Akamai-bot-walled and
the official Track API needs a paid developer app plus a shipper account
number. So this logs in the way the UPS mobile app does (UPS My Choice)
and pulls the account's "shipments to my address" list. Reverse-engineered
via the open-source ioBroker.parcel adapter
(https://github.com/TA2k/ioBroker.parcel/blob/master/main.js). The
``AccessLicenseNumber`` is a public constant baked into the app.

Needs the free "UPS My Choice" service active on the ups.com account.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any

import aiohttp
from aiohttp import ClientError, ClientSession

from .const import (
    DEFAULT_STATUS,
    GROUP_DELIVERED,
    GROUP_OUT_FOR_DELIVERY,
    GROUP_REGISTERED,
    GROUP_TRANSIT,
    GROUP_UNKNOWN,
    UPS_ACCESS_LICENSE,
    UPS_LOGIN_URL,
    UPS_MCENROLLMENT_URL,
    UPS_MYCHOICE_URL,
    UPS_TRACKING_PAGE_URL,
)
from .tracking_util import as_bool, pick, text

_LOGGER = logging.getLogger(__name__)

_TEXT_GROUP = (
    ("zugestellt", GROUP_DELIVERED),
    ("delivered", GROUP_DELIVERED),
    ("abgeholt", GROUP_DELIVERED),
    ("picked up by", GROUP_DELIVERED),
    ("out for delivery", GROUP_OUT_FOR_DELIVERY),
    ("wird heute zugestellt", GROUP_OUT_FOR_DELIVERY),
    ("in zustellung", GROUP_OUT_FOR_DELIVERY),
    ("zustellfahrzeug", GROUP_OUT_FOR_DELIVERY),
    ("on its way", GROUP_TRANSIT),
    ("unterwegs", GROUP_TRANSIT),
    ("in transit", GROUP_TRANSIT),
    ("verarbeitet", GROUP_TRANSIT),
    ("scan", GROUP_TRANSIT),
    ("departed", GROUP_TRANSIT),
    ("arrived", GROUP_TRANSIT),
    ("abholung", GROUP_TRANSIT),
    ("origin", GROUP_TRANSIT),
    ("label", GROUP_REGISTERED),
    ("auftragsdaten", GROUP_REGISTERED),
    ("versandvorbereitung", GROUP_REGISTERED),
    ("bereit", GROUP_REGISTERED),
)

# packageStatusType (when present): coarse lifecycle code.
_TYPE_GROUP = {
    "M": GROUP_REGISTERED,
    "P": GROUP_TRANSIT,
    "I": GROUP_TRANSIT,
    "O": GROUP_OUT_FOR_DELIVERY,
    "D": GROUP_DELIVERED,
    "RS": GROUP_DELIVERED,
}


class UpsApiError(Exception):
    """Generic error talking to UPS."""


class UpsAuthError(UpsApiError):
    """Login failed or the session is no longer valid."""


@dataclass
class UpsSession:
    """A logged-in UPS My Choice session."""

    auth_token: str
    address_token: str


def _group(status: str, status_type: str | None) -> str:
    if status_type and status_type.upper() in _TYPE_GROUP:
        return _TYPE_GROUP[status_type.upper()]
    haystack = (status or "").lower()
    for needle, mapped in _TEXT_GROUP:
        if needle in haystack:
            return mapped
    return GROUP_UNKNOWN


def _events(raw: dict) -> list[dict]:
    out: list[dict] = []
    activities = (
        pick(raw, "activity")
        or pick(raw, "scanEvents")
        or pick(raw, "packageActivities")
        or []
    )
    if not isinstance(activities, list):
        activities = [activities]
    for act in activities:
        if not isinstance(act, dict):
            continue
        label = (
            text(pick(act, "activityScan", "status", "description", "statusDescription"))
            or text(pick(act, "message"))
        )
        when = text(pick(act, "date", "dateTime", "activityDateTime", "gmtDateTime"))
        loc = text(pick(act, "location", "city", "activityLocation"))
        if not label and not when:
            continue
        out.append({"datum": when, "status": label or DEFAULT_STATUS, "location": loc or None})
    out.sort(key=lambda e: e.get("datum") or "", reverse=True)
    return out


def normalize_ups_shipment(raw: dict) -> dict | None:
    """One UPS My Choice shipment -> the shared shipment shape."""
    if not isinstance(raw, dict):
        return None
    number = text(
        pick(raw, "trackingNumber", "trackingNbr", "packageTrackingNumber", "id")
    )
    if not number:
        return None
    status = text(
        pick(raw, "locStatus", "status", "packageStatus", "currentStatus", "milestone")
    ) or DEFAULT_STATUS
    status_type = text(pick(raw, "packageStatusType", "statusType", "milestoneCode"))
    events = _events(raw)
    group = _group(status, status_type)
    if group == GROUP_UNKNOWN:
        group = GROUP_TRANSIT if events else GROUP_REGISTERED
    delivered = (
        group == GROUP_DELIVERED
        or (status_type or "").upper() == "D"
        or as_bool(pick(raw, "delivered", "isDelivered"))
    )
    name = text(pick(raw, "shipFromName", "senderName", "shipperName")) or f"UPS {number}"
    return {
        "id": number,
        "carrier": "ups",
        "name": name,
        "status": status,
        "group": GROUP_DELIVERED if delivered else group,
        "direction": "receive",
        "delivery_from": None,
        "delivery_to": None,
        "tracking_url": UPS_TRACKING_PAGE_URL.format(id=number),
        "events": events,
        "delivered": delivered,
        "protected": False,
    }


class UpsAccountClient:
    """Logs into a ups.com account and lists its My Choice shipments."""

    def __init__(self, session: ClientSession) -> None:
        self._session = session

    async def login(self, username: str, password: str) -> UpsSession:
        auth_token = await self._login(username, password)
        address_token = await self._enrollment(auth_token)
        return UpsSession(auth_token=auth_token, address_token=address_token)

    async def _post(self, url: str, payload: dict, headers: dict | None = None) -> Any:
        try:
            async with self._session.post(
                url,
                json=payload,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": (
                        "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/122.0 Mobile Safari/537.36"
                    ),
                    **(headers or {}),
                },
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=25),
            ) as resp:
                body = await resp.text()
                if resp.status in (301, 302, 303, 307, 308):
                    # UPS/Akamai bounces non-browser clients to an error page.
                    raise UpsApiError(
                        "UPS redirected the request away (bot protection) - the "
                        f"UPS endpoint is not reachable from here (HTTP {resp.status})"
                    )
                if resp.status in (401, 403):
                    raise UpsAuthError(f"UPS returned HTTP {resp.status}")
                if resp.status >= 400:
                    raise UpsApiError(
                        f"UPS returned HTTP {resp.status}: {body[:200]}"
                    )
                try:
                    data = await resp.json(content_type=None)
                except ValueError as err:
                    raise UpsApiError(
                        f"UPS returned no JSON (body: {body[:150]!r})"
                    ) from err
                if not isinstance(data, dict):
                    raise UpsApiError(
                        f"UPS returned unexpected JSON: {repr(data)[:150]}"
                    )
                return data
        except UpsApiError:
            raise
        except (ClientError, asyncio.TimeoutError, OSError) as err:
            raise UpsApiError(f"Network error talking to UPS: {err}") from err

    async def _login(self, username: str, password: str) -> str:
        data = await self._post(
            UPS_LOGIN_URL,
            {
                "UPSSecurity": {
                    "UsernameToken": {},
                    "ServiceAccessToken": {"AccessLicenseNumber": UPS_ACCESS_LICENSE},
                },
                "LoginSubmitUserIdRequest": {
                    "UserId": username,
                    "Password": password,
                    "Locale": "de_DE",
                    "ClientID": "native",
                    "IsMobile": "true",
                },
            },
        )
        token = (
            ((data or {}).get("LoginSubmitUserIdResponse") or {}).get("LoginResponse")
            or {}
        ).get("AuthenticationToken")
        if not token:
            if "Legal Agreement" in text(data):
                raise UpsAuthError(
                    "UPS requires accepting the Legal Agreement - log in at "
                    "ups.com once and accept it, then retry."
                )
            raise UpsAuthError("UPS login failed (no authentication token)")
        return str(token)

    async def _enrollment(self, auth_token: str) -> str:
        data = await self._post(
            UPS_MCENROLLMENT_URL,
            {
                "UPSSecurity": {
                    "UsernameToken": {"AuthenticationToken": auth_token},
                    "ServiceAccessToken": {"AccessLicenseNumber": UPS_ACCESS_LICENSE},
                },
                "GetEnrollmentsRequest": {
                    "Request": {"RequestOption": ["00"], "TransactionReference": {}},
                    "Locale": {"Language": "de", "Country": "DE"},
                },
            },
        )
        summary = (
            ((data or {}).get("GetEnrollmentsResponse") or {}).get(
                "MYCEnrollmentSummaries"
            )
            or {}
        ).get("MYCEnrollmentSummary") or {}
        if isinstance(summary, list):
            summary = summary[0] if summary else {}
        token = summary.get("AddressToken")
        if not token:
            raise UpsAuthError(
                "No UPS My Choice address found - activate UPS My Choice for "
                "this account first."
            )
        return str(token)

    async def fetch_shipments(self, session: UpsSession) -> list[dict]:
        """Return the account's My Choice shipments as normalized dicts."""
        data = await self._post(
            UPS_MYCHOICE_URL,
            {"parcelCount": "25", "disableFeature": ""},
            headers={
                "AccessLicenseNumber": UPS_ACCESS_LICENSE,
                "AuthenticationToken": session.auth_token,
                "addresstoken": session.address_token,
                "transID": uuid.uuid4().hex[:25],
                "transactionSrc": "MOBILE",
            },
        )
        response = (data or {}).get("response") or data or {}
        shipments = (
            response.get("shipments")
            or response.get("shipment")
            or response.get("packages")
            or []
        )
        if not isinstance(shipments, list):
            shipments = [shipments]
        _LOGGER.debug("UPS My Choice: %d raw shipment(s)", len(shipments))
        out: list[dict] = []
        for raw in shipments:
            item = normalize_ups_shipment(raw)
            if item:
                out.append(item)
        return out
