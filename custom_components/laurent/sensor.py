"""Support for Laurent sensors (ADC, Temperature, Counters, MPL, FLOW, Wiegand UID)."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature, UnitOfElectricPotential, UnitOfVolume
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

    # 2. ADC Channels: Laurent-5/5G has 5, Laurent-2 has 2, others default 5
    adc_count = 2 if "2" in coordinator.client.model else 5
    for ch in range(1, adc_count + 1):
        entities.append(LaurentADCSensor(coordinator, channel=ch, entry=entry))

    # 3. 1-Wire Temperature Sensors (Support up to 4 sensors)
    temp_count = 2 if "2" in coordinator.client.model else 4
    for s_id in range(1, temp_count + 1):
        entities.append(LaurentTempSensor(coordinator, sensor_id=s_id, entry=entry))

    # 4. Pulse counters on inputs (for metering / flow / debounce monitoring)
    counter_count = 2 if "2" in coordinator.client.model else 4
    for cnt_id in range(1, counter_count + 1):
        entities.append(LaurentCounterSensor(coordinator, counter_id=cnt_id, entry=entry))

    # 5. MPL IO — Meter Pulse Lines on IO pins (IO1-IO8)
    io_count = 8
    for pin in range(1, io_count + 1):
        entities.append(LaurentMPLSensor(coordinator, line_type="IO", pin=pin, entry=entry))

    # 6. MPL IN — Meter Pulse Lines on IN pins (IN1-IN6)
    in_count = 6
    for pin in range(1, in_count + 1):
        entities.append(LaurentMPLSensor(coordinator, line_type="IN", pin=pin, entry=entry))

    # 7. FLOW IO — Flow meters on IO pins (IO1-IO8): rate + total
    for pin in range(1, io_count + 1):
        entities.append(LaurentFlowRateSensor(coordinator, line_type="IO", pin=pin, entry=entry))
        entities.append(LaurentFlowTotalSensor(coordinator, line_type="IO", pin=pin, entry=entry))

    # 8. FLOW IN — Flow meters on IN pins (IN1-IN6): rate + total
    for pin in range(1, in_count + 1):
        entities.append(LaurentFlowRateSensor(coordinator, line_type="IN", pin=pin, entry=entry))
        entities.append(LaurentFlowTotalSensor(coordinator, line_type="IN", pin=pin, entry=entry))

    # 9. User Variables (VAR1 - VAR10) for internal CAT calculations & thresholds
    for var_id in range(1, 11):
        entities.append(LaurentVariableSensor(coordinator, var_id=var_id, entry=entry))

    # 10. CAT Webhook / Push parameters sensor
    entities.append(LaurentCatPushSensor(coordinator, entry=entry))

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
        if self.coordinator.data and "last_wiegand" in self.coordinator.data:
            val = self.coordinator.data["last_wiegand"].get("formatted_uid")
            if val is not None:
                return val
        return self.coordinator.client.last_wiegand.get("formatted_uid")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        last_w = (self.coordinator.data or {}).get("last_wiegand", self.coordinator.client.last_wiegand)
        return {
            "raw_uid": last_w.get("raw_uid"),
            "reader": last_w.get("reader"),
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
        if self.coordinator.data and "adc" in self.coordinator.data:
            val = self.coordinator.data["adc"].get(self.channel)
            if val is not None:
                return val
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
        if self.coordinator.data and "temperatures" in self.coordinator.data:
            val = self.coordinator.data["temperatures"].get(self.sensor_id)
            if val is not None:
                return val
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
        if self.coordinator.data and "counters" in self.coordinator.data:
            val = self.coordinator.data["counters"].get(self.counter_id)
            if val is not None:
                return val
        return self.coordinator.client.counters.get(self.counter_id, 0)


class LaurentMPLSensor(CoordinatorEntity[LaurentCoordinator], SensorEntity):
    """Meter Pulse Line (MPL) counter sensor for IO or IN pins."""

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:counter"
    _attr_native_unit_of_measurement = "pulses"

    def __init__(
        self,
        coordinator: LaurentCoordinator,
        line_type: str,
        pin: int,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self.line_type = line_type  # "IO" or "IN"
        self.pin = pin
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_mpl_{line_type.lower()}_{pin}"
        self._attr_name = f"MPL {line_type} {pin}"

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
        data_key = f"mpl_{self.line_type.lower()}"
        if self.coordinator.data and data_key in self.coordinator.data:
            val = self.coordinator.data[data_key].get(self.pin)
            if val is not None:
                return val
        source = self.coordinator.client.mpl_io if self.line_type == "IO" else self.coordinator.client.mpl_in
        return source.get(self.pin)


class LaurentFlowRateSensor(CoordinatorEntity[LaurentCoordinator], SensorEntity):
    """Flow meter instantaneous rate sensor (L/min) for IO or IN pins."""

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:water-pump"
    _attr_native_unit_of_measurement = "L/min"

    def __init__(
        self,
        coordinator: LaurentCoordinator,
        line_type: str,
        pin: int,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self.line_type = line_type
        self.pin = pin
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_flow_rate_{line_type.lower()}_{pin}"
        self._attr_name = f"Flow Rate {line_type} {pin}"

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
        data_key = f"flow_{self.line_type.lower()}"
        if self.coordinator.data and data_key in self.coordinator.data:
            flow = self.coordinator.data[data_key].get(self.pin)
            if flow is not None:
                return flow.get("instant")
        source = self.coordinator.client.flow_io if self.line_type == "IO" else self.coordinator.client.flow_in
        flow = source.get(self.pin)
        return flow.get("instant") if flow else None


class LaurentFlowTotalSensor(CoordinatorEntity[LaurentCoordinator], SensorEntity):
    """Flow meter total consumed volume sensor (L) for IO or IN pins."""

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_device_class = SensorDeviceClass.WATER
    _attr_icon = "mdi:gauge"
    _attr_native_unit_of_measurement = UnitOfVolume.LITERS

    def __init__(
        self,
        coordinator: LaurentCoordinator,
        line_type: str,
        pin: int,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self.line_type = line_type
        self.pin = pin
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_flow_total_{line_type.lower()}_{pin}"
        self._attr_name = f"Flow Total {line_type} {pin}"

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
        data_key = f"flow_{self.line_type.lower()}"
        if self.coordinator.data and data_key in self.coordinator.data:
            flow = self.coordinator.data[data_key].get(self.pin)
            if flow is not None:
                return flow.get("total")
        source = self.coordinator.client.flow_io if self.line_type == "IO" else self.coordinator.client.flow_in
        flow = source.get(self.pin)
        return flow.get("total") if flow else None


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
        if self.coordinator.data and "variables" in self.coordinator.data:
            val = self.coordinator.data["variables"].get(self.var_id)
            if val is not None:
                return val
        return self.coordinator.client.variables.get(self.var_id)


class LaurentCatPushSensor(CoordinatorEntity[LaurentCoordinator], SensorEntity):
    """Sensor showing last received CAT URL_RUN / webhook parameters."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:webhook"

    def __init__(self, coordinator: LaurentCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_cat_push_data"
        self._attr_name = "CAT Push Payload"

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
        cat_data = (self.coordinator.data or {}).get("cat_custom_data", self.coordinator.client.cat_custom_data)
        if not cat_data:
            return "Idle"
        return ", ".join(f"{k}={v}" for k, v in list(cat_data.items())[-3:])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return dict((self.coordinator.data or {}).get("cat_custom_data", self.coordinator.client.cat_custom_data))
