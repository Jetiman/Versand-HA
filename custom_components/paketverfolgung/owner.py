"""Which config entry owns the cross-provider summary sensors.

The two summary sensors ("In Zustellung", "Nächste Aktualisierung") span every
provider but live on exactly one config entry. That entry has to be the same on
every start so they don't hop between entries and pile up "_2"/"_3" registry
duplicates: the tracking-number entry if there is one, else the entry with the
lowest id.
"""
from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant

from .const import CONF_PROVIDER, DOMAIN, PROVIDER_NUMBERS

_LOGGER = logging.getLogger(__name__)


def singleton_owner_id(hass: HomeAssistant, exclude: str | None = None) -> str | None:
    """The entry that owns the summary sensors, ignoring ``exclude``."""
    entries = sorted(
        (e for e in hass.config_entries.async_entries(DOMAIN) if e.entry_id != exclude),
        key=lambda e: e.entry_id,
    )
    for entry in entries:
        if entry.data.get(CONF_PROVIDER, PROVIDER_NUMBERS) == PROVIDER_NUMBERS:
            return entry.entry_id
    return entries[0].entry_id if entries else None


async def async_hand_over_summary_sensors(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Called when an entry is removed.

    If it owned the summary sensors, they are gone with it. The remaining
    entries were set up while it was still the owner and so did not create
    them - they would stay missing (and dashboards break) until the next
    restart. Reloading the new owner creates them again.
    """
    if singleton_owner_id(hass) != entry.entry_id:
        return
    if singleton_owner_id(hass, exclude=entry.entry_id) is None:
        return
    hass.async_create_task(_reload_new_owner(hass))


async def _reload_new_owner(hass: HomeAssistant) -> None:
    await asyncio.sleep(1)  # let Home Assistant finish removing the entry
    owner_id = singleton_owner_id(hass)
    owner = hass.config_entries.async_get_entry(owner_id) if owner_id else None
    if owner is None or owner.state is not ConfigEntryState.LOADED:
        return
    _LOGGER.info(
        "Paketverfolgung: Sammel-Sensoren gehen an den Eintrag %s über", owner.title
    )
    await hass.config_entries.async_reload(owner.entry_id)
