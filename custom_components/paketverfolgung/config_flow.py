"""Config flow for the Paketverfolgung integration."""
from __future__ import annotations

import base64
import hashlib
import secrets
from typing import Any
from urllib.parse import urlencode

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_AUTO_DISCOVERY,
    CONF_DHL_REDIRECT,
    CONF_DHL_SESSION,
    CONF_TRACKING_NUMBERS,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DHL_AUTH_BASE,
    DHL_CLIENT_ID,
    DHL_REDIRECT_URI,
    DOMAIN,
    MIN_UPDATE_INTERVAL_MINUTES,
)
from .dhl_api import DhlApiClient, DhlAuthError, extract_authorization_code


def _clean_tracking_numbers(raw: list[str] | None) -> list[str]:
    if not raw:
        return []
    seen: list[str] = []
    for value in raw:
        cleaned = value.strip()
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return seen


def _tracking_numbers_schema(default: list[str]) -> dict:
    return {
        vol.Optional(
            CONF_TRACKING_NUMBERS, default=default
        ): selector.TextSelector(selector.TextSelectorConfig(multiple=True)),
    }


def _new_pkce_login() -> tuple[str, str]:
    """Create a fresh PKCE verifier and matching DHL authorization URL."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    params = {
        "redirect_uri": DHL_REDIRECT_URI,
        "state": secrets.token_urlsafe(24),
        "client_id": DHL_CLIENT_ID,
        "response_type": "code",
        "scope": "openid offline_access",
        "nonce": secrets.token_urlsafe(24),
        "prompt": "login",
        "ui_locales": "de-DE",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return verifier, f"{DHL_AUTH_BASE}/authorize?{urlencode(params)}"


def _dhl_redirect_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_DHL_REDIRECT): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
            )
        }
    )


class PaketverfolgungConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Paketverfolgung."""

    VERSION = 2

    def __init__(self) -> None:
        self._pending_data: dict[str, Any] = {}
        self._pkce_verifier: str | None = None
        self._login_url: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        if user_input is not None:
            numbers = _clean_tracking_numbers(user_input.get(CONF_TRACKING_NUMBERS))
            auto_discovery = bool(user_input.get(CONF_AUTO_DISCOVERY, False))
            self._pending_data = {
                CONF_TRACKING_NUMBERS: numbers,
                CONF_AUTO_DISCOVERY: auto_discovery,
            }
            if auto_discovery:
                self._pkce_verifier, self._login_url = _new_pkce_login()
                return await self.async_step_dhl_login()
            return await self._create_entry(self._pending_data)

        schema = vol.Schema(
            {
                **_tracking_numbers_schema([]),
                vol.Optional(CONF_AUTO_DISCOVERY, default=False): bool,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_dhl_login(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                code = extract_authorization_code(user_input[CONF_DHL_REDIRECT].strip())
                client = DhlApiClient(async_get_clientsession(self.hass))
                session = await client.exchange_code(code, self._pkce_verifier or "")
            except DhlAuthError:
                errors["base"] = "dhl_auth"
            else:
                return await self._create_entry(
                    {**self._pending_data, CONF_DHL_SESSION: session}
                )

        if not self._login_url:
            self._pkce_verifier, self._login_url = _new_pkce_login()
        return self.async_show_form(
            step_id="dhl_login",
            data_schema=_dhl_redirect_schema(),
            errors=errors,
            description_placeholders={"login_url": self._login_url},
        )

    async def _create_entry(self, data: dict[str, Any]) -> Any:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="Paketverfolgung", data=data)

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return PaketverfolgungOptionsFlow(entry)


class PaketverfolgungOptionsFlow(OptionsFlow):
    """Manage tracking numbers, account auto-discovery and update interval."""

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._pending_options: dict[str, Any] = {}
        self._pkce_verifier: str | None = None
        self._login_url: str | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        if user_input is not None:
            numbers = _clean_tracking_numbers(user_input.get(CONF_TRACKING_NUMBERS))
            auto_discovery = bool(user_input.get(CONF_AUTO_DISCOVERY, False))
            self._pending_options = {
                CONF_TRACKING_NUMBERS: numbers,
                CONF_UPDATE_INTERVAL: user_input[CONF_UPDATE_INTERVAL],
                CONF_AUTO_DISCOVERY: auto_discovery,
            }
            if auto_discovery and not self._entry.data.get(CONF_DHL_SESSION):
                self._pkce_verifier, self._login_url = _new_pkce_login()
                return await self.async_step_dhl_login()
            return self.async_create_entry(title="", data=self._pending_options)

        current_numbers = self._entry.options.get(
            CONF_TRACKING_NUMBERS,
            self._entry.data.get(CONF_TRACKING_NUMBERS, []),
        )
        current_interval = self._entry.options.get(
            CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL_MINUTES
        )
        current_auto = self._entry.options.get(
            CONF_AUTO_DISCOVERY,
            self._entry.data.get(CONF_AUTO_DISCOVERY, False),
        )
        schema = vol.Schema(
            {
                **_tracking_numbers_schema(current_numbers),
                vol.Required(
                    CONF_UPDATE_INTERVAL, default=current_interval
                ): vol.All(vol.Coerce(int), vol.Range(min=MIN_UPDATE_INTERVAL_MINUTES)),
                vol.Optional(CONF_AUTO_DISCOVERY, default=current_auto): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

    async def async_step_dhl_login(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                code = extract_authorization_code(user_input[CONF_DHL_REDIRECT].strip())
                client = DhlApiClient(async_get_clientsession(self.hass))
                session = await client.exchange_code(code, self._pkce_verifier or "")
            except DhlAuthError:
                errors["base"] = "dhl_auth"
            else:
                self.hass.config_entries.async_update_entry(
                    self._entry,
                    data={**self._entry.data, CONF_DHL_SESSION: session},
                )
                return self.async_create_entry(title="", data=self._pending_options)

        if not self._login_url:
            self._pkce_verifier, self._login_url = _new_pkce_login()
        return self.async_show_form(
            step_id="dhl_login",
            data_schema=_dhl_redirect_schema(),
            errors=errors,
            description_placeholders={"login_url": self._login_url},
        )
