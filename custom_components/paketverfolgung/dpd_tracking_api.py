"""Client for DPD's public "parcel life cycle" (PLC) tracking endpoint.

This is the JSON API the consumer tracking page at tracking.dpd.de calls.
It works by parcel number without a login and returns the full scan
history - unlike the myDPD SOAP account API (``dpd_api.py``), which only
exposes the latest status. Some parcels are postcode-protected and need
the recipient ZIP passed as ``?zip=``.

The response schema isn't officially documented, so parsing here is
deliberately defensive: unknown/renamed keys degrade to ``None`` rather
than raising, and the raw payload is logged at debug level.
"""
from __future__ import annotations

import logging
from typing import Any

from aiohttp import ClientError, ClientSession

from .const import (
    DEFAULT_STATUS,
    DPD_PLC_URL,
    DPD_TRACKING_PAGE_URL,
    GROUP_DELIVERED,
    GROUP_OUT_FOR_DELIVERY,
    GROUP_REGISTERED,
    GROUP_TRANSIT,
    GROUP_UNKNOWN,
    USER_AGENT,
)
from .tracking_util import as_bool as _bool
from .tracking_util import first as _first
from .tracking_util import pick as _get
from .tracking_util import text as _text

_LOGGER = logging.getLogger(__name__)

# Milestone code (from the response's top-level statusInfo list) -> group.
_MILESTONE_GROUP = {
    "ACCEPTED": GROUP_REGISTERED,
    "ORDER_INFORMATION_RECEIVED": GROUP_REGISTERED,
    "COLLECTED": GROUP_TRANSIT,
    "ON_THE_ROAD": GROUP_TRANSIT,
    "AT_DELIVERY_DEPOT": GROUP_TRANSIT,
    "OUT_FOR_DELIVERY": GROUP_OUT_FOR_DELIVERY,
    "DELIVERED": GROUP_DELIVERED,
}

# Fallback: substrings in the current status text -> group (German page).
_TEXT_GROUP = (
    ("zugestellt", GROUP_DELIVERED),
    ("abgeholt vom paketshop", GROUP_DELIVERED),
    ("zustellfahrzeug", GROUP_OUT_FOR_DELIVERY),
    ("in zustellung", GROUP_OUT_FOR_DELIVERY),
    ("wird zugestellt", GROUP_OUT_FOR_DELIVERY),
    ("paketshop", GROUP_OUT_FOR_DELIVERY),
    ("paketzentrum", GROUP_TRANSIT),
    ("depot", GROUP_TRANSIT),
    ("unterwegs", GROUP_TRANSIT),
    ("sortier", GROUP_TRANSIT),
    ("auftragsdaten", GROUP_REGISTERED),
    ("angekündigt", GROUP_REGISTERED),
)


class DpdTrackingApiError(Exception):
    """Error talking to DPD's public tracking endpoint."""


