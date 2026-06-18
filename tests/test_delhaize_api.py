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


class FakeClientError(Exception):
    """Stand-in for aiohttp.client_exceptions.ClientError in standalone tests."""


def _install_aiohttp_stub() -> None:
    """Install a tiny aiohttp stub when the dependency is unavailable."""
    if importlib.util.find_spec("aiohttp") is not None:
        return

    aiohttp = types.ModuleType("aiohttp")
    aiohttp.ClientResponse = object
    aiohttp.ClientSession = object

    client_exceptions = types.ModuleType("aiohttp.client_exceptions")
    client_exceptions.ClientError = FakeClientError

    sys.modules["aiohttp"] = aiohttp
    sys.modules["aiohttp.client_exceptions"] = client_exceptions


def _load_delhaize_api() -> types.ModuleType:
    """Load the API module without importing the Home Assistant integration package."""
    _install_aiohttp_stub()

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
DelhaizeAuthError = delhaize_api.DelhaizeAuthError
DelhaizeTokenRefreshRequired = delhaize_api.DelhaizeTokenRefreshRequired
REFRESH_CUSTOMER_TOKEN_HASH = delhaize_api.REFRESH_CUSTOMER_TOKEN_HASH
REFRESH_CUSTOMER_TOKEN_EXTENSIONS = delhaize_api.REFRESH_CUSTOMER_TOKEN_EXTENSIONS
GET_SUBSCRIPTIONS_EXTENSIONS = delhaize_api.GET_SUBSCRIPTIONS_EXTENSIONS


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
                FakeResponse({"data": {"getSubscriptions": []}}),
                FakeResponse(
                    {"data": {"refreshCustomerAuthCookies": None}},
                    cookies={"grocery-roatc": "new-access-token"},
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
        api = DelhaizeApi(
            session,
            cookie_header=(
                "deviceSessionId=device-1; grocery-roatc=old-access-token; "
                "grocery-rortc=refresh-token"
            ),
        )

        customer = await api.validate_session()

        assert customer["uid"] == "customer-1"
        assert [request["operation"] for request in session.requests] == [
            "CurrentCustomer",
            "getSubscriptions",
            "RefreshCustomerToken",
            "CurrentCustomer",
        ]
        assert_get_subscriptions_request(session.requests[1])
        assert_refresh_customer_token_request(session.requests[2])
        assert "grocery-roatc=new-access-token" in api.get_cookie_header()

    asyncio.run(run_test())


def test_graphql_refreshes_access_token_expired_and_retries_operation() -> None:
    """Delhaize sometimes reports token expiry as a plain error message."""

    async def run_test() -> None:
        session = FakeSession(
            [
                FakeResponse({"errors": [{"message": "Access token expired"}]}),
                FakeResponse({"data": {"getSubscriptions": []}}),
                FakeResponse(
                    {"data": {"refreshCustomerAuthCookies": None}},
                    cookies={"grocery-roatc": "new-access-token"},
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
        api = DelhaizeApi(
            session,
            cookie_header=(
                "deviceSessionId=device-1; grocery-roatc=old-access-token; "
                "grocery-rortc=refresh-token"
            ),
        )

        details = await api.get_loyalty_details()

        assert details["loyaltyPoints"]["pointsBalance"] == 42
        assert [request["operation"] for request in session.requests] == [
            "getIbizaAccountDetails",
            "getSubscriptions",
            "RefreshCustomerToken",
            "getIbizaAccountDetails",
        ]
        assert_get_subscriptions_request(session.requests[1])
        assert_refresh_customer_token_request(session.requests[2])
        assert "grocery-roatc=new-access-token" in session.requests[3]["headers"]["Cookie"]
        assert "grocery-roatc=new-access-token" in api.get_cookie_header()

    asyncio.run(run_test())


def test_refresh_stores_auth_cookie_from_raw_set_cookie_header() -> None:
    """Some Set-Cookie headers may not be exposed through response.cookies."""

    async def run_test() -> None:
        session = FakeSession(
            [
                FakeResponse({"errors": [{"message": "Access token expired"}]}),
                FakeResponse({"data": {"getSubscriptions": []}}),
                FakeResponse(
                    {"data": {"refreshCustomerAuthCookies": None}},
                    raw_set_cookie_headers=[
                        "grocery-roatc=new-access-token; Path=/; Secure; HttpOnly",
                    ],
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
        api = DelhaizeApi(
            session,
            cookie_header=(
                "deviceSessionId=device-1; grocery-roatc=old-access-token; "
                "grocery-rortc=refresh-token"
            ),
        )

        details = await api.get_loyalty_details()

        assert details["loyaltyPoints"]["pointsBalance"] == 42
        assert [request["operation"] for request in session.requests] == [
            "getIbizaAccountDetails",
            "getSubscriptions",
            "RefreshCustomerToken",
            "getIbizaAccountDetails",
        ]
        assert "grocery-roatc=new-access-token" in session.requests[3]["headers"]["Cookie"]
        assert "grocery-roatc=new-access-token" in api.get_cookie_header()

    asyncio.run(run_test())


def test_graphql_treats_invalid_access_token_as_auth_lost() -> None:
    """Invalid access tokens should reauthenticate instead of refreshing again."""

    async def run_test() -> None:
        session = FakeSession(
            [
                FakeResponse({"errors": [{"message": "Invalid access token"}]}),
            ]
        )
        api = DelhaizeApi(session, cookie_header="grocery-roatc=old-access-token")

        try:
            await api.get_loyalty_details()
        except DelhaizeAuthError:
            pass
        else:
            raise AssertionError("Expected invalid access token to require reauth")

        assert [request["operation"] for request in session.requests] == [
            "getIbizaAccountDetails",
        ]

    asyncio.run(run_test())


def test_validate_session_treats_unauthenticated_invalid_access_token_as_auth_lost() -> None:
    """Delhaize's browser treats UNAUTHENTICATED invalid-token errors as auth lost."""

    async def run_test() -> None:
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "errors": [
                            {
                                "message": "Invalid access token",
                                "path": ["currentCustomer"],
                                "extensions": {
                                    "code": "UNAUTHENTICATED",
                                    "response": {
                                        "body": {"reasonCode": "UNAUTHENTICATED"}
                                    },
                                },
                            }
                        ]
                    }
                ),
            ]
        )
        api = DelhaizeApi(session, cookie_header="grocery-roatc=old-access-token")

        try:
            await api.validate_session()
        except DelhaizeAuthError:
            pass
        else:
            raise AssertionError("Expected invalid access token to require reauth")

        assert [request["operation"] for request in session.requests] == [
            "CurrentCustomer",
        ]

    asyncio.run(run_test())


def test_refresh_operation_does_not_loop_on_expired_token() -> None:
    """A failed refresh should surface instead of retrying refresh recursively."""

    async def run_test() -> None:
        session = FakeSession(
            [
                FakeResponse({"errors": [{"message": "Access token expired"}]}),
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
            "getSubscriptions",
            "RefreshCustomerToken",
        ]

    asyncio.run(run_test())


def test_refresh_retries_when_only_anti_bot_cookies_change() -> None:
    """Delhaize may accept a refresh without visibly rotating customer auth cookies."""

    async def run_test() -> None:
        session = FakeSession(
            [
                FakeResponse({"errors": [{"message": "Access token expired"}]}),
                FakeResponse({"data": {"getSubscriptions": []}}),
                FakeResponse(
                    {"data": {"refreshCustomerAuthCookies": None}},
                    cookies={"_abck": "new-abck", "ak_bmsc": "new-bmsc", "bm_sv": "new-bm"},
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
        api = DelhaizeApi(
            session,
            cookie_header=(
                "deviceSessionId=device-1; grocery-roatc=old-access-token; "
                "grocery-rortc=refresh-token; _abck=old-abck; ak_bmsc=old-bmsc; "
                "bm_sv=old-bm"
            ),
        )

        details = await api.get_loyalty_details()

        assert details["loyaltyPoints"]["pointsBalance"] == 42
        assert [request["operation"] for request in session.requests] == [
            "getIbizaAccountDetails",
            "getSubscriptions",
            "RefreshCustomerToken",
            "getIbizaAccountDetails",
        ]
        assert_get_subscriptions_request(session.requests[1])
        assert "_abck=new-abck" in session.requests[3]["headers"]["Cookie"]
        assert "_abck=new-abck" in api.get_cookie_header()

    asyncio.run(run_test())


def test_refresh_initializes_missing_device_session_from_device_id() -> None:
    """The deviceId query is stored as deviceSessionId before token refresh."""

    async def run_test() -> None:
        session = FakeSession(
            [
                FakeResponse({"errors": [{"message": "Access token expired"}]}),
                FakeResponse({"data": {"deviceId": "device-1"}}),
                FakeResponse({"data": {"getSubscriptions": []}}),
                FakeResponse(
                    {"data": {"refreshCustomerAuthCookies": None}},
                    cookies={"grocery-roatc": "new-access-token"},
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
        api = DelhaizeApi(
            session,
            cookie_header="grocery-roatc=old-access-token; grocery-rortc=refresh-token",
        )

        details = await api.get_loyalty_details()

        assert details["loyaltyPoints"]["pointsBalance"] == 42
        assert [request["operation"] for request in session.requests] == [
            "getIbizaAccountDetails",
            "DeviceId",
            "getSubscriptions",
            "RefreshCustomerToken",
            "getIbizaAccountDetails",
        ]
        assert "deviceSessionId=device-1" in session.requests[2]["headers"]["Cookie"]
        assert "deviceSessionId=device-1" in session.requests[3]["headers"]["Cookie"]
        assert "deviceSessionId=device-1" in api.get_cookie_header()

    asyncio.run(run_test())


def test_refresh_requires_customer_auth_refresh_field() -> None:
    """An unrelated successful GraphQL response should not count as a token refresh."""

    async def run_test() -> None:
        session = FakeSession(
            [
                FakeResponse({"errors": [{"message": "Access token expired"}]}),
                FakeResponse({"data": {"getSubscriptions": []}}),
                FakeResponse({"data": {}}, cookies={"_abck": "new-abck"}),
            ]
        )
        api = DelhaizeApi(
            session,
            cookie_header=(
                "deviceSessionId=device-1; grocery-roatc=old-access-token; "
                "grocery-rortc=refresh-token"
            ),
        )

        try:
            await api.get_loyalty_details()
        except DelhaizeAuthError as err:
            assert (
                str(err)
                == "Delhaize refresh response did not include customer auth refresh data"
            )
        else:
            raise AssertionError("Expected refresh without refresh data to fail")

        assert [request["operation"] for request in session.requests] == [
            "getIbizaAccountDetails",
            "getSubscriptions",
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
                "method": "POST",
                "operation": json["operationName"],
                "payload": json,
                "headers": headers,
                "timeout": timeout,
            }
        )
        return FakeRequest(self._responses.pop(0))

    def get(
        self,
        url: str,
        *,
        params: dict[str, str],
        headers: dict[str, str],
        timeout: int,
    ) -> FakeRequest:
        """Return the next fake response for a GraphQL GET request."""
        self.requests.append(
            {
                "url": url,
                "method": "GET",
                "operation": params["operationName"],
                "params": params,
                "headers": headers,
                "timeout": timeout,
            }
        )
        return FakeRequest(self._responses.pop(0))


def assert_refresh_customer_token_request(request: dict[str, Any]) -> None:
    """Assert the refresh request matches Delhaize's browser call."""
    assert request["method"] == "POST"
    assert request["headers"]["x-do-refresh-token"] == "true"
    assert request["headers"]["X-APOLLO-OPERATION-ID"] == REFRESH_CUSTOMER_TOKEN_HASH
    assert request["headers"]["x-default-gql-refresh-token-disabled"] == "true"
    assert request["payload"] == {
        "operationName": "RefreshCustomerToken",
        "variables": {},
        "extensions": REFRESH_CUSTOMER_TOKEN_EXTENSIONS,
    }


def assert_get_subscriptions_request(request: dict[str, Any]) -> None:
    """Assert the bootstrap request matches Delhaize's persisted query GET."""
    assert request["method"] == "GET"
    assert request["headers"]["X-Apollo-Operation-Name"] == "getSubscriptions"
    assert request["headers"]["x-default-gql-refresh-token-disabled"] == "true"
    assert "Content-Type" not in request["headers"]
    assert request["params"] == {
        "operationName": "getSubscriptions",
        "variables": json_module.dumps(
            {"customerId": "current", "lang": "nl"},
            separators=(",", ":"),
        ),
        "extensions": json_module.dumps(
            GET_SUBSCRIPTIONS_EXTENSIONS,
            separators=(",", ":"),
        ),
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


class FakeHeaders:
    """Minimal response headers object with aiohttp-like getall support."""

    def __init__(self, raw_set_cookie_headers: list[str]) -> None:
        """Initialize the fake headers."""
        self._raw_set_cookie_headers = raw_set_cookie_headers

    def getall(self, key: str, default: list[str] | None = None) -> list[str]:
        """Return all header values for a key."""
        if key.lower() == "set-cookie":
            return list(self._raw_set_cookie_headers)
        return list(default or [])


class FakeResponse:
    """Minimal aiohttp-like response for GraphQL tests."""

    def __init__(
        self,
        payload: dict[str, Any],
        *,
        status: int = 200,
        cookies: dict[str, str] | None = None,
        raw_set_cookie_headers: list[str] | None = None,
    ) -> None:
        """Initialize the response."""
        self.status = status
        self._text = json_module.dumps(payload)
        self.cookies = SimpleCookie()
        for key, value in (cookies or {}).items():
            self.cookies[key] = value
        self.headers = FakeHeaders(raw_set_cookie_headers or [])

    async def text(self) -> str:
        """Return the fake response body."""
        return self._text
