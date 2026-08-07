"""Delhaize website GraphQL client."""

from __future__ import annotations

from asyncio import CancelledError, TimeoutError
from hashlib import sha1
from http.cookies import CookieError, SimpleCookie
import json
import logging
from time import time
from typing import Any
from urllib.parse import urlencode

from aiohttp import ClientResponse, ClientSession
from aiohttp.client_exceptions import ClientError

from ..const import API_URL, BASE_URL, DEFAULT_LANGUAGE

_LOGGER = logging.getLogger(__name__)

TOKEN_REFRESH_ERROR_CODE = "PENDING_TOKEN_REFRESH"
DEVICE_SESSION_COOKIE_NAME = "deviceSessionId"
APOLLO_CLIENT_NAME = "be-dll-web-stores"
APOLLO_CLIENT_VERSION = "75fd109159629c98910d73fd4b29cc162765c558"
CUSTOMER_AUTH_COOKIE_NAMES = {
    "grocery-roatc",
    "grocery-rortc",
    "grocery-wasc",
    "v_cust",
}
CUSTOMER_REFRESH_COOKIE_NAMES = {
    "grocery-rortc",
    "grocery-wasc",
}

LOGIN_MUTATION = """
mutation Login(
  $username: String!
  $password: String!
  $termsAndConditionsAccepted: Boolean
  $termsAndConditionsValidation: Boolean
  $remember: Boolean
  $prospect_token: String
  $lang: String
  $captcha: CaptchaInput
  $mobile: Boolean
  $country: String
) {
  login(
    username: $username
    password: $password
    termsAndConditionsAccepted: $termsAndConditionsAccepted
    termsAndConditionsValidation: $termsAndConditionsValidation
    remember: $remember
    prospect_token: $prospect_token
    lang: $lang
    captcha: $captcha
    mobile: $mobile
    country: $country
  ) {
    linkedCardFromProspect
    isLeakedCredentialsDetected
  }
  mergeCartAfterLogin: mergeCartAfterLoginV3 {
    reasonCode
  }
}
"""

LOGIN_WITH_MFA_MUTATION = """
mutation LoginWithMFA(
  $otpCode: String!
  $mfaToken: String!
  $termsAndConditionsAccepted: Boolean
  $termsAndConditionsValidation: Boolean
  $remember: Boolean
  $lang: String
  $captcha: CaptchaInput
  $mobile: Boolean
  $country: String
) {
  login: loginWithMFA(
    otpCode: $otpCode
    mfaToken: $mfaToken
    termsAndConditionsAccepted: $termsAndConditionsAccepted
    termsAndConditionsValidation: $termsAndConditionsValidation
    remember: $remember
    lang: $lang
    captcha: $captcha
    mobile: $mobile
    country: $country
  ) {
    linkedCardFromProspect
    isLeakedCredentialsDetected
  }
  mergeCartAfterLogin: mergeCartAfterLoginV3 {
    reasonCode
  }
}
"""

SEND_LOGIN_MFA_OTP_MUTATION = """
mutation SendLoginMfaOtpCode(
  $mfaPurpose: String!
  $mfaToken: String!
  $captcha: CaptchaInput
  $lang: String
) {
  sendMfaOtpCode(
    mfaPurpose: $mfaPurpose
    mfaToken: $mfaToken
    captcha: $captcha
    lang: $lang
  ) {
    mfaMethod
    nextPossibleSendTime
    otpTarget
  }
}
"""

DEVICE_ID_QUERY = """
query DeviceId {
  deviceId
}
"""

REFRESH_CUSTOMER_TOKEN_HASH = (
    "ec4ea2caaa6c8fc1a7b139406f910e8b9acb44301ae753fef7b02631043b552c"
)
REFRESH_CUSTOMER_TOKEN_OPERATION_ID = (
    "3d308f9cf01b362a8f2a91aca53c4170c0f505499ad1b980d779ada03bd4f57f"
)

REFRESH_CUSTOMER_TOKEN_EXTENSIONS = {
    "persistedQuery": {
        "version": 1,
        "sha256Hash": REFRESH_CUSTOMER_TOKEN_HASH,
    }
}

OPERATION_IDS = {
    "RefreshCustomerToken": REFRESH_CUSTOMER_TOKEN_OPERATION_ID,
    "Login": "dc197d47830c46cd433ba4636c471a374861bd770e4c88378947ceb2b200ba56",
    "SendLoginMfaOtpCode": (
        "3039942480cae6c3f1e536a0ac4614771d072c88ce4d8a0fe113418f4ce9e5b6"
    ),
    "LoginWithMFA": (
        "ca68ca5448f81d8e7c203ec7c9d2544018e9ee02d2c16211120fecc3c52000ed"
    ),
}
CUSTOMER_ACCESS_COOKIE_NAMES = {
    "grocery-roatc",
    "v_cust",
}

LOGIN_EXTENSIONS = {
    "persistedQuery": {
        "version": 1,
        "sha256Hash": (
            "64a9ef1f232228086f6c34c0b0a7aff5dc8d1f11d89f690733dc7561b67a5364"
        ),
    }
}
SEND_LOGIN_MFA_OTP_EXTENSIONS = {
    "persistedQuery": {
        "version": 1,
        "sha256Hash": (
            "358dbe646f693bbc90029b49a112877a5caf9d5c08af4b2bbd6744b6d4ce1e33"
        ),
    }
}
LOGIN_WITH_MFA_EXTENSIONS = {
    "persistedQuery": {
        "version": 1,
        "sha256Hash": (
            "05e41323c4567346fdaf0701b218380437033bcd8f58ca38dc637146c4d626b6"
        ),
    }
}

CURRENT_CUSTOMER_QUERY = """
query CurrentCustomer($mode: String!) {
  currentCustomer(mode: $mode) {
    uid
    customerIdHash
    firstName
    lastName
    customerType
    diplaCard
    ibizaLoyaltyProfile
  }
}
"""

IBIZA_ACCOUNT_DETAILS_QUERY = """
query getIbizaAccountDetails($lang: String) {
  loyaltyPoints(lang: $lang) {
    pointsBalance
  }
  nutriscoreBalance(lang: $lang) {
    availableToSaveThisMonth
    discount
    currentNutriBoostType
  }
  savings: savingsByPeriodV2(lang: $lang) {
    periodSavings {
      totalSavingsAmountFormatted
    }
  }
}
"""

