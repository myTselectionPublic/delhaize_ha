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
REFRESH_CUSTOMER_TOKEN_OPERATION_ID = (
    delhaize_api.REFRESH_CUSTOMER_TOKEN_OPERATION_ID
)
REFRESH_CUSTOMER_TOKEN_EXTENSIONS = delhaize_api.REFRESH_CUSTOMER_TOKEN_EXTENSIONS
APOLLO_CLIENT_NAME = delhaize_api.APOLLO_CLIENT_NAME
APOLLO_CLIENT_VERSION = delhaize_api.APOLLO_CLIENT_VERSION
COUPON_BOOK_OFFERS_QUERY = delhaize_api.COUPON_BOOK_OFFERS_QUERY
PERSONAL_OFFERS_QUERY = delhaize_api.PERSONAL_OFFERS_QUERY
PERSONAL_OFFER_PRODUCTS_QUERY = delhaize_api.PERSONAL_OFFER_PRODUCTS_QUERY


def test_personal_offers_query_requests_products_and_original_prices() -> None:
    """Personal offers should fetch eligible products with price information."""
    assert "products {" in PERSONAL_OFFERS_QUERY
    assert "formattedValue" in PERSONAL_OFFERS_QUERY
    assert "wasPrice" in PERSONAL_OFFERS_QUERY


def test_personal_offer_product_query_matches_detail_page_request() -> None:
    """The price fallback should use the product listing opened by the website."""
    assert "productList(" in PERSONAL_OFFER_PRODUCTS_QUERY
    assert "productListingType" in PERSONAL_OFFER_PRODUCTS_QUERY
    assert "offerId" in PERSONAL_OFFER_PRODUCTS_QUERY
    assert "pagination" in PERSONAL_OFFER_PRODUCTS_QUERY
    assert "wasPrice" in PERSONAL_OFFER_PRODUCTS_QUERY


def test_personal_offers_fetches_missing_product_prices() -> None:
    """A product shell from PersonalOffersV2 should be enriched from ProductList."""

    async def run_test() -> None:
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "data": {
                            "personalOffersV2": {
                                "personalOfferList": [
                                    {
                                        "id": "offer-1",
                                        "name": "75 bonus points",
                                        "points": 75,
                                        "productRangeSize": 1,
                                        "products": [
                                            {"code": "123", "name": "Product shell"}
                                        ],
                                    }
                                ]
                            }
                        }
                    }
                ),
                FakeResponse(
                    {
                        "data": {
                            "productList": {
                                "products": [
                                    {
                                        "code": "123",
                                        "name": "Priced product",
                                        "price": {
                                            "formattedValue": "€ 3,00",
                                            "value": 3,
                                        },
                                    }
                                ],
                                "pagination": {"currentPage": 0, "totalPages": 1},
                            }
                        }
                    }
                ),
            ]
        )
        api = DelhaizeApi(session, cookie_header="grocery-roatc=access-token")

        offers = await api.get_personal_offers()

        product = offers["personalOfferList"][0]["products"][0]
        assert product["name"] == "Priced product"
        assert product["price"]["value"] == 3
        assert [request["operation"] for request in session.requests] == [
            "PersonalOffersV2",
            "ProductList",
        ]
        assert session.requests[1]["payload"]["variables"] == {
            "productListingType": "PERSONAL_OFFER",
            "lang": "nl",
            "offerId": "offer-1",
            "lazyLoadCount": 20,
            "pageNumber": 0,
        }

    asyncio.run(run_test())


def test_personal_offers_keeps_complete_inline_products_without_extra_request() -> None:
    """Already priced complete products should not add a ProductList request."""

    async def run_test() -> None:
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "data": {
                            "personalOffersV2": {
                                "personalOfferList": [
                                    {
                                        "id": "offer-1",
                                        "points": 75,
                                        "productRangeSize": 1,
                                        "products": [
                                            {
                                                "code": "123",
                                                "price": {"value": 3},
                                            }
                                        ],
                                    }
                                ]
                            }
                        }
                    }
                )
            ]
        )
        api = DelhaizeApi(session, cookie_header="grocery-roatc=access-token")

        await api.get_personal_offers()

        assert [request["operation"] for request in session.requests] == [
            "PersonalOffersV2"
        ]

    asyncio.run(run_test())


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


