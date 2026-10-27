"""The Delhaize integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError

from .const import CONF_COOKIE, CONF_LANGUAGE, DEFAULT_LANGUAGE, DOMAIN
from .coordinator import DelhaizeDataUpdateCoordinator
from .delhaizeApi import DelhaizeApi, DelhaizeApiError
from .session import async_create_websession

_LOGGER = logging.getLogger(DOMAIN)

PLATFORMS: list[Platform] = [Platform.SENSOR]

SERVICE_ACTIVATE_PERSONAL_OFFERS = "activate_personal_offers"
CONF_ENTRY_ID = "entry_id"
SERVICE_ACTIVATE_PERSONAL_OFFERS_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_ENTRY_ID): str,
    }
)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the Delhaize integration."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Delhaize from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    api = DelhaizeApi(
        async_create_websession(hass),
        cookie_header=entry.data.get(CONF_COOKIE),
        language=entry.data.get(CONF_LANGUAGE, DEFAULT_LANGUAGE),
    )
    coordinator = DelhaizeDataUpdateCoordinator(hass, api, entry)

    hass.data[DOMAIN][entry.entry_id] = coordinator
    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Delhaize config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    hass.data[DOMAIN].pop(entry.entry_id, None)
    _unregister_services_if_unused(hass)
    return True


def _register_services(hass: HomeAssistant) -> None:
    """Register Delhaize services once."""
    if hass.data[DOMAIN].get("_service_registered"):
        return

    async def async_activate_personal_offers_service(service: ServiceCall) -> None:
        """Activate Delhaize personal offers for one or all entries."""
        entry_id = service.data.get(CONF_ENTRY_ID)
        coordinators = _matching_coordinators(hass, entry_id)
        if not coordinators:
            raise HomeAssistantError("No Delhaize config entry found")

        for coordinator in coordinators:
            try:
                await coordinator.async_activate_personal_offers()
            except DelhaizeApiError as err:
                raise HomeAssistantError(
                    f"Could not activate Delhaize personal offers: {err}"
                ) from err

    hass.services.async_register(
        DOMAIN,
        SERVICE_ACTIVATE_PERSONAL_OFFERS,
        async_activate_personal_offers_service,
        SERVICE_ACTIVATE_PERSONAL_OFFERS_SCHEMA,
    )
    hass.data[DOMAIN]["_service_registered"] = True


def _unregister_services_if_unused(hass: HomeAssistant) -> None:
    """Unregister services when the last entry is unloaded."""
    if any(
        isinstance(value, DelhaizeDataUpdateCoordinator)
        for value in hass.data[DOMAIN].values()
    ):
        return

    if hass.data[DOMAIN].pop("_service_registered", None):
        hass.services.async_remove(DOMAIN, SERVICE_ACTIVATE_PERSONAL_OFFERS)


def _matching_coordinators(
    hass: HomeAssistant,
    entry_id: str | None,
) -> list[DelhaizeDataUpdateCoordinator]:
    """Return coordinators matching a service call."""
    if entry_id:
        coordinator = hass.data[DOMAIN].get(entry_id)
        return [coordinator] if isinstance(coordinator, DelhaizeDataUpdateCoordinator) else []

    return [
        value
        for value in hass.data[DOMAIN].values()
        if isinstance(value, DelhaizeDataUpdateCoordinator)
    ]
