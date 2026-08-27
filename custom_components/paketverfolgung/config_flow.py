"""Config flow for the Paketverfolgung integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_DPD_PASSWORD,
    CONF_DPD_USERNAME,
    CONF_PROVIDER,
    CONF_TRACKING_NUMBERS,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
    MIN_UPDATE_INTERVAL_MINUTES,
    PROVIDER_DHL,
    PROVIDER_DPD,
)
from .dpd_api import DpdApiClient, DpdApiError, DpdAuthError

_LOGGER = logging.getLogger(__name__)


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


def _update_interval_schema(default: int) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_UPDATE_INTERVAL, default=default): vol.All(
                vol.Coerce(int), vol.Range(min=MIN_UPDATE_INTERVAL_MINUTES)
            ),
        }
    )


_DPD_LOGIN_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_DPD_USERNAME): str,
        vol.Required(CONF_DPD_PASSWORD): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
    }
)


class PaketverfolgungConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Paketverfolgung."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        if user_input is not None:
            if user_input[CONF_PROVIDER] == PROVIDER_DPD:
                return await self.async_step_dpd()
            return await self.async_step_dhl()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PROVIDER, default=PROVIDER_DHL): vol.In(
                        [PROVIDER_DHL, PROVIDER_DPD]
                    ),
                }
            ),
        )

    async def async_step_dhl(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        if user_input is not None:
            await self.async_set_unique_id(PROVIDER_DHL)
            self._abort_if_unique_id_configured()
            numbers = _clean_tracking_numbers(user_input.get(CONF_TRACKING_NUMBERS))
            return self.async_create_entry(
                title="DHL",
                data={CONF_PROVIDER: PROVIDER_DHL, CONF_TRACKING_NUMBERS: numbers},
            )

        return self.async_show_form(
            step_id="dhl",
            data_schema=_tracking_numbers_schema([]),
        )

    async def async_step_dpd(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input[CONF_DPD_USERNAME].strip()
            password = user_input[CONF_DPD_PASSWORD]
            client = DpdApiClient(async_get_clientsession(self.hass))
            try:
                await client.login(username, password)
            except DpdAuthError as err:
                _LOGGER.debug("DPD login failed: %s", err)
                errors["base"] = "invalid_auth"
            except DpdApiError as err:
                _LOGGER.debug("DPD login error: %s", err)
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(f"{PROVIDER_DPD}_{username.lower()}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"DPD ({username})",
                    data={
                        CONF_PROVIDER: PROVIDER_DPD,
                        CONF_DPD_USERNAME: username,
                        CONF_DPD_PASSWORD: password,
                    },
                )

        return self.async_show_form(
            step_id="dpd", data_schema=_DPD_LOGIN_SCHEMA, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return PaketverfolgungOptionsFlow(entry)


class PaketverfolgungOptionsFlow(OptionsFlow):
    """Manage per-provider options (tracking numbers / update interval)."""

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        if self._entry.data.get(CONF_PROVIDER, PROVIDER_DHL) == PROVIDER_DPD:
            return await self.async_step_dpd_options(user_input)
        return await self.async_step_dhl_options(user_input)

    async def async_step_dhl_options(
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
            _update_interval_schema(current_interval).schema
        )
        return self.async_show_form(step_id="dhl_options", data_schema=schema)

    async def async_step_dpd_options(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        if user_input is not None:
            return self.async_create_entry(
                title="", data={CONF_UPDATE_INTERVAL: user_input[CONF_UPDATE_INTERVAL]}
            )

        current_interval = self._entry.options.get(
            CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL_MINUTES
        )
        return self.async_show_form(
            step_id="dpd_options",
            data_schema=_update_interval_schema(current_interval),
        )