def test_credentials_and_mfa_use_captured_persisted_operations() -> None:
    """Credential authentication should match the browser's persisted requests."""

    async def run_test() -> None:
        session = FakeSession(
            [
                FakeResponse({"data": {"login": {}}}),
                FakeResponse({"data": {"sendMfaOtpCode": {}}}),
                FakeResponse({"data": {"login": {}}}),
            ]
        )
        api = DelhaizeApi(session, cookie_header="deviceSessionId=device-1")

        await api.login("person@example.com", "secret", lang="nl")
        await api.send_login_mfa_otp_code("mfa-token", lang="nl")
        await api.login_with_mfa("123456", "mfa-token", lang="nl")

        assert [request["operation"] for request in session.requests] == [
            "Login",
            "SendLoginMfaOtpCode",
            "LoginWithMFA",
        ]
        expected = [
            ("Login", delhaize_api.LOGIN_EXTENSIONS),
            ("SendLoginMfaOtpCode", delhaize_api.SEND_LOGIN_MFA_OTP_EXTENSIONS),
            ("LoginWithMFA", delhaize_api.LOGIN_WITH_MFA_EXTENSIONS),
        ]
        for request, (operation, extensions) in zip(session.requests, expected):
            assert "query" not in request["payload"]
            assert request["payload"]["extensions"] == extensions
            assert request["headers"]["X-APOLLO-OPERATION-ID"] == (
                delhaize_api.OPERATION_IDS[operation]
            )

    asyncio.run(run_test())


def test_coupon_book_query_avoids_removed_redemption_fields() -> None:
    """CouponBookPersonalOffer no longer exposes redemption date fields."""
    assert "redemptionStartDate" not in COUPON_BOOK_OFFERS_QUERY
    assert "redemptionEndDate" not in COUPON_BOOK_OFFERS_QUERY


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
            "RefreshCustomerToken",
            "CurrentCustomer",
        ]
        assert_refresh_customer_token_request(session.requests[1])
        assert (
            "grocery-roatc=old-access-token"
            in session.requests[1]["headers"]["Cookie"]
        )
        assert "grocery-roatc=new-access-token" in api.get_cookie_header()

    asyncio.run(run_test())


