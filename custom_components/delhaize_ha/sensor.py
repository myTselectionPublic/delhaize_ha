"""Sensors for the Delhaize integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .const import BASE_URL, CONF_LANGUAGE, DEFAULT_LANGUAGE, DOMAIN, NAME
from .coordinator import DelhaizeDataUpdateCoordinator


@dataclass(frozen=True, kw_only=True)
class DelhaizeSensorEntityDescription(SensorEntityDescription):
    """Describe a Delhaize sensor."""

    value_fn: Callable[[dict[str, Any]], Any]
    attr_fn: Callable[[dict[str, Any]], dict[str, Any]] = lambda data: {}


SENSOR_DESCRIPTIONS: tuple[DelhaizeSensorEntityDescription, ...] = (
    DelhaizeSensorEntityDescription(
        key="loyalty_points",
        name="Loyalty points",
        icon="mdi:star-circle",
        value_fn=lambda data: _nested(data, "loyalty", "loyaltyPoints", "pointsBalance"),
        attr_fn=lambda data: _without_none(
            {
                "nutriscore_discount": _nested(data, "loyalty", "nutriscoreBalance", "discount"),
                "nutriscore_available_to_save_this_month": _nested(
                    data,
                    "loyalty",
                    "nutriscoreBalance",
                    "availableToSaveThisMonth",
                ),
                "nutriboost_type": _nested(
                    data,
                    "loyalty",
                    "nutriscoreBalance",
                    "currentNutriBoostType",
                ),
            }
        ),
    ),
    DelhaizeSensorEntityDescription(
        key="savings",
        name="Savings",
        icon="mdi:piggy-bank-outline",
        value_fn=lambda data: _nested(
            data,
            "loyalty",
            "savings",
            "periodSavings",
            "totalSavingsAmountFormatted",
        ),
    ),
    DelhaizeSensorEntityDescription(
        key="personal_offers_available",
        name="Personal offers available",
        icon="mdi:ticket-percent-outline",
        value_fn=lambda data: _available_offers(data),
        attr_fn=lambda data: _without_none(
            {
                **_offer_count_attributes(data),
                "activation_result": data.get("activation_result"),
                "activation_error": data.get("activation_error"),
                "activation_refresh_error": data.get("activation_refresh_error"),
            }
        ),
    ),
    DelhaizeSensorEntityDescription(
        key="personal_offers_total",
        name="Personal offers total",
        icon="mdi:ticket-confirmation-outline",
        value_fn=lambda data: _total_offers(data),
        attr_fn=lambda data: _offer_count_attributes(data),
    ),
    DelhaizeSensorEntityDescription(
        key="personal_offers_activated",
        name="Personal offers activated",
        icon="mdi:offer",
        value_fn=lambda data: _activated_offers(data),
        attr_fn=lambda data: _without_none(
            {
                **_offer_count_attributes(data),
                "description_list": _activated_offer_description_list(data),
            }
        ),
    ),
    DelhaizeSensorEntityDescription(
        key="personal_offers_benefit",
        name="Personal offers benefit",
        icon="mdi:currency-eur",
        value_fn=lambda data: _nested(
            data,
            "personal_offers",
            "totalEuroBenefit",
            "formattedValue",
        ),
        attr_fn=lambda data: _without_none(
            {
                "total_points": _nested(data, "personal_offers", "totalPoints"),
                "benefit_value": _nested(data, "personal_offers", "totalEuroBenefit", "value"),
                "currency": _nested(data, "personal_offers", "totalEuroBenefit", "currencyIso"),
                "error": data.get("personal_offers_error"),
            }
        ),
    ),
    DelhaizeSensorEntityDescription(
        key="loyalty_profile",
        name="Loyalty profile",
        icon="mdi:account-star-outline",
        value_fn=lambda data: _nested(data, "customer", "ibizaLoyaltyProfile"),
    ),
    DelhaizeSensorEntityDescription(
        key="loyalty_card_number",
        name="Loyalty card number",
        icon="mdi:card-account-details-outline",
        value_fn=lambda data: _nested(data, "customer", "diplaCard"),
    ),
    DelhaizeSensorEntityDescription(
        key="account",
        name="Account",
        icon="mdi:account-circle-outline",
        value_fn=lambda data: _nested(data, "customer", "customerType"),
        attr_fn=lambda data: _without_none(
            {
                "uid": _nested(data, "customer", "uid"),
                "customer_id_hash": _nested(data, "customer", "customerIdHash"),
                "first_name": _nested(data, "customer", "firstName"),
                "last_name": _nested(data, "customer", "lastName"),
                "card": _nested(data, "customer", "diplaCard"),
            }
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Delhaize sensors."""
    coordinator: DelhaizeDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [DelhaizeSensor(coordinator, description) for description in SENSOR_DESCRIPTIONS]
    )


