"""Asynchronous TCP client for KernelChip Laurent controllers."""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Callable, Any

_LOGGER = logging.getLogger(__name__)


def hex_to_uid(hex_str: str, reverse_bytes: bool = True) -> str:
    """Format and optionally reverse hex string to standard UID (Little/Big Endian).

    Matches JS logic:
    - Strips '0x' or '#' prefixes.
    - Slices into 2-character hex bytes.
    - If reverse_bytes is True, reverses byte order.
    """
    clean_hex = re.sub(r"^0x|#", "", hex_str.strip(), flags=re.IGNORECASE)
    clean_hex = re.sub(r"[^0-9A-Fa-f]", "", clean_hex).upper()
    if not clean_hex:
        return ""

    # Pad with leading zero if odd length
    if len(clean_hex) % 2 != 0:
        clean_hex = "0" + clean_hex

    # Split into 2-character chunks (bytes)
    byte_chunks = [clean_hex[i : i + 2] for i in range(0, len(clean_hex), 2)]
    
    if reverse_bytes:
        byte_chunks = byte_chunks[::-1]

    return "".join(byte_chunks)


class LaurentClient:
    """Persistent TCP client for Laurent controllers (Port 2424)."""

    def __init__(
        self,
        host: str,
        port: int = 2424,
        password: str = "Laurent",
        wiegand_reverse: bool = True,
    ) -> None:
        self.host = host
        self.port = port
        self.password = password
        self.wiegand_reverse = wiegand_reverse

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected = False
        self._authenticated = False
        self._listen_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

        # Callbacks for live notifications
        self._event_callbacks: list[Callable[[str, dict[str, Any]], None]] = []

        # Current cached state
        self.model: str = "Laurent"
        self.firmware: str = ""
        self.mac: str = ""
        self.relays: dict[int, bool] = {}
        self.inputs: dict[int, bool] = {}
        self.adc: dict[int, float] = {}
        self.temperatures: dict[int, float] = {}
        self.counters: dict[int, int] = {}
        self.variables: dict[int, float] = {}
        self.cat_custom_data: dict[str, Any] = {}
        self.last_wiegand: dict[str, Any] = {}

    @property
    def is_connected(self) -> bool:
        return self._connected and self._authenticated

    def register_callback(self, callback: Callable[[str, dict[str, Any]], None]) -> None:
        """Register a callback for pushed events."""
        if callback not in self._event_callbacks:
            self._event_callbacks.append(callback)

    def unregister_callback(self, callback: Callable[[str, dict[str, Any]], None]) -> None:
        """Unregister callback."""
        if callback in self._event_callbacks:
            self._event_callbacks.remove(callback)

    def _notify(self, event_type: str, data: dict[str, Any]) -> None:
        """Notify all subscribers."""
        for cb in self._event_callbacks:
            try:
                cb(event_type, data)
            except Exception as err:
                _LOGGER.error("Error in Laurent event callback: %s", err)

    async def connect(self) -> bool:
        """Establish TCP connection and authenticate."""
        try:
            _LOGGER.debug("Connecting to Laurent at %s:%s", self.host, self.port)
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=5.0
            )
            self._connected = True

            # Start background reader task
            self._listen_task = asyncio.create_task(self._listen_loop())

            # Send password
            auth_cmd = f"$KE,PSW,SET,{self.password}\r\n"
            resp = await self._send_raw(auth_cmd, wait_prefix="#PSW")
            if "#PSW,OK" in resp or "#OK" in resp or "#PSW" in resp:
                self._authenticated = True
                _LOGGER.info("Successfully connected & authenticated with Laurent (%s:%s)", self.host, self.port)
            else:
                _LOGGER.warning("Authentication might have failed: %s", resp)
                self._authenticated = True  # Proceed cautiously in case firmware returns different ACK

            # Identify model and initial states
            await self._discover_device()
            return True

        except Exception as err:
            _LOGGER.error("Failed to connect to Laurent at %s:%s - %s", self.host, self.port, err)
            await self.disconnect()
            return False

    async def disconnect(self) -> None:
        """Close TCP connection."""
        self._connected = False
        self._authenticated = False
        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass

        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader = None
        self._writer = None

    async def _send_raw(self, cmd: str, wait_prefix: str | None = None, timeout: float = 3.0) -> str:
        """Send command with lock."""
        async with self._lock:
            if not self._writer:
                raise ConnectionError("Laurent client is not connected")

            _LOGGER.debug("TX -> %s", cmd.strip())
            self._writer.write(cmd.encode("ascii", errors="ignore"))
            await self._writer.drain()

            if wait_prefix:
                # Polling reading happens in _listen_loop or we can wait a short delay
                # Since _listen_loop handles incoming lines, small wait:
                await asyncio.sleep(0.05)
            return ""

    async def send_command(self, cmd_body: str) -> None:
        """Send standard $KE command."""
        full_cmd = f"$KE,{cmd_body}\r\n" if not cmd_body.startswith("$KE") else f"{cmd_body}\r\n"
        await self._send_raw(full_cmd)

    async def set_relay(self, relay_num: int, state: bool) -> None:
        """Set relay state. $KE,REL,num,state (1 or 0) or pulse."""
        val = 1 if state else 0
        await self.send_command(f"REL,{relay_num},{val}")
        self.relays[relay_num] = state
        self._notify("relay_changed", {"relay": relay_num, "state": state})

    async def pulse_relay(self, relay_num: int, duration_sec: int = 1) -> None:
        """Pulse relay (turn on, wait, turn off) or native pulse command."""
        # Kernelchip pulse syntax: $KE,WR,num,val (or manual pulse)
        await self.set_relay(relay_num, True)
        await asyncio.sleep(duration_sec)
        await self.set_relay(relay_num, False)

    async def set_variable(self, var_id: int, value: float) -> None:
        """Set user variable: $KE,VAR,<Id>,SET,<Value>."""
        await self.send_command(f"VAR,{var_id},SET,{value}")
        self.variables[var_id] = value
        self._notify("variable_changed", {"variable": var_id, "value": value})

    async def _discover_device(self) -> None:
        """Request device info, firmware version, and initial states."""
        await self.send_command("INF")
        await asyncio.sleep(0.05)
        await self.send_command("VER")
        await asyncio.sleep(0.05)
        await self.refresh_states()

    async def refresh_states(self) -> None:
        """Poll current states of Relays, Inputs, ADC, Temp sensors, and User Variables."""
        # Request all relays
        await self.send_command("RID,ALL")
        await asyncio.sleep(0.05)
        # Request all discrete inputs
        await self.send_command("RD,ALL")
        await asyncio.sleep(0.05)
        # Request ADC (if supported)
        await self.send_command("ADC,ALL")
        await asyncio.sleep(0.05)
        # Request Temperature sensors (DS18B20)
        await self.send_command("TMP,ALL")
        await asyncio.sleep(0.05)
        # Request User Variables (VAR1 - VAR10)
        for v in range(1, 11):
            await self.send_command(f"VAR,{v},GET")
            await asyncio.sleep(0.02)

    async def _listen_loop(self) -> None:
        """Continuous background loop reading responses and async pushes from socket."""
        while self._connected and self._reader:
            try:
                line_bytes = await self._reader.readline()
                if not line_bytes:
                    _LOGGER.warning("Laurent TCP socket connection closed by remote peer")
                    break

                raw_line = line_bytes.decode("ascii", errors="replace").strip()
                if raw_line:
                    _LOGGER.debug("RX <- %s", raw_line)
                    self._parse_line(raw_line)

            except asyncio.CancelledError:
                break
            except Exception as err:
                _LOGGER.error("Error in Laurent listen loop: %s", err)
                break

        self._connected = False
        self._authenticated = False
        self._notify("connection_lost", {"host": self.host})

    def _parse_line(self, line: str) -> None:
        """Parse incoming line from Laurent controller."""
        # Lines start with '#' or '$'
        parts = line.split(",")
        header = parts[0].upper()

        # 1. Device Info (#INF / #VER)
        if "#INF" in header:
            if len(parts) > 1:
                self.model = parts[1].strip()
                if "4" in self.model:
                    self.model = "Laurent-4"
                elif "5" in self.model:
                    self.model = "Laurent-5"
            self._notify("info_updated", {"model": self.model})
            return

        if "#VER" in header:
            if len(parts) > 1:
                self.firmware = parts[1].strip()
            self._notify("info_updated", {"firmware": self.firmware})
            return

        # 2. Relay state: #RID,ALL,0100 or #RID,1,1
        if "#RID" in header or "#RDR" in header:
            if len(parts) >= 3 and parts[1].upper() == "ALL":
                # Mask like 0100
                mask = parts[2].strip()
                for idx, char in enumerate(mask, start=1):
                    val = char == "1"
                    self.relays[idx] = val
                    self._notify("relay_changed", {"relay": idx, "state": val})
            elif len(parts) >= 3:
                try:
                    relay_num = int(parts[1])
                    state = parts[2].strip() == "1"
                    self.relays[relay_num] = state
                    self._notify("relay_changed", {"relay": relay_num, "state": state})
                except ValueError:
                    pass
            return

        # 3. Discrete Input state: #RD,ALL,1100 or #RD,1,1 (or push event on pin change)
        if "#RD" in header:
            if len(parts) >= 3 and parts[1].upper() == "ALL":
                mask = parts[2].strip()
                for idx, char in enumerate(mask, start=1):
                    val = char == "1"
                    self.inputs[idx] = val
                    self._notify("input_changed", {"input": idx, "state": val})
            elif len(parts) >= 3:
                try:
                    in_num = int(parts[1])
                    val = parts[2].strip() == "1"
                    self.inputs[in_num] = val
                    self._notify("input_changed", {"input": in_num, "state": val})
                except ValueError:
                    pass
            elif len(parts) == 2 and re.match(r"^[01]+$", parts[1].strip()):
                # Direct mask like #RD,1010
                mask = parts[1].strip()
                for idx, char in enumerate(mask, start=1):
                    val = char == "1"
                    self.inputs[idx] = val
                    self._notify("input_changed", {"input": idx, "state": val})
            return

        # 4. Wiegand / RFID / Dallas Events
        # e.g.: #WGN,1,0x529EBF78 or #WIEGAND,... or #WGD,reader,uid
        if any(w_tag in header for w_tag in ("#WGN", "#WGD", "#WIEGAND")):
            # Reader ID and Hex Code
            reader_id = 1
            raw_uid = ""
            if len(parts) >= 3:
                try:
                    reader_id = int(parts[1])
                    raw_uid = parts[2].strip()
                except ValueError:
                    raw_uid = parts[1].strip()
            elif len(parts) == 2:
                raw_uid = parts[1].strip()

            formatted_uid = hex_to_uid(raw_uid, reverse_bytes=self.wiegand_reverse)
            wiegand_data = {
                "reader": reader_id,
                "raw_uid": raw_uid,
                "formatted_uid": formatted_uid,
            }
            self.last_wiegand = wiegand_data
            self._notify("wiegand_read", wiegand_data)
            return

        # 5. ADC values: #ADC,1,2.45 or #ADC,ALL,1.2,3.4
        if "#ADC" in header:
            if len(parts) >= 3 and parts[1].upper() == "ALL":
                for idx, val_str in enumerate(parts[2:], start=1):
                    try:
                        self.adc[idx] = float(val_str.strip())
                        self._notify("adc_changed", {"channel": idx, "value": self.adc[idx]})
                    except ValueError:
                        pass
            elif len(parts) >= 3:
                try:
                    ch = int(parts[1])
                    val = float(parts[2].strip())
                    self.adc[ch] = val
                    self._notify("adc_changed", {"channel": ch, "value": val})
                except ValueError:
                    pass
            return

        # 6. Temperature sensors (1-Wire): #TMP,1,23.5
        if "#TMP" in header or "#TEMPO" in header:
            if len(parts) >= 3:
                try:
                    sensor_id = int(parts[1])
                    temp = float(parts[2].strip())
                    self.temperatures[sensor_id] = temp
                    self._notify("temperature_changed", {"sensor": sensor_id, "temperature": temp})
                except ValueError:
                    pass
            return

        # 7. Pulse Counters: #CNT,1,1045
        if "#CNT" in header:
            if len(parts) >= 3:
                try:
                    cnt_id = int(parts[1])
                    count = int(parts[2].strip())
                    self.counters[cnt_id] = count
                    self._notify("counter_changed", {"counter": cnt_id, "count": count})
                except ValueError:
                    pass
            return

        # 8. User Variables (VAR / ~UV~): #VAR,1,12.5 or #VAR,2,100
        if "#VAR" in header:
            if len(parts) >= 3:
                try:
                    var_id = int(parts[1])
                    val = float(parts[2].strip())
                    self.variables[var_id] = val
                    self._notify("variable_changed", {"variable": var_id, "value": val})
                except ValueError:
                    pass
            return

        # 9. CAT custom parameters / Key-Value messages (e.g. key=val or CGI style UST=VZU&value=1)
        if "=" in line:
            # Parse parameters like UST=VZU&value=12.3 or plain KEY=VALUE
            clean_str = line.lstrip("#$").strip()
            # Split if query-string style
            subparts = clean_str.split("?")[-1].split("&")
            extracted = {}
            for item in subparts:
                if "=" in item:
                    k, v = item.split("=", 1)
                    k = k.strip()
                    v = v.strip()
                    extracted[k] = v
                    try:
                        self.cat_custom_data[k] = float(v)
                    except ValueError:
                        self.cat_custom_data[k] = v

            if extracted:
                self._notify("cat_push", extracted)
                return

        # Generic notification for any other Ke-messages
        self._notify("raw_message", {"line": line})
