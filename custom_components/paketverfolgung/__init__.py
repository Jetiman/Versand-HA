"""The Paketverfolgung integration."""
from __future__ import annotations

import logging
from datetime import timedelta

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError

from .const import (
    ATTR_TRACKING_NUMBER,
    CONF_TRACKING_NUMBERS,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
    SERVICE_ADD_TRACKING_NUMBER,
)
from .coordinator import DhlDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor"]

_ADD_TRACKING_NUMBER_SCHEMA = vol.Schema(
    # HA's automation templates render a purely-numeric string back into an
    # int/float (native-type rendering), so accept anything and coerce to
    # str here rather than fighting that on the caller side.
    {vol.Required(ATTR_TRACKING_NUMBER): vol.Coerce(str)}
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the Paketverfolgung integration."""
    minutes = entry.options.get(
        CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL_MINUTES
    )
    coordinator = DhlDataUpdateCoordinator(
        hass, entry, update_interval=timedelta(minutes=minutes)
    )
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if not hass.services.has_service(DOMAIN, SERVICE_ADD_TRACKING_NUMBER):

        async def _async_add_tracking_number(call: ServiceCall) -> ServiceResponse:
            return await _add_tracking_number(hass, call.data[ATTR_TRACKING_NUMBER])

        hass.services.async_register(
            DOMAIN,
            SERVICE_ADD_TRACKING_NUMBER,
            _async_add_tracking_number,
            schema=_ADD_TRACKING_NUMBER_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )

    return True


async def _add_tracking_number(
    hass: HomeAssistant, tracking_number: str
) -> ServiceResponse:
    """Add a tracking number to the (single) config entry if not already tracked.

    Returns whether it was newly added, so callers (e.g. the Telegram
    automation) can send an accurate confirmation instead of assuming success.
    """
    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries:
        raise ServiceValidationError("Paketverfolgung ist nicht eingerichtet.")
    entry = entries[0]

    cleaned = tracking_number.strip()
    if not cleaned:
        raise ServiceValidationError("Leere Sendungsnummer.")

    current = list(
        entry.options.get(
            CONF_TRACKING_NUMBERS, entry.data.get(CONF_TRACKING_NUMBERS, [])
        )
    )
    if cleaned in current:
        _LOGGER.info("Paketverfolgung: %s wird bereits verfolgt", cleaned)
        return {"added": False, "tracking_number": cleaned}

    current.append(cleaned)
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_TRACKING_NUMBERS: current}
    )
    _LOGGER.info("Paketverfolgung: %s wurde hinzugefügt", cleaned)
    return {"added": True, "tracking_number": cleaned}


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
