"""DataUpdateCoordinators for the Paketverfolgung integration.

Two coordinators, both producing the same normalized ``dict[id, shipment]``
shape so the sensor/panel code is carrier-agnostic:

* ``TrackingNumbersDataUpdateCoordinator`` - a manually-managed list of
  tracking numbers. Each number's carrier (DHL, DPD or Hermes) is
  auto-detected and remembered, then only that carrier is queried on
  later refreshes.
* ``DpdAccountDataUpdateCoordinator`` - every parcel on a myDPD account,
  enriched with the full scan history from DPD's public tracking API.
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CARRIER_DHL,
    CARRIER_DPD,
    CARRIER_HERMES,
    CARRIER_UNKNOWN,
    CONF_DEFAULT_POSTCODE,
    CONF_DPD_PASSWORD,
    CONF_DPD_USERNAME,
    CONF_TRACKING_NUMBERS,
    DEFAULT_STATUS,
    DOMAIN,
    DPD_STATUS_GROUP,
    DPD_TRACKING_PAGE_URL,
    GROUP_DELIVERED,
    GROUP_REGISTERED,
    GROUP_UNKNOWN,
    NO_DATA_STATUS,
    PROGRESS_GROUP,
    PROGRESS_STATUS,
    TRACKING_PAGE_URL,
)
from .dhl_api import DhlApiClient, DhlApiError
from .dpd_api import DpdApiClient, DpdApiError, DpdAuthError, DpdSession
from .dpd_tracking_api import DpdTrackingApiClient, DpdTrackingApiError
from .hermes_tracking_api import HermesTrackingApiClient, HermesTrackingApiError

_LOGGER = logging.getLogger(__name__)

# Cap on DPD public-tracking calls per account poll (history enrichment),
# so a large myDPD account doesn't fan out into dozens of HTTP requests.
_HISTORY_FETCHES_PER_POLL = 12


def normalize_dhl_shipment(raw: dict) -> dict:
    """Turn a raw DHL search result into the shared shipment shape."""
    info = raw.get("sendungsinfo", {})
    details = raw.get("sendungsdetails", {})
    verlauf = details.get("sendungsverlauf", {})
    zustellung = details.get("zustellung", {})
    fortschritt = verlauf.get("fortschritt")

    status = (
        verlauf.get("kurzStatus")
        or verlauf.get("status")
        or PROGRESS_STATUS.get(fortschritt, DEFAULT_STATUS)
    )
    # DHL returns the full history oldest-first; the panel wants newest first.
    events = [
        {"datum": event.get("datum"), "status": event.get("status")}
        for event in verlauf.get("events", [])
        if event.get("status")
    ]
    events.reverse()

    shipment_id = raw["id"]
    return {
        "id": shipment_id,
        "carrier": CARRIER_DHL,
        "name": info.get("sendungsname") or f"DHL {shipment_id}",
        "status": status,
        "group": PROGRESS_GROUP.get(fortschritt, GROUP_UNKNOWN),
        "direction": info.get("sendungsrichtung"),
        "delivery_from": zustellung.get("zustellzeitfensterVon"),
        "delivery_to": zustellung.get("zustellzeitfensterBis"),
        "tracking_url": TRACKING_PAGE_URL.format(id=shipment_id),
        "events": events,
        "delivered": fortschritt == 5,
        "protected": False,
    }


def normalize_dpd_parcel(raw: dict, events: list[dict] | None) -> dict:
    """Turn a raw myDPD SOAP parcel into the shared shipment shape."""
    group = DPD_STATUS_GROUP.get(raw.get("status_id"), GROUP_UNKNOWN)
    delivered = bool(raw.get("delivered")) or group == GROUP_DELIVERED
    parcel_id = raw["id"]
    return {
        "id": parcel_id,
        "carrier": CARRIER_DPD,
        "name": raw.get("name") or f"DPD {parcel_id}",
        "status": raw.get("status") or DEFAULT_STATUS,
        "group": GROUP_DELIVERED if delivered else group,
        "direction": raw.get("direction"),
        "delivery_from": None,
        "delivery_to": None,
        "tracking_url": DPD_TRACKING_PAGE_URL.format(id=parcel_id),
        "events": events or [],
        "delivered": delivered,
        "protected": False,
    }


def _placeholder(number: str, carrier: str) -> dict:
    """Shipment shown for a number neither carrier has data for (yet)."""
    return {
        "id": number,
        "carrier": carrier,
        "name": number,
        "status": NO_DATA_STATUS,
        "group": GROUP_UNKNOWN if carrier == CARRIER_UNKNOWN else GROUP_REGISTERED,
        "direction": None,
        "delivery_from": None,
        "delivery_to": None,
        "tracking_url": None,
        "events": [],
        "delivered": False,
        "protected": False,
    }


class TrackingNumbersDataUpdateCoordinator(DataUpdateCoordinator[dict[str, dict]]):
    """Tracks a manually-managed list of DHL / DPD / Hermes tracking numbers."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, update_interval) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=update_interval)
        self.entry = entry
        session = async_get_clientsession(hass)
        self.dhl = DhlApiClient(session)
        self.dpd = DpdTrackingApiClient(session)
        self.hermes = HermesTrackingApiClient(session)
        # number -> detected carrier; kept in memory (re-detected after a
        # restart, which just means one extra probe per number).
        self.carriers: dict[str, str] = {}

    def _config(self, key, default=None):
        return self.entry.options.get(key, self.entry.data.get(key, default))

    async def _async_update_data(self) -> dict[str, dict]:
        numbers = [
            str(n).strip()
            for n in self._config(CONF_TRACKING_NUMBERS, [])
            if str(n).strip()
        ]
        postcode = (self._config(CONF_DEFAULT_POSTCODE) or "").strip() or None
        if not numbers:
            self.carriers.clear()
            return {}

        dhl_by_id = await self._dhl_lookup(
            [
                n
                for n in numbers
                if self.carriers.get(n, CARRIER_UNKNOWN)
                not in (CARRIER_DPD, CARRIER_HERMES)
            ]
        )

        result: dict[str, dict] = {}
        for number in numbers:
            carrier = self.carriers.get(number, CARRIER_UNKNOWN)
            item: dict | None = None

            if number in dhl_by_id:
                item = normalize_dhl_shipment(dhl_by_id[number])
                carrier = CARRIER_DHL
            elif carrier != CARRIER_DHL:
                # Try DPD then Hermes, respecting an already-detected carrier.
                if carrier in (CARRIER_UNKNOWN, CARRIER_DPD):
                    item = await self._dpd_lookup(number, postcode)
                    if item is not None:
                        carrier = CARRIER_DPD
                if item is None and carrier in (CARRIER_UNKNOWN, CARRIER_HERMES):
                    item = await self._hermes_lookup(number)
                    if item is not None:
                        carrier = CARRIER_HERMES

            if item is None:
                # Nothing resolved this cycle - keep the last real data
                # (transient carrier outage, or still-propagating number)
                # rather than blanking the sensor.
                previous = (self.data or {}).get(number)
                if previous and previous.get("status") != NO_DATA_STATUS:
                    item = previous
                    carrier = previous.get("carrier", carrier)
                else:
                    item = _placeholder(number, carrier)

            item["carrier"] = carrier
            self.carriers[number] = carrier
            result[number] = item

        for stale in [n for n in self.carriers if n not in numbers]:
            self.carriers.pop(stale, None)
        return result

    async def _dhl_lookup(self, candidates: list[str]) -> dict[str, dict]:
        if not candidates:
            return {}
        try:
            shipments = await self.dhl.fetch_shipments(candidates)
        except DhlApiError as err:
            # A carrier being briefly unreachable shouldn't blank every
            # sensor - keep the last data by not raising UpdateFailed.
            _LOGGER.warning("DHL lookup failed: %s", err)
            return {}
        return {s["id"]: s for s in shipments if s.get("id")}

    async def _dpd_lookup(self, number: str, postcode: str | None) -> dict | None:
        try:
            return await self.dpd.fetch(number, postcode)
        except DpdTrackingApiError as err:
            _LOGGER.warning("DPD lookup for %s failed: %s", number, err)
            return self._keep_previous(number, CARRIER_DPD)

    async def _hermes_lookup(self, number: str) -> dict | None:
        try:
            return await self.hermes.fetch(number)
        except HermesTrackingApiError as err:
            _LOGGER.warning("Hermes lookup for %s failed: %s", number, err)
            return self._keep_previous(number, CARRIER_HERMES)

    def _keep_previous(self, number: str, carrier: str) -> dict | None:
        existing = (self.data or {}).get(number)
        return existing if existing and existing.get("carrier") == carrier else None


