"""Support for Laurent sensors (ADC, Temperature, MPL, FLOW, Variables, Wiegand UID)."""
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

from .const import DOMAIN, CONF_WIEGAND_REVERSE
from .coordinator import LaurentCoordinator
from .laurent_client import hex_to_uid

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Laurent sensors from config entry."""
    coordinator: LaurentCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = []

    # 1. Wiegand Reader Sensors (1 on Laurent-5/2, 4 on Laurent-4)
    reader_count = 4 if "4" in coordinator.client.model else 1
    for r_id in range(1, reader_count + 1):
        entities.append(LaurentWiegandReaderSensor(coordinator, reader_id=r_id, entry=entry))

    # 2. ADC Channels: Laurent-5/5G has 5, Laurent-2 has 2, others default 5
    adc_count = 2 if "2" in coordinator.client.model else 5
    for ch in range(1, adc_count + 1):
        entities.append(LaurentADCSensor(coordinator, channel=ch, entry=entry))

    # 3. 1-Wire Temperature Sensors (Support up to 4 sensors)
    temp_count = 2 if "2" in coordinator.client.model else 4
    for s_id in range(1, temp_count + 1):
        entities.append(LaurentTempSensor(coordinator, sensor_id=s_id, entry=entry))

    # 4. MPL IO — Meter Pulse Lines on IO pins (IO1-IO8)
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


class LaurentWiegandReaderSensor(CoordinatorEntity[LaurentCoordinator], SensorEntity):
    """Sensor displaying the scanned card UID on a specific Wiegand Reader."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:smart-card-outline"

    def __init__(
        self,
        coordinator: LaurentCoordinator,
        reader_id: int,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self.reader_id = reader_id
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_wiegand_reader_{reader_id}"
        self._attr_name = f"Wiegand Reader {reader_id}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self.coordinator.name,
            manufacturer="KernelChip",
            model=self.coordinator.client.model,
            sw_version=self.coordinator.client.firmware or "Unknown",
        )

    def _get_reader_data(self) -> dict[str, Any]:
        """Fetch reader scan data from coordinator or client."""
        if self.coordinator.data and "wiegand_readers" in self.coordinator.data:
            r = self.coordinator.data["wiegand_readers"].get(self.reader_id)
            if r:
                return r
        r = self.coordinator.client.wiegand_readers.get(self.reader_id)
        if r:
            return r
        last_w = (self.coordinator.data or {}).get("last_wiegand", self.coordinator.client.last_wiegand)
        if last_w and last_w.get("reader") == self.reader_id:
            return last_w
        return {}

    @property
    def native_value(self) -> str | None:
        """Return the scanned card UID formatted with current transformation setting."""
        r_data = self._get_reader_data()
        raw_uid = r_data.get("raw_uid")
        if not raw_uid:
            return None

        reverse = self._entry.options.get(
            CONF_WIEGAND_REVERSE,
            self.coordinator.client.wiegand_reverse,
        )
        return hex_to_uid(raw_uid, reverse_bytes=reverse)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return detailed scan attributes (raw UID, reader ID, last scanned timestamp)."""
        r_data = self._get_reader_data()
        raw_uid = r_data.get("raw_uid")
        reverse = self._entry.options.get(
            CONF_WIEGAND_REVERSE,
            self.coordinator.client.wiegand_reverse,
        )
        return {
            "raw_uid": raw_uid,
            "formatted_uid": hex_to_uid(raw_uid, reverse_bytes=reverse) if raw_uid else None,
            "reader": self.reader_id,
            "last_scanned": r_data.get("timestamp"),
        }


class LaurentADCSensor(CoordinatorEntity[LaurentCoordinator], SensorEntity):
    """Analog-to-Digital Converter analog sensor (Voltage, Pressure, Temperature, Custom)."""

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: LaurentCoordinator, channel: int, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self.channel = channel
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_adc_{channel}"
        self._attr_name = f"ADC {channel}"

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return configured unit of measurement (e.g. V, ATM, bar, etc.)."""
        return self._entry.options.get(f"adc_{self.channel}_unit", "V")

    @property
    def device_class(self) -> SensorDeviceClass | None:
        """Return configured device class, or None to avoid restricting units."""
        dc_val = self._entry.options.get(f"adc_{self.channel}_device_class")
        if dc_val and dc_val != "none":
            try:
                return SensorDeviceClass(dc_val)
            except ValueError:
                return None
        return None

    @property
    def icon(self) -> str:
        """Return icon based on configured unit or device class."""
        unit = (self.native_unit_of_measurement or "").upper()
        if any(p in unit for p in ("ATM", "BAR", "PA", "PSI")):
            return "mdi:gauge"
        if "V" in unit:
            return "mdi:sine-wave"
        if any(t in unit for t in ("C", "F", "K", "°")):
            return "mdi:thermometer"
        if "%" in unit:
            return "mdi:percent"
        return "mdi:numeric"

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
