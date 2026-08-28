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
from datetime import datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CARRIER_DHL,
    CARRIER_DPD,
    CARRIER_HERMES,
    CARRIER_UNKNOWN,
    CARRIERS,
    CONF_CARRIER_OVERRIDES,
    CONF_DEFAULT_POSTCODE,
    CONF_DHL_AUTO_DISCOVERY,
    CONF_DHL_SESSION,
    CONF_NAMES,
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
from .dhl_account import DhlAccountClient, DhlAuthError
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


def _has_data(item: dict | None) -> bool:
    """True if a lookup returned real tracking substance (so the number can
    be *locked* to that carrier). A soft result - a DPD "postcode needed"
    placeholder, an empty shell - is shown but must not lock, so the other
    carriers still get probed next cycle.
    """
    if not item or item.get("protected"):
        return False
    return bool(
        item.get("events")
        or item.get("delivered")
        or item.get("group") not in (None, GROUP_UNKNOWN)
    )


def _dhl_has_data(raw: dict) -> bool:
    """True if DHL actually knows this shipment (not just an echo shell).

    DHL's search echoes unknown piececodes back with an otherwise-empty
    sendungsverlauf, which would be mistaken for a freshly-registered DHL
    shipment and wrongly lock the number to DHL (e.g. a Hermes number).
    """
    details = raw.get("sendungsdetails") or {}
    if raw.get("sendungNichtGefunden") or details.get("sendungNichtGefunden"):
        return False
    verlauf = details.get("sendungsverlauf") or {}
    return bool(
        verlauf.get("kurzStatus")
        or verlauf.get("status")
        or verlauf.get("events")
        or details.get("zustellung")
        or details.get("istZugestellt")
    )


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


class _BaseCoordinator(DataUpdateCoordinator[dict[str, dict]]):
    """Adds "when is the next poll" bookkeeping for the panel countdown."""

    last_poll: datetime | None = None

    @property
    def next_poll(self) -> datetime | None:
        if self.last_poll is None or self.update_interval is None:
            return None
        return self.last_poll + self.update_interval

    def _mark_polled(self) -> None:
        self.last_poll = dt_util.utcnow()