class DelhaizeSensor(CoordinatorEntity[DelhaizeDataUpdateCoordinator], SensorEntity):
    """A Delhaize account sensor."""

    entity_description: DelhaizeSensorEntityDescription

    def __init__(
        self,
        coordinator: DelhaizeDataUpdateCoordinator,
        description: DelhaizeSensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        account_label = _account_label(coordinator)
        device_name = _device_name(account_label)
        object_id_prefix = slugify(device_name) or (
            f"{DOMAIN}_{coordinator.config_entry.entry_id}"
        )

        self.entity_description = description
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{description.key}"
        self._attr_suggested_object_id = f"{object_id_prefix}_{description.key}"
        self._attr_attribution = "Data provided by delhaize.be"
        self._attr_has_entity_name = True
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.config_entry.entry_id)},
            name=device_name,
            manufacturer="Delhaize",
            configuration_url=(
                f"{BASE_URL}/{coordinator.config_entry.data.get(CONF_LANGUAGE, DEFAULT_LANGUAGE)}"
                "/my-account"
            ),
        )

    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        return self.entity_description.value_fn(self.coordinator.data or {})

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra sensor attributes."""
        return self.entity_description.attr_fn(self.coordinator.data or {})


def _nested(data: dict[str, Any], *path: str) -> Any:
    """Return a nested value from dict data."""
    value: Any = data
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _available_offers(data: dict[str, Any]) -> int | None:
    """Return number of inactive personal offers."""
    offers = _visible_personal_offers(data)
    if offers is not None:
        return sum(1 for offer in offers if offer.get("active") is False)

    total = _total_offers(data)
    activated = _activated_offers(data)
    if total is None or activated is None:
        return None
    return max(0, total - activated)


def _total_offers(data: dict[str, Any]) -> int | None:
    """Return the number of personal offers shown by the website when available."""
    offers = _visible_personal_offers(data)
    if offers is not None:
        return len(offers)
    return _int_or_none(_nested(data, "personal_offers_count", "totalCount"))


def _activated_offers(data: dict[str, Any]) -> int | None:
    """Return the number of activated personal offers."""
    offers = _visible_personal_offers(data)
    if offers is not None:
        return sum(1 for offer in offers if offer.get("active") is True)
    return _int_or_none(_nested(data, "personal_offers_count", "activatedCount"))


def _activated_offer_description_list(data: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Return activated personal offer details for sensor attributes."""
    offers = _visible_personal_offers(data)
    if offers is None:
        return None
    return [
        _offer_detail(offer)
        for offer in offers
        if offer.get("active") is True
    ]


def _offer_detail(offer: dict[str, Any]) -> dict[str, Any]:
    """Return a structured offer detail."""
    description = (
        _clean_label(offer.get("name"))
        or _clean_label(offer.get("promotion"))
        or _clean_label(offer.get("promotionId"))
        or _clean_label(offer.get("id"))
        or "Personal offer"
    )
    return _without_none(
        {
            "description": description,
            "points": _int_or_none(offer.get("points")),
            "promotion": _clean_label(offer.get("promotion")),
            "promotion_id": _clean_label(offer.get("promotionId")),
            "promotion_type": _clean_label(offer.get("promotionType")),
            "basket_promo": offer.get("basketPromo"),
        }
    )


def _offer_count_attributes(data: dict[str, Any]) -> dict[str, Any]:
    """Return diagnostic attributes for personal offer count differences."""
    offer_list = _personal_offer_list(data)
    visible_offers = _visible_personal_offers(data)
    total = _total_offers(data)
    activated = _activated_offers(data)
    api_total = _int_or_none(_nested(data, "personal_offers_count", "totalCount"))
    api_activated = _int_or_none(_nested(data, "personal_offers_count", "activatedCount"))
    attributes: dict[str, Any] = {
        "count_source": (
            "personal_offer_list" if visible_offers is not None else "personal_offers_count"
        ),
        "total": total,
        "activated": activated,
        "available": _available_offers(data),
        "api_total": api_total,
        "api_activated": api_activated,
        "api_total_delta": (
            api_total - total if api_total is not None and total is not None else None
        ),
        "api_activated_delta": (
            api_activated - activated
            if api_activated is not None and activated is not None
            else None
        ),
    }

    if offer_list is not None and visible_offers is not None:
        attributes.update(
            {
                "listed_total": len(offer_list),
                "listed_visible": len(visible_offers),
                "listed_activated": sum(
                    1 for offer in visible_offers if offer.get("active") is True
                ),
                "listed_available": sum(
                    1 for offer in visible_offers if offer.get("active") is False
                ),
                "hidden_redeemed": len(offer_list) - len(visible_offers),
            }
        )

    attributes.update(_coupon_book_flash_offer_attributes(data))

    return _without_none(attributes)


