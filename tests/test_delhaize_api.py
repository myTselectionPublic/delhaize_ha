"""Tests for the Delhaize GraphQL client."""

from __future__ import annotations

import asyncio
from http.cookies import SimpleCookie
import importlib.util
import json as json_module
from pathlib import Path
import sys
import types
from typing import Any


def _load_delhaize_api() -> types.ModuleType:
    """Load the API module without importing the Home Assistant integration package."""
    root = Path(__file__).resolve().parents[1]
    custom_components_path = root / "custom_components"
    integration_path = custom_components_path / "delhaize_ha"

    custom_components = sys.modules.setdefault(
        "custom_components",
        types.ModuleType("custom_components"),
    )
    custom_components.__path__ = [str(custom_components_path)]

    package = types.ModuleType("custom_components.delhaize_ha")
    package.__path__ = [str(integration_path)]
    sys.modules["custom_components.delhaize_ha"] = package

    spec = importlib.util.spec_from_file_location(
        "custom_components.delhaize_ha.delhaizeApi",
        integration_path / "delhaizeApi" / "__init__.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


delhaize_api = _load_delhaize_api()
DelhaizeApi = delhaize_api.DelhaizeApi
DelhaizeTokenRefreshRequired = delhaize_api.DelhaizeTokenRefreshRequired
REFRESH_CUSTOMER_TOKEN_HASH = delhaize_api.REFRESH_CUSTOMER_TOKEN_HASH


def test_validate_session_refreshes_pending_token_and_retries() -> None:
    """Delhaize's web client refreshes cookies on PENDING_TOKEN_REFRESH."""

    async def run_test() -> None:
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "errors": [
                            {
                                "message": "Pending token refresh",
                                "extensions": {"code": "PENDING_TOKEN_REFRESH"},
                            }
                        ]
                    }
                ),
                FakeResponse(
                    {"data": {"refreshCustomerAuthCookies": None}},
                    cookies={"session": "new-session"},
                ),
                FakeResponse(
                    {
                        "data": {
                            "currentCustomer": {
                                "uid": "customer-1",
                                "firstName": "Del",
                            }
                        }
                    }
                ),
            ]
        )
        api = DelhaizeApi(session, cookie_header="session=old-session")

        customer = await api.validate_session()

        assert customer["uid"] == "customer-1"
        assert [request["operation"] for request in session.requests] == [
            "CurrentCustomer",
            "RefreshCustomerToken",
            "CurrentCustomer",
        ]
        assert_refresh_customer_token_request(session.requests[1])
        assert "session=new-session" in api.get_cookie_header()

    asyncio.run(run_test())


def test_graphql_refreshes_access_token_expired_and_retries_operation() -> None:
    """Delhaize sometimes reports token expiry as a plain error message."""

    async def run_test() -> None:
        session = FakeSession(
            [
                FakeResponse({"errors": [{"message": "Access token expired"}]}),
                FakeResponse(
                    {"data": {"refreshCustomerAuthCookies": None}},
                    cookies={"session": "new-session"},
                ),
                FakeResponse(
                    {
                        "data": {
                            "loyaltyPoints": {"pointsBalance": 42},
                            "nutriscoreBalance": {},
                            "savings": {},
                        }
                    }
                ),
            ]
        )
        api = DelhaizeApi(session, cookie_header="session=old-session")

        details = await api.get_loyalty_details()

        assert details["loyaltyPoints"]["pointsBalance"] == 42
        assert [request["operation"] for request in session.requests] == [
            "getIbizaAccountDetails",
            "RefreshCustomerToken",
            "getIbizaAccountDetails",
        ]
        assert_refresh_customer_token_request(session.requests[1])
        assert "session=new-session" in session.requests[2]["headers"]["Cookie"]
        assert "session=new-session" in api.get_cookie_header()

    asyncio.run(run_test())


