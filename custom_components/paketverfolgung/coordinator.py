"""DataUpdateCoordinator for the tracked DHL shipments."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CONF_TRACKING_NUMBERS, DOMAIN
from .dhl_api import DhlApiClient, DhlApiError

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
