import re
import time
import threading
import logging
import usb.core
import usb.util

_LOGGER = logging.getLogger(__name__)

class USBConnection:
    """Helper to handle USB connection."""
    def __init__(self, vid=0x0665, pid=0x5161, timeout=5000):
        self.vid = vid
        self.pid = pid
        self.timeout = timeout
        self.dev = None
        self.interface_number = None
        self.ep_in = None
        self.ep_out = None

    def __enter__(self):
        self.dev = usb.core.find(idVendor=self.vid, idProduct=self.pid)
        if self.dev is None:
            raise ValueError(f"Device {hex(self.vid)}:{hex(self.pid)} not found")

        _LOGGER.debug("USB device found: %s", self.dev)

        detach_candidates = set()
        try:
            for cfg in self.dev:
                for intf in cfg:
                    detach_candidates.add(intf.bInterfaceNumber)
        except Exception:
            detach_candidates.add(0)

        for intf_num in sorted(detach_candidates or {0}):
            try:
                if self.dev.is_kernel_driver_active(intf_num):
                    self.dev.detach_kernel_driver(intf_num)
                    _LOGGER.debug("Detached kernel driver from interface %s", intf_num)
            except (NotImplementedError, usb.core.USBError) as e:
                _LOGGER.debug("Kernel driver detach check failed for interface %s: %s", intf_num, e)

        try:
            self.dev.set_configuration()
        except usb.core.USBError as e:
            if e.errno == 16:
                _LOGGER.debug("Device busy during set_configuration, assuming already configured.")
            else:
                _LOGGER.warning("Could not set configuration: %s", e)

        cfg = self.dev.get_active_configuration()
        for intf in cfg:
            ep_in = None
            ep_out = None
            for ep in intf:
                direction = usb.util.endpoint_direction(ep.bEndpointAddress)
                if direction == usb.util.ENDPOINT_IN:
                    ep_in = ep
                elif direction == usb.util.ENDPOINT_OUT:
                    ep_out = ep
            if ep_in is not None:
                self.interface_number = intf.bInterfaceNumber
                self.ep_in = ep_in
                self.ep_out = ep_out
                break

        if self.ep_in is None or self.interface_number is None:
            _LOGGER.error("Could not find IN endpoint. Active configuration: %s", cfg)
            raise ValueError("Could not find IN endpoint")

        try:
            usb.util.claim_interface(self.dev, self.interface_number)
            _LOGGER.debug("Claimed USB interface %s", self.interface_number)
        except usb.core.USBError as e:
            _LOGGER.error("Could not claim interface %s: %s", self.interface_number, e)
            raise e

        _LOGGER.debug("USB interface: %s", self.interface_number)
        _LOGGER.debug("USB endpoint IN: %s", self.ep_in)
        _LOGGER.debug("USB endpoint OUT: %s", self.ep_out)
        if self.ep_out is None:
            _LOGGER.debug("No OUT endpoint found. Will use Control Transfer (SET_REPORT) for writing.")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.dev is not None and self.interface_number is not None:
            try:
                usb.util.release_interface(self.dev, self.interface_number)
            except Exception:
                pass
        if self.dev is not None:
            usb.util.dispose_resources(self.dev)
        self.dev = None
        self.interface_number = None
        self.ep_in = None
        self.ep_out = None

    def write(self, data: bytes):
        chunk_size = 8
        if self.ep_out:
            for offset in range(0, len(data), chunk_size):
                chunk = data[offset:offset + chunk_size]
                _LOGGER.debug("USB TX packet: %s %r", chunk.hex(), chunk)
                self.ep_out.write(chunk, self.timeout)
                time.sleep(0.02)
        else:
            for offset in range(0, len(data), chunk_size):
                chunk = data[offset:offset + chunk_size]
                _LOGGER.debug("USB TX ctrl: %s %r", chunk.hex(), chunk)
                self.dev.ctrl_transfer(0x21, 0x09, 0x200, self.interface_number or 0, chunk, self.timeout)
                time.sleep(0.02)

    def read_until(self, terminator=b'\r') -> bytes:
        if not self.ep_in:
            return b""
        res = b""
        start = time.time()
        timeout_sec = self.timeout / 1000.0
        while (time.time() - start) < timeout_sec:
            try:
                data = bytes(self.ep_in.read(8, 500))
                _LOGGER.debug("USB RX packet: %s %r", data.hex(), data)
                res += data
                if terminator in res:
                    break
            except usb.core.USBError as e:
                if e.errno == 110:
                    continue
                _LOGGER.debug("USB read error: %s", e)
                break
        _LOGGER.debug("USB RX all: %s %r", res.hex(), res)
        return res

    def reset_input_buffer(self):
        if not self.ep_in:
            return
        while True:
            try:
                data = bytes(self.ep_in.read(8, 50))
                _LOGGER.debug("USB drain RX: %s %r", data.hex(), data)
            except usb.core.USBError:
                break

    def reset_output_buffer(self):
        pass

