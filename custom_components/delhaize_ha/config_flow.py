"""Config flow for the Delhaize integration."""

from __future__ import annotations

import logging
from typing import Any

from aiohttp.client_exceptions import ClientError
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_AUTO_ACTIVATE_OFFERS,
    CONF_COOKIE,
    CONF_LANGUAGE,
    DEFAULT_LANGUAGE,
    DOMAIN,
    NAME,
)
from .delhaizeApi import (
    DelhaizeApi,
    DelhaizeApiError,
    DelhaizeAuthError,
    DelhaizeCaptchaRequired,
    DelhaizeMfaRequired,
    DelhaizeRequestError,
    summarize_graphql_errors,
)

LANGUAGE_OPTIONS = ["nl", "fr", "en"]

CONF_AUTH_METHOD = "auth_method"
CONF_AUTH_UPDATE_METHOD = "auth_update_method"
CONF_OTP_CODE = "otp_code"

AUTH_METHOD_CREDENTIALS = "credentials"
AUTH_METHOD_COOKIE = "cookie"
AUTH_UPDATE_KEEP = "keep"

AUTH_METHOD_OPTIONS = [
    {"value": AUTH_METHOD_COOKIE, "label": "Cookie header (recommended)"},
    {
        "value": AUTH_METHOD_CREDENTIALS,
        "label": "Username and password (may require captcha)",
    },
]

AUTH_UPDATE_OPTIONS = [
    {"value": AUTH_UPDATE_KEEP, "label": "Keep current authentication"},
    {"value": AUTH_METHOD_CREDENTIALS, "label": "Update username and password"},
    {"value": AUTH_METHOD_COOKIE, "label": "Update Cookie header"},
]

_LOGGER = logging.getLogger(DOMAIN)


def _user_schema(user_input: dict[str, Any] | None = None) -> vol.Schema:
    """Return the normal setup schema."""
    defaults = user_input or {}
    return vol.Schema(
        {
            vol.Optional(
                CONF_USERNAME,
                default=defaults.get(CONF_USERNAME, ""),
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
            vol.Optional(CONF_PASSWORD): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
            vol.Optional(
                CONF_COOKIE,
                default=defaults.get(CONF_COOKIE, ""),
            ): TextSelector(
                TextSelectorConfig(type=TextSelectorType.TEXT)
            ),
            vol.Required(
                CONF_LANGUAGE,
                default=defaults.get(CONF_LANGUAGE, DEFAULT_LANGUAGE),
            ): SelectSelector(SelectSelectorConfig(options=LANGUAGE_OPTIONS)),
            vol.Required(
                CONF_AUTO_ACTIVATE_OFFERS,
                default=defaults.get(CONF_AUTO_ACTIVATE_OFFERS, False),
            ): BooleanSelector(),
        }
    )


def _cookie_schema(user_input: dict[str, Any] | None = None) -> vol.Schema:
    """Return the cookie fallback schema."""
    defaults = user_input or {}
    return vol.Schema(
        {
            vol.Required(CONF_COOKIE, default=defaults.get(CONF_COOKIE, "")): TextSelector(
                TextSelectorConfig(type=TextSelectorType.TEXT)
            )
        }
    )


def _options_schema(config_entry: config_entries.ConfigEntry) -> vol.Schema:
    """Return the options schema."""
    data = config_entry.data
    return vol.Schema(
        {
            vol.Required(
                CONF_LANGUAGE,
                default=data.get(CONF_LANGUAGE, DEFAULT_LANGUAGE),
            ): SelectSelector(SelectSelectorConfig(options=LANGUAGE_OPTIONS)),
            vol.Required(
                CONF_AUTO_ACTIVATE_OFFERS,
                default=data.get(CONF_AUTO_ACTIVATE_OFFERS, False),
            ): BooleanSelector(),
            vol.Required(
                CONF_AUTH_UPDATE_METHOD,
                default=AUTH_UPDATE_KEEP,
            ): SelectSelector(SelectSelectorConfig(options=AUTH_UPDATE_OPTIONS)),
        }
    )


def _options_credentials_schema(config_entry: config_entries.ConfigEntry) -> vol.Schema:
    """Return the credential update schema."""
    return vol.Schema(
        {
            vol.Optional(
                CONF_USERNAME,
                default=config_entry.data.get(CONF_USERNAME, ""),
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
            vol.Optional(CONF_PASSWORD): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
        }
    )


MFA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_OTP_CODE): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT)
        )
    }
)


class DelhaizeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Delhaize."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._pending_api: DelhaizeApi | None = None
        self._pending_input: dict[str, Any] | None = None
        self._mfa_token: str | None = None
        self._mfa_target: str = "your email address"
        self._reauth_entry: config_entries.ConfigEntry | None = None

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> DelhaizeOptionsFlowHandler:
        """Create the options flow."""
        return DelhaizeOptionsFlowHandler(config_entry)

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Handle the initial setup step."""
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=_user_schema())

        data = _clean_input(user_input)
        return await self._async_try_auth(data, step_id="user")

    async def async_step_cookie(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Handle the cookie fallback step."""
        if user_input is None:
            return self._show_cookie_form()

        data = {**(self._pending_input or {}), **_clean_input(user_input)}
        return await self._async_try_auth(data, step_id="cookie")

    async def async_step_reauth(
        self,
        entry_data: dict[str, Any],
    ) -> config_entries.ConfigFlowResult:
        """Handle reauthentication."""
        entry_id = self.context.get("entry_id")
        if entry_id:
            self._reauth_entry = self.hass.config_entries.async_get_entry(entry_id)

        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(
                {
                    CONF_LANGUAGE: entry_data.get(CONF_LANGUAGE, DEFAULT_LANGUAGE),
                    CONF_AUTO_ACTIVATE_OFFERS: entry_data.get(
                        CONF_AUTO_ACTIVATE_OFFERS,
                        False,
                    ),
                }
            ),
        )

    async def async_step_mfa(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Handle a Delhaize MFA one-time password."""
        if user_input is None:
            return self._show_mfa_form()

        if not self._pending_api or not self._pending_input or not self._mfa_token:
            return self.async_abort(reason="mfa_session_expired")

        try:
            _LOGGER.debug("Submitting Delhaize MFA email code: target=%s", self._mfa_target)
            await self._pending_api.login_with_mfa(
                str(user_input[CONF_OTP_CODE]).strip(),
                self._mfa_token,
                lang=self._pending_input.get(CONF_LANGUAGE, DEFAULT_LANGUAGE),
            )
            customer = await self._pending_api.validate_session()
            _LOGGER.debug("Delhaize MFA login completed and session validated")
        except DelhaizeCaptchaRequired as err:
            _LOGGER.debug(
                "Delhaize MFA login requires captcha: errors=%s",
                summarize_graphql_errors(err.errors),
            )
            return self._show_mfa_form(errors={"base": "captcha_required"})
        except DelhaizeAuthError as err:
            _LOGGER.debug(
                "Delhaize MFA login failed: errors=%s",
                summarize_graphql_errors(err.errors),
            )
            return self._show_mfa_form(errors={"base": "login_failed"})
        except (DelhaizeRequestError, ClientError, TimeoutError) as err:
            _LOGGER.debug("Delhaize MFA login connection failed: %r", err)
            return self._show_mfa_form(errors={"base": "cannot_connect"})
        except DelhaizeApiError as err:
            _LOGGER.debug(
                "Delhaize MFA login failed with API error: error=%s errors=%s",
                err,
                summarize_graphql_errors(err.errors),
            )
            return self._show_mfa_form(errors={"base": "unknown"})

        return await self._async_create_or_update_entry(
            self._pending_input,
            customer,
            self._pending_api,
        )

    async def _async_try_auth(
        self,
        data: dict[str, Any],
        *,
        step_id: str,
    ) -> config_entries.ConfigFlowResult:
        """Authenticate and create or update the entry."""
        errors: dict[str, str] = {}
        has_cookie = bool(data.get(CONF_COOKIE))
        has_credentials = _has_credentials(data) and step_id != "cookie"
        _LOGGER.debug(
            "Starting Delhaize setup authentication: username=%s credentials_present=%s cookie_present=%s language=%s auto_activate=%s step=%s",
            _mask_identifier(data.get(CONF_USERNAME)),
            has_credentials,
            has_cookie,
            data.get(CONF_LANGUAGE, DEFAULT_LANGUAGE),
            data.get(CONF_AUTO_ACTIVATE_OFFERS, False),
            step_id,
        )
        if not has_cookie and not has_credentials:
            return self._show_step_form(step_id, data, errors={"base": "missing_auth"})

        api = _api_for_data(self.hass, data)

        try:
            customer = await _authenticate(api, data, has_credentials)
        except DelhaizeMfaRequired as err:
            _LOGGER.debug(
                "Delhaize setup requires MFA: token_present=%s purpose=%s errors=%s",
                bool(err.mfa_token),
                err.mfa_purpose,
                summarize_graphql_errors(err.errors),
            )
            if not err.mfa_token:
                errors["base"] = "mfa_required"
            else:
                self._pending_api = api
                self._pending_input = data
                self._mfa_token = err.mfa_token
                self._mfa_target = await _try_send_mfa_code(
                    api,
                    err,
                    data.get(CONF_LANGUAGE, DEFAULT_LANGUAGE),
                )
                _LOGGER.debug("Delhaize MFA email code requested: target=%s", self._mfa_target)
                return self._show_mfa_form()
        except DelhaizeCaptchaRequired as err:
            _LOGGER.debug(
                "Delhaize setup requires browser captcha before MFA; no temporary email code can be sent by this integration: errors=%s",
                summarize_graphql_errors(err.errors),
            )
            if step_id != "cookie":
                self._pending_input = data
                return self._show_cookie_form(errors={"base": "captcha_required"})
            errors["base"] = "captcha_required"
        except DelhaizeAuthError as err:
            _LOGGER.debug(
                "Delhaize credential login failed: errors=%s",
                summarize_graphql_errors(err.errors),
            )
            errors["base"] = "login_failed"
        except (DelhaizeRequestError, ClientError, TimeoutError) as err:
            _LOGGER.debug("Delhaize setup connection failed: %r", err)
            errors["base"] = "cannot_connect"
        except DelhaizeApiError as err:
            _LOGGER.debug(
                "Delhaize setup failed with API error: error=%s errors=%s",
                err,
                summarize_graphql_errors(err.errors),
            )
            errors["base"] = "unknown"

        if errors:
            return self._show_step_form(step_id, data, errors=errors)

        return await self._async_create_or_update_entry(data, customer, api)

    async def _async_create_or_update_entry(
        self,
        data: dict[str, Any],
        customer: dict[str, Any],
        api: DelhaizeApi,
    ) -> config_entries.ConfigFlowResult:
        """Create or update the Delhaize config entry."""
        unique_id = _customer_unique_id(customer, data)
        await self.async_set_unique_id(str(unique_id))

        entry_data = _entry_data_with_cookie(data, api)

        if self._reauth_entry:
            if self._reauth_entry.unique_id and self._reauth_entry.unique_id != str(unique_id):
                return self.async_abort(reason="wrong_account")

            self.hass.config_entries.async_update_entry(self._reauth_entry, data=entry_data)
            await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
            return self.async_abort(reason="reauth_successful")

        self._abort_if_unique_id_configured(updates=entry_data)

        title = _entry_title(customer, data)
        return self.async_create_entry(title=title, data=entry_data)

    def _show_step_form(
        self,
        step_id: str,
        data: dict[str, Any],
        *,
        errors: dict[str, str],
    ) -> config_entries.ConfigFlowResult:
        """Show the appropriate setup form."""
        if step_id == "cookie":
            return self._show_cookie_form(errors=errors)
        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(data),
            errors=errors,
        )

    def _show_cookie_form(
        self,
        *,
        errors: dict[str, str] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Show the cookie fallback form."""
        return self.async_show_form(
            step_id="cookie",
            data_schema=_cookie_schema(),
            errors=errors or {},
        )

    def _show_mfa_form(
        self,
        *,
        errors: dict[str, str] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Show the MFA email code form."""
        return self.async_show_form(
            step_id="mfa",
            data_schema=MFA_SCHEMA,
            errors=errors or {},
            description_placeholders={"otp_target": self._mfa_target},
        )


class DelhaizeOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle Delhaize options updates."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry
        self._pending_settings: dict[str, Any] = {}
        self._pending_api: DelhaizeApi | None = None
        self._pending_input: dict[str, Any] | None = None
        self._mfa_token: str | None = None
        self._mfa_target: str = "your email address"

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Handle options menu."""
        if user_input is None:
            return self.async_show_form(
                step_id="init",
                data_schema=_options_schema(self._config_entry),
            )

        self._pending_settings = {
            CONF_LANGUAGE: user_input[CONF_LANGUAGE],
            CONF_AUTO_ACTIVATE_OFFERS: user_input[CONF_AUTO_ACTIVATE_OFFERS],
        }
        auth_update = user_input.get(CONF_AUTH_UPDATE_METHOD, AUTH_UPDATE_KEEP)

        if auth_update == AUTH_METHOD_CREDENTIALS:
            return self.async_show_form(
                step_id="credentials",
                data_schema=_options_credentials_schema(self._config_entry),
            )

        if auth_update == AUTH_METHOD_COOKIE:
            return self._show_cookie_form()

        await self._async_update_entry({**self._config_entry.data, **self._pending_settings})
        return self.async_create_entry(title="", data={})

    async def async_step_credentials(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Update Delhaize credentials."""
        if user_input is None:
            return self.async_show_form(
                step_id="credentials",
                data_schema=_options_credentials_schema(self._config_entry),
            )

        data = {
            key: value
            for key, value in self._config_entry.data.items()
            if key != CONF_COOKIE
        }
        data.update(self._pending_settings)

        username = user_input.get(CONF_USERNAME)
        password = user_input.get(CONF_PASSWORD)
        if isinstance(username, str) and username.strip():
            data[CONF_USERNAME] = username.strip()
        if isinstance(password, str) and password.strip():
            data[CONF_PASSWORD] = password.strip()

        data = _clean_input(data)
        if not _has_credentials(data):
            return self.async_show_form(
                step_id="credentials",
                data_schema=_options_credentials_schema(self._config_entry),
                errors={"base": "missing_auth"},
            )

        return await self._async_validate_and_update(data, step_id="credentials")

    async def async_step_cookie(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Update Delhaize cookie authentication."""
        if user_input is None:
            return self._show_cookie_form()

        data = _clean_input({**self._config_entry.data, **self._pending_settings, **user_input})
        return await self._async_validate_and_update(data, step_id="cookie")

    async def async_step_mfa(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Handle MFA during credential update."""
        if user_input is None:
            return self._show_mfa_form()

        if not self._pending_api or not self._pending_input or not self._mfa_token:
            return self.async_abort(reason="mfa_session_expired")

        try:
            _LOGGER.debug("Submitting Delhaize MFA email code during options update")
            await self._pending_api.login_with_mfa(
                str(user_input[CONF_OTP_CODE]).strip(),
                self._mfa_token,
                lang=self._pending_input.get(CONF_LANGUAGE, DEFAULT_LANGUAGE),
            )
            customer = await self._pending_api.validate_session()
        except DelhaizeCaptchaRequired as err:
            _LOGGER.debug(
                "Delhaize options MFA login requires captcha: errors=%s",
                summarize_graphql_errors(err.errors),
            )
            return self._show_mfa_form(errors={"base": "captcha_required"})
        except DelhaizeAuthError as err:
            _LOGGER.debug(
                "Delhaize options MFA login failed: errors=%s",
                summarize_graphql_errors(err.errors),
            )
            return self._show_mfa_form(errors={"base": "login_failed"})
        except (DelhaizeRequestError, ClientError, TimeoutError) as err:
            _LOGGER.debug("Delhaize options MFA login connection failed: %r", err)
            return self._show_mfa_form(errors={"base": "cannot_connect"})
        except DelhaizeApiError as err:
            _LOGGER.debug(
                "Delhaize options MFA login failed with API error: error=%s errors=%s",
                err,
                summarize_graphql_errors(err.errors),
            )
            return self._show_mfa_form(errors={"base": "unknown"})

        if self._config_entry.unique_id and self._config_entry.unique_id != str(
            _customer_unique_id(customer, self._pending_input)
        ):
            return self.async_abort(reason="wrong_account")

        await self._async_update_entry(
            _entry_data_with_cookie(self._pending_input, self._pending_api)
        )
        return self.async_create_entry(title="", data={})

    async def _async_validate_and_update(
        self,
        data: dict[str, Any],
        *,
        step_id: str,
    ) -> config_entries.ConfigFlowResult:
        """Validate updated auth settings and update the entry."""
        api = _api_for_data(self.hass, data)
        has_credentials = _has_credentials(data) and step_id != "cookie"
        try:
            customer = await _authenticate(api, data, has_credentials)
        except DelhaizeMfaRequired as err:
            if not err.mfa_token:
                return self._show_step_form(step_id, errors={"base": "mfa_required"})
            self._pending_api = api
            self._pending_input = data
            self._mfa_token = err.mfa_token
            self._mfa_target = await _try_send_mfa_code(
                api,
                err,
                data.get(CONF_LANGUAGE, DEFAULT_LANGUAGE),
            )
            return self._show_mfa_form()
        except DelhaizeCaptchaRequired as err:
            _LOGGER.debug(
                "Delhaize options auth requires browser captcha before MFA; no temporary email code can be sent by this integration: errors=%s",
                summarize_graphql_errors(err.errors),
            )
            if step_id != "cookie":
                self._pending_settings = _settings_from_data(data)
                return self._show_cookie_form(errors={"base": "captcha_required"})
            return self._show_step_form(step_id, errors={"base": "captcha_required"})
        except DelhaizeAuthError as err:
            _LOGGER.debug(
                "Delhaize options auth failed: errors=%s",
                summarize_graphql_errors(err.errors),
            )
            return self._show_step_form(step_id, errors={"base": "login_failed"})
        except (DelhaizeRequestError, ClientError, TimeoutError) as err:
            _LOGGER.debug("Delhaize options auth connection failed: %r", err)
            return self._show_step_form(step_id, errors={"base": "cannot_connect"})
        except DelhaizeApiError as err:
            _LOGGER.debug(
                "Delhaize options auth failed with API error: error=%s errors=%s",
                err,
                summarize_graphql_errors(err.errors),
            )
            return self._show_step_form(step_id, errors={"base": "unknown"})

        if self._config_entry.unique_id and self._config_entry.unique_id != str(
            _customer_unique_id(customer, data)
        ):
            return self.async_abort(reason="wrong_account")

        await self._async_update_entry(_entry_data_with_cookie(data, api))
        return self.async_create_entry(title="", data={})

    async def _async_update_entry(self, data: dict[str, Any]) -> None:
        """Update and reload the config entry."""
        self.hass.config_entries.async_update_entry(self._config_entry, data=data)
        await self.hass.config_entries.async_reload(self._config_entry.entry_id)

    def _show_step_form(
        self,
        step_id: str,
        *,
        errors: dict[str, str],
    ) -> config_entries.ConfigFlowResult:
        """Show an options form with errors."""
        if step_id == "cookie":
            return self._show_cookie_form(errors=errors)
        return self.async_show_form(
            step_id="credentials",
            data_schema=_options_credentials_schema(self._config_entry),
            errors=errors,
        )

    def _show_cookie_form(
        self,
        *,
        errors: dict[str, str] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Show the cookie update form."""
        return self.async_show_form(
            step_id="cookie",
            data_schema=_cookie_schema(),
            errors=errors or {},
        )

    def _show_mfa_form(
        self,
        *,
        errors: dict[str, str] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Show the MFA email code form."""
        return self.async_show_form(
            step_id="mfa",
            data_schema=MFA_SCHEMA,
            errors=errors or {},
            description_placeholders={"otp_target": self._mfa_target},
        )


async def _authenticate(
    api: DelhaizeApi,
    data: dict[str, Any],
    has_credentials: bool,
) -> dict[str, Any]:
    """Authenticate with cookie or credentials and return the customer."""
    if data.get(CONF_COOKIE):
        try:
            _LOGGER.debug("Trying Delhaize authentication with provided cookie")
            return await api.validate_session()
        except DelhaizeAuthError as err:
            _LOGGER.debug(
                "Provided Delhaize cookie did not validate: credentials_fallback=%s errors=%s",
                has_credentials,
                summarize_graphql_errors(err.errors),
            )
            if not has_credentials:
                raise

    _LOGGER.debug("Trying Delhaize authentication with username/password")
    await api.login(
        data[CONF_USERNAME],
        data[CONF_PASSWORD],
        lang=data.get(CONF_LANGUAGE, DEFAULT_LANGUAGE),
    )
    return await api.validate_session()


async def _try_send_mfa_code(
    api: DelhaizeApi,
    err: DelhaizeMfaRequired,
    language: str,
) -> str:
    """Try to trigger sending an MFA code and return the target."""
    try:
        data = await api.send_login_mfa_otp_code(
            err.mfa_token or "",
            mfa_purpose=err.mfa_purpose or "LOGIN",
            lang=language,
        )
        value = data.get("sendMfaOtpCode") if isinstance(data, dict) else None
        if isinstance(value, dict):
            _LOGGER.debug(
                "Delhaize MFA email code response: method=%s target=%s next_possible_send_time=%s",
                value.get("mfaMethod"),
                value.get("otpTarget"),
                value.get("nextPossibleSendTime"),
            )
        return _otp_target_from_response(data)
    except DelhaizeApiError as send_err:
        _LOGGER.debug(
            "Could not request Delhaize MFA email code: error=%s errors=%s",
            send_err,
            summarize_graphql_errors(send_err.errors),
        )
        return "your email address"


def _api_for_data(hass: HomeAssistant, data: dict[str, Any]) -> DelhaizeApi:
    """Create an API client for flow validation."""
    return DelhaizeApi(
        async_get_clientsession(hass),
        cookie_header=data.get(CONF_COOKIE),
        language=data.get(CONF_LANGUAGE, DEFAULT_LANGUAGE),
    )


def _otp_target_from_response(data: dict[str, Any]) -> str:
    """Extract the masked MFA target returned by Delhaize."""
    value = data.get("sendMfaOtpCode")
    if isinstance(value, dict):
        target = value.get("otpTarget")
        if target:
            return str(target)
    return "your email address"


def _entry_data_with_cookie(data: dict[str, Any], api: DelhaizeApi) -> dict[str, Any]:
    """Return config entry data with transient form keys removed and cookies refreshed."""
    entry_data = {
        key: value
        for key, value in data.items()
        if key not in {CONF_AUTH_METHOD, CONF_AUTH_UPDATE_METHOD}
    }
    cookie_header = api.get_cookie_header()
    if cookie_header:
        entry_data[CONF_COOKIE] = cookie_header
    return entry_data


def _clean_input(user_input: dict[str, Any]) -> dict[str, Any]:
    """Trim blank optional values from config flow input."""
    data = dict(user_input)
    for key in (CONF_USERNAME, CONF_PASSWORD, CONF_COOKIE):
        value = data.get(key)
        if isinstance(value, str):
            value = value.strip()
        if value:
            data[key] = value
        else:
            data.pop(key, None)
    data.setdefault(CONF_LANGUAGE, DEFAULT_LANGUAGE)
    data.setdefault(CONF_AUTO_ACTIVATE_OFFERS, False)
    return data


def _settings_from_data(data: dict[str, Any]) -> dict[str, Any]:
    """Return mutable non-auth settings from flow data."""
    return {
        CONF_LANGUAGE: data.get(CONF_LANGUAGE, DEFAULT_LANGUAGE),
        CONF_AUTO_ACTIVATE_OFFERS: data.get(CONF_AUTO_ACTIVATE_OFFERS, False),
    }


def _has_credentials(data: dict[str, Any]) -> bool:
    """Return whether username and password are available."""
    return bool(data.get(CONF_USERNAME) and data.get(CONF_PASSWORD))


def _customer_unique_id(customer: dict[str, Any], data: dict[str, Any]) -> str:
    """Return the unique id for a Delhaize account."""
    return str(
        customer.get("customerIdHash")
        or customer.get("uid")
        or data.get(CONF_USERNAME)
        or NAME
    )


def _mask_identifier(value: Any) -> str:
    """Mask a username or email for logs."""
    if not isinstance(value, str) or not value:
        return "<empty>"
    if "@" in value:
        local, domain = value.split("@", 1)
        masked_local = f"{local[:1]}***" if local else "***"
        return f"{masked_local}@{domain}"
    return f"{value[:2]}***"


def _entry_title(customer: dict[str, Any], data: dict[str, Any]) -> str:
    """Build a friendly config entry title."""
    full_name = " ".join(
        value
        for value in (customer.get("firstName"), customer.get("lastName"))
        if value
    ).strip()
    if full_name:
        return f"{NAME} {full_name}"
    if data.get(CONF_USERNAME):
        return f"{NAME} {data[CONF_USERNAME]}"
    return NAME
