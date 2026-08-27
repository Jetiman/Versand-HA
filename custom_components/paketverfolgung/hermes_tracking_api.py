"""Client for Hermes Germany's public parcel-tracking JSON endpoint.

``api.my-deliveries.de`` is what the myhermes.de "Sendungsverfolgung" page
calls - it works by parcel number, no login and no postcode. The response
schema isn't documented, so parsing is deliberately defensive (see
``tracking_util``); the raw payload is logged at debug level.
"""
from __future__ import annotations

import logging
from typing import Any

from aiohttp import ClientError, ClientSession

from .const import (
    DEFAULT_STATUS,
    GROUP_DELIVERED,
    GROUP_OUT_FOR_DELIVERY,
    GROUP_REGISTERED,
    GROUP_TRANSIT,
    GROUP_UNKNOWN,
    HERMES_PLC_URL,
    HERMES_TRACKING_PAGE_URL,
)
from .tracking_util import as_bool, first, pick, text

_LOGGER = logging.getLogger(__name__)

# Hermes parcelStatus code -> lifecycle group (best-effort; the text
# heuristic below covers anything not listed here).
_STATUS_GROUP = {
    "ANNOUNCED": GROUP_REGISTERED,
    "PREANNOUNCED": GROUP_REGISTERED,
    "NOTIFICATION": GROUP_REGISTERED,
    "DATA_RECEIVED": GROUP_REGISTERED,
    "ORDER_RECEIVED": GROUP_REGISTERED,
    "PICKED_UP": GROUP_TRANSIT,
    "IN_TRANSPORT": GROUP_TRANSIT,
    "IN_TRANSIT": GROUP_TRANSIT,
    "SORTED": GROUP_TRANSIT,
    "ARRIVED": GROUP_TRANSIT,
    "OUT_FOR_DELIVERY": GROUP_OUT_FOR_DELIVERY,
    "IN_DELIVERY": GROUP_OUT_FOR_DELIVERY,
    "DELIVERY": GROUP_OUT_FOR_DELIVERY,
    "DELIVERED": GROUP_DELIVERED,
    "DELIVERED_NEIGHBOUR": GROUP_DELIVERED,
    "DELIVERED_SHOP": GROUP_DELIVERED,
    "DELIVERED_PARCELSHOP": GROUP_DELIVERED,
    "PICKED_UP_BY_RECIPIENT": GROUP_DELIVERED,
    "RETURNED": GROUP_DELIVERED,
    "RETURN_TO_SENDER": GROUP_DELIVERED,
}

_TEXT_GROUP = (
    ("zugestellt", GROUP_DELIVERED),
    ("abgeholt", GROUP_DELIVERED),
    ("zustellfahrzeug", GROUP_OUT_FOR_DELIVERY),
    ("in zustellung", GROUP_OUT_FOR_DELIVERY),
    ("wird heute", GROUP_OUT_FOR_DELIVERY),
    ("paketshop", GROUP_OUT_FOR_DELIVERY),
    ("servicepartner", GROUP_OUT_FOR_DELIVERY),
    ("unterwegs", GROUP_TRANSIT),
    ("sortier", GROUP_TRANSIT),
    ("logistik", GROUP_TRANSIT),
    ("übernommen", GROUP_TRANSIT),
    ("transport", GROUP_TRANSIT),
    ("angekündigt", GROUP_REGISTERED),
    ("auftrag", GROUP_REGISTERED),
    ("beauftragt", GROUP_REGISTERED),
)

_HISTORY_KEYS = ("history", "parcelHistory", "statusHistory", "events", "trackingHistory")


class HermesTrackingApiError(Exception):
    """Error talking to the Hermes tracking endpoint."""


