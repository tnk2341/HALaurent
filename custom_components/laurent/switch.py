"""Support for Laurent switches (Relays)."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
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
    """Set up Laurent switches from config entry."""
    coordinator: LaurentCoordinator = hass.data[DOMAIN][entry.entry_id]

    # Detect number of relays based on model
    # Laurent-5/5G has 4 relays, Laurent-2 has 2, Laurent-112 has 12, Laurent-128 has 28
    relay_count = 4
    if "2" in coordinator.client.model and "128" not in coordinator.client.model and "12" not in coordinator.client.model:
        relay_count = 2
    elif "112" in coordinator.client.model:
        relay_count = 12
    elif "128" in coordinator.client.model:
        relay_count = 28

    entities = [
        LaurentRelaySwitch(coordinator, relay_num=i, entry=entry)
        for i in range(1, relay_count + 1)
    ]

    # General Purpose IO Lines (IO1 - IO8) as controllable output switches
    io_count = 8
    if "4" in coordinator.client.model and "5" not in coordinator.client.model and "128" not in coordinator.client.model:
        io_count = 0
    elif "2" in coordinator.client.model and "128" not in coordinator.client.model and "12" not in coordinator.client.model:
        io_count = 4

    for i in range(1, io_count + 1):
        entities.append(LaurentIOSwitch(coordinator, line_num=i, entry=entry))

    async_add_entities(entities)


class LaurentRelaySwitch(CoordinatorEntity[LaurentCoordinator], SwitchEntity):
    """Representation of a Laurent relay."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: LaurentCoordinator,
        relay_num: int,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self.relay_num = relay_num
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_relay_{relay_num}"
        self._attr_name = f"Relay {relay_num}"
        self._attr_icon = "mdi:electric-switch"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device registry information."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self.coordinator.name,
            manufacturer="KernelChip",
            model=self.coordinator.client.model,
            sw_version=self.coordinator.client.firmware or "Unknown",
        )

    @property
    def is_on(self) -> bool:
        """Return true if relay is on."""
        if self.coordinator.data and "relays" in self.coordinator.data:
            val = self.coordinator.data["relays"].get(self.relay_num)
            if val is not None:
                return val
        return self.coordinator.client.relays.get(self.relay_num, False)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn relay on."""
        await self.coordinator.client.set_relay(self.relay_num, True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn relay off."""
        await self.coordinator.client.set_relay(self.relay_num, False)
        self.async_write_ha_state()


class LaurentIOSwitch(CoordinatorEntity[LaurentCoordinator], SwitchEntity):
    """Representation of an IO line (IO1 - IO8) on Laurent in output mode."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:toggle-switch"

    def __init__(
        self,
        coordinator: LaurentCoordinator,
        line_num: int,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self.line_num = line_num
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_io_output_{line_num}"
        self._attr_name = f"IO {line_num} Output"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device registry information."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self.coordinator.name,
            manufacturer="KernelChip",
            model=self.coordinator.client.model,
            sw_version=self.coordinator.client.firmware or "Unknown",
        )

    @property
    def is_on(self) -> bool:
        """Return true if IO line is HIGH."""
        if self.coordinator.data and "io_lines" in self.coordinator.data:
            val = self.coordinator.data["io_lines"].get(self.line_num)
            if val is not None:
                return bool(val)
        return bool(self.coordinator.client.io_lines.get(self.line_num, False))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Set IO line to HIGH (3.3V): $KE,IO,SET,<Line>,1."""
        await self.coordinator.client.set_io_line(self.line_num, True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Set IO line to LOW (0V): $KE,IO,SET,<Line>,0."""
        await self.coordinator.client.set_io_line(self.line_num, False)
        self.async_write_ha_state()
