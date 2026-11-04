from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, get_model_name
from .coordinator import OpenFirenetCoordinator


@dataclass(frozen=True, kw_only=True)
class OpenFirenetBinarySensorDescription(BinarySensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], bool | None]
    optional: bool = False


BINARY_SENSOR_TYPES: tuple[OpenFirenetBinarySensorDescription, ...] = (
    OpenFirenetBinarySensorDescription(
        key="connected",
        name="Connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn=lambda data: data.get("device", {}).get("connected", False),
    ),
    OpenFirenetBinarySensorDescription(
        key="burning",
        name="Combustion Active",
        icon="mdi:fire",
        value_fn=lambda data: data.get("stove", {}).get("is_burning", False),
    ),
    OpenFirenetBinarySensorDescription(
        key="problem",
        name="Stove Error",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda data: data.get("stove", {}).get("has_error", False),
    ),
    OpenFirenetBinarySensorDescription(
        key="hopper_lid",
        name="Pellet Hopper Lid",
        device_class=BinarySensorDeviceClass.DOOR,
        icon="mdi:tray-arrow-up",
        value_fn=lambda data: (
            data.get("raw_sensors", {}).get("hopperLidClosed") == 0
            if "hopperLidClosed" in data.get("raw_sensors", {})
            else bool(data.get("stove", {}).get("state_mask", 0) & 2)
        ),
    ),
    OpenFirenetBinarySensorDescription(
        key="eco_mode_possible",
        name="Eco Mode Possible",
        icon="mdi:leaf",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: bool(data.get("controls", {}).get("eco_mode_possible", False)),
    ),
    OpenFirenetBinarySensorDescription(
        key="pellet_low",
        name="Pellet Level Low",
        device_class=BinarySensorDeviceClass.PROBLEM,
        icon="mdi:alert-circle-outline",
        optional=True,
        value_fn=lambda data: bool(data.get("pellet", {}).get("low_level", False)) if data.get("pellet", {}).get("available") else None,
    ),
)


# On while schedule times were changed in Home Assistant and not sent to the stove yet (see schedule.py).
# Diagnostic and not configuration: Home Assistant does not accept a binary sensor in the configuration category.
SCHEDULE_PENDING = OpenFirenetBinarySensorDescription(
    key="schedule_pending",
    name="Schedule changes pending",
    icon="mdi:calendar-alert",
    entity_category=EntityCategory.DIAGNOSTIC,
    entity_registry_enabled_default=False,
    value_fn=lambda data: False,
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: OpenFirenetCoordinator = hass.data[DOMAIN][entry.entry_id]
    registry = er.async_get(hass)

    def _offered(description: OpenFirenetBinarySensorDescription) -> bool:
        if not description.optional or description.value_fn(coordinator.data) is not None:
            return True
        unique_id = f"{entry.entry_id}_{description.key}"
        return registry.async_get_entity_id("binary_sensor", DOMAIN, unique_id) is not None

    async_add_entities(
        [
            OpenFirenetBinarySensor(coordinator, entry, description)
            for description in BINARY_SENSOR_TYPES
            if _offered(description)
        ]
        + [OpenFirenetSchedulePendingSensor(coordinator, entry, SCHEDULE_PENDING)]
    )


class OpenFirenetBinarySensor(
    CoordinatorEntity[OpenFirenetCoordinator], BinarySensorEntity
):
    entity_description: OpenFirenetBinarySensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OpenFirenetCoordinator,
        entry: ConfigEntry,
        description: OpenFirenetBinarySensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._entry = entry
        self._attr_translation_key = description.key
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"

    @property
    def device_info(self) -> dict:
        device = self.coordinator.data.get("device", {})
        stove = self.coordinator.data.get("stove", {})
        model_name = stove.get("model_name") or get_model_name(stove.get("model"))
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": device.get("name", "Open Firenet"),
            "manufacturer": "Open Firenet",
            "model": f"RIKA {model_name}",
            "sw_version": f"Firmware v{device.get('version', '2.0.0')} (MB {stove.get('mainboard_version') or '?'})",
            "configuration_url": f"http://{self.coordinator.host}",
        }

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.coordinator.data)


class OpenFirenetSchedulePendingSensor(OpenFirenetBinarySensor):
    @property
    def is_on(self) -> bool:
        return self.coordinator.schedule_has_pending
