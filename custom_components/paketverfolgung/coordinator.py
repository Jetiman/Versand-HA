"""DataUpdateCoordinator for DHL shipments."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_AUTO_DISCOVERY,
    CONF_DHL_SESSION,
    CONF_TRACKING_NUMBERS,
    DOMAIN,
)
from .dhl_api import DhlApiClient, DhlApiError, DhlAuthError

_LOGGER = logging.getLogger(__name__)


class DhlDataUpdateCoordinator(DataUpdateCoordinator[dict[str, dict]]):
    """Fetch details for manually tracked and account-discovered DHL shipments."""

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
        tracking_numbers = list(
            self.entry.options.get(
                CONF_TRACKING_NUMBERS,
                self.entry.data.get(CONF_TRACKING_NUMBERS, []),
            )
        )
        auto_discovery = self.entry.options.get(
            CONF_AUTO_DISCOVERY,
            self.entry.data.get(CONF_AUTO_DISCOVERY, False),
        )

        try:
            if auto_discovery:
                session = self.entry.data.get(CONF_DHL_SESSION)
                if not session:
                    raise DhlAuthError("DHL auto-discovery is enabled but no session exists")

                refreshed = await self.client.refresh_session(dict(session))
                if refreshed != session:
                    self.hass.config_entries.async_update_entry(
                        self.entry,
                        data={**self.entry.data, CONF_DHL_SESSION: refreshed},
                    )
                    session = refreshed

                account_numbers = await self.client.fetch_account_tracking_numbers(session)
                for shipment_id in account_numbers:
                    if shipment_id not in tracking_numbers:
                        tracking_numbers.append(shipment_id)

            shipments = await self.client.fetch_shipments(tracking_numbers)
        except DhlAuthError as err:
            raise UpdateFailed(f"DHL account authentication error: {err}") from err
        except DhlApiError as err:
            raise UpdateFailed(f"Error fetching DHL shipments: {err}") from err

        _LOGGER.debug(
            "Paketverfolgung: %d shipment(s) fetched from %d tracking id(s)",
            len(shipments),
            len(tracking_numbers),
        )
        return {shipment["id"]: shipment for shipment in shipments if shipment.get("id")}