def _coupon_book_flash_offer_attributes(data: dict[str, Any]) -> dict[str, Any]:
    """Return coupon book flash offer attributes."""
    offers = _coupon_book_flash_offer_list(data)
    if offers is None:
        return _without_none(
            {
                "coupon_book_error": data.get("coupon_book_offers_error"),
            }
        )

    return _without_none(
        {
            "coupon_book_total": _int_or_none(
                _nested(data, "coupon_book_offers", "totalOffersCount")
            ),
            "coupon_book_activated": _int_or_none(
                _nested(data, "coupon_book_offers", "activatedOffersCount")
            ),
            "coupon_book_total_points": _int_or_none(
                _nested(data, "coupon_book_offers", "totalPoints")
            ),
            "flash_total": len(offers),
            "flash_activated": sum(1 for offer in offers if offer.get("active") is True),
            "flash_available": sum(1 for offer in offers if offer.get("active") is False),
            "flash_offer_list": [_flash_offer_detail(offer) for offer in offers],
            "coupon_book_error": data.get("coupon_book_offers_error"),
        }
    )


def _flash_offer_detail(offer: dict[str, Any]) -> dict[str, Any]:
    """Return a structured coupon book flash offer detail."""
    return _without_none(
        {
            **_offer_detail(offer),
            "id": _clean_label(offer.get("id")),
            "active": offer.get("active"),
            "more_details": _clean_label(offer.get("moreDetails")),
            "activation_start_date": _clean_label(offer.get("activationStartDate")),
            "activation_end_date": _clean_label(offer.get("activationEndDate")),
            "redemption_start_date": _clean_label(offer.get("redemptionStartDate")),
            "redemption_end_date": _clean_label(offer.get("redemptionEndDate")),
        }
    )


def _visible_personal_offers(data: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Return personal offers that should be visible in the website offer list."""
    offers = _personal_offer_list(data)
    if offers is None:
        return None
    return [offer for offer in offers if offer.get("offerRedeemed") is not True]


def _personal_offer_list(data: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Return the detailed personal offer list when available."""
    offers = _nested(data, "personal_offers", "personalOfferList")
    if not isinstance(offers, list):
        return None
    return [offer for offer in offers if isinstance(offer, dict)]


def _coupon_book_flash_offer_list(data: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Return the detailed coupon book flash offer list when available."""
    offers = _nested(data, "coupon_book_offers", "flashOffers")
    if not isinstance(offers, list):
        return None
    return [offer for offer in offers if isinstance(offer, dict)]


def _int_or_none(value: Any) -> int | None:
    """Return a value as int when possible."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _without_none(data: dict[str, Any]) -> dict[str, Any]:
    """Drop attributes with unknown values."""
    return {key: value for key, value in data.items() if value is not None}


def _account_label(coordinator: DelhaizeDataUpdateCoordinator) -> str:
    """Return the best available label for one Delhaize account."""
    data = coordinator.data or {}
    customer = data.get("customer") if isinstance(data.get("customer"), dict) else {}

    full_name = " ".join(
        value
        for value in (
            _clean_label(customer.get("firstName")),
            _clean_label(customer.get("lastName")),
        )
        if value
    ).strip()
    if full_name:
        return full_name

    title = _clean_label(coordinator.config_entry.title)
    if title and title.lower() != NAME.lower():
        title_prefix = f"{NAME} "
        if title.lower().startswith(title_prefix.lower()):
            return title[len(title_prefix) :].strip() or title
        return title

    username = _clean_label(coordinator.config_entry.data.get(CONF_USERNAME))
    if username:
        return username

    for value in (
        customer.get("customerIdHash"),
        customer.get("uid"),
        coordinator.config_entry.unique_id,
    ):
        label = _clean_label(value)
        if label:
            return f"Account {label[-8:]}"

    return f"Account {coordinator.config_entry.entry_id[:8]}"


def _device_name(account_label: str) -> str:
    """Return a Home Assistant device name for one Delhaize account."""
    label = account_label.lower()
    if label == NAME.lower() or label.startswith(f"{NAME.lower()} "):
        return account_label
    return f"{NAME} {account_label}"


def _clean_label(value: Any) -> str | None:
    """Return a stripped non-empty string."""
    if value is None:
        return None
    label = str(value).strip()
    return label or None
