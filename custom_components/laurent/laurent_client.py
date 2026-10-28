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
        self.mpl_io: dict[int, int] = {}
        self.mpl_in: dict[int, int] = {}
        self.flow_io: dict[int, dict[str, float]] = {}
        self.flow_in: dict[int, dict[str, float]] = {}
        self.variables: dict[int, float] = {}
        self.cat_custom_data: dict[str, Any] = {}
        self.last_wiegand: dict[str, Any] = {}
        self._ow_rom_map: dict[str, int] = {}

    @property
    def max_relays(self) -> int:
        """Maximum number of mechanical relays supported by this hardware model."""
        if "2" in self.model and "128" not in self.model and "12" not in self.model:
            return 2
        if "112" in self.model:
            return 12
        if "128" in self.model:
            return 28
        return 4  # Laurent-5, Laurent-5G, Laurent-4 default to 4 relays

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
        for attempt in range(2):
            try:
                _LOGGER.debug("Connecting to Laurent at %s:%s (attempt %d)", self.host, self.port, attempt + 1)
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port), timeout=5.0
                )
                self._connected = True
                break
            except Exception as err:
                if attempt == 0:
                    _LOGGER.debug("First connect attempt failed (%s), retrying in 1s...", err)
                    await asyncio.sleep(1.0)
                else:
                    _LOGGER.error("Failed to connect to Laurent at %s:%s - %s", self.host, self.port, err)
                    await self.disconnect()
                    return False

        try:
            # Start background reader task
            self._listen_task = asyncio.create_task(self._listen_loop())

            # Send password
            auth_cmd = f"$KE,PSW,SET,{self.password}\r\n"
            await self._send_raw(auth_cmd)
            await asyncio.sleep(0.2)
            self._authenticated = True
            _LOGGER.info("Connected to Laurent (%s:%s)", self.host, self.port)

            # Enable streaming of Ke-messages to TCP Server
            for msg_code in ("EIN", "EIOI", "EREL", "RELE", "IN", "ADCV", "1WT", "ECAT", "RFID"):
                await self.send_command(f"MSG,S,{msg_code},SET,ON")
                await asyncio.sleep(0.02)

            # Identify model and initial states
            await self._discover_device()
            # Wait for responses to arrive over socket
            await asyncio.sleep(0.6)
            return True

        except Exception as err:
            _LOGGER.error("Error during Laurent initialization (%s:%s): %s", self.host, self.port, err)
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
                _LOGGER.debug("Cannot send '%s': Laurent client is not connected", cmd.strip())
                return ""

            _LOGGER.debug("TX -> %s", cmd.strip())
            try:
                self._writer.write(cmd.encode("ascii", errors="ignore"))
                await self._writer.drain()
            except Exception as err:
                _LOGGER.warning("Error writing to Laurent socket: %s", err)
                return ""

            if wait_prefix:
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
        await asyncio.sleep(0.1)
        await self.send_command("VER")
        await asyncio.sleep(0.1)
        await self.refresh_states()

    async def refresh_states(self) -> None:
        """Poll current states of Relays, Inputs, ADC, Temp sensors, and User Variables."""
        # 1. Relays (RDR for mechanical relays on Laurent-2/4/5/128)
        # Note: Do not poll RID here — RID is for discrete transistor outputs (OUT1-OUT5)
        await self.send_command("RDR,ALL")
        await asyncio.sleep(0.08)

        # 2. Discrete inputs
        await self.send_command("RD,ALL")
        await asyncio.sleep(0.08)

        # 3. Temperature: $KE,TMP works on all single-sensor boards; $KE,TMP,ALL on multi-sensor
        await self.send_command("TMP")
        await asyncio.sleep(0.08)
        await self.send_command("TMP,ALL")
        await asyncio.sleep(0.08)

        # 4. ADC channels: poll ALL, RAW and channels 1-5
        await self.send_command("ADC,ALL")
        await asyncio.sleep(0.06)
        await self.send_command("ADC,RAW")
        await asyncio.sleep(0.06)
        for ch in range(1, 6):
            await self.send_command(f"ADC,{ch}")
            await asyncio.sleep(0.04)

        # 5. Pulse counters (CNT 1-4) & Meter Pulse Lines (MPL IO 1-8)
        for cnt in range(1, 5):
            await self.send_command(f"CNT,{cnt}")
            await asyncio.sleep(0.03)
        for io_pin in range(1, 9):
            await self.send_command(f"MPL,IO,{io_pin}")
            await asyncio.sleep(0.03)

        # 6. Flow meters (FLOW IO 1-8)
        for io_pin in range(1, 9):
            await self.send_command(f"FLOW,IO,{io_pin}")
            await asyncio.sleep(0.03)

        # 7. User Variables (VAR1 - VAR10)
        for v in range(1, 11):
            await self.send_command(f"VAR,{v},GET")
            await asyncio.sleep(0.03)

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
        data_parts = parts[1:]

        # 0. Auth response
        if "#PSW" in header:
            if "OK" in line.upper():
                self._authenticated = True
                _LOGGER.info("Laurent password accepted")
            elif "ERR" in line.upper():
                _LOGGER.error("Laurent password rejected: check configured password in HA!")
            return

        # 0.1. Ke-Messages: #M,<MsgName>,<Parameters...>
        if header == "#M":
            if not data_parts:
                return
            msg_name = data_parts[0].upper().strip()
            msg_params = data_parts[1:]

            # A. #M,EIN,<Line>,<Value> (Discrete inputs push event)
            # B. #M,EIOI,<Line>,<Value> (IO pin event)
            if msg_name in ("EIN", "EIOI"):
                if len(msg_params) >= 2:
                    try:
                        line_num = int(msg_params[0].strip())
                        state = msg_params[1].strip() == "1"
                        self.inputs[line_num] = state
                        self._notify("input_changed", {"input": line_num, "state": state})
                    except ValueError:
                        pass
                return

            # C. #M,IN,<Mask> (Periodic discrete inputs status e.g. 000001)
            if msg_name == "IN":
                if msg_params:
                    val_str = msg_params[0].strip()
                    if re.match(r"^[01]+$", val_str):
                        for idx, ch in enumerate(val_str, start=1):
                            val = ch == "1"
                            self.inputs[idx] = val
                            self._notify("input_changed", {"input": idx, "state": val})
                return

            # D. #M,EREL,<Relay>,<Value> (Relay event)
            if msg_name == "EREL":
                if len(msg_params) >= 2:
                    try:
                        relay_num = int(msg_params[0].strip())
                        if relay_num <= self.max_relays:
                            state = msg_params[1].strip() == "1"
                            self.relays[relay_num] = state
                            self._notify("relay_changed", {"relay": relay_num, "state": state})
                    except ValueError:
                        pass
                return

            # E. #M,RELE,<Mask> (Periodic relay status e.g. 0100)
            if msg_name == "RELE":
                if msg_params:
                    val_str = msg_params[0].strip()
                    if re.match(r"^[01]+$", val_str):
                        for idx, ch in enumerate(val_str, start=1):
                            if idx <= self.max_relays:
                                val = ch == "1"
                                self.relays[idx] = val
                                self._notify("relay_changed", {"relay": idx, "state": val})
                return

            # F. #M,ADCV,<val1>,<val2>,<val3>,<val4>... (ADC voltages)
            if msg_name in ("ADCV", "ADC"):
                for idx, p in enumerate(msg_params, start=1):
                    try:
                        val = float(p.strip())
                        self.adc[idx] = val
                        self._notify("adc_changed", {"channel": idx, "value": val})
                    except ValueError:
                        pass
                return

            # G. #M,1WT,<SerialOrIndex>,<Temp> (1-Wire temperatures)
            if msg_name in ("1WT", "TMP", "ETMP"):
                if len(msg_params) == 1:
                    try:
                        val = float(msg_params[0].strip())
                        self.temperatures[1] = val
                        self._notify("temperature_changed", {"sensor": 1, "temperature": val})
                    except ValueError:
                        pass
                elif len(msg_params) >= 2:
                    sensor_key = msg_params[0].strip()
                    try:
                        temp_val = float(msg_params[1].strip())
                        if sensor_key.isdigit():
                            s_id = int(sensor_key)
                        else:
                            if sensor_key not in self._ow_rom_map:
                                self._ow_rom_map[sensor_key] = len(self._ow_rom_map) + 1
                            s_id = self._ow_rom_map[sensor_key]
                        self.temperatures[s_id] = temp_val
                        self._notify("temperature_changed", {"sensor": s_id, "temperature": temp_val})
                    except ValueError:
                        pass
                return

            # H. #M,RFID or #M,WGN or #M,EWGN or #M,IBUT
            if any(k in msg_name for k in ("RFID", "WGN", "IBUT")):
                raw_uid = msg_params[-1].strip() if msg_params else ""
                reader_id = 1
                if len(msg_params) >= 2 and msg_params[0].strip().isdigit():
                    reader_id = int(msg_params[0].strip())
                formatted_uid = hex_to_uid(raw_uid, reverse_bytes=self.wiegand_reverse)
                w_data = {
                    "reader": reader_id,
                    "raw_uid": raw_uid,
                    "formatted_uid": formatted_uid,
                }
                self.last_wiegand = w_data
                self._notify("wiegand_read", w_data)
                return

            # I. #M,ECAT
            if msg_name == "ECAT":
                cat_dict = {}
                for idx, p in enumerate(msg_params, start=1):
                    if "=" in p:
                        k, v = p.split("=", 1)
                        cat_dict[k.strip()] = v.strip()
                    else:
                        cat_dict[f"param_{idx}"] = p.strip()
                self._notify("cat_push", cat_dict)
                return

            # J. #M,MPL,IO,<pin>,<count> or #M,MPL,IN,<pin>,<count>
            if msg_name == "MPL":
                if len(msg_params) >= 3:
                    line_type = msg_params[0].strip().upper()
                    try:
                        pin = int(msg_params[1].strip())
                        count = int(msg_params[2].strip())
                        if line_type == "IO":
                            self.mpl_io[pin] = count
                        elif line_type == "IN":
                            self.mpl_in[pin] = count
                        self._notify("mpl_changed", {"type": line_type, "pin": pin, "count": count})
                    except ValueError:
                        pass
                return

            # K. #M,FLOW,IO,<pin>,<instant>,<total> or #M,FLOW,IN,<pin>,<instant>,<total>
            if msg_name == "FLOW":
                if len(msg_params) >= 4:
                    line_type = msg_params[0].strip().upper()
                    try:
                        pin = int(msg_params[1].strip())
                        instant = float(msg_params[2].strip())
                        total = float(msg_params[3].strip())
                        flow_data = {"instant": instant, "total": total}
                        if line_type == "IO":
                            self.flow_io[pin] = flow_data
                        elif line_type == "IN":
                            self.flow_in[pin] = flow_data
                        self._notify("flow_changed", {"type": line_type, "pin": pin, **flow_data})
                    except ValueError:
                        pass
                return
            return

        # 1. Device Info (#INF / #VER)
        if "#INF" in header:
            if len(parts) > 1:
                raw_model = parts[1].strip()
                self.model = raw_model
                if "128" in raw_model:
                    self.model = "Laurent-128"
                elif "112" in raw_model:
                    self.model = "Laurent-112"
                elif "5" in raw_model:
                    self.model = "Laurent-5"
                elif "4" in raw_model:
                    self.model = "Laurent-4"
                elif "2" in raw_model:
                    self.model = "Laurent-2"
            if len(parts) > 2:
                self.firmware = parts[2].strip()
            self._notify("info_updated", {"model": self.model, "firmware": self.firmware})
            return

        if "#VER" in header:
            if len(parts) > 1:
                self.firmware = parts[1].strip()
            self._notify("info_updated", {"firmware": self.firmware})
            return

        # 2. Relay state: #RDR,ALL,0100 or #RDR,01 or #RDR,ALL,0,1 or #RDR,1,1
        # Only parse mechanical relays (#RDR, #REL), never #RID (which is for OUT lines)
        if any(h in header for h in ("#RDR", "#REL")):
            r_parts = list(data_parts)
            if r_parts and r_parts[0].upper() == "ALL":
                r_parts = r_parts[1:]
            if len(r_parts) == 1:
                val_str = r_parts[0].strip()
                if re.match(r"^[01]+$", val_str):
                    for idx, ch in enumerate(val_str, start=1):
                        if idx <= self.max_relays:
                            val = ch == "1"
                            self.relays[idx] = val
                            self._notify("relay_changed", {"relay": idx, "state": val})
            elif len(r_parts) > 1:
                if all(p.strip() in ("0", "1") for p in r_parts):
                    for idx, p in enumerate(r_parts, start=1):
                        if idx <= self.max_relays:
                            val = p.strip() == "1"
                            self.relays[idx] = val
                            self._notify("relay_changed", {"relay": idx, "state": val})
                else:
                    try:
                        r_id = int(r_parts[0].strip())
                        if r_id <= self.max_relays:
                            val = r_parts[1].strip() == "1"
                            self.relays[r_id] = val
                            self._notify("relay_changed", {"relay": r_id, "state": val})
                    except ValueError:
                        pass
            return

        # 3. Discrete Input state: #RD,ALL,10 or #RD,10 or #RD,ALL,1,0 or #RD,1,1
        if "#RD" in header:
            in_parts = list(data_parts)
            if in_parts and in_parts[0].upper() == "ALL":
                in_parts = in_parts[1:]
            if len(in_parts) == 1:
                val_str = in_parts[0].strip()
                if re.match(r"^[01]+$", val_str):
                    for idx, ch in enumerate(val_str, start=1):
                        val = ch == "1"
                        self.inputs[idx] = val
                        self._notify("input_changed", {"input": idx, "state": val})
            elif len(in_parts) > 1:
                if all(p.strip() in ("0", "1") for p in in_parts):
                    for idx, p in enumerate(in_parts, start=1):
                        val = p.strip() == "1"
                        self.inputs[idx] = val
                        self._notify("input_changed", {"input": idx, "state": val})
                else:
                    try:
                        in_num = int(in_parts[0].strip())
                        val = in_parts[1].strip() == "1"
                        self.inputs[in_num] = val
                        self._notify("input_changed", {"input": in_num, "state": val})
                    except ValueError:
                        pass
            return

        # 4. Wiegand / RFID / Dallas Events
        if any(w_tag in header for w_tag in ("#WGN", "#WGD", "#WIEGAND")):
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

        # 5. ADC values: #ADC,2.45 or #ADC,1,2.45 or #ADC,1,602,2.06 or #ADC,ALL,2.45,3.10
        #    Format with 3 parts per channel: ch, raw_code, voltage — take voltage (last)
        if "#ADC" in header:
            adc_parts = list(data_parts)
            # #ADC,RAW,... — raw codes, skip (we want voltages)
            if adc_parts and adc_parts[0].upper() == "RAW":
                return

            if adc_parts and adc_parts[0].upper() == "ALL":
                adc_parts = adc_parts[1:]
                for idx, p in enumerate(adc_parts, start=1):
                    try:
                        val = float(p.strip())
                        self.adc[idx] = val
                        self._notify("adc_changed", {"channel": idx, "value": val})
                    except ValueError:
                        pass
                return

            if len(adc_parts) == 1:
                try:
                    val = float(adc_parts[0].strip())
                    self.adc[1] = val
                    self._notify("adc_changed", {"channel": 1, "value": val})
                except ValueError:
                    pass
                return

            # #ADC,ch,code,voltage (3 parts) — take last as voltage
            if len(adc_parts) >= 3:
                try:
                    ch = int(adc_parts[0].strip())
                    val = float(adc_parts[-1].strip())  # last part is voltage
                    self.adc[ch] = val
                    self._notify("adc_changed", {"channel": ch, "value": val})
                except ValueError:
                    pass
                return

            # #ADC,ch,voltage (2 parts)
            if len(adc_parts) >= 2:
                try:
                    ch = int(adc_parts[0].strip())
                    val = float(adc_parts[1].strip())
                    self.adc[ch] = val
                    self._notify("adc_changed", {"channel": ch, "value": val})
                except ValueError:
                    pass
                return
            return

        # 6. Temperature sensors (1-Wire): #TMP,23.5 or #TMP,1,23.5 or #TMP,ALL,23.5,24.1
        if "#TMP" in header or "#TEMPO" in header:
            t_parts = list(data_parts)
            if t_parts and t_parts[0].upper() == "ALL":
                t_parts = t_parts[1:]
                for idx, p in enumerate(t_parts, start=1):
                    try:
                        temp = float(p.strip())
                        self.temperatures[idx] = temp
                        self._notify("temperature_changed", {"sensor": idx, "temperature": temp})
                    except ValueError:
                        pass
                return

            if len(t_parts) == 1:
                try:
                    temp = float(t_parts[0].strip())
                    self.temperatures[1] = temp
                    self._notify("temperature_changed", {"sensor": 1, "temperature": temp})
                except ValueError:
                    pass
                return

            if len(t_parts) >= 2:
                try:
                    sensor_id = int(t_parts[0].strip())
                    temp = float(t_parts[1].strip())
                    self.temperatures[sensor_id] = temp
                    self._notify("temperature_changed", {"sensor": sensor_id, "temperature": temp})
                except ValueError:
                    pass
                return
            return

        # 7. Pulse Counters: #CNT,1045 or #CNT,1,1045 or #CNT,ALL,100,200
        if "#CNT" in header:
            cnt_parts = list(data_parts)
            if cnt_parts and cnt_parts[0].upper() == "ALL":
                cnt_parts = cnt_parts[1:]
                for idx, p in enumerate(cnt_parts, start=1):
                    try:
                        count = int(p.strip())
                        self.counters[idx] = count
                        self._notify("counter_changed", {"counter": idx, "count": count})
                    except ValueError:
                        pass
                return

            if len(cnt_parts) == 1:
                try:
                    count = int(cnt_parts[0].strip())
                    self.counters[1] = count
                    self._notify("counter_changed", {"counter": 1, "count": count})
                except ValueError:
                    pass
                return

            if len(cnt_parts) >= 2:
                try:
                    cnt_id = int(cnt_parts[0].strip())
                    count = int(cnt_parts[1].strip())
                    self.counters[cnt_id] = count
                    self._notify("counter_changed", {"counter": cnt_id, "count": count})
                except ValueError:
                    pass
                return
            return

        # 8. User Variables (VAR / ~UV~): #VAR,1,12.5 or #VAR,1,GET,12.5 or #VAR,12.5
        if "#VAR" in header:
            cleaned_parts = [p.strip() for p in data_parts if p.strip().upper() != "GET"]
            if len(cleaned_parts) >= 2:
                try:
                    var_id = int(cleaned_parts[0])
                    val = float(cleaned_parts[1])
                    self.variables[var_id] = val
                    self._notify("variable_changed", {"variable": var_id, "value": val})
                except ValueError:
                    pass
            elif len(cleaned_parts) == 1:
                try:
                    val = float(cleaned_parts[0])
                    self.variables[1] = val
                    self._notify("variable_changed", {"variable": 1, "value": val})
                except ValueError:
                    pass
            return

        # 9. Meter Pulse Lines: #MPL,IO,<pin>,<count> or #MPL,IN,<pin>,<count>
        if "#MPL" in header:
            mpl_parts = list(data_parts)
            if len(mpl_parts) >= 3:
                line_type = mpl_parts[0].strip().upper()  # IO or IN
                try:
                    pin = int(mpl_parts[1].strip())
                    count = int(mpl_parts[2].strip())
                    if line_type == "IO":
                        self.mpl_io[pin] = count
                    elif line_type == "IN":
                        self.mpl_in[pin] = count
                    self._notify("mpl_changed", {"type": line_type, "pin": pin, "count": count})
                except ValueError:
                    pass
            return

        # 10. Flow meters: #FLOW,IO,<pin>,<instant>,<total> or #FLOW,IN,<pin>,<instant>,<total>
        if "#FLOW" in header:
            flow_parts = list(data_parts)
            if len(flow_parts) >= 4:
                line_type = flow_parts[0].strip().upper()  # IO or IN
                try:
                    pin = int(flow_parts[1].strip())
                    instant = float(flow_parts[2].strip())
                    total = float(flow_parts[3].strip())
                    flow_data = {"instant": instant, "total": total}
                    if line_type == "IO":
                        self.flow_io[pin] = flow_data
                    elif line_type == "IN":
                        self.flow_in[pin] = flow_data
                    self._notify("flow_changed", {"type": line_type, "pin": pin, **flow_data})
                except ValueError:
                    pass
            return

        # 11. CAT custom parameters / Key-Value messages (e.g. key=val or CGI style UST=VZU&value=1)
        if "=" in line:
            clean_str = line.lstrip("#$").strip()
            subparts = clean_str.split("?")[-1].split("&")
            extracted = {}
            for item in subparts:
                if "=" in item:
                    k, v = item.split("=", 1)
                    k = k.strip()
                    v = v.strip()
                    val: Any = v
                    try:
                        val = float(v)
                    except ValueError:
                        pass
                    extracted[k] = val
                    self.cat_custom_data[k] = val

                    # Map to VAR, ADC, TMP if key matches
                    k_upper = k.upper()
                    var_match = re.match(r"^(?:VAR|UV)[_]?(\d+)$", k_upper)
                    if var_match and isinstance(val, (int, float)):
                        self.variables[int(var_match.group(1))] = float(val)

                    adc_match = re.match(r"^(?:ADC|AD)[_]?(\d+)$", k_upper)
                    if adc_match and isinstance(val, (int, float)):
                        self.adc[int(adc_match.group(1))] = float(val)

                    tmp_match = re.match(r"^(?:TMP|TEMP|TEMPO)[_]?(\d+)?$", k_upper)
                    if tmp_match and isinstance(val, (int, float)):
                        t_id = int(tmp_match.group(1)) if tmp_match.group(1) else 1
                        self.temperatures[t_id] = float(val)

            if extracted:
                self._notify("cat_push", extracted)
                return

        if "#ERR" in header or "$ERR" in header:
            _LOGGER.debug("Laurent returned #ERR (command not supported on this model / firmware): %s", line)
            return

        # Generic notification for any other Ke-messages
        self._notify("raw_message", {"line": line})
