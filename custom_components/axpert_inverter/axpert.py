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

        # Most Axpert USB HID adapters expose interface 0, but keep this dynamic.
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

    def write(self, data: bytes):
        if self.ep_out:
            chunk_size = 8
            for offset in range(0, len(data), chunk_size):
                chunk = data[offset:offset + chunk_size]
                _LOGGER.debug("USB TX packet: %s %r", chunk.hex(), chunk)
                self.ep_out.write(chunk, self.timeout)
                time.sleep(0.02)
        else:
            chunk_size = 8
            for offset in range(0, len(data), chunk_size):
                chunk = data[offset:offset + chunk_size]
                _LOGGER.debug("USB TX ctrl: %s %r", chunk.hex(), chunk)
                try:
                    self.dev.ctrl_transfer(0x21, 0x09, 0x200, self.interface_number or 0, chunk, self.timeout)
                except usb.core.USBError as e:
                    _LOGGER.error("Control transfer failed: %s", e)
                    raise e
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
    """Class to communicate with the Axpert Inverter via HID."""

    def __init__(self, device_path: str):
        """Initialize the inverter interface."""
        self._device_path = device_path
        self._lock = threading.Lock()
        self._last_command_time = 0

    def _get_crc(self, cmd: str | bytes) -> bytes:
        """Calculate CRC16-XMODEM."""
        crc = 0
        if isinstance(cmd, str):
            da = bytearray(cmd, 'utf8')
        else:
            da = bytearray(cmd)
        
        for byte in da:
            crc ^= byte << 8
            for _ in range(8):
                if (crc & 0x8000):
                    crc = ((crc << 1) ^ 0x1021) & 0xFFFF
                else:
                    crc = (crc << 1) & 0xFFFF
        
        low = crc & 0xFF
        high = (crc >> 8) & 0xFF

        if low in (0x28, 0x0d, 0x0a):
            low += 1
        
        if high in (0x28, 0x0d, 0x0a):
            high += 1

        return bytes([high, low])

    def send_command(self, command: str) -> str:
        """Send a command to the inverter and return the response."""
        with self._lock:
            time_since_last = time.time() - self._last_command_time
            if time_since_last < 0.5:
                time.sleep(0.5 - time_since_last)

            for attempt in range(2):
                try:
                    with USBConnection(timeout=5000) as ser:
                        crc = self._get_crc(command)
                        full_command = command.encode() + crc + b'\r'
                        
                        _LOGGER.debug("Sending command: %s (%s %r)", command, full_command.hex(), full_command)
                        
                        ser.reset_input_buffer()
                        ser.reset_output_buffer()
                        ser.write(full_command)
                        response = ser.read_until(b'\r')
                    
                    if not response:
                        raise Exception("No response from inverter")
    
                    if response.endswith(b'\r'):
                        response = response[:-1]
                    
                    if response == b'(ACK' or response == b'ACK':
                        return 'ACK'
                    if response == b'(NAK' or response == b'NAK':
                        if attempt == 0:
                            _LOGGER.warning("Got NAK for command %s, retrying in 1s...", command)
                            time.sleep(1)
                            continue
                        raise Exception(f"Command \"{command}\" not supported")

                    valid_chars = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 (.-:")
                    
                    if len(response) > 2:
                        std_data = response[:-2]
                        std_crc_received = response[-2:]
                        std_crc_calc = self._get_crc(std_data)
                        
                        if std_crc_calc == std_crc_received:
                            raw_data = std_data
                        else:
                            found_smart = False
                            for i in range(len(response)-2, 0, -1):
                                candidate_data = response[:i]
                                if all(b in valid_chars for b in candidate_data):
                                    candidate_crc = response[i:i+2]
                                    if self._get_crc(candidate_data) == candidate_crc:
                                        _LOGGER.debug("Smart CRC scan recovered data for %s. Garbage detected at end of response.", command)
                                        raw_data = candidate_data
                                        found_smart = True
                                        break
                                        
                            if not found_smart:
                                _LOGGER.warning(
                                    "CRC mismatch for %s: Recv %s vs Calc %s. Raw=%s %r",
                                    command,
                                    std_crc_received.hex(),
                                    std_crc_calc.hex(),
                                    response.hex(),
                                    response,
                                )
                                raw_data = std_data
                    else:
                        raw_data = response

                    try:
                        decoded_response = raw_data.decode('iso-8859-1', errors='ignore')
                        decoded_response = decoded_response.replace('\x00', '').strip()
                    except Exception:
                        decoded_response = raw_data.decode('utf-8', errors='ignore').replace('\x00', '').strip()

                    if '(' in decoded_response:
                        decoded_response = decoded_response[decoded_response.find('(')+1:]

                    _LOGGER.debug("Response from inverter for %s: %s", command, decoded_response)
                    return decoded_response
    
                except Exception as e:
                    if attempt == 1:
                        _LOGGER.error("Failed to communicate with inverter after retries: %s", e)
                        raise e
                    _LOGGER.warning("Failed to communicate with inverter: %s", e)
                    time.sleep(0.5)
                finally:
                    self._last_command_time = time.time()

    def get_general_status(self) -> dict:
        """Get general status parameters (QPIGS)."""
        raw = self.send_command("QPIGS")
        if not raw:
             return {}

        parts = raw.split()
        if len(parts) < 16:
            _LOGGER.warning(f"QPIGS response too short: {raw}")
            return {}
        
        try:
            data = {
                "grid_voltage": float(parts[0]),
                "grid_frequency": float(parts[1]),
                "ac_output_voltage": float(parts[2]),
                "ac_output_frequency": float(parts[3]),
                "ac_output_apparent_power": int(parts[4]),
                "ac_output_active_power": int(parts[5]),
                "output_load_percent": int(parts[6]),
                "bus_voltage": int(parts[7]),
                "battery_voltage": float(parts[8]),
                "battery_charging_current": int(parts[9]),
                "battery_capacity": int(parts[10]),
                "heat_sink_temperature": int(parts[11]),
                "pv_input_current": float(parts[12]),
                "pv_input_voltage": float(parts[13]),
                "scc_voltage": float(parts[14]),
                "battery_discharge_current": int(parts[15]),
                "status_binary": parts[16],
            }
            
            if len(parts) > 19:
                data["pv_charging_power"] = int(parts[19])
            
            return data
        except (ValueError, IndexError) as e:
            _LOGGER.error(f"Error parsing QPIGS data: {e} | Raw: {raw}")
            return {}

    def get_warnings(self) -> str:
        """Get warning status (QPIWS)."""
        try:
            return self.send_command("QPIWS")
        except Exception as e:
            _LOGGER.error(f"Error getting warnings: {e}")
            return ""

    def get_mode(self) -> str:
        """Get Device Mode (QMOD)."""
        return self.send_command("QMOD")

    def get_device_id(self) -> str:
        """Get Device ID (QID)."""
        return self.send_command("QID")
    
    def set_ac_input_range(self, mode_code: str) -> bool:
        """Set AC Input Range. PGR00 or PGR01."""
        resp = self.send_command(mode_code)
        return "ACK" in resp
        
    def get_rated_information(self) -> dict:
        """Get Rated Information (QPIRI)."""
        raw = self.send_command("QPIRI")
        if not raw:
            return {}
            
        parts = raw.split()
        if len(parts) < 17:
             _LOGGER.warning(f"QPIRI response too short: {raw}")
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
                data["max_ac_charging_current"] = int(parts[13])
                
            if len(parts) > 14:
                data["max_charging_current"] = int(parts[14])
                
            if len(parts) > 15:
                data["ac_input_range"] = parts[15]
            
            if len(parts) > 19:
                data["machine_type"] = parts[19]

            return data
        except Exception as e:
            _LOGGER.error(f"Error parsing QPIRI: {e}")
            return {}

    def set_output_source_priority(self, priority: str) -> bool:
        """Set Output Source Priority. 00/01/02."""
        return "ACK" in self.send_command(f"POP{priority}")

    def set_charger_source_priority(self, priority: str) -> bool:
        """Set Charger Source Priority. 00/01/02/03."""
        return "ACK" in self.send_command(f"PCP{priority}")
    
    def set_max_charging_current(self, current: int) -> bool:
        """Set Max Charging Current. MNCHGC<nnn>."""
        cmd = f"MNCHGC{current:03}"
        return "ACK" in self.send_command(cmd)

    def set_max_utility_charging_current(self, current: int) -> bool:
        """Set Max Utility Charging Current. MUCHGC<nnn>."""
        cmd = f"MUCHGC{current:03}"
        return "ACK" in self.send_command(cmd)

    def set_battery_type(self, batt_type: str) -> bool:
        """Set Battery Type. PBT<nn>. 00:AGM, 01:Flooded, 02:User."""
        cmd = f"PBT{batt_type}"
        return "ACK" in self.send_command(cmd)

    def set_battery_cutoff_voltage(self, voltage: float) -> bool:
        """Set Battery Cut-off Voltage. PSDV<nn.n>."""
        cmd = f"PSDV{voltage:04.1f}"
        return "ACK" in self.send_command(cmd)

    def set_battery_bulk_voltage(self, voltage: float) -> bool:
        """Set Battery Bulk (C.V.) Voltage. PCVV<nn.n>."""
        cmd = f"PCVV{voltage:04.1f}"
        return "ACK" in self.send_command(cmd)

    def set_battery_float_voltage(self, voltage: float) -> bool:
        """Set Battery Float Voltage. PBFT<nn.n>."""
        cmd = f"PBFT{voltage:04.1f}"
        return "ACK" in self.send_command(cmd)

    def get_firmware_version(self) -> str:
        """Get Main CPU Firmware Version (QVFW)."""
        try:
            raw = self.send_command("QVFW")
            if "VERFW:" in raw:
                return raw.split("VERFW:")[1]
            return raw
        except Exception:
            return "Unknown"

    def get_model_id(self) -> str | None:
        """Get Model Name ID (QGMN)."""
        try:
            return self.send_command("QGMN")
        except Exception:
            return None

    def get_model_name(self) -> str | None:
        """Get Model Name (QGMN)."""
        try:
            raw = self.get_model_id()
            if not raw: return None
            code = raw.replace('(', '').strip()
            mapping = {
                "001": "VP-5000",
                "002": "VM-5000",
                "003": "VP-3000",
                "004": "VM-3000",
                "005": "MKS+-2000-48-LV-LY",
                "006": "Axpert MLV 3K-24",
                "007": "Axpert PLV 3K-24",
                "008": "Axpert MKS 3KP",
                "009": "Axpert KS 3KP",
                "010": "Axpert MKS 5KP",
                "011": "Axpert KS 5KP",
                "012": "Axpert MKS 4K/5K 64VDC",
                "013": "Axpert KS 4K/5K 64VDC",
                "014": "Axpert MKS 4K/5K",
                "015": "Axpert KS 4K/5K",
                "016": "ALFA M-5000",
                "017": "ALFA P-5000",
                "018": "Axpert Plus Duo/Tri 5KVA",
                "019": "Axpert EPS 5KW",
                "020": "Axpert EPS M-5KW",
                "021": "Axpert EPS 33-5KW",
                "022": "Axpert MKS II 5KW",
                "023": "AXPERT KING 5KW",
                "024": "AXPERT KING 3KW",
                "025": "APT MKS II 5KW (Feed-in grid)",
                "026": "Axpert MLV 5KW-48V",
                "027": "AXPERT VMIII",
                "028": "APT VMIII 3.2KW (Feed-in grid)",
                "029": "AXPERT VMII",
                "030": "Fusion VMII (Feed-in grid)",
                "031": "Phocos MKS II 5KW",
                "032": "Axpert MKS Zero LV 0.7KW",
                "033": "Axpert MKS Zero LV 1.4KW",
                "034": "Axpert MKS Zero LV 2.6KW",
                "035": "AXPERT KING 5KW (Energy)",
                "036": "AXPERT KING 3KW (Energy)",
                "037": "AXPERT VMIII (Energy)",
                "038": "Phocos MKS II 5KW (Energy)",
                "039": "Phocos MKS II 5KW LV",
                "040": "Axpert SE 3.5K",
                "041": "Axpert SE 5.5K",
                "042": "AXPERT MKS III 5KW",
                "043": "MAX 3.6K",
                "044": "MAX 7.2K",
                "045": "MAX 5K LV",
            }
            return mapping.get(code, code)
        except Exception:
            return None