class AxpertInverter:
    def __init__(self, device_path: str):
        self._device_path = device_path
        self._lock = threading.Lock()
        self._last_command_time = 0
        self._usb = None

    def _get_usb(self) -> USBConnection:
        if self._usb is None or self._usb.dev is None:
            self._usb = USBConnection(timeout=5000)
            self._usb.__enter__()
        return self._usb

    def _close_usb(self):
        if self._usb is not None:
            try:
                self._usb.__exit__(None, None, None)
            except Exception:
                pass
            self._usb = None

    def __del__(self):
        self._close_usb()

    def _get_crc(self, cmd: str | bytes) -> bytes:
        crc = 0
        da = bytearray(cmd, 'utf8') if isinstance(cmd, str) else bytearray(cmd)
        for byte in da:
            crc ^= byte << 8
            for _ in range(8):
                if crc & 0x8000:
                    crc = ((crc << 1) ^ 0x1021) & 0xFFFF
                else:
                    crc = (crc << 1) & 0xFFFF
        high = (crc >> 8) & 0xFF
        low = crc & 0xFF
        if low in (0x28, 0x0d, 0x0a):
            low += 1
        if high in (0x28, 0x0d, 0x0a):
            high += 1
        return bytes([high, low])

    def _decode_frame(self, command: str, response: bytes) -> str:
        raw = response
        if b'\r' in response:
            response = response[:response.index(b'\r')]
        response = response.replace(b'\x00', b'').strip()

        if response in (b'(ACK', b'ACK'):
            return 'ACK'
        if response in (b'(NAK', b'NAK'):
            return 'NAK'

        if b'(' in response:
            response = response[response.find(b'('):]
        else:
            _LOGGER.debug("Frame for %s has no '(' after cleanup: %s %r", command, response.hex(), response)

        candidates = []
        if len(response) > 2:
            candidates.append((response[:-2], response[-2:]))
            for i in range(len(response) - 2, 0, -1):
                candidates.append((response[:i], response[i:i + 2]))

        for data, crc in candidates:
            if self._get_crc(data) == crc:
                return data.lstrip(b'(').decode('iso-8859-1', errors='ignore').strip()

        text = response.lstrip(b'(').decode('iso-8859-1', errors='ignore').replace('\x00', '').strip()
        if command in ("QPIGS", "QPIRI"):
            matches = re.findall(r"[-A-Za-z0-9:.]+", text)
            if matches and len(matches[-1]) <= 2 and not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", matches[-1]):
                matches = matches[:-1]
            cleaned = " ".join(matches)
        elif command == "QPIWS":
            m = re.search(r"[01]{1,40}", text)
            cleaned = m.group(0)[:40] if m else text
        else:
            cleaned = text

        _LOGGER.debug("CRC mismatch for %s, using cleaned payload. Raw=%s %r Clean=%s", command, raw.hex(), raw, cleaned)
        return cleaned

    def send_command(self, command: str) -> str:
        with self._lock:
            time_since_last = time.time() - self._last_command_time
            if time_since_last < 0.5:
                time.sleep(0.5 - time_since_last)

            for attempt in range(2):
                try:
                    ser = self._get_usb()
                    crc = self._get_crc(command)
                    full_command = command.encode() + crc + b'\r'
                    _LOGGER.debug("Sending command: %s (%s %r)", command, full_command.hex(), full_command)
                    ser.reset_input_buffer()
                    ser.reset_output_buffer()
                    ser.write(full_command)
                    response = ser.read_until(b'\r')
                    if not response:
                        raise Exception("No response from inverter")
                    decoded_response = self._decode_frame(command, response)
                    if decoded_response == 'NAK':
                        if attempt == 0:
                            _LOGGER.warning("Got NAK for command %s, retrying in 1s...", command)
                            time.sleep(1)
                            continue
                        raise Exception(f"Command \"{command}\" not supported")
                    _LOGGER.debug("Response from inverter for %s: %s", command, decoded_response)
                    return decoded_response
                except Exception as e:
                    self._close_usb()
                    if attempt == 1:
                        _LOGGER.error("Failed to communicate with inverter after retries: %s", e)
                        raise e
                    _LOGGER.warning("Failed to communicate with inverter: %s", e)
                    time.sleep(0.5)
                finally:
                    self._last_command_time = time.time()

    def _numeric_parts(self, raw: str) -> list[str]:
        return re.findall(r"-?\d+(?:\.\d+)?|[01]{8,40}", raw)

    def _is_vmiii_24v_qpigs(self, parts: list[str]) -> bool:
        if len(parts) < 20:
            return False
        try:
            first = float(parts[0])
            battery_candidate = float(parts[7])
            capacity_candidate = int(float(parts[9]))
            temp_candidate = int(float(parts[10]))
            return 0 <= first <= 70 and 18 <= battery_candidate <= 32 and 0 <= capacity_candidate <= 100 and 0 <= temp_candidate <= 100
        except (ValueError, IndexError):
            return False

    def get_general_status(self) -> dict:
        raw = self.send_command("QPIGS")
        if not raw:
            return {}
        parts = self._numeric_parts(raw)
        if len(parts) < 19:
            _LOGGER.warning("QPIGS response too short after cleanup: %s | Parts: %s", raw, parts)
            return {}

        vmiii_24v = self._is_vmiii_24v_qpigs(parts)

        try:
            if vmiii_24v:
                data = {
                    "grid_voltage": 0.0,
                    "grid_frequency": float(parts[0]),
                    "ac_output_voltage": float(parts[1]),
                    "ac_output_frequency": float(parts[2]),
                    "ac_output_apparent_power": int(float(parts[3])),
                    "ac_output_active_power": int(float(parts[4])),
                    "output_load_percent": int(float(parts[5])),
                    "bus_voltage": int(float(parts[6])),
                    "battery_voltage": float(parts[7]),
                    "battery_charging_current": int(float(parts[8])),
                    "battery_capacity": int(float(parts[9])),
                    "heat_sink_temperature": int(float(parts[10])),
                    "pv_input_current": float(parts[11]),
                    "pv_input_voltage": float(parts[12]),
                    "scc_voltage": float(parts[13]),
                    "battery_discharge_current": int(float(parts[14])),
                    "status_binary": parts[15],
                }
                if len(parts) > 18:
                    data["pv_charging_power"] = int(float(parts[18]))
                return data

            data = {
                "grid_voltage": float(parts[0]),
                "grid_frequency": float(parts[1]),
                "ac_output_voltage": float(parts[2]),
                "ac_output_frequency": float(parts[3]),
                "ac_output_apparent_power": int(float(parts[4])),
                "ac_output_active_power": int(float(parts[5])),
                "output_load_percent": int(float(parts[6])),
                "bus_voltage": int(float(parts[7])),
                "battery_voltage": float(parts[8]),
                "battery_charging_current": int(float(parts[9])),
                "battery_capacity": int(float(parts[10])),
                "heat_sink_temperature": int(float(parts[11])),
                "pv_input_current": float(parts[12]),
                "pv_input_voltage": float(parts[13]),
                "scc_voltage": float(parts[14]),
                "battery_discharge_current": int(float(parts[15])),
                "status_binary": parts[16],
            }
            if len(parts) > 19:
                data["pv_charging_power"] = int(float(parts[19]))
            return data
        except (ValueError, IndexError) as e:
            _LOGGER.error("Error parsing QPIGS data: %s | Raw: %s | Parts: %s", e, raw, parts)
            return {}

    def get_warnings(self) -> str:
        try:
            raw = self.send_command("QPIWS")
            m = re.search(r"[01]{1,40}", raw)
            return m.group(0)[:40] if m else raw
        except Exception as e:
            _LOGGER.error(f"Error getting warnings: {e}")
            return ""

    def get_mode(self) -> str:
        return self.send_command("QMOD")

    def get_device_id(self) -> str:
        return self.send_command("QID")

    def set_ac_input_range(self, mode_code: str) -> bool:
        return "ACK" in self.send_command(mode_code)

    def get_rated_information(self) -> dict:
        raw = self.send_command("QPIRI")
        if not raw:
            return {}
        parts = self._numeric_parts(raw)
        if len(parts) < 17:
            _LOGGER.warning("QPIRI response too short after cleanup: %s | Parts: %s", raw, parts)
            return {}
        try:
            data = {}
            if len(parts) > 16:
                data["output_source_priority"] = parts[16]
            if len(parts) > 17:
                data["charger_source_priority"] = parts[17]
            if len(parts) > 9:
                data["battery_cutoff_voltage"] = float(parts[9])
            if len(parts) > 10:
                data["battery_bulk_voltage"] = float(parts[10])
            if len(parts) > 11:
                data["battery_float_voltage"] = float(parts[11])
            if len(parts) > 12:
                data["battery_type"] = parts[12]
            if len(parts) > 13:
                data["max_ac_charging_current"] = int(float(parts[13]))
            if len(parts) > 14:
                data["max_charging_current"] = int(float(parts[14]))
            if len(parts) > 15:
                data["ac_input_range"] = parts[15]
            if len(parts) > 19:
                data["machine_type"] = parts[19]
            return data
        except Exception as e:
            _LOGGER.error("Error parsing QPIRI: %s | Raw: %s | Parts: %s", e, raw, parts)
            return {}

    def set_output_source_priority(self, priority: str) -> bool:
        return "ACK" in self.send_command(f"POP{priority}")

    def set_charger_source_priority(self, priority: str) -> bool:
        return "ACK" in self.send_command(f"PCP{priority}")

    def set_max_charging_current(self, current: int) -> bool:
        return "ACK" in self.send_command(f"MNCHGC{current:03}")

    def set_max_utility_charging_current(self, current: int) -> bool:
        return "ACK" in self.send_command(f"MUCHGC{current:03}")

    def set_battery_type(self, batt_type: str) -> bool:
        return "ACK" in self.send_command(f"PBT{batt_type}")

    def set_battery_cutoff_voltage(self, voltage: float) -> bool:
        return "ACK" in self.send_command(f"PSDV{voltage:04.1f}")

    def set_battery_bulk_voltage(self, voltage: float) -> bool:
        return "ACK" in self.send_command(f"PCVV{voltage:04.1f}")

    def set_battery_float_voltage(self, voltage: float) -> bool:
        return "ACK" in self.send_command(f"PBFT{voltage:04.1f}")

    def get_firmware_version(self) -> str:
        try:
            raw = self.send_command("QVFW")
            if "VERFW:" in raw:
                return raw.split("VERFW:")[1]
            return raw
        except Exception:
            return "Unknown"

    def get_model_id(self) -> str | None:
        try:
            return self.send_command("QGMN")
        except Exception:
            return None

    def get_model_name(self) -> str | None:
        try:
            raw = self.get_model_id()
            if not raw:
                return None
            return raw.replace('(', '').strip()
        except Exception:
            return None
