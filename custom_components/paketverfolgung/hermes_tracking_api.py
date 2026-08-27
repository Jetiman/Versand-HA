"""Client for Hermes Germany's public parcel-tracking JSON endpoint.

``api.my-deliveries.de`` is what the myhermes.de "Sendungsverfolgung" page
calls - it works by parcel number, no login and no postcode. Verified
against the live v2 endpoint 2026-08-27; the schema is undocumented, so
parsing stays defensive and the raw payload is logged at debug level.
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
from .tracking_util import as_bool, pick, text

_LOGGER = logging.getLogger(__name__)

# Hermes parcelStatus code -> lifecycle group (from live parcelProgress
# entries; the text heuristic below covers anything not listed here).
_STATUS_GROUP = {
    "ANNOUNCED": GROUP_REGISTERED,
    "ORDER_INFO_RECEIVED": GROUP_REGISTERED,
    "PREANNOUNCED": GROUP_REGISTERED,
    "SHIPMENT_PICKED_UP": GROUP_TRANSIT,
    "TAKEN_OVER_BY_HERMES": GROUP_TRANSIT,
    "HANDED_OVER_TO_HERMES": GROUP_TRANSIT,
    "IN_TRANSIT": GROUP_TRANSIT,
    "SORTED": GROUP_TRANSIT,
    "ARRIVED_AT_DEPOT": GROUP_TRANSIT,
    "ARRIVED_AT_DELIVERY_DEPOT": GROUP_TRANSIT,
    "DELIVERY_TOUR_STARTED": GROUP_OUT_FOR_DELIVERY,
    "OUT_FOR_DELIVERY": GROUP_OUT_FOR_DELIVERY,
    "NEXT_STOP": GROUP_OUT_FOR_DELIVERY,
    "READY_FOR_COLLECTION": GROUP_OUT_FOR_DELIVERY,
    "DELIVERED_HOMEDELIVERY": GROUP_DELIVERED,
    "DELIVERED_NEIGHBOUR": GROUP_DELIVERED,
    "DELIVERED_PARCELSHOP": GROUP_DELIVERED,
    "DELIVERED_PARCELBOX": GROUP_DELIVERED,
    "DELIVERED": GROUP_DELIVERED,
    "PICKED_UP_BY_RECIPIENT": GROUP_DELIVERED,
    "COLLECTED": GROUP_DELIVERED,
    "RETURN_TO_SENDER": GROUP_DELIVERED,
}

_TEXT_GROUP = (
    ("zugestellt", GROUP_DELIVERED),
    ("abgeholt", GROUP_DELIVERED),
    ("zustellfahrzeug", GROUP_OUT_FOR_DELIVERY),
    ("in zustellung", GROUP_OUT_FOR_DELIVERY),
    ("voraussichtlich heute", GROUP_OUT_FOR_DELIVERY),
    ("paketshop", GROUP_OUT_FOR_DELIVERY),
    ("übernommen", GROUP_TRANSIT),
    ("versand vorbereitet", GROUP_TRANSIT),
    ("unterwegs", GROUP_TRANSIT),
    ("sortier", GROUP_TRANSIT),
    ("angekündigt", GROUP_REGISTERED),
)


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
    # The v2 endpoint returns a list of shipments; take the first.
    if isinstance(payload, list):
        payload = payload[0] if payload else None
    if not isinstance(payload, dict) or not payload:
        return None

    attrs = pick(payload, "parcelAttributes") or {}
    progress = pick(payload, "parcelProgress") or []
    if not isinstance(progress, list):
        progress = []

    events = []
    for entry in progress:
        if not isinstance(entry, dict):
            continue
        when = text(pick(entry, "timestamp", "date"))
        label = (
            text(pick(entry, "headlineText"))
            or text(pick(entry, "historyText"))
            or text(pick(entry, "infoText"))
            or text(pick(entry, "parcelStatus"))
        )
        if not label and not when:
            continue
        events.append({"datum": when, "status": label})
    # parcelProgress is newest-first already.
    events.sort(key=lambda e: e.get("datum") or "", reverse=True)

    current = progress[0] if progress and isinstance(progress[0], dict) else {}
    status_code = str(pick(current, "parcelStatus") or "").upper()
    status_text = (
        text(pick(current, "headlineText"))
        or text(pick(current, "historyText"))
        or (events[0]["status"] if events else "")
    )

    delivered = as_bool(pick(attrs, "delivered")) or status_code.startswith("DELIVERED")

    group = _STATUS_GROUP.get(status_code, GROUP_UNKNOWN)
    if group == GROUP_UNKNOWN:
        haystack = status_text.lower()
        for needle, mapped in _TEXT_GROUP:
            if needle in haystack:
                group = mapped
                break
        else:
            group = GROUP_TRANSIT if events else GROUP_REGISTERED

    if not status_text and not events and not status_code:
        return None

    direction_enum = str(pick(attrs, "directionEnum") or "").upper()
    direction = "send" if direction_enum.startswith("SHIP") else "receive"

    sender = text(pick(pick(payload, "atg") or {}, "companyName"))
    return {
        "id": number,
        "carrier": "hermes",
        "name": sender or number,
        "status": status_text or DEFAULT_STATUS,
        "group": GROUP_DELIVERED if delivered else group,
        "direction": direction,
        "delivery_from": None,
        "delivery_to": None,
        "tracking_url": HERMES_TRACKING_PAGE_URL.format(id=number),
        "events": events,
        "delivered": delivered,
        "protected": False,
    }
