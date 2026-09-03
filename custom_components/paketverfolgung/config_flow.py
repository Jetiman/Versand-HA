"""Config flow for the Paketverfolgung integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .amazon_api import (
    AmazonApiClient,
    AmazonAuthError,
    AmazonCaptchaError,
    AmazonOtpChallenge,
)
from .amazon_session import export_cookie_store
from .const import (
    CONF_AMAZON_COOKIES,
    CONF_AMAZON_OTP,
    CONF_AMAZON_PASSWORD,
    CONF_AMAZON_USERNAME,
    CONF_CARRIER_OVERRIDES,
    CONF_DIRECTION_OVERRIDES,
    CONF_UPS_CLIENT_ID,
    CONF_UPS_CLIENT_SECRET,
    CONF_DEFAULT_POSTCODE,
    CONF_DHL_AUTO_DISCOVERY,
    CONF_DHL_REDIRECT,
    CONF_DHL_SESSION,
    CONF_NAMES,
    CONF_DPD_PASSWORD,
    CONF_DPD_USERNAME,
    CONF_PROVIDER,
    CONF_TRACKING_NUMBERS,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
    MIN_UPDATE_INTERVAL_MINUTES,
    PROVIDER_AMAZON,
    PROVIDER_DPD,
    PROVIDER_NUMBERS,
)
from .dhl_account import DhlAccountClient, DhlAuthError, build_login, extract_code
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


def _tracking_numbers_schema(
    default: list[str], postcode: str | None = None
) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(
                CONF_TRACKING_NUMBERS, default=default
            ): selector.TextSelector(selector.TextSelectorConfig(multiple=True)),
            vol.Optional(
                CONF_DEFAULT_POSTCODE, default=postcode or ""
            ): selector.TextSelector(),
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

_AMAZON_LOGIN_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_AMAZON_USERNAME): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.EMAIL)
        ),
        vol.Required(CONF_AMAZON_PASSWORD): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
    }
)

_AMAZON_OTP_SCHEMA = vol.Schema(
    {vol.Required(CONF_AMAZON_OTP): selector.TextSelector(selector.TextSelectorConfig())}
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
            if user_input[CONF_PROVIDER] == PROVIDER_AMAZON:
                return await self.async_step_amazon()
            return await self.async_step_dhl()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_PROVIDER, default=PROVIDER_NUMBERS
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[PROVIDER_NUMBERS, PROVIDER_DPD, PROVIDER_AMAZON],
                            translation_key="provider",
                        )
                    ),
                }
            ),
        )

    # step_id stays "dhl" for config-entry / translation stability; it's
    # really the carrier-neutral tracking-number list now.
    async def async_step_dhl(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        if user_input is not None:
            await self.async_set_unique_id(PROVIDER_NUMBERS)
            self._abort_if_unique_id_configured()
            numbers = _clean_tracking_numbers(user_input.get(CONF_TRACKING_NUMBERS))
            return self.async_create_entry(
                title="Sendungsnummern",
                data={
                    CONF_PROVIDER: PROVIDER_NUMBERS,
                    CONF_TRACKING_NUMBERS: numbers,
                    CONF_DEFAULT_POSTCODE: (
                        user_input.get(CONF_DEFAULT_POSTCODE) or ""
                    ).strip(),
                },
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

    async def async_step_amazon(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Authenticate an Amazon.de account. Only the session is persisted."""
        errors: dict[str, str] = {}
        if user_input is not None:
            client = AmazonApiClient()
            try:
                result = await client.login(
                    user_input[CONF_AMAZON_USERNAME].strip(),
                    user_input[CONF_AMAZON_PASSWORD],
                )
            except AmazonCaptchaError as err:
                _LOGGER.debug("Amazon captcha/manual login required: %s", err)
                errors["base"] = "amazon_captcha"
            except AmazonAuthError as err:
                _LOGGER.debug("Amazon login failed: %s", err)
                errors["base"] = "amazon_auth"
            else:
                if result.otp is not None:
                    self._amazon_challenge = result.otp
                    return await self.async_step_amazon_otp()
                await self.async_set_unique_id(PROVIDER_AMAZON)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="Amazon",
                    data={
                        CONF_PROVIDER: PROVIDER_AMAZON,
                        CONF_AMAZON_COOKIES: export_cookie_store(client),
                    },
                )
            finally:
                await client.close()

        return self.async_show_form(
            step_id="amazon", data_schema=_AMAZON_LOGIN_SCHEMA, errors=errors
        )

    async def async_step_amazon_otp(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Complete an Amazon MFA / SMS verification challenge."""
        challenge: AmazonOtpChallenge | None = getattr(
            self, "_amazon_challenge", None
        )
        if challenge is None:
            return await self.async_step_amazon()

        errors: dict[str, str] = {}
        if user_input is not None:
            client = AmazonApiClient(challenge.cookies)
            try:
                await client.submit_otp(
                    challenge, user_input[CONF_AMAZON_OTP].strip()
                )
            except AmazonCaptchaError as err:
                _LOGGER.debug("Amazon captcha during OTP: %s", err)
                errors["base"] = "amazon_captcha"
            except AmazonAuthError as err:
                _LOGGER.debug("Amazon OTP failed: %s", err)
                errors["base"] = "amazon_otp"
            else:
                await self.async_set_unique_id(PROVIDER_AMAZON)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="Amazon",
                    data={
                        CONF_PROVIDER: PROVIDER_AMAZON,
                        CONF_AMAZON_COOKIES: export_cookie_store(client),
                    },
                )
            finally:
                await client.close()

        return self.async_show_form(
            step_id="amazon_otp", data_schema=_AMAZON_OTP_SCHEMA, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return PaketverfolgungOptionsFlow(entry)


class PaketverfolgungOptionsFlow(OptionsFlow):
    """Manage per-provider options (tracking numbers / update interval)."""

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._pending: dict[str, Any] = {}
        self._verifier: str | None = None
        self._login_url: str | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        provider = self._entry.data.get(CONF_PROVIDER, PROVIDER_NUMBERS)
        if provider == PROVIDER_DPD:
            return await self.async_step_dpd_options(user_input)
        if provider == PROVIDER_AMAZON:
            return await self.async_step_amazon_options(user_input)
        return await self.async_step_dhl_options(user_input)

    def _current(self, key, default=None):
        return self._entry.options.get(key, self._entry.data.get(key, default))

    async def async_step_dhl_options(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        if user_input is not None:
            numbers = _clean_tracking_numbers(user_input.get(CONF_TRACKING_NUMBERS))

            # Preserve per-number overrides/labels, dropping any for numbers
            # that were just removed here.
            def _kept(key: str) -> dict:
                return {
                    k: v
                    for k, v in dict(self._entry.options.get(key, {})).items()
                    if k in numbers
                }

            self._pending = {
                CONF_TRACKING_NUMBERS: numbers,
                CONF_CARRIER_OVERRIDES: _kept(CONF_CARRIER_OVERRIDES),
                CONF_NAMES: _kept(CONF_NAMES),
                CONF_DIRECTION_OVERRIDES: _kept(CONF_DIRECTION_OVERRIDES),
                CONF_DEFAULT_POSTCODE: (
                    user_input.get(CONF_DEFAULT_POSTCODE) or ""
                ).strip(),
                CONF_UPDATE_INTERVAL: user_input[CONF_UPDATE_INTERVAL],
                CONF_DHL_AUTO_DISCOVERY: bool(
                    user_input.get(CONF_DHL_AUTO_DISCOVERY)
                ),
                CONF_UPS_CLIENT_ID: (
                    user_input.get(CONF_UPS_CLIENT_ID) or ""
                ).strip(),
                CONF_UPS_CLIENT_SECRET: (
                    user_input.get(CONF_UPS_CLIENT_SECRET) or ""
                ).strip(),
            }
            if self._pending[CONF_DHL_AUTO_DISCOVERY]:
                return await self.async_step_dhl_login()
            return self.async_create_entry(title="", data=self._pending)

        schema = (
            _tracking_numbers_schema(
                self._current(CONF_TRACKING_NUMBERS, []),
                self._current(CONF_DEFAULT_POSTCODE),
            )
            .extend(
                _update_interval_schema(
                    self._current(
                        CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL_MINUTES
                    )
                ).schema
            )
            .extend(
                {
                    vol.Optional(
                        CONF_DHL_AUTO_DISCOVERY,
                        default=bool(
                            self._current(CONF_DHL_AUTO_DISCOVERY, False)
                        ),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_UPS_CLIENT_ID,
                        default=self._current(CONF_UPS_CLIENT_ID, "") or "",
                    ): selector.TextSelector(),
                    vol.Optional(
                        CONF_UPS_CLIENT_SECRET,
                        default=self._current(CONF_UPS_CLIENT_SECRET, "") or "",
                    ): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.PASSWORD
                        )
                    ),
                }
            )
        )
        return self.async_show_form(step_id="dhl_options", data_schema=schema)

    async def async_step_dhl_login(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Connect a DHL account (OAuth) so its shipments are auto-discovered.

        Shows the DHL login URL, the user signs in and pastes back the
        ``dhllogin://`` redirect; its ``code`` is exchanged for a token
        pair that is stored in the config entry's ``data`` (no password).
        """
        errors: dict[str, str] = {}
        has_session = bool(self._entry.data.get(CONF_DHL_SESSION))

        if user_input is not None:
            redirect = (user_input.get(CONF_DHL_REDIRECT) or "").strip()
            if not redirect and has_session:
                # Keep the existing login, just persist the toggled option.
                return self.async_create_entry(title="", data=self._pending)
            try:
                code = extract_code(redirect)
                client = DhlAccountClient(async_get_clientsession(self.hass))
                session = await client.exchange_code(code, self._verifier or "")
            except DhlAuthError as err:
                _LOGGER.debug("DHL account login failed: %s", err)
                errors["base"] = "dhl_auth"
            else:
                self.hass.config_entries.async_update_entry(
                    self._entry,
                    data={**self._entry.data, CONF_DHL_SESSION: session},
                )
                return self.async_create_entry(title="", data=self._pending)

        self._verifier, self._login_url = build_login()
        return self.async_show_form(
            step_id="dhl_login",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_DHL_REDIRECT, default=""
                    ): selector.TextSelector(),
                }
            ),
            errors=errors,
            description_placeholders={
                "login_url": self._login_url,
                "known": (
                    "Es ist bereits ein DHL-Login hinterlegt – Feld leer lassen, "
                    "um ihn zu behalten.\n\n"
                    if has_session
                    else ""
                ),
            },
        )

    async def async_step_dpd_options(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    # keep per-parcel labels and anything else already set
                    **self._entry.options,
                    CONF_UPDATE_INTERVAL: user_input[CONF_UPDATE_INTERVAL],
                    CONF_DEFAULT_POSTCODE: (
                        user_input.get(CONF_DEFAULT_POSTCODE) or ""
                    ).strip(),
                },
            )

        schema = (
            _update_interval_schema(
                self._current(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL_MINUTES)
            )
            .extend(
                {
                    vol.Optional(
                        CONF_DEFAULT_POSTCODE,
                        default=self._current(CONF_DEFAULT_POSTCODE) or "",
                    ): selector.TextSelector(),
                }
            )
        )
        return self.async_show_form(step_id="dpd_options", data_schema=schema)

    async def async_step_amazon_options(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Amazon exposes the refresh interval and the notification toggle."""
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    **self._entry.options,
                    CONF_UPDATE_INTERVAL: user_input[CONF_UPDATE_INTERVAL],
                },
            )
        return self.async_show_form(
            step_id="amazon_options",
            data_schema=_update_interval_schema(
                self._current(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL_MINUTES)
            ),
        )
