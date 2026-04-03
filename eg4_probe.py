#!/usr/bin/env python3
"""
eg4_probe.py — Path A: EG4 GridBOSS dongle local TCP probe
============================================================
Connects to the dongle's TCP endpoint (default: 10.10.10.1:8000) and attempts
to read registers using Modbus TCP, falling back to Modbus RTU-over-TCP.

SAFETY: Only read functions (FC 03, FC 04) are issued. No writes.
        Be conservative — replicate observed patterns only.

Usage:
    python3 eg4_probe.py [--host HOST] [--port PORT] [OPTIONS]

Examples:
    # Auto-detect protocol, scan all likely register blocks, dump to CSV
    python3 eg4_probe.py --host 10.10.10.1 --port 8000 --scan --csv output.csv

    # Read a specific range of holding registers (FC 03)
    python3 eg4_probe.py --fc 3 --start 0 --count 50

    # Read input registers (FC 04), single block
    python3 eg4_probe.py --fc 4 --start 0 --count 100 --unit-id 1

    # Replay a raw hex frame (from capture analysis)
    python3 eg4_probe.py --raw-frame "0001000000060103000000C8"

    # Live poll every 10 seconds, print decoded values
    python3 eg4_probe.py --scan --interval 10
"""

import argparse
import csv
import datetime
import socket
import struct
import sys
import time
import traceback
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Modbus constants
# ---------------------------------------------------------------------------

FC_READ_HOLDING = 0x03
FC_READ_INPUT   = 0x04

# CRC table for Modbus RTU CRC-16/IBM (poly 0xA001, init 0xFFFF)
_CRC_TABLE: list[int] = []

def _build_crc_table() -> None:
    for i in range(256):
        reg = i
        for _ in range(8):
            if reg & 1:
                reg = (reg >> 1) ^ 0xA001
            else:
                reg >>= 1
        _CRC_TABLE.append(reg)

_build_crc_table()


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc = (crc >> 8) ^ _CRC_TABLE[(crc ^ b) & 0xFF]
    return crc


# ---------------------------------------------------------------------------
# Frame builders
# ---------------------------------------------------------------------------

def build_modbus_tcp_frame(
    unit_id: int,
    function_code: int,
    start_address: int,
    count: int,
    transaction_id: int = 1,
) -> bytes:
    """Build a Modbus TCP MBAP + PDU frame (no CRC)."""
    pdu = struct.pack(">BHH", function_code, start_address, count)
    length = 1 + len(pdu)          # unit_id byte + PDU
    mbap = struct.pack(">HHHB", transaction_id, 0x0000, length, unit_id)
    return mbap + pdu


def build_modbus_rtu_frame(
    unit_id: int,
    function_code: int,
    start_address: int,
    count: int,
) -> bytes:
    """Build a Modbus RTU frame (with CRC, no MBAP header)."""
    pdu = struct.pack(">BBHH", unit_id, function_code, start_address, count)
    crc = crc16_modbus(pdu)
    return pdu + struct.pack("<H", crc)   # CRC is little-endian in RTU


# ---------------------------------------------------------------------------
# Response parsers
# ---------------------------------------------------------------------------

class ModbusError(Exception):
    """Modbus exception response received from device."""
    EXCEPTION_CODES = {
        0x01: "ILLEGAL_FUNCTION",
        0x02: "ILLEGAL_DATA_ADDRESS",
        0x03: "ILLEGAL_DATA_VALUE",
        0x04: "SLAVE_DEVICE_FAILURE",
        0x05: "ACKNOWLEDGE",
        0x06: "SLAVE_DEVICE_BUSY",
        0x0A: "GATEWAY_PATH_UNAVAILABLE",
        0x0B: "GATEWAY_TARGET_FAILED",
    }

    def __init__(self, function_code: int, exception_code: int) -> None:
        self.function_code = function_code
        self.exception_code = exception_code
        name = self.EXCEPTION_CODES.get(exception_code, f"UNKNOWN(0x{exception_code:02X})")
        super().__init__(f"Modbus exception: FC=0x{function_code:02X} EC=0x{exception_code:02X} ({name})")


