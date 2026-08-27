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
    COMBINED_SENSOR_ADDED_KEY,
    CONF_PROVIDER,
    CONF_TRACKING_NUMBERS,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
    PROVIDER_DHL,
    PROVIDER_DPD,
    SERVICE_ADD_TRACKING_NUMBER,
)
from .coordinator import DhlDataUpdateCoordinator, DpdDataUpdateCoordinator

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
    provider = entry.data.get(CONF_PROVIDER, PROVIDER_DHL)
    if provider == PROVIDER_DPD:
        coordinator = DpdDataUpdateCoordinator(
            hass, entry, update_interval=timedelta(minutes=minutes)
        )
    else:
        coordinator = DhlDataUpdateCoordinator(
            hass, entry, update_interval=timedelta(minutes=minutes)
        )
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if provider == PROVIDER_DHL and not hass.services.has_service(
        DOMAIN, SERVICE_ADD_TRACKING_NUMBER
    ):

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
    """Add a tracking number to the DHL config entry if not already tracked.

    Returns whether it was newly added, so callers (e.g. the Telegram
    automation) can send an accurate confirmation instead of assuming success.
    """
    entries = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.data.get(CONF_PROVIDER, PROVIDER_DHL) == PROVIDER_DHL
    ]
    if not entries:
        raise ServiceValidationError("DHL-Paketverfolgung ist nicht eingerichtet.")
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
        # The combined "Heute in Zustellung" sensor was added under
        # *some* entry's platform setup - only clear the guard if that was
        # *this* entry (it just got torn down along with it), so the next
        # setup re-adds it. If a different entry owns it, its copy is
        # still alive and must not be duplicated.
        if hass.data[DOMAIN].get(COMBINED_SENSOR_ADDED_KEY) == entry.entry_id:
            hass.data[DOMAIN].pop(COMBINED_SENSOR_ADDED_KEY, None)
    return unload_ok