class DpdTrackingApiClient:
    """Fetches a single parcel's status + history from tracking.dpd.de."""

    def __init__(self, session: ClientSession) -> None:
        self._session = session

    async def fetch(self, number: str, postcode: str | None = None) -> dict | None:
        """Return a normalized shipment dict, or None if DPD doesn't know it.

        Raises DpdTrackingApiError on network/transport problems (so the
        caller can keep a previously-known state instead of dropping the
        parcel). A postcode-protected parcel we can't unlock comes back as
        a dict with ``protected: True`` and minimal fields.
        """
        params = {"zip": postcode} if postcode else None
        headers = {
            "accept": "application/json",
            "user-agent": USER_AGENT,
            "accept-language": "de-de",
        }
        try:
            async with self._session.get(
                DPD_PLC_URL.format(id=number),
                params=params,
                headers=headers,
                timeout=20,
            ) as resp:
                if resp.status == 404:
                    return None
                if resp.status != 200:
                    raise DpdTrackingApiError(
                        f"DPD tracking for {number} returned status {resp.status}"
                    )
                payload = await resp.json(content_type=None)
        except ClientError as err:
            raise DpdTrackingApiError(
                f"Network error fetching DPD tracking for {number}: {err}"
            ) from err
        except ValueError as err:  # bad JSON
            raise DpdTrackingApiError(
                f"DPD tracking for {number} returned no JSON: {err}"
            ) from err

        _LOGGER.debug("DPD PLC response for %s: %s", number, payload)
        return self._parse(number, payload, bool(postcode))

    def _parse(self, number: str, payload: Any, had_postcode: bool) -> dict | None:
        response = (
            _get(payload, "parcellifecycleResponse", "parcelLifecycleResponse")
            or payload
            or {}
        )
        data = _get(response, "parcelLifeCycleData", "parcelLifecycleData")

        if not data:
            error = _text(
                _get(response, "errorJSON", "errorCode", "error", "message")
            ) or _text(_get(payload, "errorJSON", "errorCode", "error", "message"))
            error_l = error.lower()
            if any(w in error_l for w in ("protect", "zip", "postal", "plz")):
                return {
                    "id": number,
                    "carrier": "dpd",
                    "name": number,
                    "status": (
                        "Postleitzahl nötig"
                        if not had_postcode
                        else "Sendung nicht gefunden"
                    ),
                    "group": GROUP_UNKNOWN,
                    "direction": "receive",
                    "delivery_from": None,
                    "delivery_to": None,
                    "tracking_url": DPD_TRACKING_PAGE_URL.format(id=number),
                    "events": [],
                    "delivered": False,
                    "protected": not had_postcode,
                }
            _LOGGER.debug("DPD PLC: no parcelLifeCycleData for %s (%s)", number, error)
            return None

        shipment_info = _get(data, "shipmentInfo") or {}
        milestones = _get(data, "statusInfo") or []
        scans = _get(_get(data, "scanInfo") or {}, "scan") or []

        events = self._events(scans)
        status_text, group = self._current(milestones, scans, events)
        delivered = group == GROUP_DELIVERED or _bool(
            _get(shipment_info, "isDelivered", "delivered")
        )

        name = (
            _text(_get(shipment_info, "parcelNumberReference", "reference"))
            or number
        )
        return {
            "id": number,
            "carrier": "dpd",
            "name": name,
            "status": status_text or DEFAULT_STATUS,
            "group": GROUP_DELIVERED if delivered else group,
            "direction": "receive",
            "delivery_from": None,
            "delivery_to": None,
            "tracking_url": DPD_TRACKING_PAGE_URL.format(id=number),
            "events": events,
            "delivered": delivered,
            "protected": False,
        }

    @staticmethod
    def _events(scans: list) -> list[dict]:
        events: list[dict] = []
        for scan in scans:
            if not isinstance(scan, dict):
                continue
            when = _text(_get(scan, "date", "dateTime", "scanDate"))
            scan_data = _get(scan, "scanData") or {}
            label = (
                _text(_get(scan, "scanDescription"))
                or _text(_get(scan_data, "scanDescription"))
                or _text(_first(_get(scan_data, "statusInfo")))
                or _text(_get(scan_data, "statusCode"))
            )
            location = _text(_get(scan, "location")) or _text(
                _get(scan_data, "location")
            )
            if not label and not when:
                continue
            if location and location not in label:
                label = f"{label} ({location})" if label else location
            events.append({"datum": when, "status": label})
        # DPD returns newest-first already; keep whatever order, newest first.
        events.sort(key=lambda e: e.get("datum") or "", reverse=True)
        return events

    @staticmethod
    def _current(
        milestones: list, scans: list, events: list[dict]
    ) -> tuple[str, str]:
        # Prefer the explicit "current" milestone if the payload marks one.
        current_label = ""
        group = GROUP_UNKNOWN
        reached = [
            m
            for m in milestones
            if isinstance(m, dict) and _bool(_get(m, "statusHasBeenReached", "reached"))
        ]
        for milestone in milestones:
            if not isinstance(milestone, dict):
                continue
            if _bool(_get(milestone, "isCurrentStatus", "current")):
                current_label = _text(_get(milestone, "label", "description"))
                code = str(_get(milestone, "status", "statusCode") or "").upper()
                group = _MILESTONE_GROUP.get(code, GROUP_UNKNOWN)
                break
        else:
            if reached:
                last = reached[-1]
                current_label = _text(_get(last, "label", "description"))
                code = str(_get(last, "status", "statusCode") or "").upper()
                group = _MILESTONE_GROUP.get(code, GROUP_UNKNOWN)

        if not current_label and events:
            current_label = events[0]["status"]

        if group == GROUP_UNKNOWN:
            haystack = current_label.lower()
            for needle, mapped in _TEXT_GROUP:
                if needle in haystack:
                    group = mapped
                    break
            else:
                group = GROUP_TRANSIT if events else GROUP_REGISTERED

        return current_label, group
