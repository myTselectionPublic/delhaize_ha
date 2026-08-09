"""Standalone tests for Delhaize sensor value helpers."""

from __future__ import annotations

from dataclasses import dataclass
import importlib.util
from pathlib import Path
import sys
import types


def _module(name: str) -> types.ModuleType:
    """Create and register a module stub."""
    module = types.ModuleType(name)
    sys.modules[name] = module
    return module


def _load_sensor() -> types.ModuleType:
    """Load sensor.py without requiring a Home Assistant installation."""
    root = Path(__file__).resolve().parents[1]
    custom_components_path = root / "custom_components"
    integration_path = custom_components_path / "delhaize_ha"

    custom_components = sys.modules.setdefault(
        "custom_components", types.ModuleType("custom_components")
    )
    custom_components.__path__ = [str(custom_components_path)]
    package = types.ModuleType("custom_components.delhaize_ha")
    package.__path__ = [str(integration_path)]
    sys.modules["custom_components.delhaize_ha"] = package

    homeassistant = _module("homeassistant")
    homeassistant.__path__ = []
    components = _module("homeassistant.components")
    components.__path__ = []
    sensor_stub = _module("homeassistant.components.sensor")

    class SensorEntity:
        """Sensor entity stub."""

    @dataclass(frozen=True, kw_only=True)
    class SensorEntityDescription:
        """Sensor description stub."""

        key: str
        name: str | None = None
        icon: str | None = None

    sensor_stub.SensorEntity = SensorEntity
    sensor_stub.SensorEntityDescription = SensorEntityDescription

    config_entries = _module("homeassistant.config_entries")
    config_entries.ConfigEntry = object
    constants = _module("homeassistant.const")
    constants.CONF_USERNAME = "username"
    core = _module("homeassistant.core")
    core.HomeAssistant = object

    helpers = _module("homeassistant.helpers")
    helpers.__path__ = []
    entity = _module("homeassistant.helpers.entity")
    entity.DeviceInfo = dict
    entity_platform = _module("homeassistant.helpers.entity_platform")
    entity_platform.AddEntitiesCallback = object
    update_coordinator = _module("homeassistant.helpers.update_coordinator")

    class CoordinatorEntity:
        """Generic coordinator entity stub."""

        @classmethod
        def __class_getitem__(cls, item: object) -> type[CoordinatorEntity]:
            return cls

    update_coordinator.CoordinatorEntity = CoordinatorEntity
    util = _module("homeassistant.util")
    util.slugify = lambda value: value.lower().replace(" ", "_")

    coordinator = _module("custom_components.delhaize_ha.coordinator")
    coordinator.DelhaizeDataUpdateCoordinator = object

    spec = importlib.util.spec_from_file_location(
        "custom_components.delhaize_ha.sensor",
        integration_path / "sensor.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sensor = _load_sensor()


def test_personal_offer_products_use_points_value_over_original_price() -> None:
    """Personal point percentage should use wasPrice and one cent per point."""
    data = {
        "personal_offers": {
            "personalOfferList": [
                {
                    "id": "offer-1",
                    "name": "75 bonus points",
                    "active": False,
                    "points": 75,
                    "offerRedeemed": False,
                    "products": [
                        {
                            "code": "123",
                            "name": "Test product",
                            "price": {
                                "wasPrice": "€ 3,00",
                                "formattedValue": "€ 2,50",
                                "value": 2.5,
                            },
                        }
                    ],
                }
            ]
        }
    }

    assert sensor._personal_offer_product_discount_list(data) == [
        {
            "offer": "75 bonus points",
            "active": False,
            "product": "Test product",
            "product_code": "123",
            "points": 75,
            "points_value": 0.75,
            "original_price": "€ 3,00",
            "original_price_value": 3.0,
            "discount_percentage": 25.0,
            "discount_percentage_formatted": "25%",
        }
    ]


def test_personal_offer_products_fall_back_to_current_price() -> None:
    """The normal product value should be used when wasPrice is absent."""
    offer = {
        "name": "100 bonus points",
        "points": "100",
        "products": [
            {
                "name": "Second product",
                "price": {"formattedValue": "€ 4,00", "value": 4},
            }
        ],
    }

    assert sensor._personal_offer_product_discount_list_for_offer(offer)[0] == {
        "offer": "100 bonus points",
        "product": "Second product",
        "points": 100,
        "points_value": 1.0,
        "original_price": "€ 4,00",
        "original_price_value": 4.0,
        "discount_percentage": 25.0,
        "discount_percentage_formatted": "25%",
    }


def test_personal_offer_percentage_uses_required_item_quantity() -> None:
    """Points awarded for two items should be divided by both items' price."""
    offer = {
        "name": "100 punten voor 2",
        "points": 100,
        "products": [
            {
                "code": "multi-1",
                "name": "Multi-buy product",
                "price": {"formattedValue": "€ 4,00", "value": 4},
            }
        ],
    }

    assert sensor._personal_offer_product_discount_list_for_offer(offer)[0] == {
        "offer": "100 punten voor 2",
        "product": "Multi-buy product",
        "product_code": "multi-1",
        "points": 100,
        "points_value": 1.0,
        "required_quantity": 2,
        "qualifying_price_value": 8.0,
        "original_price": "€ 4,00",
        "original_price_value": 4.0,
        "discount_percentage": 12.5,
        "discount_percentage_formatted": "12.5%",
    }


def test_personal_offer_quantity_can_span_name_and_promotion() -> None:
    """The points and quantity text may be split and reversed across fields."""
    offer = {
        "name": "Bij aankoop van minstens 3 producten",
        "promotion": "150 punten",
        "points": 150,
        "products": [{"price": {"value": 2}}],
    }

    product = sensor._personal_offer_product_discount_list_for_offer(offer)[0]

    assert product["required_quantity"] == 3
    assert product["qualifying_price_value"] == 6.0
    assert product["discount_percentage"] == 25.0
