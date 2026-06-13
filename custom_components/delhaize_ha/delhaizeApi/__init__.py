"""Delhaize website GraphQL client."""

from __future__ import annotations

from asyncio import CancelledError, TimeoutError
from datetime import datetime, timedelta, timezone
from http.cookies import CookieError, SimpleCookie
import json
import logging
from typing import Any

from aiohttp import ClientResponse, ClientSession
from aiohttp.client_exceptions import ClientError

from ..const import API_URL, BASE_URL, DEFAULT_LANGUAGE

_LOGGER = logging.getLogger(__name__)

TOKEN_REFRESH_ERROR_CODE = "PENDING_TOKEN_REFRESH"
CUSTOMER_AUTH_COOKIE_REFRESH_INTERVAL = timedelta(hours=6)

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

REFRESH_CUSTOMER_TOKEN_MUTATION = (
    "mutation RefreshCustomerToken{refreshCustomerAuthCookies}"
)

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

ACTIVATE_ALL_PERSONAL_OFFERS_MUTATION = """
mutation ActivateAllPersonalOffers {
  activateAllPersonalOffers
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
        self._last_customer_auth_refresh_attempt: datetime | None = None
        if cookie_header:
            self.set_cookie_header(cookie_header)

    def set_cookie_header(self, cookie_header: str) -> None:
        """Import a browser Cookie header into the client cookie jar."""
        value = cookie_header.strip()
        if value.lower().startswith("cookie:"):
            value = value.split(":", 1)[1].strip()

        parsed = SimpleCookie()
        try:
            parsed.load(value)
        except CookieError:
            parsed = SimpleCookie()

        if parsed:
            for key, morsel in parsed.items():
                self._cookies[key] = morsel.value
            return

        for part in value.split(";"):
            if "=" not in part:
                continue
            key, cookie_value = part.split("=", 1)
            key = key.strip()
            if key:
                self._cookies[key] = cookie_value.strip()

    def get_cookie_header(self) -> str:
        """Return the current Cookie header value."""
        return "; ".join(f"{key}={value}" for key, value in sorted(self._cookies.items()))

    async def refresh_customer_auth_cookies_if_due(
        self,
        *,
        interval: timedelta = CUSTOMER_AUTH_COOKIE_REFRESH_INTERVAL,
    ) -> bool:
        """Refresh customer auth cookies periodically before the access token expires."""
        if not self.get_cookie_header():
            return False

        now = datetime.now(timezone.utc)
        if (
            self._last_customer_auth_refresh_attempt is not None
            and now - self._last_customer_auth_refresh_attempt < interval
        ):
            return False

        await self.refresh_customer_auth_cookies()
        return True

    async def get_device_id(self) -> str | None:
        """Initialize Delhaize device cookies and return the device id."""
        _LOGGER.debug("Initializing Delhaize device session")
        data = await self.graphql("DeviceId", DEVICE_ID_QUERY)
        return data.get("deviceId")

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
        await self.get_device_id()
        variables = {
            "username": username,
            "password": password,
            "termsAndConditionsAccepted": False,
            "termsAndConditionsValidation": False,
            "remember": remember,
            "prospect_token": None,
            "lang": lang or self.language,
            "captcha": None,
            "mobile": False,
            "country": "BE",
        }
        return await self.graphql("Login", LOGIN_MUTATION, variables=variables)

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
            SEND_LOGIN_MFA_OTP_MUTATION,
            variables=variables,
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
            "termsAndConditionsAccepted": False,
            "termsAndConditionsValidation": False,
            "remember": remember,
            "lang": lang or self.language,
            "captcha": None,
            "mobile": False,
            "country": "BE",
        }
        return await self.graphql("LoginWithMFA", LOGIN_WITH_MFA_MUTATION, variables=variables)

    async def refresh_customer_auth_cookies(self) -> bool:
        """Refresh Delhaize customer auth cookies when a refresh cookie is present."""
        before_cookies = dict(self._cookies)
        before_cookie_names = sorted(before_cookies)
        self._last_customer_auth_refresh_attempt = datetime.now(timezone.utc)
        _LOGGER.debug(
            "Refreshing Delhaize customer auth cookies: cookie_names_before=%s",
            before_cookie_names,
        )
        try:
            data = await self.graphql(
                "RefreshCustomerToken",
                REFRESH_CUSTOMER_TOKEN_MUTATION,
                extra_headers={
                    "X-APOLLO-OPERATION-ID": REFRESH_CUSTOMER_TOKEN_HASH,
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

        refreshed = "refreshCustomerAuthCookies" in data
        _LOGGER.debug(
            "Delhaize customer auth cookie refresh response: refreshed=%s cookie_changes=%s cookie_names_after=%s",
            refreshed,
            _cookie_change_summary(before_cookies, self._cookies),
            self._cookie_names(),
        )
        if not refreshed:
            raise DelhaizeAuthError(
                "Delhaize refresh response did not include refreshCustomerAuthCookies"
            )
        return refreshed

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

    async def activate_all_personal_offers(self) -> Any:
        """Activate all available personal offers."""
        data = await self.graphql(
            "ActivateAllPersonalOffers",
            ACTIVATE_ALL_PERSONAL_OFFERS_MUTATION,
        )
        return data.get("activateAllPersonalOffers")

    async def fetch_summary(self, *, auto_activate: bool = False) -> dict[str, Any]:
        """Return all data used by the Home Assistant entities."""
        summary: dict[str, Any] = {
            "customer": await self.validate_session(),
            "loyalty": {},
            "personal_offers_count": {},
            "personal_offers": {},
        }

        summary["loyalty"] = await self.get_loyalty_details()
        summary["personal_offers_count"] = await self.get_personal_offers_count()

        try:
            summary["personal_offers"] = await self.get_personal_offers()
        except DelhaizeApiError as err:
            summary["personal_offers_error"] = str(err)
            _LOGGER.debug("Could not fetch Delhaize personal offer details: %s", err)

        if auto_activate and self._has_inactive_personal_offers(summary):
            inactive_offers = self._inactive_personal_offers(summary)
            _LOGGER.debug(
                "Auto-activating Delhaize personal offers: inactive_count=%s",
                len(inactive_offers) if inactive_offers else None,
            )
            try:
                summary["activation_result"] = await self.activate_all_personal_offers()
            except DelhaizeApiError as err:
                summary["activation_error"] = str(err)
                _LOGGER.debug("Could not activate Delhaize personal offers: %s", err)
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
    ) -> dict[str, Any]:
        """Execute a GraphQL operation."""
        try:
            return await self._graphql(
                operation_name,
                query,
                variables=variables,
                extensions=extensions,
                extra_headers=extra_headers,
            )
        except DelhaizeTokenRefreshRequired:
            if not allow_token_refresh or not self.get_cookie_header():
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
    ) -> dict[str, Any]:
        """Execute one GraphQL HTTP request."""
        payload: dict[str, Any] = {
            "operationName": operation_name,
            "variables": variables or {},
        }
        if query is not None:
            payload["query"] = query
        if extensions is not None:
            payload["extensions"] = extensions
        headers = self._headers(operation_name, extra_headers=extra_headers)
        _LOGGER.debug(
            "Sending Delhaize GraphQL request: operation=%s variables=%s query_present=%s extensions_present=%s cookie_present=%s extra_headers=%s",
            operation_name,
            sorted(payload["variables"].keys()),
            "query" in payload,
            "extensions" in payload,
            "Cookie" in headers,
            sorted((extra_headers or {}).keys()),
        )

        try:
            async with self.websession.post(
                API_URL,
                json=payload,
                headers=headers,
                timeout=30,
            ) as response:
                response_text = await response.text()
                self._store_response_cookies(response)
                status = response.status
                cookie_names = sorted(response.cookies.keys())
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
    ) -> dict[str, str]:
        """Build request headers matching the Delhaize web client."""
        headers = {
            "Accept": "*/*",
            "Content-Type": "application/json",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/{self.language}/login",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            ),
            "X-Apollo-Operation-Name": operation_name,
            "x-default-gql-refresh-token-disabled": "true",
        }
        cookie_header = self.get_cookie_header()
        if cookie_header:
            headers["Cookie"] = cookie_header
        if extra_headers:
            headers.update(extra_headers)
        return headers

    def _store_response_cookies(self, response: ClientResponse) -> None:
        """Store cookies returned by Delhaize."""
        for key, morsel in response.cookies.items():
            if morsel.value:
                self._cookies[key] = morsel.value
            else:
                self._cookies.pop(key, None)

    def _cookie_names(self) -> list[str]:
        """Return stored cookie names for sanitized diagnostics."""
        return sorted(self._cookies)

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
        or "invalid access token" in text
    )


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