class DpdAccountDataUpdateCoordinator(DataUpdateCoordinator[dict[str, dict]]):
    """Fetches every parcel on a myDPD account, with full scan history."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, update_interval) -> None:
        super().__init__(
            hass, _LOGGER, name=f"{DOMAIN}_dpd", update_interval=update_interval
        )
        self.entry = entry
        session = async_get_clientsession(hass)
        self.client = DpdApiClient(session)
        self.tracking = DpdTrackingApiClient(session)
        # Kept in memory only (see the note in the old coordinator): DPD
        # rotates the token on most calls and persisting it would reload
        # the entry on every poll.
        self._session: DpdSession | None = None
        self._events: dict[str, list[dict]] = {}

    async def _async_update_data(self) -> dict[str, dict]:
        if self._session is None:
            await self._login()

        try:
            parcels, self._session = await self.client.fetch_parcels(self._session)
        except DpdAuthError:
            _LOGGER.info("DPD session expired, logging in again")
            await self._login()
            try:
                parcels, self._session = await self.client.fetch_parcels(self._session)
            except DpdApiError as err:
                raise UpdateFailed(f"Error fetching DPD parcels: {err}") from err
        except DpdApiError as err:
            raise UpdateFailed(f"Error fetching DPD parcels: {err}") from err

        postcode = (
            self.entry.options.get(CONF_DEFAULT_POSTCODE)
            or self.entry.data.get(CONF_DEFAULT_POSTCODE)
            or ""
        ).strip() or None

        # In-flight parcels first, then delivered ones - so a large account
        # (mostly old delivered parcels) backfills its history a few per
        # poll instead of firing dozens of HTTP calls at once.
        ordered = sorted(
            (p for p in parcels if p.get("id")),
            key=lambda p: bool(p.get("delivered")),
        )
        budget = _HISTORY_FETCHES_PER_POLL

        result: dict[str, dict] = {}
        for parcel in ordered:
            parcel_id = parcel["id"]
            events = self._events.get(parcel_id, [])
            # Refresh history for in-flight parcels; for delivered ones only
            # backfill once (when we have nothing cached).
            wants_history = not parcel.get("delivered") or not events
            if wants_history and budget > 0:
                budget -= 1
                fetched = await self._history(parcel_id, postcode)
                if fetched:
                    events = fetched
                    self._events[parcel_id] = fetched
            result[parcel_id] = normalize_dpd_parcel(parcel, events)

        for stale in [p for p in self._events if p not in result]:
            self._events.pop(stale, None)
        _LOGGER.debug("Paketverfolgung (DPD): %d parcel(s) fetched", len(result))
        return result

    async def _history(self, parcel_id: str, postcode: str | None) -> list[dict]:
        try:
            detail = await self.tracking.fetch(parcel_id, postcode)
        except DpdTrackingApiError as err:
            _LOGGER.debug("DPD history for %s failed: %s", parcel_id, err)
            return []
        return detail.get("events", []) if detail else []

    async def _login(self) -> None:
        username = self.entry.data[CONF_DPD_USERNAME]
        password = self.entry.data[CONF_DPD_PASSWORD]
        try:
            self._session = await self.client.login(username, password)
        except DpdAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err


# Backwards-compatible aliases (old names used elsewhere / in the wild).
DhlDataUpdateCoordinator = TrackingNumbersDataUpdateCoordinator
DpdDataUpdateCoordinator = DpdAccountDataUpdateCoordinator
