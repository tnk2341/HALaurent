"""Support for Laurent discrete inputs as binary sensors."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import LaurentCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Laurent binary sensors from config entry."""
    coordinator: LaurentCoordinator = hass.data[DOMAIN][entry.entry_id]

    # Typically Laurent-4 has 4 opto-isolated inputs, Laurent-5 has 4 or 6.
    input_count = 4
    if "5" in coordinator.client.model:
        input_count = 6
    elif coordinator.client.inputs:
        input_count = max(coordinator.client.inputs.keys())

    entities = [
        LaurentInputBinarySensor(coordinator, input_num=i, entry=entry)
        for i in range(1, input_count + 1)
    ]
    # Connectivity sensor
    entities.append(LaurentConnectivitySensor(coordinator, entry=entry))

    async_add_entities(entities)


class LaurentInputBinarySensor(CoordinatorEntity[LaurentCoordinator], BinarySensorEntity):
    """Representation of an opto-isolated or digital input on Laurent."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: LaurentCoordinator,
        input_num: int,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self.input_num = input_num
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_input_{input_num}"
        self._attr_name = f"Input {input_num}"
        self._attr_device_class = BinarySensorDeviceClass.OPENING
        self._attr_icon = "mdi:ray-vertex"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self.coordinator.name,
            manufacturer="KernelChip",
            model=self.coordinator.client.model,
            sw_version=self.coordinator.client.firmware or "Unknown",
        )

    @property
    def is_on(self) -> bool:
        """Return true if input is closed / high."""
        return self.coordinator.client.inputs.get(self.input_num, False)


class LaurentConnectivitySensor(CoordinatorEntity[LaurentCoordinator], BinarySensorEntity):
    """Diagnostic binary sensor for TCP connection status."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: LaurentCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_tcp_connectivity"
        self._attr_name = "TCP Connection"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self.coordinator.name,
            manufacturer="KernelChip",
            model=self.coordinator.client.model,
            sw_version=self.coordinator.client.firmware or "Unknown",
        )

    @property
    def is_on(self) -> bool:
        return self.coordinator.client.is_connected
