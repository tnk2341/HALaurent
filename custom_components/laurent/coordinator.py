"""DataUpdateCoordinator for Laurent Integration."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import json
import logging
import re
from typing import Any
import urllib.parse

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
        clean_psw = self.client.password.strip('"\'')
        encoded_psw = urllib.parse.quote(clean_psw)
        url = f"http://{self.client.host}/json_sensor.cgi?psw={encoded_psw}"
        try:
            session = async_get_clientsession(self.hass)
            async with asyncio.timeout(5):
                resp = await session.get(url)
                if resp.status != 200:
                    _LOGGER.debug("Laurent HTTP API returned status %s for %s", resp.status, url)
                    return
                raw_text = await resp.text()
                data = json.loads(raw_text)
        except asyncio.TimeoutError:
            _LOGGER.debug("Laurent HTTP API request timed out (%s)", url)
            return
        except Exception as err:
            _LOGGER.debug("Laurent HTTP API request failed (%s): %s", url, err)
            return

        self._parse_http_telemetry(data)

    def _parse_http_telemetry(self, data: dict[str, Any]) -> None:
        """Parse JSON response from json_sensor.cgi and update client state.

        Supports Laurent-5/5G native format:
        - "fw": "L527"
        - "mac": "00:04:A3:61:F3:EF"
        - "rele": "0000" (4 mechanical relays)
        - "in": "111110" (6 opto-isolated inputs)
        - "adc": [["0","0.00"], ["599","2.05"], ...] (5 ADC channels [code, voltage])
        - "owi_temp": [["A","283FCF7662210609","Temperatura1","11.56"]] (1-Wire temps)
        - "impl_io": ["0", ..., "1239396", "1198769"] (Meter Pulse Lines on IO1-IO8)
        - "impl_in": ["0", ...] (Meter Pulse Lines on IN1-IN6)
        - "flm_io": [["0.00","0.00"], ..., ["0.00","50839.69"]] (Flow meters IO1-IO8 [rate, total])
        - "flm_in": [["0.00","0.00"], ...] (Flow meters IN1-IN6 [rate, total])
        - "uvars": ["0.00", ...] (User variables VAR1-VAR10)
        """
        if not isinstance(data, dict):
            return

        # Firmware and MAC
        if "fw" in data and not self.client.firmware:
            self.client.firmware = str(data["fw"])
        if "mac" in data and not self.client.mac:
            self.client.mac = str(data["mac"])

        # 1. Relays: "rele": "0000" or list of dicts
        rele_data = data.get("rele", data.get("relay", data.get("RELE")))
        if isinstance(rele_data, str) and re.match(r"^[01]+$", rele_data):
            for idx, ch in enumerate(rele_data, start=1):
                if idx <= self.client.max_relays:
                    self.client.relays[idx] = (ch == "1")
        elif isinstance(rele_data, list):
            for idx, item in enumerate(rele_data, start=1):
                if isinstance(item, dict):
                    r_id = int(item.get("id", idx))
                    if r_id <= self.client.max_relays:
                        self.client.relays[r_id] = bool(int(item.get("state", 0)))
                elif isinstance(item, (int, str)) and str(item).strip() in ("0", "1"):
                    if idx <= self.client.max_relays:
                        self.client.relays[idx] = (str(item).strip() == "1")

        # Clean up any stale relays exceeding hardware max
        for r_k in list(self.client.relays.keys()):
            if r_k > self.client.max_relays:
                self.client.relays.pop(r_k, None)

        # 2. Inputs: "in": "111110" or list
        in_data = data.get("in", data.get("input", data.get("IN")))
        if isinstance(in_data, str) and re.match(r"^[01]+$", in_data):
            for idx, ch in enumerate(in_data, start=1):
                self.client.inputs[idx] = (ch == "1")
        elif isinstance(in_data, list):
            for idx, item in enumerate(in_data, start=1):
                if isinstance(item, dict):
                    in_id = int(item.get("id", idx))
                    self.client.inputs[in_id] = bool(int(item.get("state", 0)))
                elif isinstance(item, (int, str)) and str(item).strip() in ("0", "1"):
                    self.client.inputs[idx] = (str(item).strip() == "1")

        # 3. ADC: [["0","0.00"], ["599","2.05"], ...]
        adc_data = data.get("adc", data.get("ADC"))
        if isinstance(adc_data, list):
            for idx, item in enumerate(adc_data, start=1):
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    try:
                        self.client.adc[idx] = float(item[1])
                    except (ValueError, TypeError):
                        pass
                elif isinstance(item, (int, float, str)):
                    try:
                        self.client.adc[idx] = float(item)
                    except (ValueError, TypeError):
                        pass
                elif isinstance(item, dict):
                    try:
                        ch = int(item.get("id", idx))
                        v = item.get("voltage", item.get("val", item.get("value")))
                        if v is not None:
                            self.client.adc[ch] = float(v)
                    except (ValueError, TypeError):
                        pass

        # 4. Temperature: "owi_temp": [["A","ROM","Name","11.56"]] or "tmp"
        temp_data = data.get("owi_temp", data.get("tmp", data.get("temp", data.get("1wt"))))
        if isinstance(temp_data, list):
            for idx, item in enumerate(temp_data, start=1):
                if isinstance(item, (list, tuple)) and item:
                    try:
                        temp_str = str(item[-1]).strip()
                        if temp_str not in ("---", "", "null", "None"):
                            self.client.temperatures[idx] = float(temp_str)
                    except (ValueError, TypeError):
                        pass
                elif isinstance(item, (int, float, str)):
                    try:
                        temp_str = str(item).strip()
                        if temp_str not in ("---", "", "null", "None"):
                            self.client.temperatures[idx] = float(temp_str)
                    except (ValueError, TypeError):
                        pass
                elif isinstance(item, dict):
                    try:
                        t_id = int(item.get("id", idx))
                        v = item.get("value", item.get("temp", item.get("temperature")))
                        if v is not None and str(v).strip() not in ("---", "", "null"):
                            self.client.temperatures[t_id] = float(v)
                    except (ValueError, TypeError):
                        pass

        # 5. Meter Pulse Lines: "impl_io" (IO1..IO8) and "impl_in" (IN1..IN6)
        impl_io = data.get(
            "impl_io",
            data.get("mpl_io", data.get("mpl", {}).get("io") if isinstance(data.get("mpl"), dict) else None)
        )
        if isinstance(impl_io, list):
            for pin, val in enumerate(impl_io, start=1):
                try:
                    if isinstance(val, dict):
                        cnt = val.get("count", val.get("cnt", val.get("val", 0)))
                    else:
                        cnt = val
                    self.client.mpl_io[pin] = int(cnt)
                except (ValueError, TypeError):
                    pass

        impl_in = data.get(
            "impl_in",
            data.get("mpl_in", data.get("mpl", {}).get("in") if isinstance(data.get("mpl"), dict) else None)
        )
        if isinstance(impl_in, list):
            for pin, val in enumerate(impl_in, start=1):
                try:
                    if isinstance(val, dict):
                        cnt = val.get("count", val.get("cnt", val.get("val", 0)))
                    else:
                        cnt = val
                    self.client.mpl_in[pin] = int(cnt)
                except (ValueError, TypeError):
                    pass

        # 6. Flow meters: "flm_io" (IO1..IO8) and "flm_in" (IN1..IN6)
        flm_io = data.get(
            "flm_io",
            data.get("flow_io", data.get("flow", {}).get("io") if isinstance(data.get("flow"), dict) else None)
        )
        if isinstance(flm_io, list):
            for pin, item in enumerate(flm_io, start=1):
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    try:
                        self.client.flow_io[pin] = {
                            "instant": float(item[0]),
                            "total": float(item[1]),
                        }
                    except (ValueError, TypeError):
                        pass
                elif isinstance(item, dict):
                    try:
                        self.client.flow_io[pin] = {
                            "instant": float(item.get("instant", item.get("rate", 0.0))),
                            "total": float(item.get("total", item.get("vol", 0.0))),
                        }
                    except (ValueError, TypeError):
                        pass

        flm_in = data.get(
            "flm_in",
            data.get("flow_in", data.get("flow", {}).get("in") if isinstance(data.get("flow"), dict) else None)
        )
        if isinstance(flm_in, list):
            for pin, item in enumerate(flm_in, start=1):
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    try:
                        self.client.flow_in[pin] = {
                            "instant": float(item[0]),
                            "total": float(item[1]),
                        }
                    except (ValueError, TypeError):
                        pass
                elif isinstance(item, dict):
                    try:
                        self.client.flow_in[pin] = {
                            "instant": float(item.get("instant", item.get("rate", 0.0))),
                            "total": float(item.get("total", item.get("vol", 0.0))),
                        }
                    except (ValueError, TypeError):
                        pass

        # 7. User Variables: "uvars": ["0.00", ...]
        uvars = data.get("uvars", data.get("variables", data.get("var")))
        if isinstance(uvars, list):
            for idx, val in enumerate(uvars, start=1):
                try:
                    self.client.variables[idx] = float(val)
                except (ValueError, TypeError):
                    pass

        _LOGGER.debug(
            "Laurent HTTP telemetry parsed: Relays=%s, Inputs=%s, ADC=%s, Temp=%s, MPL_IO=%s, FLOW_IO=%s",
            dict(self.client.relays),
            dict(self.client.inputs),
            dict(self.client.adc),
            dict(self.client.temperatures),
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
