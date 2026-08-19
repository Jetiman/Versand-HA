"""DataUpdateCoordinator for a DHL account (Paketverfolgung integration)."""
from __future__ import annotations

import logging
import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_EXPIRES_AT,
    CONF_ID_TOKEN,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    TOKEN_REFRESH_MARGIN_SECONDS,
)
from .dhl_api import DhlApiClient, DhlApiError, DhlAuthError, TokenSet

_LOGGER = logging.getLogger(__name__)


class DhlDataUpdateCoordinator(DataUpdateCoordinator[dict[str, dict]]):
    """Fetches DHL shipments and keeps the login token fresh."""

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
        # Dedicated session so this integration's cookies (login + the
        # session/CSRF cookie DHL sets on first contact and expects back
        # on the next request) aren't mixed into the HA-shared session's
        # jar. Home Assistant closes it automatically on entry unload.
        self.client = DhlApiClient(async_create_clientsession(hass))
        self._tokens = TokenSet(
            access_token=entry.data[CONF_ACCESS_TOKEN],
            id_token=entry.data[CONF_ID_TOKEN],
            refresh_token=entry.data[CONF_REFRESH_TOKEN],
            expires_at=entry.data[CONF_EXPIRES_AT],
        )

    async def _async_update_data(self) -> dict[str, dict]:
        await self._ensure_valid_token()
        try:
            shipments = await self.client.fetch_shipments(self._tokens.id_token)
        except DhlApiError as err:
            raise UpdateFailed(f"Error fetching DHL shipments: {err}") from err

        _LOGGER.debug("Paketverfolgung: %d active DHL shipment(s) found", len(shipments))
        return {s["id"]: s for s in shipments if s.get("id")}

    async def _ensure_valid_token(self) -> None:
        if time.time() < self._tokens.expires_at - TOKEN_REFRESH_MARGIN_SECONDS:
            return
        try:
            self._tokens = await self.client.refresh(self._tokens.refresh_token)
        except DhlAuthError as err:
            raise ConfigEntryAuthFailed(
                "DHL login expired, please log in again"
            ) from err
        self.hass.config_entries.async_update_entry(
            self.entry,
            data={
                **self.entry.data,
                CONF_ACCESS_TOKEN: self._tokens.access_token,
                CONF_ID_TOKEN: self._tokens.id_token,
                CONF_REFRESH_TOKEN: self._tokens.refresh_token,
                CONF_EXPIRES_AT: self._tokens.expires_at,
            },
        )
