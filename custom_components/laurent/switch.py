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

    # Detect number of relays or default to 4 (Laurent-4 has 4, Laurent-5 has 4/8, Laurent-128 up to 128)
    relay_count = 4
    if "5" in coordinator.client.model:
        relay_count = 4
    elif "4" in coordinator.client.model:
        relay_count = 4
    elif coordinator.client.relays:
        relay_count = max(coordinator.client.relays.keys())

    entities = [
        LaurentRelaySwitch(coordinator, relay_num=i, entry=entry)
        for i in range(1, relay_count + 1)
    ]
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
        return self.coordinator.client.relays.get(self.relay_num, False)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn relay on."""
        await self.coordinator.client.set_relay(self.relay_num, True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn relay off."""
        await self.coordinator.client.set_relay(self.relay_num, False)
        self.async_write_ha_state()
