"""DataUpdateCoordinator for Laurent Integration."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN, EVENT_LAURENT_WIEGAND, EVENT_LAURENT_INPUT_CHANGED, EVENT_LAURENT_CAT_PUSH
from .laurent_client import LaurentClient

_LOGGER = logging.getLogger(__name__)


class LaurentCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Manages persistent TCP connection and synchronization with Laurent controller."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: LaurentClient,
        scan_interval_sec: int = 5,
        name: str = "Laurent",
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=name,
            update_interval=timedelta(seconds=scan_interval_sec),
        )
        self.client = client
        self._reconnect_task: asyncio.Task | None = None
        self._shutdown = False

        # Register callback for live socket push events
        self.client.register_callback(self._on_client_event)

    def _on_client_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Handle pushed messages directly from the Laurent TCP socket."""
        _LOGGER.debug("Laurent event received [%s]: %s", event_type, data)

        # 1. Fire Home Assistant Bus Events
        if event_type == "wiegand_read":
            self.hass.bus.async_fire(
                EVENT_LAURENT_WIEGAND,
                {
                    "device": self.name,
                    "host": self.client.host,
                    "reader": data.get("reader"),
                    "raw_uid": data.get("raw_uid"),
                    "formatted_uid": data.get("formatted_uid"),
                },
            )
        elif event_type == "input_changed":
            self.hass.bus.async_fire(
                EVENT_LAURENT_INPUT_CHANGED,
                {
                    "device": self.name,
                    "host": self.client.host,
                    "input": data.get("input"),
                    "state": data.get("state"),
                },
            )
        elif event_type == "cat_push":
            self.hass.bus.async_fire(
                EVENT_LAURENT_CAT_PUSH,
                {
                    "device": self.name,
                    "host": self.client.host,
                    **data,
                },
            )
        elif event_type == "connection_lost":
            _LOGGER.warning("Laurent (%s) connection lost. Scheduling auto-reconnect...", self.client.host)
            if not self._shutdown and (self._reconnect_task is None or self._reconnect_task.done()):
                self._reconnect_task = self.hass.async_create_task(self._reconnect_loop())

        # Notify Home Assistant entity listeners immediately
        self.async_set_updated_data(self._build_state_data())

    def _build_state_data(self) -> dict[str, Any]:
        """Aggregate current state snapshot for all entities."""
        return {
            "connected": self.client.is_connected,
            "model": self.client.model,
            "firmware": self.client.firmware,
            "relays": dict(self.client.relays),
            "inputs": dict(self.client.inputs),
            "adc": dict(self.client.adc),
            "temperatures": dict(self.client.temperatures),
            "counters": dict(self.client.counters),
            "variables": dict(self.client.variables),
            "cat_custom_data": dict(self.client.cat_custom_data),
            "last_wiegand": dict(self.client.last_wiegand),
        }

    async def _async_update_data(self) -> dict[str, Any]:
        """Periodic background refresh for slow analog sensors (ADC, 1-Wire, counters)."""
        if not self.client.is_connected:
            if not self._shutdown and (self._reconnect_task is None or self._reconnect_task.done()):
                self._reconnect_task = self.hass.async_create_task(self._reconnect_loop())
            return self._build_state_data()

        try:
            await self.client.refresh_states()
        except Exception as err:
            _LOGGER.error("Error refreshing Laurent states: %s", err)

        return self._build_state_data()

    async def _reconnect_loop(self) -> None:
        """Auto-reconnect with exponential backoff."""
        delay = 2
        while not self._shutdown and not self.client.is_connected:
            _LOGGER.debug("Attempting to reconnect to Laurent (%s) in %s seconds...", self.client.host, delay)
            await asyncio.sleep(delay)
            try:
                success = await self.client.connect()
                if success:
                    _LOGGER.info("Reconnected to Laurent at %s", self.client.host)
                    self.async_set_updated_data(self._build_state_data())
                    break
            except Exception as err:
                _LOGGER.debug("Reconnect failed: %s", err)
            delay = min(delay * 2, 60)

    async def async_shutdown(self) -> None:
        """Clean shutdown of TCP connection and background tasks."""
        self._shutdown = True
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
        self.client.unregister_callback(self._on_client_event)
        await self.client.disconnect()
