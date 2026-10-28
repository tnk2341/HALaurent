"""Support for Laurent Event entities (Wiegand and Debounce signals)."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, EVENT_LAURENT_WIEGAND
from .coordinator import LaurentCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Laurent event entities."""
    coordinator: LaurentCoordinator = hass.data[DOMAIN][entry.entry_id]

    # Add Wiegand reader event entities (up to 4 readers on Laurent-4)
    entities = [
        LaurentWiegandEventEntity(coordinator, reader_id=i, entry=entry)
        for i in range(1, 5)
    ]
    async_add_entities(entities)


class LaurentWiegandEventEntity(CoordinatorEntity[LaurentCoordinator], EventEntity):
    """Event entity triggering on each Wiegand swipe/pulse."""

    _attr_has_entity_name = True
    _attr_event_types = ["card_read"]
    _attr_icon = "mdi:card-bulleted"

    def __init__(
        self,
        coordinator: LaurentCoordinator,
        reader_id: int,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self.reader_id = reader_id
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_wiegand_reader_event_{reader_id}"
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

    async def async_added_to_hass(self) -> None:
        """Register listener for Wiegand HA bus events."""
        await super().async_added_to_hass()

        @callback
        def _handle_wiegand_event(event: Any) -> None:
            data = event.data
            if data.get("host") == self.coordinator.client.host and data.get("reader") == self.reader_id:
                self._trigger_event(
                    "card_read",
                    {
                        "raw_uid": data.get("raw_uid"),
                        "formatted_uid": data.get("formatted_uid"),
                    },
                )
                self.async_write_ha_state()

        self.async_on_remove(
            self.hass.bus.async_listen(EVENT_LAURENT_WIEGAND, _handle_wiegand_event)
        )
