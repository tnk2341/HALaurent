"""DataUpdateCoordinator for Laurent Integration."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.helpers.aiohttp_client import async_get_clientsession

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
            "mpl_io": dict(self.client.mpl_io),
            "mpl_in": dict(self.client.mpl_in),
            "flow_io": {k: dict(v) for k, v in self.client.flow_io.items()},
            "flow_in": {k: dict(v) for k, v in self.client.flow_in.items()},
            "variables": dict(self.client.variables),
            "cat_custom_data": dict(self.client.cat_custom_data),
            "last_wiegand": dict(self.client.last_wiegand),
        }

    async def _async_update_data(self) -> dict[str, Any]:
        """Periodic background refresh for slow analog sensors (ADC, 1-Wire, counters).

        Uses both TCP commands and HTTP JSON API for comprehensive data.
        """
        if not self.client.is_connected:
            if not self._shutdown and (self._reconnect_task is None or self._reconnect_task.done()):
                self._reconnect_task = self.hass.async_create_task(self._reconnect_loop())
            # Even if TCP is down, try HTTP API
            await self._fetch_http_telemetry()
            return self._build_state_data()

        try:
            await self.client.refresh_states()
        except Exception as err:
            _LOGGER.error("Error refreshing Laurent states via TCP: %s", err)

        # Supplement with HTTP JSON API (more reliable for ADC, MPL, FLOW)
        await self._fetch_http_telemetry()

        return self._build_state_data()

    async def _fetch_http_telemetry(self) -> None:
        """Fetch full telemetry from Laurent HTTP API (json_sensor.cgi).

        This provides all ADC, MPL, FLOW, relay, input, and temperature data
        in a single reliable HTTP request, supplementing TCP polling.
        """
        try:
            session = async_get_clientsession(self.hass)
            url = f"http://{self.client.host}/json_sensor.cgi?psw={self.client.password}"
            async with asyncio.timeout(5):
                resp = await session.get(url)
                if resp.status != 200:
                    _LOGGER.debug("Laurent HTTP API returned status %s", resp.status)
                    return
                data = await resp.json(content_type=None)
        except asyncio.TimeoutError:
            _LOGGER.debug("Laurent HTTP API request timed out")
            return
        except Exception as err:
            _LOGGER.debug("Laurent HTTP API request failed: %s", err)
            return

        self._parse_http_telemetry(data)

    def _parse_http_telemetry(self, data: dict[str, Any]) -> None:
        """Parse JSON response from json_sensor.cgi and update client state.

        Expected JSON structure (Laurent-5/5G):
        {
            "adc": [{"id":1,"code":0,"voltage":"0.000"}, ...],
            "rele": [{"id":1,"state":0}, ...],
            "in": [{"id":1,"state":0}, ...],
            "tmp": [{"id":1,"value":"---"}, ...],
            "mpl_io": [{"id":1,"count":0}, ...],
            "mpl_in": [{"id":1,"count":0}, ...],
            "flow_io": [{"id":1,"instant":"0.000","total":"0.000"}, ...],
            "flow_in": [{"id":1,"instant":"0.000","total":"0.000"}, ...]
        }
        """
        if not isinstance(data, dict):
            return

        # ADC channels
        adc_list = data.get("adc")
        if isinstance(adc_list, list):
            for item in adc_list:
                if not isinstance(item, dict):
                    continue
                try:
                    ch_id = int(item.get("id", 0))
                    voltage = item.get("voltage")
                    if ch_id > 0 and voltage is not None:
                        self.client.adc[ch_id] = float(voltage)
                except (ValueError, TypeError):
                    pass

        # Relays
        rele_list = data.get("rele")
        if isinstance(rele_list, list):
            for item in rele_list:
                if not isinstance(item, dict):
                    continue
                try:
                    r_id = int(item.get("id", 0))
                    state = item.get("state")
                    if r_id > 0 and state is not None:
                        self.client.relays[r_id] = bool(int(state))
                except (ValueError, TypeError):
                    pass

        # Inputs
        in_list = data.get("in")
        if isinstance(in_list, list):
            for item in in_list:
                if not isinstance(item, dict):
                    continue
                try:
                    in_id = int(item.get("id", 0))
                    state = item.get("state")
                    if in_id > 0 and state is not None:
                        self.client.inputs[in_id] = bool(int(state))
                except (ValueError, TypeError):
                    pass

        # Temperature sensors
        tmp_list = data.get("tmp")
        if isinstance(tmp_list, list):
            for item in tmp_list:
                if not isinstance(item, dict):
                    continue
                try:
                    t_id = int(item.get("id", 0))
                    value = item.get("value")
                    if t_id > 0 and value is not None and str(value).strip() not in ("---", ""):
                        self.client.temperatures[t_id] = float(value)
                except (ValueError, TypeError):
                    pass

        # MPL IO (Meter Pulse Lines on IO pins)
        mpl_io_list = data.get("mpl_io")
        if isinstance(mpl_io_list, list):
            for item in mpl_io_list:
                if not isinstance(item, dict):
                    continue
                try:
                    pin = int(item.get("id", 0))
                    count = item.get("count")
                    if pin > 0 and count is not None:
                        self.client.mpl_io[pin] = int(count)
                except (ValueError, TypeError):
                    pass

        # MPL IN (Meter Pulse Lines on IN pins)
        mpl_in_list = data.get("mpl_in")
        if isinstance(mpl_in_list, list):
            for item in mpl_in_list:
                if not isinstance(item, dict):
                    continue
                try:
                    pin = int(item.get("id", 0))
                    count = item.get("count")
                    if pin > 0 and count is not None:
                        self.client.mpl_in[pin] = int(count)
                except (ValueError, TypeError):
                    pass

        # FLOW IO (Flow meters on IO pins)
        flow_io_list = data.get("flow_io")
        if isinstance(flow_io_list, list):
            for item in flow_io_list:
                if not isinstance(item, dict):
                    continue
                try:
                    pin = int(item.get("id", 0))
                    instant = item.get("instant")
                    total = item.get("total")
                    if pin > 0 and instant is not None and total is not None:
                        self.client.flow_io[pin] = {
                            "instant": float(instant),
                            "total": float(total),
                        }
                except (ValueError, TypeError):
                    pass

        # FLOW IN (Flow meters on IN pins)
        flow_in_list = data.get("flow_in")
        if isinstance(flow_in_list, list):
            for item in flow_in_list:
                if not isinstance(item, dict):
                    continue
                try:
                    pin = int(item.get("id", 0))
                    instant = item.get("instant")
                    total = item.get("total")
                    if pin > 0 and instant is not None and total is not None:
                        self.client.flow_in[pin] = {
                            "instant": float(instant),
                            "total": float(total),
                        }
                except (ValueError, TypeError):
                    pass

        _LOGGER.debug(
            "HTTP telemetry parsed: ADC=%s, MPL_IO=%s, FLOW_IO=%s",
            dict(self.client.adc),
            dict(self.client.mpl_io),
            {k: v for k, v in self.client.flow_io.items()},
        )

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
