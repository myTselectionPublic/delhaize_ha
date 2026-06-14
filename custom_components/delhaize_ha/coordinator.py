"""Data update coordinator for the Delhaize integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_AUTO_ACTIVATE_OFFERS,
    CONF_COOKIE,
    CONF_LANGUAGE,
    DEFAULT_LANGUAGE,
    DOMAIN,
    UPDATE_INTERVAL,
)
from .delhaizeApi import (
    DelhaizeApi,
    DelhaizeApiError,
    DelhaizeAuthError,
    DelhaizeCaptchaRequired,
    DelhaizeMfaRequired,
    DelhaizeRequestError,
)

_LOGGER = logging.getLogger(DOMAIN)


class DelhaizeDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinate Delhaize account data updates."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        api: DelhaizeApi,
        config_entry: ConfigEntry,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=config_entry,
            update_interval=UPDATE_INTERVAL,
        )
        self.api = api
        self.config_entry = config_entry

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from Delhaize."""
        try:
            data = await self._fetch_summary()
        except (DelhaizeCaptchaRequired, DelhaizeMfaRequired) as exc:
            raise ConfigEntryAuthFailed(str(exc)) from exc
        except DelhaizeAuthError as exc:
            if not self._has_credentials:
                raise ConfigEntryAuthFailed(str(exc)) from exc
            try:
                await self._login_with_stored_credentials()
                data = await self._fetch_summary()
            except (DelhaizeCaptchaRequired, DelhaizeMfaRequired, DelhaizeAuthError) as login_exc:
                raise ConfigEntryAuthFailed(str(login_exc)) from login_exc
            except DelhaizeApiError as login_exc:
                raise UpdateFailed(str(login_exc)) from login_exc
        except DelhaizeRequestError as exc:
            raise UpdateFailed(str(exc)) from exc
        except DelhaizeApiError as exc:
            raise UpdateFailed(str(exc)) from exc

        self._persist_cookie_if_changed()
        return data

    async def async_activate_personal_offers(self) -> Any:
        """Activate all personal offers and refresh coordinator data."""
        result = await self.api.activate_all_personal_offers()
        self._persist_cookie_if_changed()
        await self.async_request_refresh()
        return result

    async def _fetch_summary(self) -> dict[str, Any]:
        """Fetch the account summary from the API."""
        self._load_cookie_from_entry()
        self.api.language = self.config_entry.data.get(CONF_LANGUAGE, DEFAULT_LANGUAGE)
        return await self.api.fetch_summary(
            auto_activate=self.config_entry.data.get(CONF_AUTO_ACTIVATE_OFFERS, False)
        )

    async def _login_with_stored_credentials(self) -> None:
        """Create a fresh session from stored credentials."""
        await self.api.login(
            self.config_entry.data[CONF_USERNAME],
            self.config_entry.data[CONF_PASSWORD],
            lang=self.config_entry.data.get(CONF_LANGUAGE, DEFAULT_LANGUAGE),
        )

    def _load_cookie_from_entry(self) -> None:
        """Ensure the API has the stored cookie after reloads or updates."""
        cookie_header = self.config_entry.data.get(CONF_COOKIE)
        if cookie_header and not self.api.get_cookie_header():
            self.api.set_cookie_header(cookie_header)

    def _persist_cookie_if_changed(self) -> None:
        """Persist updated Delhaize cookies in the config entry."""
        cookie_header = self.api.get_cookie_header()
        if not cookie_header or cookie_header == self.config_entry.data.get(CONF_COOKIE):
            return

        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={**self.config_entry.data, CONF_COOKIE: cookie_header},
        )

    @property
    def _has_credentials(self) -> bool:
        """Return whether username and password are stored."""
        return bool(
            self.config_entry.data.get(CONF_USERNAME)
            and self.config_entry.data.get(CONF_PASSWORD)
        )