PERSONAL_OFFERS_COUNT_QUERY = """
query PersonalOffersCount($lang: String!) {
  personalOffersCount(lang: $lang) {
    totalCount
    activatedCount
  }
}
"""

PERSONAL_OFFERS_QUERY = """
query PersonalOffersV2($lang: String!) {
  personalOffersV2(lang: $lang) {
    totalEuroBenefit {
      formattedValue
      value
      currencyIso
      currencySymbol
    }
    totalPoints
    personalOfferList {
      id
      name
      active
      points
      validity
      promotion
      promotionId
      promotionType
      offerRedeemed
      basketPromo
    }
  }
}
"""

COUPON_BOOK_OFFERS_QUERY = """
query CouponBookOffers($activationStatus: String, $lang: String, $mode: String) {
  couponBookOffers(activationStatus: $activationStatus, lang: $lang, mode: $mode) {
    activatedOffersCount
    totalOffersCount
    totalPoints
    flashOffers {
      id
      name
      active
      points
      promotion
      promotionId
      promotionType
      basketPromo
      moreDetails
      activationStartDate
      activationEndDate
    }
    personalOffers {
      id
      name
      active
      points
      promotion
      promotionId
      promotionType
      basketPromo
      moreDetails
      activationStartDate
      activationEndDate
    }
  }
}
"""

BURNABLE_OFFERS_LIST_QUERY = """
query BurnOffersList($lang: String!) {
  customerBurnOffersList(lang: $lang) {
    burnableOfferList {
      ...BurnableOfferDetails
    }
  }
}

fragment BurnableOfferDetails on BurnableOffer {
  id
  name
  picture
  offerRedeemed
  active
  range
  priceToBurn
  deactivationAllowed
  enoughPointsToBurn
  productRangeSize
  missingPointsToBurn
  daysRemaining
  maxUses
  productAvailable
  activationAllowed
  availableRedemptions
  registeredRedemptions
  onlineOffer
  products {
    ...BurnableProductDetails
  }
}

fragment BurnableProductDetails on Product {
  code
  name
  manufacturerName
  manufacturerSubBrandName
  available
  url
  price {
    currencyIso
    currencySymbol
    formattedValue
    discountedPriceFormatted
    discountedUnitPriceFormatted
    unitPriceFormatted
    value
    wasPrice
  }
  stock {
    inStock
    inStockBeforeMaxAdvanceOrderingDate
    availableFromDate
  }
}
"""

BURNABLE_OFFER_RANGE_QUERY = """
query BurnableOfferRange($offerId: String!, $lang: String!) {
  customerBurnOfferRangeDetailed(offerId: $offerId, lang: $lang) {
    ...BurnableOfferDetails
  }
}

fragment BurnableOfferDetails on BurnableOffer {
  id
  name
  picture
  offerRedeemed
  active
  range
  priceToBurn
  deactivationAllowed
  enoughPointsToBurn
  productRangeSize
  missingPointsToBurn
  daysRemaining
  maxUses
  productAvailable
  activationAllowed
  availableRedemptions
  registeredRedemptions
  onlineOffer
  products {
    ...BurnableProductDetails
  }
}

fragment BurnableProductDetails on Product {
  code
  name
  manufacturerName
  manufacturerSubBrandName
  available
  url
  price {
    currencyIso
    currencySymbol
    formattedValue
    discountedPriceFormatted
    discountedUnitPriceFormatted
    unitPriceFormatted
    value
    wasPrice
  }
  stock {
    inStock
    inStockBeforeMaxAdvanceOrderingDate
    availableFromDate
  }
}
"""

ACTIVATE_ALL_PERSONAL_OFFERS_MUTATION = """
mutation ActivateAllPersonalOffers {
  activateAllPersonalOffers
}
"""

ACTIVATE_COUPON_BOOK_OFFER_MUTATION = """
mutation ActivateCouponBookOffer($offerId: String!) {
  activateCouponBookOffer(offerId: $offerId)
}
"""


class DelhaizeApiError(Exception):
    """Base error for Delhaize API failures."""

    def __init__(self, message: str, *, errors: list[dict[str, Any]] | None = None) -> None:
        """Initialize the error."""
        super().__init__(message)
        self.errors = errors or []


class DelhaizeRequestError(DelhaizeApiError):
    """Raised when the HTTP request itself failed."""


class DelhaizeAuthError(DelhaizeApiError):
    """Raised when the Delhaize session is not authenticated."""


class DelhaizeCaptchaRequired(DelhaizeAuthError):
    """Raised when Delhaize requires a captcha challenge."""


class DelhaizeMfaRequired(DelhaizeAuthError):
    """Raised when Delhaize requires a one-time password."""

    def __init__(
        self,
        message: str,
        *,
        errors: list[dict[str, Any]] | None = None,
        mfa_token: str | None = None,
        mfa_purpose: str | None = None,
    ) -> None:
        """Initialize the error."""
        super().__init__(message, errors=errors)
        self.mfa_token = mfa_token
        self.mfa_purpose = mfa_purpose


class DelhaizeTokenRefreshRequired(DelhaizeAuthError):
    """Raised when Delhaize asks the client to refresh auth cookies."""