def test_graphql_refreshes_access_token_expired_and_retries_operation() -> None:
    """Delhaize sometimes reports token expiry as a plain error message."""

    async def run_test() -> None:
        session = FakeSession(
            [
                FakeResponse({"errors": [{"message": "Access token expired"}]}),
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
            "RefreshCustomerToken",
            "getIbizaAccountDetails",
        ]
        assert_refresh_customer_token_request(session.requests[1])
        assert "grocery-roatc=new-access-token" in session.requests[2]["headers"]["Cookie"]
        assert "grocery-roatc=new-access-token" in api.get_cookie_header()

    asyncio.run(run_test())


def test_refresh_stores_auth_cookie_from_raw_set_cookie_header() -> None:
    """Some Set-Cookie headers may not be exposed through response.cookies."""

    async def run_test() -> None:
        session = FakeSession(
            [
                FakeResponse({"errors": [{"message": "Access token expired"}]}),
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
            "RefreshCustomerToken",
            "getIbizaAccountDetails",
        ]
        assert "grocery-roatc=new-access-token" in session.requests[2]["headers"]["Cookie"]
        assert "grocery-roatc=new-access-token" in api.get_cookie_header()

    asyncio.run(run_test())


def test_refresh_keeps_auth_cookie_when_later_set_cookie_deletes_domain_cookie() -> None:
    """Browser refresh responses may set a token and then delete wider-domain cookies."""

    async def run_test() -> None:
        session = FakeSession(
            [
                FakeResponse({"errors": [{"message": "Access token expired"}]}),
                FakeResponse(
                    {"data": {"refreshCustomerAuthCookies": None}},
                    raw_set_cookie_headers=[
                        (
                            "grocery-roatc=new-access-token; Max-Age=43199; "
                            "Path=/; HttpOnly; Secure; SameSite=None"
                        ),
                        (
                            "grocery-roatc=; Max-Age=0; Domain=.delhaize.be; "
                            "Path=/; HttpOnly; Secure; SameSite=None"
                        ),
                        (
                            "grocery-rortc=; Max-Age=0; Domain=.delhaize.be; "
                            "Path=/; HttpOnly; Secure; SameSite=None"
                        ),
                        (
                            "grocery-wasc=; Max-Age=0; Domain=.delhaize.be; "
                            "Path=/; HttpOnly; Secure; SameSite=None"
                        ),
                        (
                            "grocery-roatc=; Max-Age=0; Domain=.api.delhaize.be; "
                            "Path=/; HttpOnly; Secure; SameSite=None"
                        ),
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
                "grocery-rortc=refresh-token; grocery-wasc=old-wasc"
            ),
        )

        details = await api.get_loyalty_details()

        assert details["loyaltyPoints"]["pointsBalance"] == 42
        assert "grocery-roatc=new-access-token" in api.get_cookie_header()
        assert "grocery-roatc=new-access-token" in session.requests[2]["headers"]["Cookie"]

    asyncio.run(run_test())


def test_refresh_retries_after_anti_bot_cookie_update_then_auth_cookie() -> None:
    """Akamai cookie updates may need to settle before Delhaize rotates auth."""

    async def run_test() -> None:
        session = FakeSession(
            [
                FakeResponse({"errors": [{"message": "Access token expired"}]}),
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
            "RefreshCustomerToken",
            "RefreshCustomerToken",
            "getIbizaAccountDetails",
        ]
        assert_refresh_customer_token_request(session.requests[1])
        assert_refresh_customer_token_request(session.requests[2])
        assert "_abck=new-abck" in session.requests[2]["headers"]["Cookie"]
        assert "grocery-roatc=new-access-token" in session.requests[3]["headers"]["Cookie"]
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


def test_validate_session_refreshes_when_access_cookie_is_missing() -> None:
    """A remembered customer session without its access cookie can be refreshed."""

    async def run_test() -> None:
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "errors": [
                            {
                                "message": (
                                    "Customer was authenticated previously, but "
                                    "customer token not sent"
                                ),
                                "path": ["currentCustomer"],
                                "extensions": {"code": "UNAUTHENTICATED"},
                            }
                        ]
                    }
                ),
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
                "deviceSessionId=device-1; grocery-rortc=refresh-token; "
                "grocery-wasc=remembered-session"
            ),
        )

        customer = await api.validate_session()

        assert customer["uid"] == "customer-1"
        assert [request["operation"] for request in session.requests] == [
            "CurrentCustomer",
            "RefreshCustomerToken",
            "CurrentCustomer",
        ]
        assert_refresh_customer_token_request(session.requests[1])
        assert "grocery-roatc=new-access-token" in session.requests[2]["headers"]["Cookie"]

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
            "RefreshCustomerToken",
            "CurrentCustomer",
        ]
        assert_refresh_customer_token_request(session.requests[1])
        assert "grocery-roatc=new-access-token" in session.requests[2]["headers"]["Cookie"]
        assert "grocery-roatc=new-access-token" in api.get_cookie_header()

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
            "RefreshCustomerToken",
        ]

    asyncio.run(run_test())


