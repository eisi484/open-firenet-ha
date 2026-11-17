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
        value_fn=lambda data: (bool(data.get("pellet", {}).get("low_level", False)) or data.get("stove", {}).get("error_code") == 1) if data.get("pellet", {}).get("available") else None,
    ),
    OpenFirenetBinarySensorDescription(
        key="pellet_empty",
        name="Pellet Hopper Empty",
        device_class=BinarySensorDeviceClass.PROBLEM,
        icon="mdi:alert-circle-outline",
        value_fn=lambda data: data.get("stove", {}).get("error_code") == 1,
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

    # Dynamic generic extension discovery for boolean/binary alerts
    known_ext_bin_keys: set[tuple[str, str]] = set()

    def _discover_binary_extensions() -> None:
        new_entities: list[BinarySensorEntity] = []
        extensions = coordinator.data.get("extensions")
        if isinstance(extensions, dict):
            for ext_id, ext_data in extensions.items():
                if not isinstance(ext_data, dict):
                    continue
                # If legacy pellet sensor already handles pellet_low, skip duplicate
                if ext_id == "pellet_level" and "pellet" in coordinator.data:
                    continue
                for val_key, val in ext_data.items():
                    if val_key in ("enabled", "running", "healthy", "status"):
                        continue
                    if isinstance(val, bool):
                        key_pair = (ext_id, val_key)
                        if key_pair not in known_ext_bin_keys:
                            known_ext_bin_keys.add(key_pair)
                            new_entities.append(
                                OpenFirenetExtensionBinarySensor(coordinator, entry, ext_id, val_key)
                            )
        if new_entities:
            async_add_entities(new_entities)

    _discover_binary_extensions()
    entry.async_on_unload(coordinator.async_add_listener(_discover_binary_extensions))


def _infer_binary_metadata(ext_id: str, val_key: str) -> dict[str, Any]:
    key = val_key.lower()
    if "problem" in key or "error" in key or "fault" in key:
        return {"device_class": BinarySensorDeviceClass.PROBLEM, "icon": "mdi:alert-circle"}
    if "low" in key:
        return {"device_class": BinarySensorDeviceClass.PROBLEM, "icon": "mdi:alert-circle-outline"}
    if "motion" in key:
        return {"device_class": BinarySensorDeviceClass.MOTION, "icon": "mdi:motion-sensor"}
    if "door" in key or "lid" in key or "window" in key or "open" in key:
        return {"device_class": BinarySensorDeviceClass.OPENING, "icon": "mdi:door"}
    return {"device_class": None, "icon": "mdi:check-circle-outline"}


class OpenFirenetExtensionBinarySensor(
    CoordinatorEntity[OpenFirenetCoordinator], BinarySensorEntity
):
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OpenFirenetCoordinator,
        entry: ConfigEntry,
        ext_id: str,
        val_key: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._ext_id = ext_id
        self._val_key = val_key

        ext_title = ext_id.replace("_", " ").title()
        val_title = val_key.replace("_", " ").title()
        self._attr_name = f"{ext_title} {val_title}"
        self._attr_unique_id = f"{entry.entry_id}_ext_bin_{ext_id}_{val_key}"

        meta = _infer_binary_metadata(ext_id, val_key)
        self._attr_device_class = meta.get("device_class")
        self._attr_icon = meta.get("icon")

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
    def available(self) -> bool:
        ext = self.coordinator.data.get("extensions", {}).get(self._ext_id)
        if not isinstance(ext, dict):
            return False
        return bool(ext.get("enabled", False) and ext.get("running", False) and ext.get("healthy", False))

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.data.get("extensions", {}).get(self._ext_id, {}).get(self._val_key, False))


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