def parse_modbus_tcp_response(data: bytes) -> list[int]:
    """
    Parse Modbus TCP response, return list of register values (uint16).
    Raises ModbusError on exception response, ValueError on bad frame.
    """
    if len(data) < 9:
        raise ValueError(f"Response too short for Modbus TCP: {len(data)} bytes")

    trans_id, proto_id, length, unit_id, fc = struct.unpack(">HHHBB", data[:8])
    # Allow proto_id != 0 in case device uses non-standard value
    payload = data[8:]

    if fc & 0x80:
        # Exception response
        exc_code = payload[0] if payload else 0
        raise ModbusError(fc & 0x7F, exc_code)

    if fc not in (FC_READ_HOLDING, FC_READ_INPUT):
        raise ValueError(f"Unexpected function code in response: 0x{fc:02X}")

    byte_count = payload[0]
    reg_bytes = payload[1:1 + byte_count]
    if len(reg_bytes) < byte_count:
        raise ValueError(f"Truncated payload: expected {byte_count} bytes, got {len(reg_bytes)}")

    return list(struct.unpack(f">{byte_count // 2}H", reg_bytes))


def parse_modbus_rtu_response(data: bytes) -> list[int]:
    """
    Parse Modbus RTU response, return list of register values (uint16).
    """
    if len(data) < 5:
        raise ValueError(f"RTU response too short: {len(data)} bytes")

    unit_id, fc = data[0], data[1]
    if fc & 0x80:
        exc_code = data[2] if len(data) > 2 else 0
        raise ModbusError(fc & 0x7F, exc_code)

    byte_count = data[2]
    reg_bytes = data[3:3 + byte_count]

    # Verify CRC
    crc_recv = struct.unpack("<H", data[3 + byte_count:3 + byte_count + 2])[0]
    crc_calc = crc16_modbus(data[:3 + byte_count])
    if crc_recv != crc_calc:
        print(f"  [WARN] CRC mismatch: received 0x{crc_recv:04X}, calculated 0x{crc_calc:04X}",
              file=sys.stderr)

    return list(struct.unpack(f">{byte_count // 2}H", reg_bytes))


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

