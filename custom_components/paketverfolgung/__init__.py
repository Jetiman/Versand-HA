"""The Paketverfolgung integration."""
from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

import voluptuous as vol
from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError

from .const import (
    ATTR_CARRIER,
    ATTR_TRACKING_NUMBER,
    CARRIER_AUTO,
    CARRIERS,
    CONF_CARRIER_OVERRIDES,
    CONF_PROVIDER,
    CONF_TRACKING_NUMBERS,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
    PANEL_ICON,
    PANEL_STATIC_URL,
    PANEL_TITLE,
    PANEL_URL_PATH,
    PANEL_VERSION,
    PROVIDER_DPD,
    PROVIDER_NUMBERS,
    SERVICE_ADD_TRACKING_NUMBER,
    SERVICE_REMOVE_TRACKING_NUMBER,
    SERVICE_SET_CARRIER,
)
from .coordinator import (
    DpdAccountDataUpdateCoordinator,
    TrackingNumbersDataUpdateCoordinator,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor"]

_STATIC_PATH_KEY = "_static_path_registered"


async def _async_register_panel(hass: HomeAssistant) -> None:
    """Register (or refresh) the Paketverfolgung sidebar panel.

    Serves the buildless web component straight from the integration's
    `frontend/` folder. The static path can only be registered once per HA
    run, but the panel itself is re-registered on every setup so an
    integration *reload* is enough to pick up a bumped PANEL_VERSION
    (i.e. new panel JS) - no full restart needed.
    """
    if not hass.data[DOMAIN].get(_STATIC_PATH_KEY):
        hass.data[DOMAIN][_STATIC_PATH_KEY] = True
        frontend_dir = Path(__file__).parent / "frontend"
        try:
            await hass.http.async_register_static_paths(
                [StaticPathConfig(PANEL_STATIC_URL, str(frontend_dir), False)]
            )
        except RuntimeError:
            # Path already registered earlier this run.
            pass

    if PANEL_URL_PATH in hass.data.get(frontend.DATA_PANELS, {}):
        frontend.async_remove_panel(hass, PANEL_URL_PATH)

    try:
        await panel_custom.async_register_panel(
            hass,
            frontend_url_path=PANEL_URL_PATH,
            webcomponent_name="paketverfolgung-panel",
            module_url=(
                f"{PANEL_STATIC_URL}/paketverfolgung-panel.js?v={PANEL_VERSION}"
            ),
            sidebar_title=PANEL_TITLE,
            sidebar_icon=PANEL_ICON,
            require_admin=False,
        )
    except ValueError:
        _LOGGER.debug("Paketverfolgung panel already registered")


_ADD_TRACKING_NUMBER_SCHEMA = vol.Schema(
    # HA's automation templates render a purely-numeric string back into an
    # int/float (native-type rendering), so accept anything and coerce to
    # str here rather than fighting that on the caller side.
    {vol.Required(ATTR_TRACKING_NUMBER): vol.Coerce(str)}
)

_SET_CARRIER_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_TRACKING_NUMBER): vol.Coerce(str),
        vol.Required(ATTR_CARRIER): vol.In([*CARRIERS, CARRIER_AUTO]),
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the Paketverfolgung integration."""
    minutes = entry.options.get(
        CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL_MINUTES
    )
    provider = entry.data.get(CONF_PROVIDER, PROVIDER_NUMBERS)
    if provider == PROVIDER_DPD:
        coordinator = DpdAccountDataUpdateCoordinator(
            hass, entry, update_interval=timedelta(minutes=minutes)
        )
    else:
        # The number list used to be DHL-only; rename the old default title
        # so its options dialog (which now covers DPD too) isn't confusing.
        if entry.title == "DHL":
            hass.config_entries.async_update_entry(entry, title="Sendungsnummern")
        coordinator = TrackingNumbersDataUpdateCoordinator(
            hass, entry, update_interval=timedelta(minutes=minutes)
        )
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await _async_register_panel(hass)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if provider == PROVIDER_NUMBERS and not hass.services.has_service(
        DOMAIN, SERVICE_ADD_TRACKING_NUMBER
    ):

        async def _async_add_tracking_number(call: ServiceCall) -> ServiceResponse:
            return await _add_tracking_number(hass, call.data[ATTR_TRACKING_NUMBER])

        async def _async_remove_tracking_number(call: ServiceCall) -> ServiceResponse:
            return await _remove_tracking_number(hass, call.data[ATTR_TRACKING_NUMBER])

        async def _async_set_carrier(call: ServiceCall) -> ServiceResponse:
            return await _set_carrier(
                hass, call.data[ATTR_TRACKING_NUMBER], call.data[ATTR_CARRIER]
            )

        hass.services.async_register(
            DOMAIN,
            SERVICE_ADD_TRACKING_NUMBER,
            _async_add_tracking_number,
            schema=_ADD_TRACKING_NUMBER_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_REMOVE_TRACKING_NUMBER,
            _async_remove_tracking_number,
            schema=_ADD_TRACKING_NUMBER_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_CARRIER,
            _async_set_carrier,
            schema=_SET_CARRIER_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )

    return True


def _numbers_entry(hass: HomeAssistant):
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get(CONF_PROVIDER, PROVIDER_NUMBERS) == PROVIDER_NUMBERS:
            return entry
    raise ServiceValidationError(
        "Die Sendungsnummern-Verfolgung ist nicht eingerichtet."
    )


def _current_numbers(entry) -> list[str]:
    return list(
        entry.options.get(
            CONF_TRACKING_NUMBERS, entry.data.get(CONF_TRACKING_NUMBERS, [])
        )
    )


async def _add_tracking_number(
    hass: HomeAssistant, tracking_number: str
) -> ServiceResponse:
    """Add a tracking number to the tracking-number entry if not already tracked.

    The carrier (DHL or DPD) is auto-detected on the next coordinator
    refresh. Returns whether it was newly added, so callers (e.g. the
    Telegram automation) can send an accurate confirmation instead of
    assuming success.
    """
    entry = _numbers_entry(hass)

    cleaned = tracking_number.strip()
    if not cleaned:
        raise ServiceValidationError("Leere Sendungsnummer.")

    current = _current_numbers(entry)
    if cleaned in current:
        _LOGGER.info("Paketverfolgung: %s wird bereits verfolgt", cleaned)
        return {"added": False, "tracking_number": cleaned}

    current.append(cleaned)
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_TRACKING_NUMBERS: current}
    )
    _LOGGER.info("Paketverfolgung: %s wurde hinzugefügt", cleaned)
    return {"added": True, "tracking_number": cleaned}


async def _remove_tracking_number(
    hass: HomeAssistant, tracking_number: str
) -> ServiceResponse:
    """Remove a tracking number from the tracking-number entry.

    Returns whether it was actually tracked, so callers can give an
    accurate confirmation.
    """
    entry = _numbers_entry(hass)
    cleaned = tracking_number.strip()
    current = _current_numbers(entry)
    if cleaned not in current:
        return {"removed": False, "tracking_number": cleaned}

    current = [n for n in current if n != cleaned]
    overrides = {
        k: v
        for k, v in dict(entry.options.get(CONF_CARRIER_OVERRIDES, {})).items()
        if k != cleaned
    }
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_TRACKING_NUMBERS: current,
            CONF_CARRIER_OVERRIDES: overrides,
        },
    )
    _LOGGER.info("Paketverfolgung: %s wurde entfernt", cleaned)
    return {"removed": True, "tracking_number": cleaned}


async def _set_carrier(
    hass: HomeAssistant, tracking_number: str, carrier: str
) -> ServiceResponse:
    """Pin a tracking number to a carrier (or 'auto' to clear the override)."""
    entry = _numbers_entry(hass)
    cleaned = tracking_number.strip()
    if cleaned not in _current_numbers(entry):
        raise ServiceValidationError(f"{cleaned} wird nicht verfolgt.")

    overrides = dict(entry.options.get(CONF_CARRIER_OVERRIDES, {}))
    if carrier == CARRIER_AUTO:
        overrides.pop(cleaned, None)
    else:
        overrides[cleaned] = carrier

    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_CARRIER_OVERRIDES: overrides}
    )
    _LOGGER.info("Paketverfolgung: %s -> Anbieter %s", cleaned, carrier)
    return {"tracking_number": cleaned, "carrier": carrier}


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