def test_graphql_refreshes_invalid_access_token_and_retries_operation() -> None:
    """Delhaize can report stale auth cookies as an invalid access token."""

    async def run_test() -> None:
        session = FakeSession(
            [
                FakeResponse({"errors": [{"message": "Invalid access token"}]}),
                FakeResponse(
                    {"data": {"refreshCustomerAuthCookies": None}},
                    cookies={"session": "new-session"},
                ),
                FakeResponse(
                    {
                        "data": {
                            "loyaltyPoints": {"pointsBalance": 42},
                            "nutriscoreBalance": {},
                            "savings": {},
                        }
                    }
                ),
            ]
        )
        api = DelhaizeApi(session, cookie_header="session=old-session")

        details = await api.get_loyalty_details()

        assert details["loyaltyPoints"]["pointsBalance"] == 42
        assert [request["operation"] for request in session.requests] == [
            "getIbizaAccountDetails",
            "RefreshCustomerToken",
            "getIbizaAccountDetails",
        ]
        assert_refresh_customer_token_request(session.requests[1])
        assert "session=new-session" in session.requests[2]["headers"]["Cookie"]
        assert "session=new-session" in api.get_cookie_header()

    asyncio.run(run_test())


def test_refresh_operation_does_not_loop_on_expired_token() -> None:
    """A failed refresh should surface instead of retrying refresh recursively."""

    async def run_test() -> None:
        session = FakeSession(
            [
                FakeResponse({"errors": [{"message": "Access token expired"}]}),
                FakeResponse({"errors": [{"message": "Access token expired"}]}),
            ]
        )
        api = DelhaizeApi(session, cookie_header="session=old-session")

        try:
            await api.get_loyalty_details()
        except DelhaizeTokenRefreshRequired:
            pass
        else:
            raise AssertionError("Expected token refresh failure to be raised")

        assert [request["operation"] for request in session.requests] == [
            "getIbizaAccountDetails",
            "RefreshCustomerToken",
        ]

    asyncio.run(run_test())


def test_refresh_operation_does_not_loop_on_invalid_access_token() -> None:
    """A failed invalid-token refresh should surface without recursive refresh."""

    async def run_test() -> None:
        session = FakeSession(
            [
                FakeResponse({"errors": [{"message": "Invalid access token"}]}),
                FakeResponse({"errors": [{"message": "Invalid access token"}]}),
            ]
        )
        api = DelhaizeApi(session, cookie_header="session=old-session")

        try:
            await api.get_loyalty_details()
        except DelhaizeTokenRefreshRequired:
            pass
        else:
            raise AssertionError("Expected token refresh failure to be raised")

        assert [request["operation"] for request in session.requests] == [
            "getIbizaAccountDetails",
            "RefreshCustomerToken",
        ]

    asyncio.run(run_test())


class FakeSession:
    """Minimal aiohttp-like session for GraphQL tests."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        """Initialize the fake response queue."""
        self._responses = responses
        self.requests: list[dict[str, Any]] = []

    def post(
        self,
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str],
        timeout: int,
    ) -> FakeRequest:
        """Return the next fake response."""
        self.requests.append(
            {
                "url": url,
                "operation": json["operationName"],
                "payload": json,
                "headers": headers,
                "timeout": timeout,
            }
        )
        return FakeRequest(self._responses.pop(0))


def assert_refresh_customer_token_request(request: dict[str, Any]) -> None:
    """Assert the refresh request matches Delhaize's persisted-query browser call."""
    assert request["headers"]["x-do-refresh-token"] == "true"
    assert request["payload"] == {
        "operationName": "RefreshCustomerToken",
        "variables": {},
        "extensions": {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": REFRESH_CUSTOMER_TOKEN_HASH,
            }
        },
    }


class FakeRequest:
    """Async context manager returned by FakeSession.post."""

    def __init__(self, response: FakeResponse) -> None:
        """Initialize the request."""
        self._response = response

    async def __aenter__(self) -> FakeResponse:
        """Enter the fake request context."""
        return self._response

    async def __aexit__(self, *args: object) -> None:
        """Exit the fake request context."""


class FakeResponse:
    """Minimal aiohttp-like response for GraphQL tests."""

    def __init__(
        self,
        payload: dict[str, Any],
        *,
        status: int = 200,
        cookies: dict[str, str] | None = None,
    ) -> None:
        """Initialize the response."""
        self.status = status
        self._text = json_module.dumps(payload)
        self.cookies = SimpleCookie()
        for key, value in (cookies or {}).items():
            self.cookies[key] = value

    async def text(self) -> str:
        """Return the fake response body."""
        return self._text