class DongleTransport:
    """Low-level TCP socket to the dongle. Handles connect/recv/send."""

    RECV_TIMEOUT = 5.0   # seconds
    RECV_BUFSIZE = 4096

    def __init__(self, host: str, port: int, verbose: bool = False) -> None:
        self.host = host
        self.port = port
        self.verbose = verbose
        self._sock: Optional[socket.socket] = None

    def connect(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(self.RECV_TIMEOUT)
        self._sock.connect((self.host, self.port))
        if self.verbose:
            print(f"[+] Connected to {self.host}:{self.port}")

    def close(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def send(self, data: bytes) -> None:
        if self.verbose:
            print(f"  >> {data.hex()}")
        assert self._sock is not None
        self._sock.sendall(data)

    def recv_frame(self, expected_bytes: int = 256) -> bytes:
        """Read up to expected_bytes from socket, with timeout."""
        assert self._sock is not None
        buf = b""
        deadline = time.monotonic() + self.RECV_TIMEOUT
        while time.monotonic() < deadline:
            try:
                chunk = self._sock.recv(self.RECV_BUFSIZE)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
            if len(buf) >= expected_bytes:
                break
        if self.verbose:
            print(f"  << {buf.hex()}")
        return buf


# ---------------------------------------------------------------------------
# Protocol detector
# ---------------------------------------------------------------------------

PROTOCOL_TCP = "modbus_tcp"
PROTOCOL_RTU = "modbus_rtu_tcp"
PROTOCOL_UNKNOWN = "unknown"


def detect_protocol(transport: DongleTransport, unit_id: int = 1) -> str:
    """
    Send a single Modbus TCP probe for 1 holding register at address 0.
    If the response looks like valid Modbus TCP, return PROTOCOL_TCP.
    Try Modbus RTU over TCP next.
    Return PROTOCOL_UNKNOWN if neither works.
    """
    print("[*] Probing protocol type...")

    # --- Try Modbus TCP ---
    try:
        transport.connect()
        frame = build_modbus_tcp_frame(unit_id, FC_READ_HOLDING, 0, 1, transaction_id=1)
        transport.send(frame)
        resp = transport.recv_frame(16)
        transport.close()

        if len(resp) >= 9:
            trans_id, proto_id, length = struct.unpack(">HHH", resp[:6])
            if proto_id == 0x0000 and length >= 3:
                print(f"[+] Modbus TCP detected (proto_id=0x{proto_id:04X}, length={length})")
                return PROTOCOL_TCP
            else:
                print(f"[~] Response received but proto_id=0x{proto_id:04X} — may still be Modbus TCP variant")
                # Check if byte 1/2 suggest a different known protocol
                if resp[0] == 0xA5:
                    print("[~] 0xA5 start byte — possible SolarmanV5 framing (not yet implemented)")
                return PROTOCOL_TCP  # tentative; let parse logic handle it
        else:
            print(f"[~] Short response ({len(resp)} bytes) to Modbus TCP probe, trying RTU...")
    except (socket.error, OSError) as e:
        print(f"[~] Modbus TCP probe error: {e}, trying RTU...")
        transport.close()

    # --- Try Modbus RTU over TCP ---
    try:
        transport.connect()
        frame = build_modbus_rtu_frame(unit_id, FC_READ_HOLDING, 0, 1)
        transport.send(frame)
        resp = transport.recv_frame(16)
        transport.close()

        if len(resp) >= 5 and (resp[0] == unit_id) and (resp[1] in (FC_READ_HOLDING, FC_READ_INPUT, 0x83, 0x84)):
            print("[+] Modbus RTU-over-TCP detected")
            return PROTOCOL_RTU
    except (socket.error, OSError) as e:
        print(f"[~] Modbus RTU probe error: {e}")
        transport.close()

    print("[!] Could not determine protocol. Capture and manual analysis needed.")
    return PROTOCOL_UNKNOWN


# ---------------------------------------------------------------------------
# Register reader
# ---------------------------------------------------------------------------

@dataclass
class ReadResult:
    timestamp: str
    protocol: str
    function_code: int
    start_address: int
    registers: list[int]
    raw_response: bytes
    error: Optional[str] = None


def read_registers(
    transport: DongleTransport,
    function_code: int,
    start_address: int,
    count: int,
    unit_id: int,
    protocol: str,
    transaction_id: int = 1,
) -> ReadResult:
    """Issue a single read request and return raw + parsed result."""
    ts = datetime.datetime.now().isoformat()
    raw = b""
    try:
        transport.connect()
        if protocol == PROTOCOL_TCP:
            frame = build_modbus_tcp_frame(unit_id, function_code, start_address, count, transaction_id)
        else:
            frame = build_modbus_rtu_frame(unit_id, function_code, start_address, count)

        transport.send(frame)
        # Modbus TCP response size: 9 bytes header + 2 bytes/register
        expected = 9 + count * 2
        raw = transport.recv_frame(expected)
        transport.close()

        if protocol == PROTOCOL_TCP:
            regs = parse_modbus_tcp_response(raw)
        else:
            regs = parse_modbus_rtu_response(raw)

        return ReadResult(ts, protocol, function_code, start_address, regs, raw)

    except ModbusError as e:
        transport.close()
        return ReadResult(ts, protocol, function_code, start_address, [], raw, str(e))
    except Exception as e:
        transport.close()
        return ReadResult(ts, protocol, function_code, start_address, [], raw, str(e))


# ---------------------------------------------------------------------------
# Register block scan plan
# ---------------------------------------------------------------------------

# Each tuple: (function_code, start_address, count, label)
# These blocks cover typical EG4 / Growatt-family register layouts.
# All addresses and interpretations are SPECULATIVE until confirmed by capture.
SCAN_BLOCKS = [
    # Holding registers — device info, settings
    (FC_READ_HOLDING, 0,    50,  "holding_0_49"),
    (FC_READ_HOLDING, 50,   50,  "holding_50_99"),
    (FC_READ_HOLDING, 100,  50,  "holding_100_149"),

    # Input registers — real-time operational data (most likely here)
    (FC_READ_INPUT,   0,    50,  "input_0_49"),
    (FC_READ_INPUT,   50,   50,  "input_50_99"),
    (FC_READ_INPUT,   100,  50,  "input_100_149"),
    (FC_READ_INPUT,   150,  50,  "input_150_199"),
]


# ---------------------------------------------------------------------------
# Output / display helpers
# ---------------------------------------------------------------------------

SIGNED_REGISTERS: set[int] = set()  # Populated from field map; empty = unsigned default


def reg_as_signed(val: int) -> int:
    """Convert a uint16 Modbus register value to signed int16."""
    return val if val < 0x8000 else val - 0x10000


def print_registers(result: ReadResult, apply_field_map: bool = True) -> None:
    fc_name = "Hold" if result.function_code == FC_READ_HOLDING else "Inpt"
    prefix = "H" if result.function_code == FC_READ_HOLDING else "I"

    print(f"\n  [{result.timestamp}] FC{result.function_code:02X} ({fc_name}) "
          f"@ {result.start_address}–{result.start_address + len(result.registers) - 1}")
    print(f"  Raw response ({len(result.raw_response)} bytes): {result.raw_response.hex()}")

    if result.error:
        print(f"  ERROR: {result.error}")
        return

    for i, val in enumerate(result.registers):
        addr = result.start_address + i
        signed = reg_as_signed(val)
        name = FIELD_NAME_MAP.get((result.function_code, addr), "")
        unit = FIELD_UNIT_MAP.get((result.function_code, addr), "")
        scale = FIELD_SCALE_MAP.get((result.function_code, addr), 1.0)
        scaled = val * scale if not FIELD_SIGNED_MAP.get((result.function_code, addr)) else signed * scale

        hint = ""
        if apply_field_map and name:
            hint = f"  ← {name}"
            if unit:
                hint += f" [{unit}] = {scaled:.2f}{unit}"

        print(f"    {prefix}{addr:04d} (0x{addr:04X})  raw={val:6d} (0x{val:04X})  "
              f"signed={signed:7d}{hint}")


def print_hex_dump(data: bytes, indent: str = "    ") -> None:
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        hex_part = " ".join(f"{b:02X}" for b in chunk)
        asc_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print(f"{indent}{i:04X}  {hex_part:<47}  {asc_part}")


# ---------------------------------------------------------------------------
# Field maps (speculative — to be refined after capture analysis)
# ---------------------------------------------------------------------------
# Format: {(function_code, address): value}
# These are based on typical EG4/Growatt-family register maps.
# EVERY entry here is SPECULATIVE until confirmed by capture correlation.

FIELD_NAME_MAP: dict[tuple[int, int], str] = {
    # Holding registers
    (FC_READ_HOLDING, 0):   "device_status",
    (FC_READ_HOLDING, 1):   "output_mode",

    # Input registers — speculative Growatt-family layout
    (FC_READ_INPUT, 0):    "inverter_status",
    (FC_READ_INPUT, 1):    "pv1_voltage_0.1V",
    (FC_READ_INPUT, 2):    "pv1_current_0.1A",
    (FC_READ_INPUT, 3):    "pv1_power_W_hi",      # hi word of 32-bit
    (FC_READ_INPUT, 4):    "pv1_power_W_lo",      # lo word of 32-bit
    (FC_READ_INPUT, 5):    "pv2_voltage_0.1V",
    (FC_READ_INPUT, 6):    "pv2_current_0.1A",
    (FC_READ_INPUT, 7):    "pv2_power_W_hi",
    (FC_READ_INPUT, 8):    "pv2_power_W_lo",
    (FC_READ_INPUT, 9):    "grid_voltage_0.1V",
    (FC_READ_INPUT, 10):   "grid_frequency_0.01Hz",
    (FC_READ_INPUT, 11):   "grid_power_W_hi",
    (FC_READ_INPUT, 12):   "grid_power_W_lo",
    (FC_READ_INPUT, 13):   "output_voltage_0.1V",
    (FC_READ_INPUT, 14):   "output_frequency_0.01Hz",
    (FC_READ_INPUT, 15):   "output_power_W_hi",
    (FC_READ_INPUT, 16):   "output_power_W_lo",
    (FC_READ_INPUT, 17):   "load_power_W_hi",
    (FC_READ_INPUT, 18):   "load_power_W_lo",
    (FC_READ_INPUT, 70):   "battery_voltage_0.01V",
    (FC_READ_INPUT, 71):   "battery_current_0.1A",  # may be signed
    (FC_READ_INPUT, 74):   "battery_power_W_hi",    # may be signed
    (FC_READ_INPUT, 75):   "battery_power_W_lo",
    (FC_READ_INPUT, 103):  "battery_soc_pct",
    (FC_READ_INPUT, 105):  "battery_temperature_0.1C",
    (FC_READ_INPUT, 106):  "radiator_temperature_0.1C",
}

FIELD_UNIT_MAP: dict[tuple[int, int], str] = {
    (FC_READ_INPUT, 1):   "V",
    (FC_READ_INPUT, 2):   "A",
    (FC_READ_INPUT, 9):   "V",
    (FC_READ_INPUT, 70):  "V",
    (FC_READ_INPUT, 71):  "A",
    (FC_READ_INPUT, 103): "%",
}

FIELD_SCALE_MAP: dict[tuple[int, int], float] = {
    (FC_READ_INPUT, 1):   0.1,
    (FC_READ_INPUT, 2):   0.1,
    (FC_READ_INPUT, 9):   0.1,
    (FC_READ_INPUT, 10):  0.01,
    (FC_READ_INPUT, 70):  0.01,
    (FC_READ_INPUT, 71):  0.1,
    (FC_READ_INPUT, 105): 0.1,
    (FC_READ_INPUT, 106): 0.1,
}

FIELD_SIGNED_MAP: dict[tuple[int, int], bool] = {
    (FC_READ_INPUT, 71):  True,   # battery current: negative = discharge
    (FC_READ_INPUT, 74):  True,   # battery power: negative = discharge
    (FC_READ_INPUT, 11):  True,   # grid power: negative = export
}


# ---------------------------------------------------------------------------
# CSV logger
# ---------------------------------------------------------------------------

class CsvLogger:
    def __init__(self, path: str) -> None:
        self.path = path
        self._file = open(path, "w", newline="")
        self._writer: Optional[csv.writer] = None
        self._header_written = False

    def log(self, result: ReadResult) -> None:
        if result.error:
            return
        if not self._header_written:
            headers = ["timestamp", "fc", "start_addr"] + [
                f"reg_{result.start_address + i}" for i in range(len(result.registers))
            ]
            self._writer = csv.writer(self._file)
            self._writer.writerow(headers)
            self._header_written = True

        row = [result.timestamp, result.function_code, result.start_address] + result.registers
        self._writer.writerow(row)
        self._file.flush()

    def close(self) -> None:
        self._file.close()


# ---------------------------------------------------------------------------
# Raw frame replay
# ---------------------------------------------------------------------------

def replay_raw_frame(transport: DongleTransport, hex_frame: str) -> None:
    """Send a raw hex frame and print the response. For debugging captures."""
    data = bytes.fromhex(hex_frame.replace(" ", ""))
    print(f"[*] Replaying raw frame ({len(data)} bytes): {data.hex()}")
    transport.connect()
    transport.send(data)
    resp = transport.recv_frame()
    transport.close()
    print(f"[*] Response ({len(resp)} bytes):")
    print_hex_dump(resp)


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="EG4 GridBOSS dongle probe — read-only Modbus TCP/RTU-over-TCP"
    )
    p.add_argument("--host",     default="10.10.10.1", help="Dongle host/IP")
    p.add_argument("--port",     type=int, default=8000, help="Dongle TCP port")
    p.add_argument("--unit-id",  type=int, default=1,   help="Modbus unit/slave ID")
    p.add_argument("--protocol", choices=["auto", "tcp", "rtu"], default="auto",
                   help="Force protocol (default: auto-detect)")
    p.add_argument("--scan",     action="store_true",
                   help="Scan all predefined register blocks")
    p.add_argument("--fc",       type=int, choices=[3, 4], default=4,
                   help="Function code: 3=Hold, 4=Input (used with --start/--count)")
    p.add_argument("--start",    type=int, default=0,   help="Start register address")
    p.add_argument("--count",    type=int, default=50,  help="Number of registers to read")
    p.add_argument("--interval", type=float, default=0,
                   help="Poll interval in seconds (0 = one-shot)")
    p.add_argument("--csv",      default="",           help="CSV output file path")
    p.add_argument("--raw-frame", default="",
                   help="Replay a raw hex frame (bypasses all Modbus logic)")
    p.add_argument("--verbose",  action="store_true",  help="Show raw hex I/O")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    transport = DongleTransport(args.host, args.port, verbose=args.verbose)

    # --- Raw frame replay mode ---
    if args.raw_frame:
        replay_raw_frame(transport, args.raw_frame)
        return

    # --- Protocol detection ---
    if args.protocol == "auto":
        protocol = detect_protocol(transport, args.unit_id)
        if protocol == PROTOCOL_UNKNOWN:
            print("[!] Protocol unknown — run a packet capture and analyze before probing further.")
            sys.exit(1)
    elif args.protocol == "tcp":
        protocol = PROTOCOL_TCP
    else:
        protocol = PROTOCOL_RTU

    print(f"[+] Using protocol: {protocol}")

    csv_logger = CsvLogger(args.csv) if args.csv else None

    # Build the read plan
    if args.scan:
        plan = SCAN_BLOCKS
    else:
        plan = [(args.fc, args.start, args.count, "custom")]

    iteration = 0
    try:
        while True:
            iteration += 1
            print(f"\n{'='*60}")
            print(f"[*] Poll #{iteration} at {datetime.datetime.now().isoformat()}")

            for fc, start, count, label in plan:
                # Clamp count to Modbus maximum of 125 registers per request
                for chunk_start in range(start, start + count, 125):
                    chunk_count = min(125, start + count - chunk_start)
                    result = read_registers(
                        transport, fc, chunk_start, chunk_count,
                        args.unit_id, protocol,
                        transaction_id=(iteration & 0xFFFF)
                    )
                    print_registers(result)
                    if csv_logger:
                        csv_logger.log(result)
                    # Small inter-request delay — be gentle on the dongle
                    time.sleep(0.1)

            if args.interval <= 0:
                break

            print(f"\n[*] Sleeping {args.interval}s before next poll...")
            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\n[*] Interrupted by user.")
    finally:
        transport.close()
        if csv_logger:
            csv_logger.close()
            print(f"[+] CSV saved to: {args.csv}")


if __name__ == "__main__":
    main()
