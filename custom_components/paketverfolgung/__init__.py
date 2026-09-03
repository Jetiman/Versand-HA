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

from .amazon_coordinator import AmazonAccountDataUpdateCoordinator
from .const import (
    ATTR_CARRIER,
    ATTR_DIRECTION,
    ATTR_NAME,
    ATTR_TRACKING_NUMBER,
    CARRIER_AUTO,
    CARRIERS,
    CONF_AMAZON_COOKIES,
    CONF_CARRIER_OVERRIDES,
    CONF_DHL_SESSION,
    CONF_DIRECTION_OVERRIDES,
    CONF_NAMES,
    CONF_NOTIFY_ENABLED,
    CONF_NOTIFY_TARGETS,
    CONF_PROVIDER,
    CONF_TRACKING_NUMBERS,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DIRECTION_AUTO,
    DIRECTIONS,
    DOMAIN,
    PANEL_ICON,
    PANEL_STATIC_URL,
    PANEL_TITLE,
    PANEL_URL_PATH,
    PANEL_VERSION,
    PROVIDER_AMAZON,
    PROVIDER_DPD,
    PROVIDER_NUMBERS,
    SERVICE_ADD_TRACKING_NUMBER,
    SERVICE_REMOVE_TRACKING_NUMBER,
    SERVICE_SET_CARRIER,
    SERVICE_SET_DIRECTION,
    SERVICE_SET_NAME,
    SERVICE_SET_NOTIFICATIONS,
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

_SET_NAME_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_TRACKING_NUMBER): vol.Coerce(str),
        vol.Optional(ATTR_NAME, default=""): vol.Coerce(str),
    }
)

_SET_DIRECTION_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_TRACKING_NUMBER): vol.Coerce(str),
        vol.Required(ATTR_DIRECTION): vol.In([*DIRECTIONS, DIRECTION_AUTO]),
    }
)


def _as_str_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


