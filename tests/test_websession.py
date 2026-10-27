"""Two Delhaize accounts must not share cookies through one HTTP session."""

from __future__ import annotations

import asyncio

import pytest

aiohttp = pytest.importorskip("aiohttp")
from aiohttp import web  # noqa: E402

from .test_delhaize_api import DelhaizeApi, delhaize_api  # noqa: E402


async def _start_server() -> tuple[web.AppRunner, str, list[str]]:
    """Serve CurrentCustomer and renew the access cookie on every response."""
    seen: list[str] = []

    async def handler(request: web.Request) -> web.Response:
        cookie = request.cookies.get("grocery-roatc", "")
        seen.append(cookie)
        response = web.json_response({"data": {"currentCustomer": {"uid": cookie}}})
        response.set_cookie("grocery-roatc", cookie + "+", path="/")
        return response

    app = web.Application()
    app.router.add_post("/api/v1/", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}/api/v1/", seen


def test_shared_cookie_jar_sends_one_accounts_cookie_for_another(monkeypatch) -> None:
    """Why the integration does not use Home Assistant's shared session."""

    async def run_test() -> None:
        runner, url, seen = await _start_server()
        monkeypatch.setattr(delhaize_api, "API_URL", url)
        # Like the shared session, this one keeps a cookie jar. unsafe=True only lets it
        # accept cookies from 127.0.0.1, as it does for www.delhaize.be.
        jar = aiohttp.CookieJar(unsafe=True)
        async with aiohttp.ClientSession(cookie_jar=jar) as shared:
            first = DelhaizeApi(shared, cookie_header="grocery-roatc=first")
            second = DelhaizeApi(shared, cookie_header="grocery-roatc=second")
            await first.current_customer()
            await second.current_customer()
        await runner.cleanup()
        # The jar's cookie overrides the one the second account sent explicitly.
        assert seen == ["first", "first+"]

    asyncio.run(run_test())


def test_sessions_without_cookie_jar_keep_accounts_apart(monkeypatch) -> None:
    """Each account sends only its own cookies and still keeps renewed ones."""

    async def run_test() -> None:
        runner, url, seen = await _start_server()
        monkeypatch.setattr(delhaize_api, "API_URL", url)
        async with (
            aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as one,
            aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as other,
        ):
            first = DelhaizeApi(one, cookie_header="grocery-roatc=first")
            second = DelhaizeApi(other, cookie_header="grocery-roatc=second")
            await first.current_customer()
            await second.current_customer()
            await first.current_customer()
        await runner.cleanup()
        assert seen == ["first", "second", "first+"]
        assert first.get_cookie_header() == "grocery-roatc=first++"
        assert second.get_cookie_header() == "grocery-roatc=second+"

    asyncio.run(run_test())
