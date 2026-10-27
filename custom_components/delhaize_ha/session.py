"""HTTP sessions for Delhaize accounts."""

from __future__ import annotations

from aiohttp import ClientSession, DummyCookieJar

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession


def async_create_websession(hass: HomeAssistant) -> ClientSession:
    """Return a session that does not keep cookies of its own.

    DelhaizeApi stores each account's cookies itself and sends them explicitly.
    Home Assistant's shared session has one cookie jar for every caller: cookies
    from any Delhaize response land there, are added to every later request and
    override explicitly sent cookies with the same name. With two accounts, one
    account's session cookies would then be sent for the other.
    """
    return async_create_clientsession(hass, cookie_jar=DummyCookieJar())
