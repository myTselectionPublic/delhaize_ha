"""Sensors for the Delhaize integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import re
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
        attr_fn=lambda data: _offer_count_attributes(data),
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
        key="burnable_offer_discounts",
        name="Burnable offer discounts",
        icon="mdi:cart-percent",
        value_fn=lambda data: _burnable_offer_product_count(data),
        attr_fn=lambda data: _burnable_offer_discount_attributes(data),
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
            "promotion_type": _clean_label(offer.get("promotionType")),
            "available_until": _offer_available_until(offer),
            "product_discount_list": _personal_offer_product_discount_list_for_offer(
                offer
            ),
        }
    )


def _offer_count_attributes(data: dict[str, Any]) -> dict[str, Any]:
    """Return offer attributes used by the dashboard Markdown example."""
    return _without_none(
        {
            **_coupon_book_flash_offer_attributes(data),
            "personal_offer_product_discount_list": (
                _personal_offer_product_discount_list(data)
            ),
        }
    )


def _personal_offer_product_discount_list(
    data: dict[str, Any],
) -> list[dict[str, Any]] | None:
    """Return products earning personal-offer points and their value percentage."""
    offers = _visible_personal_offers(data)
    if offers is None:
        return None

    return [
        product
        for offer in offers
        for product in _personal_offer_product_discount_list_for_offer(offer)
    ]


def _personal_offer_product_discount_list_for_offer(
    offer: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return product value rows for one personal points offer."""
    offer_products = offer.get("products")
    if not isinstance(offer_products, list):
        return []

    points = _int_or_none(offer.get("points"))
    points_value = _points_to_euro(points)
    required_quantity = _personal_offer_required_quantity(offer)
    offer_label = (
        _clean_label(offer.get("name"))
        or _clean_label(offer.get("promotion"))
        or "Personal offer"
    )
    products: list[dict[str, Any]] = []
    for product in offer_products:
        if not isinstance(product, dict):
            continue
        price = product.get("price") if isinstance(product.get("price"), dict) else {}
        original_price_value = _price_amount(price.get("wasPrice"))
        if original_price_value is None:
            original_price_value = _price_amount(price.get("value"))
        qualifying_price_value = (
            round(original_price_value * required_quantity, 2)
            if original_price_value is not None
            else None
        )
        discount_percentage = _value_percentage(points_value, qualifying_price_value)
        products.append(
            _without_none(
                {
                    "offer": offer_label,
                    "active": offer.get("active"),
                    "product": _clean_label(product.get("name")),
                    "product_code": _clean_label(product.get("code")),
                    "points": points,
                    "points_value": points_value,
                    "required_quantity": (
                        required_quantity if required_quantity > 1 else None
                    ),
                    "qualifying_price_value": (
                        qualifying_price_value if required_quantity > 1 else None
                    ),
                    "original_price": _clean_label(price.get("wasPrice"))
                    or _clean_label(price.get("formattedValue")),
                    "original_price_value": original_price_value,
                    "discount_percentage": discount_percentage,
                    "discount_percentage_formatted": _format_percentage(
                        discount_percentage
                    ),
                }
            )
        )
    return products


