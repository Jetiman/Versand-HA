"""Config flow for the Paketverfolgung integration."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_TRACKING_NUMBERS,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
    MIN_UPDATE_INTERVAL_MINUTES,
)


def _clean_tracking_numbers(raw: list[str] | None) -> list[str]:
    if not raw:
        return []
    seen: list[str] = []
    for value in raw:
        cleaned = value.strip()
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return seen


def _tracking_numbers_schema(default: list[str]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(
                CONF_TRACKING_NUMBERS, default=default
            ): selector.TextSelector(selector.TextSelectorConfig(multiple=True)),
        }
    )


class PaketverfolgungConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Paketverfolgung."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        if user_input is not None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            numbers = _clean_tracking_numbers(user_input.get(CONF_TRACKING_NUMBERS))
            return self.async_create_entry(
                title="Paketverfolgung", data={CONF_TRACKING_NUMBERS: numbers}
            )

        return self.async_show_form(
            step_id="user",
            data_schema=_tracking_numbers_schema([]),
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return PaketverfolgungOptionsFlow(entry)


class PaketverfolgungOptionsFlow(OptionsFlow):
    """Manage tracking numbers and the update interval."""

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        if user_input is not None:
            numbers = _clean_tracking_numbers(user_input.get(CONF_TRACKING_NUMBERS))
            return self.async_create_entry(
                title="",
                data={
                    CONF_TRACKING_NUMBERS: numbers,
                    CONF_UPDATE_INTERVAL: user_input[CONF_UPDATE_INTERVAL],
                },
            )

        current_numbers = self._entry.options.get(
            CONF_TRACKING_NUMBERS,
            self._entry.data.get(CONF_TRACKING_NUMBERS, []),
        )
        current_interval = self._entry.options.get(
            CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL_MINUTES
        )
        schema = _tracking_numbers_schema(current_numbers).extend(
            {
                vol.Required(
                    CONF_UPDATE_INTERVAL, default=current_interval
                ): vol.All(vol.Coerce(int), vol.Range(min=MIN_UPDATE_INTERVAL_MINUTES)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
