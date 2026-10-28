"""Support for Laurent pulse buttons (e.g. open door / trigger pulse)."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import LaurentCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Laurent buttons from config entry."""
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
        LaurentPulseButton(coordinator, relay_num=i, entry=entry)
        for i in range(1, relay_count + 1)
    ]
    async_add_entities(entities)


class LaurentPulseButton(ButtonEntity):
    """Button to trigger a short pulse on a relay (ideal for access control doors)."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:door-open"

    def __init__(
        self,
        coordinator: LaurentCoordinator,
        relay_num: int,
        entry: ConfigEntry,
    ) -> None:
        self.coordinator = coordinator
        self.relay_num = relay_num
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_pulse_relay_{relay_num}"
        self._attr_name = f"Open Door / Pulse Relay {relay_num}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self.coordinator.name,
            manufacturer="KernelChip",
            model=self.coordinator.client.model,
            sw_version=self.coordinator.client.firmware or "Unknown",
        )

    async def async_press(self) -> None:
        """Pulse the relay for 1 second."""
        await self.coordinator.client.pulse_relay(self.relay_num, duration_sec=1)