def _personal_offer_required_quantity(offer: dict[str, Any]) -> int:
    """Return the item count required by labels such as '100 punten voor 2'."""
    labels = [
        value
        for value in (
            _clean_label(offer.get("name")),
            _clean_label(offer.get("promotion")),
            _clean_label(offer.get("promotionId")),
        )
        if value
    ]
    # Delhaize may split the reward and condition between name and promotion,
    # and their order differs between API responses and languages.
    label = " ".join(labels + list(reversed(labels)))
    quantity_condition = (
        r"(?:voor|pour|vanaf|d[eè]s|bij\s+(?:de\s+)?aankoop\s+van|"
        r"(?:a|à)\s+l['’]achat\s+de)\s+"
        r"(?:(?:minstens|minimum|au\s+moins)\s+)?(\d+)\b"
    )
    patterns = (
        rf"\b(?:punten|points?)\b.{{0,80}}?{quantity_condition}",
        rf"{quantity_condition}.{{0,80}}?\b(?:punten|points?)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, label, flags=re.IGNORECASE)
        if match is None:
            continue
        quantity = _int_or_none(match.group(1))
        if quantity is not None and quantity > 1:
            return quantity
    return 1


def _coupon_book_flash_offer_attributes(data: dict[str, Any]) -> dict[str, Any]:
    """Return coupon book flash offer attributes."""
    flash_offers = _coupon_book_flash_offer_list(data)
    personal_offers = _coupon_book_personal_offer_list(data)
    if flash_offers is None and personal_offers is None:
        return {}

    flash_offers = flash_offers or []
    personal_offers = personal_offers or []
    return _without_none(
        {
            "coupon_book_personal_offer_list": [
                _coupon_book_offer_detail(offer) for offer in personal_offers
            ],
            "flash_total": len(flash_offers),
            "flash_available": sum(1 for offer in flash_offers if offer.get("active") is False),
            "flash_offer_list": [_coupon_book_offer_detail(offer) for offer in flash_offers],
        }
    )


def _coupon_book_offer_detail(offer: dict[str, Any]) -> dict[str, Any]:
    """Return a structured coupon book offer detail."""
    return _without_none(
        {
            **_offer_detail(offer),
            "active": offer.get("active"),
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


def _coupon_book_personal_offer_list(data: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Return the detailed coupon book personal offer list when available."""
    offers = _nested(data, "coupon_book_offers", "personalOffers")
    if not isinstance(offers, list):
        return None
    return [offer for offer in offers if isinstance(offer, dict)]


def _burnable_offer_product_count(data: dict[str, Any]) -> int | None:
    """Return the number of burnable offer products with discount details."""
    products = _burnable_offer_discount_product_list(data)
    if products is None:
        return None
    return len(products)


def _burnable_offer_discount_attributes(data: dict[str, Any]) -> dict[str, Any]:
    """Return burnable offer products and their discount values."""
    products = _burnable_offer_discount_product_list(data)
    if products is None:
        return {}

    return _without_none(
        {
            "product_discount_list": products,
        }
    )


def _burnable_offer_discount_product_list(
    data: dict[str, Any],
) -> list[dict[str, Any]] | None:
    """Return burnable offer product rows with point and euro discount values."""
    offers = _burnable_offer_sources(data)
    if offers is None:
        return None

    products: list[dict[str, Any]] = []
    for offer in offers:
        if offer.get("error"):
            continue
        offer_products = offer.get("products")
        if not isinstance(offer_products, list):
            continue
        discount_points = _int_or_none(offer.get("priceToBurn"))
        point_price_value = _points_to_euro(discount_points)
        for product in offer_products:
            if not isinstance(product, dict):
                continue
            price = product.get("price") if isinstance(product.get("price"), dict) else {}
            original_price_value = _price_amount(price.get("wasPrice"))
            if original_price_value is None:
                original_price_value = _price_amount(price.get("value"))
            discount_percentage = _percentage(point_price_value, original_price_value)
            products.append(
                _without_none(
                    {
                        "offer": _clean_label(offer.get("name")),
                        "discount_points": discount_points,
                        "point_price_value": point_price_value,
                        "original_price_value": original_price_value,
                        "discount_percentage": discount_percentage,
                        "discount_percentage_formatted": _format_percentage(
                            discount_percentage
                        ),
                        "days_remaining": _int_or_none(offer.get("daysRemaining")),
                        "product": _clean_label(product.get("name")),
                        "original_price": _clean_label(price.get("wasPrice"))
                        or _clean_label(price.get("formattedValue")),
                    }
                )
            )
    return products


def _burnable_offer_sources(data: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Return burnable offers, preferring detailed range responses when available."""
    offer_list = _nested(data, "burnable_offers", "burnableOfferList")
    range_list = data.get("burnable_offer_ranges")
    if not isinstance(offer_list, list) and not isinstance(range_list, list):
        return None

    offers_by_id: dict[str, dict[str, Any]] = {}
    for offer in offer_list or []:
        if isinstance(offer, dict):
            offer_id = _clean_label(offer.get("id"))
            if offer_id:
                offers_by_id[offer_id] = offer
    for offer in range_list or []:
        if isinstance(offer, dict):
            offer_id = _clean_label(offer.get("id"))
            if offer_id:
                offers_by_id[offer_id] = offer
    return list(offers_by_id.values())


def _offer_available_until(offer: dict[str, Any]) -> str | None:
    """Return the best available end date or validity label for an offer."""
    for key in ("redemptionEndDate", "activationEndDate", "validity"):
        value = _clean_label(offer.get(key))
        if value:
            return value
    return None


def _points_to_euro(points: int | None) -> float | None:
    """Return the euro value of Delhaize points."""
    if points is None:
        return None
    return round(points * 0.01, 2)


def _percentage(value: float | None, total: float | None) -> float | None:
    """Return the saving when value is the discounted price and total is original."""
    if value is None or total in (None, 0):
        return None
    return round((1 - (value / total)) * 100, 1)


def _value_percentage(value: float | None, total: float | None) -> float | None:
    """Return a reward value as a percentage of the original price."""
    if value is None or total in (None, 0):
        return None
    return round((value / total) * 100, 1)


def _format_percentage(value: float | None) -> str | None:
    """Return a compact percentage label."""
    if value is None:
        return None
    if value.is_integer():
        return f"{int(value)}%"
    return f"{value}%"


def _int_or_none(value: Any) -> int | None:
    """Return a value as int when possible."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _price_amount(value: Any) -> float | None:
    """Return a price amount from numeric or formatted values."""
    if isinstance(value, int | float):
        return float(value)
    if value is None:
        return None

    label = str(value).strip()
    if not label:
        return None
    numeric = "".join(
        character
        for character in label
        if character.isdigit() or character in {",", ".", "-"}
    )
    if not numeric:
        return None

    if "," in numeric and "." in numeric:
        numeric = numeric.replace(".", "").replace(",", ".")
    elif "," in numeric:
        numeric = numeric.replace(",", ".")

    return _float_or_none(numeric)


def _float_or_none(value: Any) -> float | None:
    """Return a value as float when possible."""
    try:
        return float(value)
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
