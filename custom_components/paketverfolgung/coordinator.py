"""DataUpdateCoordinators for the Paketverfolgung integration (DHL, DPD)."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CONF_DPD_PASSWORD, CONF_DPD_USERNAME, CONF_TRACKING_NUMBERS, DOMAIN
from .dhl_api import DhlApiClient, DhlApiError
from .dpd_api import DpdApiClient, DpdApiError, DpdAuthError, DpdSession

_LOGGER = logging.getLogger(__name__)


class DhlDataUpdateCoordinator(DataUpdateCoordinator[dict[str, dict]]):
    """Fetches details for the tracked DHL shipments."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        update_interval,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=update_interval,
        )
        self.entry = entry
        self.client = DhlApiClient(async_get_clientsession(hass))

    async def _async_update_data(self) -> dict[str, dict]:
        tracking_numbers = self.entry.options.get(
            CONF_TRACKING_NUMBERS,
            self.entry.data.get(CONF_TRACKING_NUMBERS, []),
        )
        try:
            shipments = await self.client.fetch_shipments(tracking_numbers)
        except DhlApiError as err:
            raise UpdateFailed(f"Error fetching DHL shipments: {err}") from err

        _LOGGER.debug("Paketverfolgung: %d shipment(s) fetched", len(shipments))
        return {s["id"]: s for s in shipments if s.get("id")}


class DpdDataUpdateCoordinator(DataUpdateCoordinator[dict[str, dict]]):
    """Fetches parcels from a DPD (myDPD) account."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        update_interval,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_dpd",
            update_interval=update_interval,
        )
        self.entry = entry
        self.client = DpdApiClient(async_get_clientsession(hass))
        # Kept in memory only (not persisted to entry.data): DPD rotates the
        # token on most calls, and persisting it via async_update_entry would
        # fire our own options-update listener and reload the entry on every
        # poll. Re-logging in after a restart is cheap (two SOAP calls), so
        # there's no real downside to not persisting it.
        self._session: DpdSession | None = None

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

        _LOGGER.debug("Paketverfolgung (DPD): %d parcel(s) fetched", len(parcels))
        return {p["id"]: p for p in parcels if p.get("id")}

    async def _login(self) -> None:
        username = self.entry.data[CONF_DPD_USERNAME]
        password = self.entry.data[CONF_DPD_PASSWORD]
        try:
            self._session = await self.client.login(username, password)
        except DpdAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