class HermesTrackingApiClient:
    """Fetches a single parcel's status + history from Hermes Germany."""

    def __init__(self, session: ClientSession) -> None:
        self._session = session

    async def fetch(self, number: str) -> dict | None:
        """Return a normalized shipment dict, or None if Hermes doesn't know it.

        Raises HermesTrackingApiError on transport problems so the caller
        can keep the last-known state.
        """
        headers = {
            "accept": "application/json",
            "user-agent": (
                "Mozilla/5.0 (X11; Linux x86_64; rv:132.0) "
                "Gecko/20100101 Firefox/132.0"
            ),
            "referer": "https://www.myhermes.de/",
            "accept-language": "de-de",
        }
        try:
            async with self._session.get(
                HERMES_PLC_URL.format(id=number), headers=headers, timeout=20
            ) as resp:
                if resp.status in (400, 404):
                    return None
                if resp.status != 200:
                    raise HermesTrackingApiError(
                        f"Hermes tracking for {number} returned status {resp.status}"
                    )
                payload = await resp.json(content_type=None)
        except ClientError as err:
            raise HermesTrackingApiError(
                f"Network error fetching Hermes tracking for {number}: {err}"
            ) from err
        except ValueError as err:  # bad JSON
            raise HermesTrackingApiError(
                f"Hermes tracking for {number} returned no JSON: {err}"
            ) from err

        _LOGGER.debug("Hermes response for %s: %s", number, payload)
        return _parse(number, payload)


def _parse(number: str, payload: Any) -> dict | None:
    data = payload
    if isinstance(payload, dict):
        data = (
            pick(payload, "parcelDetails", "parcel", "data", "result")
            if not pick(payload, "status", "parcelStatus")
            else payload
        ) or payload
    if not isinstance(data, dict) or not data:
        return None

    status_obj = pick(data, "status") or {}
    status_code = str(
        pick(status_obj, "parcelStatus", "statusCode", "code")
        or pick(data, "parcelStatus", "status")
        or ""
    ).upper()
    status_text = (
        text(pick(status_obj, "text"))
        or text(pick(status_obj, "longText", "shortText", "description"))
        or text(pick(data, "statusText"))
    )

    events = _events(data)
    if not status_text and events:
        status_text = events[0]["status"]

    group = _STATUS_GROUP.get(status_code, GROUP_UNKNOWN)
    if group == GROUP_UNKNOWN:
        haystack = f"{status_code} {status_text}".lower()
        for needle, mapped in _TEXT_GROUP:
            if needle in haystack:
                group = mapped
                break
        else:
            group = GROUP_TRANSIT if events else GROUP_REGISTERED

    delivered = group == GROUP_DELIVERED or as_bool(
        pick(data, "delivered", "isDelivered")
    )

    # Nothing that looks like a real parcel? Treat as "unknown to Hermes".
    if not status_text and not events and status_code == "":
        return None

    name = text(pick(data, "parcelReference", "reference", "customerReference")) or number
    return {
        "id": number,
        "carrier": "hermes",
        "name": name,
        "status": status_text or DEFAULT_STATUS,
        "group": GROUP_DELIVERED if delivered else group,
        "direction": "receive",
        "delivery_from": None,
        "delivery_to": None,
        "tracking_url": HERMES_TRACKING_PAGE_URL.format(id=number),
        "events": events,
        "delivered": delivered,
        "protected": False,
    }


def _events(data: dict) -> list[dict]:
    raw = None
    for key in _HISTORY_KEYS:
        candidate = pick(data, key)
        if isinstance(candidate, list) and candidate:
            raw = candidate
            break
    if raw is None:
        return []

    events: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        when = text(
            pick(entry, "timestamp", "datetime", "date", "statusTimestamp", "eventTime")
        )
        label = (
            text(pick(entry, "text"))
            or text(pick(entry, "longText", "shortText", "description", "statusText"))
            or text(pick(entry, "status"))
        )
        location = text(pick(entry, "location", "place", "city")) or text(
            first(pick(entry, "locations"))
        )
        if not label and not when:
            continue
        if location and location.lower() not in label.lower():
            label = f"{label} ({location})" if label else location
        events.append({"datum": when, "status": label})

    events.sort(key=lambda e: e.get("datum") or "", reverse=True)
    return events