class DelhaizeApi:
    """Client for Delhaize website GraphQL operations."""

    def __init__(
        self,
        websession: ClientSession,
        *,
        cookie_header: str | None = None,
        language: str = DEFAULT_LANGUAGE,
    ) -> None:
        """Initialize the API client."""
        self.websession = websession
        self.language = language
        self._cookies: dict[str, str] = {}
        if cookie_header:
            self.set_cookie_header(cookie_header)

    def set_cookie_header(self, cookie_header: str) -> None:
        """Import a browser Cookie header into the client cookie jar."""
        value = cookie_header.strip()
        if value.lower().startswith("cookie:"):
            value = value.split(":", 1)[1].strip()

        pairs = _cookie_header_pairs(value)
        if pairs:
            for key, cookie_value in pairs:
                self._cookies[key] = cookie_value
            return

        parsed = SimpleCookie()
        try:
            parsed.load(value)
        except CookieError:
            return

        for key, morsel in parsed.items():
            self._cookies[key] = morsel.value

    def get_cookie_header(self) -> str:
        """Return the current Cookie header value."""
        return "; ".join(f"{key}={value}" for key, value in self._cookies.items())

    async def get_device_id(self, *, allow_token_refresh: bool = True) -> str | None:
        """Initialize Delhaize device cookies and return the device id."""
        _LOGGER.debug("Initializing Delhaize device session")
        data = await self.graphql(
            "DeviceId",
            DEVICE_ID_QUERY,
            allow_token_refresh=allow_token_refresh,
        )
        device_id = data.get("deviceId")
        if device_id:
            self._cookies[DEVICE_SESSION_COOKIE_NAME] = str(device_id)
        return device_id

    async def login(
        self,
        username: str,
        password: str,
        *,
        lang: str | None = None,
        remember: bool = True,
    ) -> dict[str, Any]:
        """Log in with username and password."""
        _LOGGER.debug(
            "Starting Delhaize credential login: language=%s remember=%s cookie_present=%s",
            lang or self.language,
            remember,
            bool(self.get_cookie_header()),
        )
        if DEVICE_SESSION_COOKIE_NAME not in self._cookies:
            await self.get_device_id()
        variables = {
            "username": username,
            "password": password,
            "termsAndConditionsValidation": False,
            "remember": remember,
            "lang": lang or self.language,
            "captcha": None,
            "mobile": False,
        }
        return await self.graphql(
            "Login",
            variables=variables,
            extensions=LOGIN_EXTENSIONS,
        )

    async def send_login_mfa_otp_code(
        self,
        mfa_token: str,
        *,
        mfa_purpose: str = "LOGIN",
        lang: str | None = None,
    ) -> dict[str, Any]:
        """Ask Delhaize to send an MFA one-time password."""
        _LOGGER.debug(
            "Requesting Delhaize MFA email code: purpose=%s language=%s token_present=%s",
            mfa_purpose,
            lang or self.language,
            bool(mfa_token),
        )
        variables = {
            "mfaPurpose": mfa_purpose,
            "mfaToken": mfa_token,
            "captcha": None,
            "lang": lang or self.language,
        }
        return await self.graphql(
            "SendLoginMfaOtpCode",
            variables=variables,
            extensions=SEND_LOGIN_MFA_OTP_EXTENSIONS,
        )

    async def login_with_mfa(
        self,
        otp_code: str,
        mfa_token: str,
        *,
        lang: str | None = None,
        remember: bool = True,
    ) -> dict[str, Any]:
        """Complete login with a one-time password."""
        _LOGGER.debug(
            "Completing Delhaize MFA login: language=%s remember=%s token_present=%s code_length=%s",
            lang or self.language,
            remember,
            bool(mfa_token),
            len(otp_code or ""),
        )
        variables = {
            "otpCode": otp_code,
            "mfaToken": mfa_token,
            "termsAndConditionsValidation": False,
            "remember": remember,
            "lang": lang or self.language,
            "captcha": None,
            "mobile": False,
        }
        return await self.graphql(
            "LoginWithMFA",
            variables=variables,
            extensions=LOGIN_WITH_MFA_EXTENSIONS,
        )

    async def refresh_customer_auth_cookies(self) -> bool:
        """Refresh Delhaize customer auth cookies when a refresh cookie is present."""
        before_cookies = dict(self._cookies)
        before_cookie_names = sorted(before_cookies)
        before_auth_cookies = _customer_auth_cookies(before_cookies)
        old_access_cookies = {
            name: self._cookies.pop(name)
            for name in CUSTOMER_ACCESS_COOKIE_NAMES
            if name in self._cookies
        }
        _LOGGER.debug(
            "Refreshing Delhaize customer auth cookies: device_session_present=%s expired_access_cookies_removed=%s cookie_names_before=%s",
            DEVICE_SESSION_COOKIE_NAME in self._cookies,
            sorted(old_access_cookies),
            before_cookie_names,
        )
        for attempt in range(2):
            attempt_before_cookies = dict(self._cookies)
            try:
                data = await self.graphql(
                    "RefreshCustomerToken",
                    extensions=REFRESH_CUSTOMER_TOKEN_EXTENSIONS,
                    extra_headers={
                        "x-do-refresh-token": "true",
                    },
                    allow_token_refresh=False,
                )
            except DelhaizeApiError as err:
                _LOGGER.warning(
                    "Delhaize customer auth cookie refresh failed: error=%s errors=%s cookie_names_before=%s cookie_names_after=%s",
                    err,
                    summarize_graphql_errors(err.errors),
                    before_cookie_names,
                    self._cookie_names(),
                )
                raise

            auth_cookie_changes = _cookie_change_summary(
                before_auth_cookies,
                _customer_auth_cookies(self._cookies),
            )
            cookie_changes = _cookie_change_summary(attempt_before_cookies, self._cookies)
            refresh_field_present = "refreshCustomerAuthCookies" in data
            new_access_cookies = {
                name: self._cookies[name]
                for name in CUSTOMER_ACCESS_COOKIE_NAMES
                if self._cookies.get(name)
            }
            refreshed = refresh_field_present and any(
                old_access_cookies.get(name) != value
                for name, value in new_access_cookies.items()
            )
            _LOGGER.debug(
                "Delhaize customer auth cookie refresh response: attempt=%s refreshed=%s auth_cookie_changes=%s cookie_changes=%s cookie_names_after=%s",
                attempt + 1,
                refreshed,
                auth_cookie_changes,
                cookie_changes,
                self._cookie_names(),
            )
            if not refresh_field_present:
                raise DelhaizeAuthError(
                    "Delhaize refresh response did not include customer auth refresh data"
                )
            if refreshed:
                return refreshed
            if attempt == 0 and _has_cookie_changes(cookie_changes):
                _LOGGER.debug(
                    "Retrying Delhaize customer auth cookie refresh after non-auth cookie changes"
                )
                continue
            raise DelhaizeAuthError(
                "Delhaize refresh did not update customer auth cookies"
            )

        raise DelhaizeAuthError("Delhaize refresh did not update customer auth cookies")

    async def current_customer(self, *, mode: str = "FULL") -> dict[str, Any]:
        """Return the logged-in customer."""
        data = await self.graphql(
            "CurrentCustomer",
            CURRENT_CUSTOMER_QUERY,
            variables={"mode": mode},
        )
        customer = data.get("currentCustomer")
        if not customer:
            raise DelhaizeAuthError("Delhaize did not return a logged-in customer")
        return customer

    async def validate_session(self) -> dict[str, Any]:
        """Validate the current session."""
        _LOGGER.debug("Validating Delhaize session")
        return await self.current_customer()

    async def get_loyalty_details(self, *, lang: str | None = None) -> dict[str, Any]:
        """Return loyalty points, savings, and Nutri-Boost details."""
        return await self.graphql(
            "getIbizaAccountDetails",
            IBIZA_ACCOUNT_DETAILS_QUERY,
            variables={"lang": lang or self.language},
        )

    async def get_personal_offers_count(self, *, lang: str | None = None) -> dict[str, Any]:
        """Return personal offer counts."""
        data = await self.graphql(
            "PersonalOffersCount",
            PERSONAL_OFFERS_COUNT_QUERY,
            variables={"lang": lang or self.language},
        )
        return data.get("personalOffersCount") or {}

    async def get_personal_offers(self, *, lang: str | None = None) -> dict[str, Any]:
        """Return personal offers and their aggregated benefit."""
        data = await self.graphql(
            "PersonalOffersV2",
            PERSONAL_OFFERS_QUERY,
            variables={"lang": lang or self.language},
        )
        return data.get("personalOffersV2") or {}

    async def get_coupon_book_offers(
        self,
        *,
        lang: str | None = None,
        activation_status: str | None = None,
        mode: str | None = None,
    ) -> dict[str, Any]:
        """Return coupon book offers, including flash e-deals."""
        variables: dict[str, Any] = {"lang": lang or self.language}
        if activation_status is not None:
            variables["activationStatus"] = activation_status
        if mode is not None:
            variables["mode"] = mode

        data = await self.graphql(
            "CouponBookOffers",
            COUPON_BOOK_OFFERS_QUERY,
            variables=variables,
        )
        return data.get("couponBookOffers") or {}

    async def get_burnable_offers_list(self, *, lang: str | None = None) -> dict[str, Any]:
        """Return burnable SuperPlus offers."""
        data = await self.graphql(
            "BurnOffersList",
            BURNABLE_OFFERS_LIST_QUERY,
            variables={"lang": lang or self.language},
        )
        return data.get("customerBurnOffersList") or {}

    async def get_burnable_offer_range(
        self,
        offer_id: str,
        *,
        lang: str | None = None,
    ) -> dict[str, Any]:
        """Return detailed product range for one burnable offer."""
        data = await self.graphql(
            "BurnableOfferRange",
            BURNABLE_OFFER_RANGE_QUERY,
            variables={"offerId": offer_id, "lang": lang or self.language},
        )
        return data.get("customerBurnOfferRangeDetailed") or {}

    async def get_burnable_offer_ranges(
        self,
        offers: list[dict[str, Any]] | None = None,
        *,
        lang: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return detailed ranges for burnable offers that have product ranges."""
        if offers is None:
            offers = (await self.get_burnable_offers_list()).get("burnableOfferList")
        if not isinstance(offers, list):
            return []

        ranges: list[dict[str, Any]] = []
        for offer in offers:
            if not isinstance(offer, dict) or offer.get("range") is not True:
                continue
            offer_id = offer.get("id")
            if offer_id is None:
                continue
            try:
                ranges.append(
                    await self.get_burnable_offer_range(str(offer_id), lang=lang)
                )
            except DelhaizeApiError as err:
                ranges.append({"id": str(offer_id), "error": str(err)})
                _LOGGER.debug(
                    "Could not fetch Delhaize burnable offer range %s: %s",
                    offer_id,
                    err,
                )
        return ranges

    async def _activate_all_personal_offers(self) -> Any:
        """Activate all available personal offers."""
        data = await self.graphql(
            "ActivateAllPersonalOffers",
            ACTIVATE_ALL_PERSONAL_OFFERS_MUTATION,
        )
        return data.get("activateAllPersonalOffers")

    async def activate_coupon_book_offer(self, offer_id: str) -> Any:
        """Activate one coupon book offer."""
        data = await self.graphql(
            "ActivateCouponBookOffer",
            ACTIVATE_COUPON_BOOK_OFFER_MUTATION,
            variables={"offerId": offer_id},
        )
        return data.get("activateCouponBookOffer")

    async def activate_inactive_coupon_book_flash_offers(self) -> list[dict[str, Any]]:
        """Activate inactive flash e-deals from the coupon book."""
        coupon_book_offers = await self.get_coupon_book_offers()
        inactive_offers = self._inactive_coupon_book_flash_offers(
            {"coupon_book_offers": coupon_book_offers}
        )
        if not inactive_offers:
            return []

        results: list[dict[str, Any]] = []
        for offer in inactive_offers:
            offer_id = offer.get("id")
            if offer_id is None:
                continue
            offer_id = str(offer_id)
            results.append(
                {
                    "id": offer_id,
                    "name": offer.get("name"),
                    "result": await self.activate_coupon_book_offer(offer_id),
                }
            )
        return results

    async def activate_all_personal_offers(self) -> dict[str, Any]:
        """Activate all available personal offers and flash e-deals."""
        result: dict[str, Any] = {
            "personal_offers": await self._activate_all_personal_offers()
        }

        try:
            result["coupon_book_flash_offers"] = (
                await self.activate_inactive_coupon_book_flash_offers()
            )
        except DelhaizeApiError as err:
            result["coupon_book_flash_offers_error"] = str(err)
            _LOGGER.debug(
                "Could not activate Delhaize coupon book flash offers: %s",
                err,
            )

        return result

    async def fetch_summary(self, *, auto_activate: bool = False) -> dict[str, Any]:
        """Return all data used by the Home Assistant entities."""
        summary: dict[str, Any] = {
            "customer": await self.validate_session(),
            "loyalty": {},
            "personal_offers_count": {},
            "personal_offers": {},
            "coupon_book_offers": {},
            "burnable_offers": {},
            "burnable_offer_ranges": [],
        }

        summary["loyalty"] = await self.get_loyalty_details()
        summary["personal_offers_count"] = await self.get_personal_offers_count()

        try:
            summary["personal_offers"] = await self.get_personal_offers()
        except DelhaizeApiError as err:
            summary["personal_offers_error"] = str(err)
            _LOGGER.debug("Could not fetch Delhaize personal offer details: %s", err)

        try:
            summary["coupon_book_offers"] = await self.get_coupon_book_offers()
        except DelhaizeApiError as err:
            summary["coupon_book_offers_error"] = str(err)
            _LOGGER.debug("Could not fetch Delhaize coupon book offers: %s", err)

        try:
            summary["burnable_offers"] = await self.get_burnable_offers_list()
        except DelhaizeApiError as err:
            summary["burnable_offers_error"] = str(err)
            _LOGGER.debug("Could not fetch Delhaize burnable offers: %s", err)
        else:
            try:
                burnable_offer_list = summary["burnable_offers"].get("burnableOfferList")
                summary["burnable_offer_ranges"] = await self.get_burnable_offer_ranges(
                    burnable_offer_list
                )
            except DelhaizeApiError as err:
                summary["burnable_offer_ranges_error"] = str(err)
                _LOGGER.debug("Could not fetch Delhaize burnable offer ranges: %s", err)

        if auto_activate and self._has_inactive_offers(summary):
            inactive_offers = self._inactive_personal_offers(summary)
            inactive_flash_offers = self._inactive_coupon_book_flash_offers(summary)
            _LOGGER.debug(
                "Auto-activating Delhaize offers: inactive_personal_count=%s inactive_flash_count=%s",
                len(inactive_offers) if inactive_offers else None,
                len(inactive_flash_offers) if inactive_flash_offers else None,
            )
            try:
                summary["activation_result"] = await self.activate_all_personal_offers()
            except DelhaizeApiError as err:
                summary["activation_error"] = str(err)
                _LOGGER.debug("Could not activate Delhaize offers: %s", err)
            else:
                try:
                    summary["personal_offers_count"] = await self.get_personal_offers_count()
                except DelhaizeApiError as err:
                    summary["activation_refresh_error"] = str(err)
                    _LOGGER.debug(
                        "Could not refresh Delhaize personal offer count after activation: %s",
                        err,
                    )
                try:
                    summary["personal_offers"] = await self.get_personal_offers()
                except DelhaizeApiError as err:
                    summary["personal_offers_error"] = str(err)
                    _LOGGER.debug(
                        "Could not refresh Delhaize personal offer details after activation: %s",
                        err,
                    )
                try:
                    summary["coupon_book_offers"] = await self.get_coupon_book_offers()
                except DelhaizeApiError as err:
                    summary["coupon_book_offers_error"] = str(err)
                    _LOGGER.debug(
                        "Could not refresh Delhaize coupon book offers after activation: %s",
                        err,
                    )

        return summary

    async def graphql(
        self,
        operation_name: str,
        query: str | None = None,
        *,
        variables: dict[str, Any] | None = None,
        extensions: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        allow_token_refresh: bool = True,
        method: str = "POST",
    ) -> dict[str, Any]:
        """Execute a GraphQL operation."""
        try:
            return await self._graphql(
                operation_name,
                query,
                variables=variables,
                extensions=extensions,
                extra_headers=extra_headers,
                method=method,
            )
        except DelhaizeTokenRefreshRequired:
            if not allow_token_refresh or not self._has_customer_refresh_cookie():
                if allow_token_refresh:
                    _LOGGER.debug(
                        "Delhaize token refresh unavailable for %s: cookie_names=%s",
                        operation_name,
                        self._cookie_names(),
                    )
                raise

            _LOGGER.debug(
                "Delhaize access token expired for %s; refreshing cookies and retrying once: cookie_names=%s",
                operation_name,
                self._cookie_names(),
            )
            try:
                await self.refresh_customer_auth_cookies()
                return await self._graphql(
                    operation_name,
                    query,
                    variables=variables,
                    extensions=extensions,
                    extra_headers=extra_headers,
                    method=method,
                )
            except DelhaizeApiError as err:
                _LOGGER.warning(
                    "Delhaize GraphQL retry after token refresh failed: operation=%s error=%s errors=%s cookie_names=%s",
                    operation_name,
                    err,
                    summarize_graphql_errors(err.errors),
                    self._cookie_names(),
                )
                raise

    async def _graphql(
        self,
        operation_name: str,
        query: str | None = None,
        *,
        variables: dict[str, Any] | None = None,
        extensions: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        method: str = "POST",
    ) -> dict[str, Any]:
        """Execute one GraphQL HTTP request."""
        method = method.upper()
        payload: dict[str, Any] = {
            "operationName": operation_name,
            "variables": variables or {},
        }
        if query is not None:
            payload["query"] = query
        if extensions is not None:
            payload["extensions"] = extensions
        headers = self._headers(
            operation_name,
            extra_headers=extra_headers,
            include_content_type=method != "GET",
        )
        _LOGGER.debug(
            "Sending Delhaize GraphQL request: operation=%s method=%s variables=%s query_present=%s extensions_present=%s cookie_present=%s extra_headers=%s",
            operation_name,
            method,
            sorted(payload["variables"].keys()),
            "query" in payload,
            "extensions" in payload,
            "Cookie" in headers,
            sorted((extra_headers or {}).keys()),
        )

        try:
            if method == "GET":
                request = self.websession.get(
                    API_URL,
                    params=_graphql_get_params(payload),
                    headers=headers,
                    timeout=30,
                )
            else:
                request = self.websession.post(
                    API_URL,
                    json=payload,
                    headers=headers,
                    timeout=30,
                )

            async with request as response:
                response_text = await response.text()
                response_cookies = _response_cookie_items(response)
                self._store_response_cookies(response_cookies)
                status = response.status
                cookie_names = sorted(response_cookies)
        except (ClientError, TimeoutError, CancelledError) as err:
            _LOGGER.debug("Delhaize GraphQL request failed: operation=%s error=%r", operation_name, err)
            raise DelhaizeRequestError(f"Could not reach Delhaize: {err}") from err

        _LOGGER.debug(
            "Received Delhaize GraphQL response: operation=%s status=%s bytes=%s set_cookies=%s",
            operation_name,
            status,
            len(response_text),
            cookie_names,
        )
        result = self._decode_response(response_text, operation_name)
        if status >= 400:
            errors = result.get("errors") if isinstance(result, dict) else None
            if errors:
                self._raise_graphql_errors(operation_name, errors)
            raise DelhaizeRequestError(
                f"Delhaize returned HTTP {status} for {operation_name}"
            )

        errors = result.get("errors") if isinstance(result, dict) else None
        if errors:
            self._raise_graphql_errors(operation_name, errors)

        data = result.get("data") if isinstance(result, dict) else None
        if data is None:
            raise DelhaizeApiError(f"Delhaize returned no data for {operation_name}")
        return data

    def _headers(
        self,
        operation_name: str,
        *,
        extra_headers: dict[str, str] | None = None,
        include_content_type: bool = True,
    ) -> dict[str, str]:
        """Build request headers matching the Delhaize web client."""
        headers = self._browser_context_headers(
            content_type="application/json" if include_content_type else None,
            referer=self._referer(operation_name),
        )
        headers["X-APOLLO-OPERATION-NAME"] = operation_name
        headers["x-default-gql-refresh-token-disabled"] = "true"
        operation_id = OPERATION_IDS.get(operation_name)
        if operation_id:
            headers["X-APOLLO-OPERATION-ID"] = operation_id
        if extra_headers:
            headers.update(extra_headers)
        return headers

    def _browser_context_headers(
        self,
        *,
        content_type: str | None = None,
        referer: str | None = None,
    ) -> dict[str, str]:
        """Return fetch headers shared by browser context requests."""
        headers = {
            "Accept": "*/*",
            "Accept-Language": _accept_language(self.language),
            "Origin": BASE_URL,
            "Referer": referer or f"{BASE_URL}/{self.language}/my-account/dashboard",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/151.0.0.0 Safari/537.36"
            ),
            "Apollographql-Client-Name": APOLLO_CLIENT_NAME,
            "Apollographql-Client-Version": APOLLO_CLIENT_VERSION,
            "Sec-CH-UA": (
                '"Not=A?Brand";v="99", "Google Chrome";v="151", '
                '"Chromium";v="151"'
            ),
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": '"Windows"',
        }
        if content_type:
            headers["Content-Type"] = content_type
        cookie_header = self.get_cookie_header()
        if cookie_header:
            headers["Cookie"] = cookie_header
        return headers

    def _referer(self, operation_name: str) -> str:
        """Return the browser page that would usually issue this operation."""
        if operation_name.lower() in {
            "login",
            "loginwithmfa",
            "sendloginmfaotpcode",
            "getloginwithssolink",
        }:
            return f"{BASE_URL}/login"
        return f"{BASE_URL}/"

    def _store_response_cookies(self, response_cookies: dict[str, str]) -> None:
        """Store cookies returned by Delhaize."""
        for key, value in response_cookies.items():
            if value:
                self._cookies[key] = value
            else:
                self._cookies.pop(key, None)

    def _cookie_names(self) -> list[str]:
        """Return stored cookie names for sanitized diagnostics."""
        return sorted(self._cookies)

    def _has_customer_refresh_cookie(self) -> bool:
        """Return whether the stored cookies can refresh customer auth."""
        return any(name in self._cookies for name in CUSTOMER_REFRESH_COOKIE_NAMES)

    def _decode_response(self, response_text: str, operation_name: str) -> dict[str, Any]:
        """Decode a JSON GraphQL response."""
        try:
            return json.loads(response_text)
        except json.JSONDecodeError as err:
            _LOGGER.debug("Non-JSON response for %s: %s", operation_name, response_text[:500])
            raise DelhaizeRequestError(
                f"Delhaize returned an invalid response for {operation_name}"
            ) from err

    def _raise_graphql_errors(
        self,
        operation_name: str,
        errors: list[dict[str, Any]],
    ) -> None:
        """Raise a typed exception for GraphQL errors."""
        messages = [str(error.get("message") or "Unknown Delhaize error") for error in errors]
        combined = "; ".join(messages)
        text = " ".join([combined, *(_error_codes(errors))]).lower()
        summary = summarize_graphql_errors(errors)
        _LOGGER.debug(
            "Delhaize GraphQL returned errors: operation=%s errors=%s",
            operation_name,
            summary,
        )

        if "otp" in text or "mfa" in text:
            raise DelhaizeMfaRequired(
                combined,
                errors=errors,
                mfa_token=_find_value(errors, "mfaToken") or _find_value(errors, "mfa_token"),
                mfa_purpose=(
                    _find_value(errors, "mfaOtpPurpose")
                    or _find_value(errors, "mfa_otp_purpose")
                    or _find_value(errors, "mfaPurpose")
                    or _find_value(errors, "mfa_purpose")
                ),
            )

        if "captcha" in text or "recaptcha" in text:
            raise DelhaizeCaptchaRequired(combined, errors=errors)

        if _has_error_code(errors, TOKEN_REFRESH_ERROR_CODE):
            raise DelhaizeTokenRefreshRequired(combined, errors=errors)

        if (
            _has_error_code(errors, "TOKEN_EXPIRED")
            or _has_error_code(errors, "ACCESS_TOKEN_EXPIRED")
        ):
            raise DelhaizeTokenRefreshRequired(combined, errors=errors)

        if (
            operation_name.lower() not in {"login", "loginwithmfa"}
            and _is_invalid_access_token_error(text)
        ):
            raise DelhaizeTokenRefreshRequired(combined, errors=errors)

        if _is_token_expired_error(text):
            raise DelhaizeTokenRefreshRequired(combined, errors=errors)

        if (
            _has_error_code(errors, "UNAUTHENTICATED")
            or _has_error_code(errors, "AGENT_UNAUTHENTICATED")
            or _has_error_code(errors, "FORBIDDEN")
        ):
            raise DelhaizeAuthError(combined, errors=errors)

        if (
            "forbidden" in text
            or "unauthenticated" in text
            or "unauthorized" in text
            or "anonymous user" in text
            or "invalid_grant" in text
            or "invalidcredentials" in text.replace("_", "")
            or operation_name.lower() in {"login", "loginwithmfa"}
        ):
            raise DelhaizeAuthError(combined, errors=errors)

        raise DelhaizeApiError(combined, errors=errors)

    @staticmethod
    def _has_inactive_personal_offers(summary: dict[str, Any]) -> bool:
        """Return whether the offer count says there are inactive offers."""
        inactive_offers = DelhaizeApi._inactive_personal_offers(summary)
        if inactive_offers:
            return True

        if inactive_offers == []:
            return False

        counts = summary.get("personal_offers_count") or {}
        try:
            total = int(counts.get("totalCount") or 0)
            active = int(counts.get("activatedCount") or 0)
        except (TypeError, ValueError):
            return False
        return total > active

    @staticmethod
    def _has_inactive_offers(summary: dict[str, Any]) -> bool:
        """Return whether any supported offer source has inactive offers."""
        return DelhaizeApi._has_inactive_personal_offers(summary) or bool(
            DelhaizeApi._inactive_coupon_book_flash_offers(summary)
        )

    @staticmethod
    def _inactive_personal_offers(summary: dict[str, Any]) -> list[dict[str, Any]] | None:
        """Return inactive personal offers when the detailed offer list is available."""
        offers = summary.get("personal_offers") or {}
        offer_list = offers.get("personalOfferList") if isinstance(offers, dict) else None
        if not isinstance(offer_list, list):
            return None
        return [
            offer
            for offer in offer_list
            if isinstance(offer, dict)
            and offer.get("active") is False
            and offer.get("offerRedeemed") is not True
        ]

    @staticmethod
    def _inactive_coupon_book_flash_offers(
        summary: dict[str, Any],
    ) -> list[dict[str, Any]] | None:
        """Return inactive flash e-deals when the coupon book list is available."""
        offers = summary.get("coupon_book_offers") or {}
        offer_list = offers.get("flashOffers") if isinstance(offers, dict) else None
        if not isinstance(offer_list, list):
            return None
        return [
            offer
            for offer in offer_list
            if isinstance(offer, dict) and offer.get("active") is False
        ]


def _error_codes(errors: list[dict[str, Any]]) -> list[str]:
    """Extract reason and extension codes from GraphQL errors."""
    codes: list[str] = []
    for error in errors:
        extensions = error.get("extensions") or {}
        response = extensions.get("response") if isinstance(extensions, dict) else None
        response_body = response.get("body") if isinstance(response, dict) else None
        code_sources = [error, extensions]
        if isinstance(response_body, dict):
            code_sources.append(response_body)

        for source in code_sources:
            for key in ("code", "reasonCode", "type"):
                value = source.get(key)
                if value is not None:
                    codes.append(str(value))
    return codes


def _has_error_code(errors: list[dict[str, Any]], code: str) -> bool:
    """Return whether a GraphQL error has the given code or reason code."""
    expected = code.upper()
    return any(value.upper() == expected for value in _error_codes(errors))


def _response_body_reason_code(extensions: dict[str, Any]) -> Any:
    """Return a nested GraphQL response reason code, when present."""
    response = extensions.get("response")
    if not isinstance(response, dict):
        return None
    body = response.get("body")
    if not isinstance(body, dict):
        return None
    return body.get("reasonCode") or body.get("code")


def _is_token_expired_error(text: str) -> bool:
    """Return whether GraphQL error text describes a stale auth token."""
    return (
        ("token" in text and "expired" in text)
        or "jwt expired" in text
        or "access token expired" in text
    )


def _is_invalid_access_token_error(text: str) -> bool:
    """Return whether GraphQL error text describes a rejected access token."""
    return "invalid access token" in text


def _customer_auth_cookies(cookies: dict[str, str]) -> dict[str, str]:
    """Return cookies that carry Delhaize customer auth state."""
    return {
        key: value
        for key, value in cookies.items()
        if key in CUSTOMER_AUTH_COOKIE_NAMES
    }


def _has_cookie_changes(changes: dict[str, list[str]]) -> bool:
    """Return whether a cookie change summary has any changed entries."""
    return any(changes.values())


def _cookie_change_summary(
    before: dict[str, str],
    after: dict[str, str],
) -> dict[str, list[str]]:
    """Return changed cookie names for logs without exposing values."""
    before_set = set(before)
    after_set = set(after)
    return {
        "added": sorted(after_set - before_set),
        "changed": sorted(
            key for key in before_set & after_set if before[key] != after[key]
        ),
        "removed": sorted(before_set - after_set),
    }


def _response_cookie_items(response: ClientResponse) -> dict[str, str]:
    """Return response cookies, including raw Set-Cookie headers if needed."""
    headers = getattr(response, "headers", None)
    getall = getattr(headers, "getall", None)
    raw_headers = getall("Set-Cookie", []) if callable(getall) else []
    if not raw_headers:
        return {key: morsel.value for key, morsel in response.cookies.items()}

    cookies: dict[str, str] = {}
    nonempty_response_cookies: set[str] = set()
    for header in raw_headers:
        parsed = _cookies_from_set_cookie_header(str(header))
        for key, value in parsed.items():
            if value:
                cookies[key] = value
                nonempty_response_cookies.add(key)
            elif key not in nonempty_response_cookies:
                cookies[key] = ""

    return cookies


def _cookies_from_set_cookie_header(header: str) -> dict[str, str]:
    """Parse one Set-Cookie header without exposing values in logs."""
    parsed = SimpleCookie()
    try:
        parsed.load(header)
    except CookieError:
        parsed = SimpleCookie()

    if parsed:
        return {key: morsel.value for key, morsel in parsed.items()}

    cookie_pair = header.split(";", 1)[0]
    if "=" not in cookie_pair:
        return {}
    key, value = cookie_pair.split("=", 1)
    key = key.strip()
    if not key:
        return {}
    return {key: value.strip()}


def _cookie_header_pairs(header: str) -> list[tuple[str, str]]:
    """Parse a browser Cookie header while preserving pair order."""
    pairs: list[tuple[str, str]] = []
    for part in header.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        if key:
            pairs.append((key, value.strip()))
    return pairs


def _graphql_get_params(payload: dict[str, Any]) -> dict[str, str]:
    """Return GraphQL GET parameters matching Apollo persisted query requests."""
    params = {"operationName": str(payload["operationName"])}
    for key in ("variables", "extensions"):
        value = payload.get(key)
        if value is not None:
            params[key] = json.dumps(value, separators=(",", ":"))
    if "query" in payload:
        params["query"] = str(payload["query"])
    return params


def _accept_language(language: str) -> str:
    """Return an Accept-Language header close to a Belgian browser."""
    normalized = language.lower()
    if normalized == "fr":
        return "fr-BE,fr;q=0.9,nl-BE;q=0.8,nl;q=0.7,en-US;q=0.6,en;q=0.5"
    if normalized == "en":
        return "en-US,en;q=0.9,nl-BE;q=0.8,nl;q=0.7,fr-BE;q=0.6,fr;q=0.5"
    return "nl-BE,nl;q=0.9,fr-BE;q=0.8,fr;q=0.7,en-US;q=0.6,en;q=0.5"


def _akamai_pixel_payload(cookie_header: str, language: str) -> str:
    """Return a best-effort Akamai pixel form body matching browser shape."""
    now_ms = int(time() * 1000)
    seed = f"{cookie_header}|{language}|{now_ms}"
    token = sha1(seed.encode("utf-8")).hexdigest()
    cv = sha1(f"cv|{seed}".encode("utf-8")).hexdigest()
    user_token = sha1(f"u|{seed}".encode("utf-8")).hexdigest()
    nav_languages = ["en-US", "nl", "en", "fr"]

    payload = {
        "ap": "true",
        "bt": json.dumps(
            {
                "charging": False,
                "chargingTime": "Infinity",
                "dischargingTime": 3948,
                "level": 0.69,
                "onchargingchange": None,
                "onchargingtimechange": None,
                "ondischargingtimechange": None,
                "onlevelchange": None,
            },
            separators=(",", ":"),
        ),
        "fonts": "null",
        "fh": "null",
        "timing": json.dumps(
            {
                "1": 63,
                "2": 2885,
                "profile": {
                    "bp": 6,
                    "sr": 1,
                    "dp": 1,
                    "lt": 0,
                    "ps": 0,
                    "cv": 30,
                    "fp": 0,
                    "sp": 1,
                    "br": 0,
                    "ieps": 0,
                    "av": 0,
                    "z1": 21,
                    "jsv": 1,
                    "nav": 0,
                    "nap": 1,
                    "crc": 0,
                    "z2": 3,
                },
                "main": 1629,
                "compute": 63,
                "send": 2885,
            },
            separators=(",", ":"),
        ),
        "bp": "",
        "sr": json.dumps(
            {
                "inner": [1920, 911],
                "outer": [1920, 1032],
                "screen": [0, 0],
                "pageOffset": [0, 0],
                "avail": [1920, 1032],
                "size": [1920, 1080],
                "client": [1905, 3271],
                "colorDepth": 24,
                "pixelDepth": 24,
            },
            separators=(",", ":"),
        ),
        "dp": json.dumps(
            {
                "XDomainRequest": 0,
                "createPopup": 0,
                "removeEventListener": 1,
                "globalStorage": 0,
                "openDatabase": 0,
                "indexedDB": 1,
                "attachEvent": 0,
                "ActiveXObject": 0,
                "dispatchEvent": 1,
                "addBehavior": 0,
                "addEventListener": 1,
                "detachEvent": 0,
                "fireEvent": 0,
                "MutationObserver": 1,
                "HTMLMenuItemElement": 0,
                "Int8Array": 1,
                "postMessage": 1,
                "querySelector": 1,
                "getElementsByClassName": 1,
                "images": 1,
                "compatMode": "CSS1Compat",
                "documentMode": 0,
                "all": 1,
                "now": 1,
                "contextMenu": 0,
            },
            separators=(",", ":"),
        ),
        "lt": f"{now_ms}+2",
        "ps": "true,true",
        "cv": cv,
        "fp": "false",
        "sp": "false",
        "br": "Chrome",
        "ieps": "false",
        "av": "false",
        "z": json.dumps(
            {"a": int(token[:8], 16), "b": 1, "c": 1},
            separators=(",", ":"),
        ),
        "zh": "",
        "jsv": "1.5",
        "nav": json.dumps(
            {
                "userAgent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/150.0.0.0 Safari/537.36"
                ),
                "appName": "Netscape",
                "appCodeName": "Mozilla",
                "appVersion": (
                    "5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/150.0.0.0 Safari/537.36"
                ),
                "appMinorVersion": 0,
                "product": "Gecko",
                "productSub": "20030107",
                "vendor": "Google Inc.",
                "vendorSub": "",
                "buildID": 0,
                "platform": "Win32",
                "oscpu": 0,
                "hardwareConcurrency": 8,
                "language": "en-US",
                "languages": nav_languages,
                "systemLanguage": 0,
                "userLanguage": 0,
                "doNotTrack": None,
                "msDoNotTrack": 0,
                "cookieEnabled": True,
                "geolocation": 1,
                "vibrate": 1,
                "maxTouchPoints": 0,
                "webdriver": False,
                "plugins": [],
            },
            separators=(",", ":"),
        ),
        "crc": json.dumps(
            {
                "window.chrome": {
                    "app": {
                        "isInstalled": False,
                        "InstallState": {
                            "DISABLED": "disabled",
                            "INSTALLED": "installed",
                            "NOT_INSTALLED": "not_installed",
                        },
                        "RunningState": {
                            "CANNOT_RUN": "cannot_run",
                            "READY_TO_RUN": "ready_to_run",
                            "RUNNING": "running",
                        },
                    }
                }
            },
            separators=(",", ":"),
        ),
        "t": token,
        "u": user_token,
    }
    return urlencode(payload)


def summarize_graphql_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return sanitized GraphQL error details for debug logs."""
    summary: list[dict[str, Any]] = []
    sensitive_keys = {
        "captcha",
        "mfaToken",
        "mfa_token",
        "otpCode",
        "otp_code",
        "password",
        "refreshToken",
        "token",
    }

    for error in errors:
        extensions = error.get("extensions") or {}
        item: dict[str, Any] = {
            "message": error.get("message"),
            "path": error.get("path"),
            "code": error.get("code") or extensions.get("code"),
            "reasonCode": (
                error.get("reasonCode")
                or extensions.get("reasonCode")
                or _response_body_reason_code(extensions)
            ),
            "type": error.get("type") or extensions.get("type"),
            "extension_keys": sorted(extensions.keys()),
        }
        for key in sensitive_keys:
            if _find_value(error, key) is not None:
                item[f"{key}_present"] = True
        purpose = (
            _find_value(error, "mfaOtpPurpose")
            or _find_value(error, "mfa_otp_purpose")
            or _find_value(error, "mfaPurpose")
            or _find_value(error, "mfa_purpose")
        )
        if purpose is not None:
            item["mfa_purpose"] = purpose
        summary.append({key: value for key, value in item.items() if value is not None})

    return summary


def _find_value(value: Any, key: str) -> Any:
    """Recursively find a key in a nested response."""
    if isinstance(value, dict):
        if key in value:
            return value[key]
        for item in value.values():
            found = _find_value(item, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_value(item, key)
            if found is not None:
                return found
    return None


__all__ = [
    "DelhaizeApi",
    "DelhaizeApiError",
    "DelhaizeAuthError",
    "DelhaizeCaptchaRequired",
    "DelhaizeMfaRequired",
    "DelhaizeRequestError",
    "DelhaizeTokenRefreshRequired",
    "summarize_graphql_errors",
]