_SET_NOTIFICATIONS_SCHEMA = vol.Schema(
    {
        vol.Optional("enabled", default=True): vol.Coerce(bool),
        vol.Optional("targets", default=list): _as_str_list,
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
    elif provider == PROVIDER_AMAZON:
        coordinator = AmazonAccountDataUpdateCoordinator(
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
    coordinator._reload_snapshot = _reload_relevant(entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    # Keep the coordinator polling for the entry's whole lifetime, even when
    # it currently has no shipment entities (a DPD/Amazon account between
    # parcels) - a DataUpdateCoordinator stops its timer once its last
    # listener goes away, and it still needs to poll to discover new ones.
    entry.async_on_unload(coordinator.async_add_listener(lambda: None))
    await _async_register_panel(hass)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if not hass.services.has_service(DOMAIN, SERVICE_ADD_TRACKING_NUMBER):

        async def _async_add_tracking_number(call: ServiceCall) -> ServiceResponse:
            return await _add_tracking_number(hass, call.data[ATTR_TRACKING_NUMBER])

        async def _async_remove_tracking_number(call: ServiceCall) -> ServiceResponse:
            return await _remove_tracking_number(hass, call.data[ATTR_TRACKING_NUMBER])

        async def _async_set_carrier(call: ServiceCall) -> ServiceResponse:
            return await _set_carrier(
                hass, call.data[ATTR_TRACKING_NUMBER], call.data[ATTR_CARRIER]
            )

        async def _async_set_name(call: ServiceCall) -> ServiceResponse:
            return await _set_name(
                hass, call.data[ATTR_TRACKING_NUMBER], call.data.get(ATTR_NAME, "")
            )

        async def _async_set_direction(call: ServiceCall) -> ServiceResponse:
            return await _set_direction(
                hass, call.data[ATTR_TRACKING_NUMBER], call.data[ATTR_DIRECTION]
            )

        async def _async_set_notifications(call: ServiceCall) -> None:
            _set_notifications(
                hass,
                bool(call.data.get("enabled", True)),
                call.data.get("targets", []),
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
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_DIRECTION,
            _async_set_direction,
            schema=_SET_DIRECTION_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_NAME,
            _async_set_name,
            schema=_SET_NAME_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_NOTIFICATIONS,
            _async_set_notifications,
            schema=_SET_NOTIFICATIONS_SCHEMA,
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

    def _without(key: str) -> dict:
        return {
            k: v
            for k, v in dict(entry.options.get(key, {})).items()
            if k != cleaned
        }

    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_TRACKING_NUMBERS: current,
            CONF_CARRIER_OVERRIDES: _without(CONF_CARRIER_OVERRIDES),
            CONF_NAMES: _without(CONF_NAMES),
            CONF_DIRECTION_OVERRIDES: _without(CONF_DIRECTION_OVERRIDES),
        },
    )
    _LOGGER.info("Paketverfolgung: %s wurde entfernt", cleaned)
    return {"removed": True, "tracking_number": cleaned}


def _entry_for_shipment(hass: HomeAssistant, number: str):
    """The config entry whose coordinator currently holds this number."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if coordinator is not None and number in (
            getattr(coordinator, "data", None) or {}
        ):
            return entry
    return _numbers_entry(hass)


async def _set_name(
    hass: HomeAssistant, tracking_number: str, name: str
) -> ServiceResponse:
    """Give a shipment a custom label (empty string clears it)."""
    cleaned = tracking_number.strip()
    display = (name or "").strip()
    entry = _entry_for_shipment(hass, cleaned)
    names = {
        k: v
        for k, v in dict(entry.options.get(CONF_NAMES, {})).items()
        if k != cleaned
    }
    if display:
        names[cleaned] = display
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_NAMES: names}
    )
    _LOGGER.info("Paketverfolgung: %s -> Name %r", cleaned, display)
    return {"tracking_number": cleaned, "name": display}


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


async def _set_direction(
    hass: HomeAssistant, tracking_number: str, direction: str
) -> ServiceResponse:
    """Pin a shipment's "Richtung" (or 'auto' to clear the override).

    Anonymous carrier tracking can't tell whether you are the sender or the
    recipient, so this lets a shipment you sent be labelled correctly.
    """
    cleaned = tracking_number.strip()
    entry = _entry_for_shipment(hass, cleaned)
    overrides = {
        k: v
        for k, v in dict(entry.options.get(CONF_DIRECTION_OVERRIDES, {})).items()
        if k != cleaned
    }
    if direction != DIRECTION_AUTO:
        overrides[cleaned] = direction
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_DIRECTION_OVERRIDES: overrides}
    )
    _LOGGER.info("Paketverfolgung: %s -> Richtung %s", cleaned, direction)
    return {"tracking_number": cleaned, "direction": direction}


def _set_notifications(
    hass: HomeAssistant, enabled: bool, targets: list[str]
) -> None:
    """Store the notification on/off flag + target list on every entry."""
    clean = [
        t[len("notify."):] if t.startswith("notify.") else t
        for t in (str(x).strip() for x in targets)
        if t and t.strip()
    ]
    for entry in hass.config_entries.async_entries(DOMAIN):
        hass.config_entries.async_update_entry(
            entry,
            options={
                **entry.options,
                CONF_NOTIFY_ENABLED: enabled,
                CONF_NOTIFY_TARGETS: clean,
            },
        )
    for coordinator in hass.data.get(DOMAIN, {}).values():
        if hasattr(coordinator, "async_update_listeners"):
            coordinator.async_update_listeners()
    _LOGGER.info(
        "Paketverfolgung: Benachrichtigungen %s -> %s",
        "an" if enabled else "aus",
        clean,
    )


def _reload_relevant(entry: ConfigEntry) -> str:
    """A signature of the entry parts whose change warrants a reload.

    Excluded: the rotating DHL OAuth token / Amazon session cookies that
    the coordinators persist back into ``entry.data`` themselves, and the
    notification options (read live each poll) so the panel toggle doesn't
    reload the integration.
    """
    skip_data = (CONF_DHL_SESSION, CONF_AMAZON_COOKIES)
    skip_options = (CONF_NOTIFY_ENABLED, CONF_NOTIFY_TARGETS)
    data = {k: v for k, v in entry.data.items() if k not in skip_data}
    options = {k: v for k, v in entry.options.items() if k not in skip_options}
    return repr(sorted(data.items())) + "|" + repr(sorted(options.items()))


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    now = _reload_relevant(entry)
    if getattr(coordinator, "_reload_snapshot", None) == now:
        return  # only the DHL token was refreshed - no reload needed
    if coordinator is not None:
        coordinator._reload_snapshot = now
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
