"""Config flow for the DHL Paketverfolgung integration."""
from __future__ import annotations

import base64
import json
import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    AUTHORIZE_URL,
    CONF_ACCESS_TOKEN,
    CONF_EXPIRES_AT,
    CONF_ID_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
    MIN_UPDATE_INTERVAL_MINUTES,
)
from .dhl_api import DhlApiClient, DhlAuthError, TokenSet, extract_code

_LOGGER = logging.getLogger(__name__)

CODE_INPUT_KEY = "dhl_login_url"


def _decode_email_from_id_token(id_token: str) -> str | None:
    """Best-effort decode of the JWT payload to get an email for the title."""
    try:
        payload_b64 = id_token.split(".")[1]
        padding = "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
        return payload.get("email")
    except Exception:  # noqa: BLE001 - purely cosmetic, never fatal
        return None


async def _exchange_and_build_entry_data(
    hass, dhl_login_url: str
) -> tuple[dict[str, Any], str | None]:
    code = extract_code(dhl_login_url)
    client = DhlApiClient(async_get_clientsession(hass))
    tokens: TokenSet = await client.exchange_code(code)
    data = {
        CONF_ACCESS_TOKEN: tokens.access_token,
        CONF_ID_TOKEN: tokens.id_token,
        CONF_REFRESH_TOKEN: tokens.refresh_token,
        CONF_EXPIRES_AT: tokens.expires_at,
    }
    return data, _decode_email_from_id_token(tokens.id_token)


class DhlTrackingConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for DHL Paketverfolgung."""

    VERSION = 1

    def __init__(self) -> None:
        super().__init__()
        self._reauth_entry: ConfigEntry | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                data, email = await _exchange_and_build_entry_data(
                    self.hass, user_input[CODE_INPUT_KEY]
                )
            except (ValueError, DhlAuthError) as err:
                _LOGGER.debug("DHL login failed: %s", err)
                errors["base"] = "invalid_code"
            else:
                await self.async_set_unique_id(email or "dhl_tracking")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=email or "DHL Paketverfolgung", data=data
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CODE_INPUT_KEY): str}),
            description_placeholders={"authorize_url": AUTHORIZE_URL},
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> Any:
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                data, _email = await _exchange_and_build_entry_data(
                    self.hass, user_input[CODE_INPUT_KEY]
                )
            except (ValueError, DhlAuthError) as err:
                _LOGGER.debug("DHL re-login failed: %s", err)
                errors["base"] = "invalid_code"
            else:
                self.hass.config_entries.async_update_entry(
                    self._reauth_entry, data=data
                )
                await self.hass.config_entries.async_reload(
                    self._reauth_entry.entry_id
                )
                return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CODE_INPUT_KEY): str}),
            description_placeholders={"authorize_url": AUTHORIZE_URL},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return DhlTrackingOptionsFlow(entry)


class DhlTrackingOptionsFlow(OptionsFlow):
    """Handle options (update interval)."""

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self._entry.options.get(
            CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL_MINUTES
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_UPDATE_INTERVAL, default=current): vol.All(
                        vol.Coerce(int), vol.Range(min=MIN_UPDATE_INTERVAL_MINUTES)
                    )
                }
            ),
        )