class TrackingNumbersDataUpdateCoordinator(_BaseCoordinator):
    """Tracks a manually-managed list of DHL / DPD / Hermes tracking numbers."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, update_interval) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=update_interval)
        self.entry = entry
        session = async_get_clientsession(hass)
        self.dhl = DhlApiClient(session)
        self.dpd = DpdTrackingApiClient(session)
        self.hermes = HermesTrackingApiClient(session)
        self.dhl_account = DhlAccountClient(session)
        # number -> detected carrier; kept in memory (re-detected after a
        # restart, which just means one extra probe per number).
        self.carriers: dict[str, str] = {}
        # last DHL-account-discovery outcome, for diagnostics
        self.dhl_account_status: str | None = None

    def _config(self, key, default=None):
        return self.entry.options.get(key, self.entry.data.get(key, default))

    async def _async_update_data(self) -> dict[str, dict]:
        self._mark_polled()
        numbers = [
            str(n).strip()
            for n in self._config(CONF_TRACKING_NUMBERS, [])
            if str(n).strip()
        ]
        numbers = await self._merge_dhl_account_numbers(numbers)
        postcode = (self._config(CONF_DEFAULT_POSTCODE) or "").strip() or None
        overrides = {
            str(k).strip(): v
            for k, v in (self._config(CONF_CARRIER_OVERRIDES, {}) or {}).items()
            if v in CARRIERS
        }
        names = {
            str(k).strip(): str(v).strip()
            for k, v in (self._config(CONF_NAMES, {}) or {}).items()
            if str(v).strip()
        }
        if not numbers:
            self.carriers.clear()
            return {}

        # Only batch-query DHL for numbers that could still be DHL: not
        # locked to another carrier, and not pinned to a non-DHL one.
        dhl_by_id = await self._dhl_lookup(
            [
                n
                for n in numbers
                if overrides.get(n, CARRIER_DHL) == CARRIER_DHL
                and self.carriers.get(n, CARRIER_UNKNOWN)
                not in (CARRIER_DPD, CARRIER_HERMES)
            ]
        )

        result: dict[str, dict] = {}
        for number in numbers:
            forced = overrides.get(number)
            known = self.carriers.get(number, CARRIER_UNKNOWN)
            carrier = forced or known
            item: dict | None = None

            raw_dhl = dhl_by_id.get(number)
            dhl_confirmed = raw_dhl is not None and _dhl_has_data(raw_dhl)

            if forced == CARRIER_DHL:
                item = normalize_dhl_shipment(raw_dhl) if raw_dhl is not None else None
            elif forced == CARRIER_DPD:
                item = await self._dpd_lookup(number, postcode)
            elif forced == CARRIER_HERMES:
                item = await self._hermes_lookup(number)
            elif dhl_confirmed:
                item, carrier = normalize_dhl_shipment(raw_dhl), CARRIER_DHL
            else:
                item, carrier = await self._detect(number, postcode, known)

            if item is None:
                # Nothing this cycle - keep the last real data (transient
                # carrier outage, still-propagating number) over blanking.
                previous = (self.data or {}).get(number)
                if previous and previous.get("status") != NO_DATA_STATUS:
                    item = previous
                    if not forced and carrier == CARRIER_UNKNOWN:
                        carrier = previous.get("carrier", carrier)
                else:
                    item = _placeholder(number, carrier)

            item["carrier"] = carrier
            item["forced"] = forced
            # Keep the carrier's own name so a cleared custom name reverts.
            item.setdefault("carrier_name", item.get("name"))
            item["custom_name"] = names.get(number)
            item["name"] = names.get(number) or item["carrier_name"]
            self.carriers[number] = carrier
            result[number] = item

        for stale in [n for n in self.carriers if n not in numbers]:
            self.carriers.pop(stale, None)
        return result

    async def _merge_dhl_account_numbers(self, numbers: list[str]) -> list[str]:
        """If DHL account discovery is on, add the account's shipment ids."""
        if not self._config(CONF_DHL_AUTO_DISCOVERY):
            self.dhl_account_status = None
            return numbers
        dhl_session = self.entry.data.get(CONF_DHL_SESSION)
        if not dhl_session:
            self.dhl_account_status = "Kein DHL-Login hinterlegt"
            _LOGGER.warning("Paketverfolgung: DHL-Erkennung an, aber kein Login")
            return numbers
        try:
            fresh = await self.dhl_account.ensure_fresh(dict(dhl_session))
            if fresh != dhl_session:
                self.hass.config_entries.async_update_entry(
                    self.entry,
                    data={**self.entry.data, CONF_DHL_SESSION: fresh},
                )
            account_ids = await self.dhl_account.fetch_shipment_ids(fresh)
        except DhlAuthError as err:
            self.dhl_account_status = str(err)
            _LOGGER.warning("Paketverfolgung: DHL-Kontoabfrage fehlgeschlagen: %s", err)
            return numbers

        merged = list(numbers)
        for shipment_id in account_ids:
            if shipment_id not in merged:
                merged.append(shipment_id)
                self.carriers.setdefault(shipment_id, CARRIER_DHL)
        self.dhl_account_status = f"{len(account_ids)} Sendung(en) erkannt"
        _LOGGER.debug("Paketverfolgung: DHL-Konto -> %s", account_ids)
        return merged

    async def _detect(
        self, number: str, postcode: str | None, known: str
    ) -> tuple[dict | None, str]:
        """Probe DPD then Hermes and return (item, carrier).

        A carrier is *locked* only on a substantive result. A soft result
        (DPD "postcode needed", an empty shell) is shown but leaves the
        number open for re-detection. An already-known carrier is probed
        first (cheap path) and its lock is kept through a transient
        failure as long as we still hold substantive history for it.
        """
        order = [CARRIER_HERMES, CARRIER_DPD] if known == CARRIER_HERMES else [
            CARRIER_DPD,
            CARRIER_HERMES,
        ]
        soft: dict | None = None
        for candidate in order:
            if candidate == CARRIER_DPD:
                res = await self._dpd_lookup(number, postcode)
            else:
                res = await self._hermes_lookup(number)
            if _has_data(res):
                return res, candidate
            if res is not None and soft is None:
                soft = res

        previous = (self.data or {}).get(number)
        if known in (CARRIER_DPD, CARRIER_HERMES) and _has_data(previous):
            return previous, known  # transient blip - keep the lock
        if soft is not None:
            return soft, CARRIER_UNKNOWN  # show it, stay open for re-detection
        return None, CARRIER_UNKNOWN

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
        """Fresh DPD result, or None on any failure (the caller decides
        whether to fall back to the last-known data)."""
        try:
            return await self.dpd.fetch(number, postcode)
        except DpdTrackingApiError as err:
            _LOGGER.warning("DPD lookup for %s failed: %s", number, err)
            return None

    async def _hermes_lookup(self, number: str) -> dict | None:
        try:
            return await self.hermes.fetch(number)
        except HermesTrackingApiError as err:
            _LOGGER.warning("Hermes lookup for %s failed: %s", number, err)
            return None


class DpdAccountDataUpdateCoordinator(_BaseCoordinator):
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
        self._mark_polled()
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
        names = {
            str(k).strip(): str(v).strip()
            for k, v in (self.entry.options.get(CONF_NAMES, {}) or {}).items()
            if str(v).strip()
        }

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
            item = normalize_dpd_parcel(parcel, events)
            item["forced"] = None
            item["carrier_name"] = item["name"]
            item["custom_name"] = names.get(parcel_id)
            item["name"] = names.get(parcel_id) or item["carrier_name"]
            result[parcel_id] = item

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
