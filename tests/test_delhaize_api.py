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


def test_cookie_header_preserves_browser_order_and_updates_values() -> None:
    """Refreshing response cookies should not alphabetize the browser Cookie header."""
    api = DelhaizeApi(
        FakeSession([]),
        cookie_header=(
            "z_cookie=last; grocery-roatc=old-access-token; "
            "a_cookie=first; grocery-rortc=refresh-token"
        ),
    )

    assert api.get_cookie_header() == (
        "z_cookie=last; grocery-roatc=old-access-token; "
        "a_cookie=first; grocery-rortc=refresh-token"
    )

    api._store_response_cookies(  # noqa: SLF001
        {"grocery-roatc": "new-access-token", "bm_sv": "new-bm"}
    )

    assert api.get_cookie_header() == (
        "z_cookie=last; grocery-roatc=new-access-token; "
        "a_cookie=first; grocery-rortc=refresh-token; bm_sv=new-bm"
    )


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
                FakeResponse({"data": {"deviceId": "device-1"}}),
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
            "DeviceId",
            "getSubscriptions",
            "RefreshCustomerToken",
            "CurrentCustomer",
        ]
        assert_get_subscriptions_request(session.requests[2])
        assert_refresh_customer_token_request(session.requests[3])
        assert "grocery-roatc=new-access-token" in api.get_cookie_header()

    asyncio.run(run_test())


def test_graphql_refreshes_access_token_expired_and_retries_operation() -> None:
    """Delhaize sometimes reports token expiry as a plain error message."""

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
            cookie_header=(
                "deviceSessionId=device-1; grocery-roatc=old-access-token; "
                "grocery-rortc=refresh-token"
            ),
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
        assert_get_subscriptions_request(session.requests[2])
        assert_refresh_customer_token_request(session.requests[3])
        assert "grocery-roatc=new-access-token" in session.requests[4]["headers"]["Cookie"]
        assert "grocery-roatc=new-access-token" in api.get_cookie_header()

    asyncio.run(run_test())


def test_refresh_stores_auth_cookie_from_raw_set_cookie_header() -> None:
    """Some Set-Cookie headers may not be exposed through response.cookies."""

    async def run_test() -> None:
        session = FakeSession(
            [
                FakeResponse({"errors": [{"message": "Access token expired"}]}),
                FakeResponse({"data": {"deviceId": "device-1"}}),
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
            "DeviceId",
            "getSubscriptions",
            "RefreshCustomerToken",
            "getIbizaAccountDetails",
        ]
        assert "grocery-roatc=new-access-token" in session.requests[4]["headers"]["Cookie"]
        assert "grocery-roatc=new-access-token" in api.get_cookie_header()

    asyncio.run(run_test())


def test_refresh_retries_after_anti_bot_cookie_update_then_auth_cookie() -> None:
    """Akamai cookie updates may need to settle before Delhaize rotates auth."""

    async def run_test() -> None:
        session = FakeSession(
            [
                FakeResponse({"errors": [{"message": "Access token expired"}]}),
                FakeResponse({"data": {"deviceId": "device-1"}}),
                FakeResponse({"data": {"getSubscriptions": []}}),
                FakeResponse(
                    {"data": {"refreshCustomerAuthCookies": None}},
                    cookies={"_abck": "new-abck", "ak_bmsc": "new-bmsc"},
                ),
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
                "grocery-rortc=refresh-token; _abck=old-abck; ak_bmsc=old-bmsc"
            ),
        )

        details = await api.get_loyalty_details()

        assert details["loyaltyPoints"]["pointsBalance"] == 42
        assert [request["operation"] for request in session.requests] == [
            "getIbizaAccountDetails",
            "DeviceId",
            "getSubscriptions",
            "RefreshCustomerToken",
            "RefreshCustomerToken",
            "getIbizaAccountDetails",
        ]
        assert_refresh_customer_token_request(session.requests[3])
        assert_refresh_customer_token_request(session.requests[4])
        assert "_abck=new-abck" in session.requests[4]["headers"]["Cookie"]
        assert "grocery-roatc=new-access-token" in session.requests[5]["headers"]["Cookie"]
        assert "grocery-roatc=new-access-token" in api.get_cookie_header()

    asyncio.run(run_test())


def test_graphql_treats_invalid_access_token_without_refresh_cookie_as_auth_lost() -> None:
    """Invalid access tokens without a refresh cookie should reauthenticate."""

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


def test_validate_session_refreshes_unauthenticated_invalid_access_token() -> None:
    """Delhaize may report an expired access cookie as UNAUTHENTICATED."""

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
                FakeResponse({"data": {"deviceId": "device-1"}}),
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
            "DeviceId",
            "getSubscriptions",
            "RefreshCustomerToken",
            "CurrentCustomer",
        ]
        assert_get_subscriptions_request(session.requests[2])
        assert_refresh_customer_token_request(session.requests[3])
        assert "grocery-roatc=new-access-token" in session.requests[4]["headers"]["Cookie"]
        assert "grocery-roatc=new-access-token" in api.get_cookie_header()

    asyncio.run(run_test())


