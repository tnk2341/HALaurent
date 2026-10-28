"""Support for Laurent sensors (ADC, Temperature, Counters, Wiegand UID)."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature, UnitOfElectricPotential
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
    """Set up Laurent sensors from config entry."""
    coordinator: LaurentCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = []

    # 1. Wiegand / RFID Last Scanned Card Sensor
    entities.append(LaurentWiegandSensor(coordinator, entry=entry))

    # 2. ADC Channels (Typically 4 on Laurent-5)
    adc_count = 4 if "5" in coordinator.client.model else 2
    for ch in range(1, adc_count + 1):
        entities.append(LaurentADCSensor(coordinator, channel=ch, entry=entry))

    # 3. 1-Wire Temperature Sensors (Support up to 4 sensors by default)
    for s_id in range(1, 5):
        entities.append(LaurentTempSensor(coordinator, sensor_id=s_id, entry=entry))

    # 4. Pulse counters on inputs (for metering / flow / debounce monitoring)
    counter_count = 4
    for cnt_id in range(1, counter_count + 1):
        entities.append(LaurentCounterSensor(coordinator, counter_id=cnt_id, entry=entry))

    # 5. User Variables (VAR1 - VAR10) for internal CAT calculations & thresholds
    for var_id in range(1, 11):
        entities.append(LaurentVariableSensor(coordinator, var_id=var_id, entry=entry))

    async_add_entities(entities)


class LaurentWiegandSensor(CoordinatorEntity[LaurentCoordinator], SensorEntity):
    """Sensor holding the last scanned RFID / Wiegand card UID."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:smart-card-outline"

    def __init__(self, coordinator: LaurentCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_wiegand_last_uid"
        self._attr_name = "Last Wiegand UID"

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
    def native_value(self) -> str | None:
        return self.coordinator.client.last_wiegand.get("formatted_uid")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "raw_uid": self.coordinator.client.last_wiegand.get("raw_uid"),
            "reader": self.coordinator.client.last_wiegand.get("reader"),
        }


class LaurentADCSensor(CoordinatorEntity[LaurentCoordinator], SensorEntity):
    """Analog-to-Digital Converter voltage sensor."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.VOLTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT

    def __init__(self, coordinator: LaurentCoordinator, channel: int, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self.channel = channel
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_adc_{channel}"
        self._attr_name = f"ADC {channel}"

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
    def native_value(self) -> float | None:
        return self.coordinator.client.adc.get(self.channel)


class LaurentTempSensor(CoordinatorEntity[LaurentCoordinator], SensorEntity):
    """1-Wire DS18B20 Temperature sensor."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator: LaurentCoordinator, sensor_id: int, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self.sensor_id = sensor_id
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_temp_{sensor_id}"
        self._attr_name = f"Temperature {sensor_id}"

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
    def native_value(self) -> float | None:
        return self.coordinator.client.temperatures.get(self.sensor_id)


class LaurentCounterSensor(CoordinatorEntity[LaurentCoordinator], SensorEntity):
    """Input Pulse Counter sensor for meter reading and debounce validation."""

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:counter"

    def __init__(self, coordinator: LaurentCoordinator, counter_id: int, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self.counter_id = counter_id
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_counter_{counter_id}"
        self._attr_name = f"Counter {counter_id}"

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
    def native_value(self) -> int | None:
        return self.coordinator.client.counters.get(self.counter_id, 0)


class LaurentVariableSensor(CoordinatorEntity[LaurentCoordinator], SensorEntity):
    """Sensor for Laurent internal user variable (VAR1 - VAR10 / ~UV~)."""

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:variable"

    def __init__(self, coordinator: LaurentCoordinator, var_id: int, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self.var_id = var_id
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_var_{var_id}"
        self._attr_name = f"Variable {var_id}"

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
    def native_value(self) -> float | None:
        return self.coordinator.client.variables.get(self.var_id)