def test_refresh_rejects_when_only_anti_bot_cookies_change() -> None:
    """Anti-bot cookie changes alone do not refresh the customer token."""

    async def run_test() -> None:
        session = FakeSession(
            [
                FakeResponse({"errors": [{"message": "Access token expired"}]}),
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
            "RefreshCustomerToken",
            "RefreshCustomerToken",
        ]
        assert_refresh_customer_token_request(session.requests[1])
        assert_refresh_customer_token_request(session.requests[2])
        assert "_abck=new-abck" in session.requests[2]["headers"]["Cookie"]
        assert "_abck=new-abck" in api.get_cookie_header()

    asyncio.run(run_test())


def test_refresh_does_not_add_browser_bootstrap_requests() -> None:
    """The captured browser flow refreshes directly without bootstrap requests."""

    async def run_test() -> None:
        session = FakeSession(
            [
                FakeResponse({"errors": [{"message": "Access token expired"}]}),
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
            "RefreshCustomerToken",
            "getIbizaAccountDetails",
        ]
        assert "deviceSessionId=" not in api.get_cookie_header()

    asyncio.run(run_test())


def test_refresh_requires_customer_auth_refresh_field() -> None:
    """An unrelated successful GraphQL response should not count as a token refresh."""

    async def run_test() -> None:
        session = FakeSession(
            [
                FakeResponse({"errors": [{"message": "Access token expired"}]}),
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

    def __init__(
        self,
        responses: list[FakeResponse],
        *,
        home_response: FakeResponse | None = None,
        script_response: FakeResponse | None = None,
    ) -> None:
        """Initialize the fake response queue."""
        self._responses = responses
        self._home_response = home_response
        self._script_response = script_response
        self.requests: list[dict[str, Any]] = []

    def post(
        self,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        data: str | None = None,
        headers: dict[str, str],
        timeout: int,
    ) -> FakeRequest:
        """Return the next fake response."""
        operation = json["operationName"] if json is not None else "AkamaiPixel"
        self.requests.append(
            {
                "url": url,
                "method": "POST",
                "operation": operation,
                "operation": operation,
                "payload": json,
                "data": data,
                "headers": headers,
                "timeout": timeout,
            }
        )
        if operation in {"BlueConicContext", "DigitalContentHome"}:
            return FakeRequest(FakeResponse([]))
        return FakeRequest(self._responses.pop(0))

    def get(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str],
        timeout: int,
    ) -> FakeRequest:
        """Return the next fake response for a GraphQL GET request."""
        if params:
            operation = params["operationName"]
        elif url == "https://bc.delhaize.be/script.js":
            operation = "BlueConicScript"
        else:
            operation = "DelhaizeHome"
        self.requests.append(
            {
                "url": url,
                "method": "GET",
                "operation": operation,
                "params": params or {},
                "headers": headers,
                "timeout": timeout,
            }
        )
        if operation == "DelhaizeHome":
            return FakeRequest(self._home_response or FakeResponse({}))
        if operation == "BlueConicScript":
            return FakeRequest(self._script_response or FakeResponse(""))
        return FakeRequest(self._responses.pop(0))


def assert_refresh_customer_token_request(request: dict[str, Any]) -> None:
    """Assert the refresh request matches Delhaize's browser call."""
    assert request["method"] == "POST"
    assert request["headers"]["x-do-refresh-token"] == "true"
    assert request["headers"]["X-APOLLO-OPERATION-NAME"] == "RefreshCustomerToken"
    assert (
        request["headers"]["X-APOLLO-OPERATION-ID"]
        == REFRESH_CUSTOMER_TOKEN_OPERATION_ID
    )
    assert request["headers"]["Apollographql-Client-Name"] == APOLLO_CLIENT_NAME
    assert request["headers"]["Apollographql-Client-Version"] == APOLLO_CLIENT_VERSION
    assert request["headers"]["x-default-gql-refresh-token-disabled"] == "true"
    assert "grocery-rortc=refresh-token" in request["headers"]["Cookie"]
    assert request["headers"]["Referer"] == "https://www.delhaize.be/"
    assert request["headers"]["Sec-CH-UA-Platform"] == '"Windows"'
    assert request["payload"] == {
        "operationName": "RefreshCustomerToken",
        "variables": {},
        "extensions": REFRESH_CUSTOMER_TOKEN_EXTENSIONS,
    }


def assert_akamai_pixel_request(request: dict[str, Any]) -> None:
    """Assert the pre-refresh Akamai pixel matches the browser sequence."""
    assert request["method"] == "POST"
    assert request["operation"] == "AkamaiPixel"
    assert request["url"] == "https://www.delhaize.be/akam/13/pixel_36d02966"
    assert request["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert (
        request["headers"]["Referer"]
        == "https://www.delhaize.be/nl/my-account/dashboard"
    )
    assert request["headers"]["Sec-Fetch-Site"] == "same-origin"
    assert request["payload"] is None
    assert request["data"]
    assert "ap=true" in request["data"]
    assert "jsv=1.5" in request["data"]


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

    def __init__(
        self,
        raw_set_cookie_headers: list[str],
        headers: dict[str, str] | None = None,
    ) -> None:
        """Initialize the fake headers."""
        self._raw_set_cookie_headers = raw_set_cookie_headers
        self._headers = {key.lower(): value for key, value in (headers or {}).items()}

    def get(self, key: str, default: str | None = None) -> str | None:
        """Return a single header value for a key."""
        return self._headers.get(key.lower(), default)

    def getall(self, key: str, default: list[str] | None = None) -> list[str]:
        """Return all header values for a key."""
        if key.lower() == "set-cookie":
            return list(self._raw_set_cookie_headers)
        return list(default or [])


class FakeResponse:
    """Minimal aiohttp-like response for GraphQL tests."""

    def __init__(
        self,
        payload: Any,
        *,
        status: int = 200,
        cookies: dict[str, str] | None = None,
        raw_set_cookie_headers: list[str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Initialize the response."""
        self.status = status
        self._text = payload if isinstance(payload, str) else json_module.dumps(payload)
        self.cookies = SimpleCookie()
        for key, value in (cookies or {}).items():
            self.cookies[key] = value
        self.headers = FakeHeaders(raw_set_cookie_headers or [], headers)

    async def text(self) -> str:
        """Return the fake response body."""
        return self._text