def test_refresh_operation_does_not_loop_on_expired_token() -> None:
    """A failed refresh should surface instead of retrying refresh recursively."""

    async def run_test() -> None:
        session = FakeSession(
            [
                FakeResponse({"errors": [{"message": "Access token expired"}]}),
                FakeResponse({"data": {"deviceId": "device-1"}}),
                FakeResponse({"errors": [{"message": "Access token expired"}]}),
                FakeResponse({"errors": [{"message": "Access token expired"}]}),
            ]
        )
        api = DelhaizeApi(
            session,
            cookie_header="deviceSessionId=device-1; grocery-rortc=refresh-token",
        )

        try:
            await api.get_loyalty_details()
        except DelhaizeTokenRefreshRequired:
            pass
        else:
            raise AssertionError("Expected token refresh failure to be raised")

        assert [request["operation"] for request in session.requests] == [
            "getIbizaAccountDetails",
            "DeviceId",
            "getSubscriptions",
            "RefreshCustomerToken",
        ]

    asyncio.run(run_test())


def test_refresh_rejects_when_only_anti_bot_cookies_change() -> None:
    """Anti-bot cookie changes alone do not refresh the customer token."""

    async def run_test() -> None:
        session = FakeSession(
            [
                FakeResponse({"errors": [{"message": "Access token expired"}]}),
                FakeResponse({"data": {"deviceId": "device-1"}}),
                FakeResponse({"data": {"getSubscriptions": []}}),
                FakeResponse(
                    {"data": {"refreshCustomerAuthCookies": None}},
                    cookies={"_abck": "new-abck", "ak_bmsc": "new-bmsc", "bm_sv": "new-bm"},
                ),
                FakeResponse({"data": {"refreshCustomerAuthCookies": None}}),
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

        try:
            await api.get_loyalty_details()
        except DelhaizeAuthError as err:
            assert str(err) == "Delhaize refresh did not update customer auth cookies"
        else:
            raise AssertionError("Expected refresh without auth cookie changes to fail")

        assert [request["operation"] for request in session.requests] == [
            "getIbizaAccountDetails",
            "DeviceId",
            "getSubscriptions",
            "RefreshCustomerToken",
            "RefreshCustomerToken",
        ]
        assert_get_subscriptions_request(session.requests[2])
        assert_refresh_customer_token_request(session.requests[3])
        assert_refresh_customer_token_request(session.requests[4])
        assert "_abck=new-abck" in session.requests[4]["headers"]["Cookie"]
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
                FakeResponse({"data": {"deviceId": "device-1"}}),
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
            "DeviceId",
            "getSubscriptions",
            "RefreshCustomerToken",
        ]

    asyncio.run(run_test())


def test_activate_all_personal_offers_also_activates_flash_e_deals() -> None:
    """Flash e-deals live in the coupon book API, not personalOffersV2."""

    async def run_test() -> None:
        session = FakeSession(
            [
                FakeResponse({"data": {"activateAllPersonalOffers": True}}),
                FakeResponse(
                    {
                        "data": {
                            "couponBookOffers": {
                                "flashOffers": [
                                    {
                                        "id": "flash-1",
                                        "name": "Flash deal",
                                        "active": False,
                                    },
                                    {
                                        "id": "flash-2",
                                        "name": "Already active",
                                        "active": True,
                                    },
                                ]
                            }
                        }
                    }
                ),
                FakeResponse({"data": {"activateCouponBookOffer": True}}),
            ]
        )
        api = DelhaizeApi(session, cookie_header="grocery-roatc=access-token")

        result = await api.activate_all_personal_offers()

        assert result == {
            "personal_offers": True,
            "coupon_book_flash_offers": [
                {"id": "flash-1", "name": "Flash deal", "result": True}
            ],
        }
        assert [request["operation"] for request in session.requests] == [
            "ActivateAllPersonalOffers",
            "CouponBookOffers",
            "ActivateCouponBookOffer",
        ]
        assert session.requests[1]["payload"]["variables"] == {"lang": "nl"}
        assert session.requests[2]["payload"]["variables"] == {"offerId": "flash-1"}

    asyncio.run(run_test())


def test_inactive_offer_detection_includes_flash_e_deals() -> None:
    """Auto-activation should run when only a flash e-deal is inactive."""
    summary = {
        "personal_offers_count": {"totalCount": 2, "activatedCount": 2},
        "personal_offers": {"personalOfferList": []},
        "coupon_book_offers": {
            "flashOffers": [
                {"id": "flash-1", "active": False},
                {"id": "flash-2", "active": True},
            ]
        },
    }

    assert DelhaizeApi._has_inactive_offers(summary)  # noqa: SLF001


def test_burnable_offer_ranges_fetches_only_range_offers() -> None:
    """BurnableOfferRange is used to expand range offers into products."""

    async def run_test() -> None:
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "data": {
                            "customerBurnOffersList": {
                                "burnableOfferList": [
                                    {"id": "range-1", "range": True, "name": "Range"},
                                    {"id": "single-1", "range": False, "name": "Single"},
                                ]
                            }
                        }
                    }
                ),
                FakeResponse(
                    {
                        "data": {
                            "customerBurnOfferRangeDetailed": {
                                "id": "range-1",
                                "name": "Range",
                                "range": True,
                                "priceToBurn": 150,
                                "products": [{"code": "123", "name": "Product"}],
                            }
                        }
                    }
                ),
            ]
        )
        api = DelhaizeApi(session, cookie_header="grocery-roatc=access-token")

        offers = await api.get_burnable_offers_list()
        ranges = await api.get_burnable_offer_ranges(offers["burnableOfferList"])

        assert ranges == [
            {
                "id": "range-1",
                "name": "Range",
                "range": True,
                "priceToBurn": 150,
                "products": [{"code": "123", "name": "Product"}],
            }
        ]
        assert [request["operation"] for request in session.requests] == [
            "BurnOffersList",
            "BurnableOfferRange",
        ]
        assert session.requests[0]["payload"]["variables"] == {"lang": "nl"}
        assert session.requests[1]["payload"]["variables"] == {
            "offerId": "range-1",
            "lang": "nl",
        }

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
    assert request["headers"]["X-APOLLO-OPERATION-NAME"] == "RefreshCustomerToken"
    assert request["headers"]["X-APOLLO-OPERATION-ID"] == REFRESH_CUSTOMER_TOKEN_HASH
    assert request["headers"]["x-default-gql-refresh-token-disabled"] == "true"
    assert request["headers"]["Referer"] == "https://www.delhaize.be/nl/my-account/dashboard"
    assert request["payload"] == {
        "operationName": "RefreshCustomerToken",
        "variables": {},
        "extensions": REFRESH_CUSTOMER_TOKEN_EXTENSIONS,
    }


def assert_get_subscriptions_request(request: dict[str, Any]) -> None:
    """Assert the bootstrap request matches Delhaize's persisted query GET."""
    assert request["method"] == "GET"
    assert request["headers"]["X-APOLLO-OPERATION-NAME"] == "getSubscriptions"
    assert request["headers"]["x-default-gql-refresh-token-disabled"] == "true"
    assert request["headers"]["Referer"] == "https://www.delhaize.be/nl/my-account/dashboard"
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
