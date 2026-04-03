"""
Low-level Modbus TCP / Modbus RTU-over-TCP client for the EG4 dongle.
Pure stdlib — no external dependencies.
SAFETY: Only read function codes (0x03, 0x04) are ever issued.
"""
from __future__ import annotations

import asyncio
import logging
import struct
from typing import Optional

from .const import (
    FC_READ_HOLDING,
    FC_READ_INPUT,
    PROTOCOL_AUTO,
    PROTOCOL_RTU,
    PROTOCOL_TCP,
)

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CRC-16/IBM for Modbus RTU
# ---------------------------------------------------------------------------

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


def _crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc = (crc >> 8) ^ _CRC_TABLE[(crc ^ b) & 0xFF]
    return crc


# ---------------------------------------------------------------------------
# Frame builders
# ---------------------------------------------------------------------------


def _build_tcp_frame(
    unit_id: int,
    fc: int,
    start: int,
    count: int,
    tid: int = 1,
) -> bytes:
    pdu = struct.pack(">BHH", fc, start, count)
    mbap = struct.pack(">HHHB", tid, 0x0000, 1 + len(pdu), unit_id)
    return mbap + pdu


def _build_rtu_frame(unit_id: int, fc: int, start: int, count: int) -> bytes:
    body = struct.pack(">BBHH", unit_id, fc, start, count)
    return body + struct.pack("<H", _crc16(body))


# ---------------------------------------------------------------------------
# Exception types
# ---------------------------------------------------------------------------


class ModbusError(Exception):
    _NAMES = {
        0x01: "ILLEGAL_FUNCTION",
        0x02: "ILLEGAL_DATA_ADDRESS",
        0x03: "ILLEGAL_DATA_VALUE",
        0x04: "SLAVE_DEVICE_FAILURE",
    }

    def __init__(self, fc: int, ec: int) -> None:
        name = self._NAMES.get(ec, f"0x{ec:02X}")
        super().__init__(f"Modbus exception FC=0x{fc:02X} EC={name}")
        self.exception_code = ec


class ModbusFrameError(Exception):
    """Malformed or unexpected response frame."""


# ---------------------------------------------------------------------------
# Response parsers
# ---------------------------------------------------------------------------


def _parse_tcp(data: bytes) -> list[int]:
    if len(data) < 9:
        raise ModbusFrameError(f"TCP response too short: {len(data)} bytes")
    _tid, _proto, _length, _uid, fc = struct.unpack(">HHHBB", data[:8])
    payload = data[8:]
    if fc & 0x80:
        raise ModbusError(fc & 0x7F, payload[0] if payload else 0)
    if fc not in (FC_READ_HOLDING, FC_READ_INPUT):
        raise ModbusFrameError(f"Unexpected FC in response: 0x{fc:02X}")
    byte_count = payload[0]
    raw = payload[1: 1 + byte_count]
    return list(struct.unpack(f">{byte_count // 2}H", raw))


def _parse_rtu(data: bytes, unit_id: int) -> list[int]:
    if len(data) < 5:
        raise ModbusFrameError(f"RTU response too short: {len(data)} bytes")
    uid, fc = data[0], data[1]
    if fc & 0x80:
        raise ModbusError(fc & 0x7F, data[2] if len(data) > 2 else 0)
    byte_count = data[2]
    raw = data[3: 3 + byte_count]
    recv_crc = struct.unpack("<H", data[3 + byte_count: 3 + byte_count + 2])[0]
    calc_crc = _crc16(data[: 3 + byte_count])
    if recv_crc != calc_crc:
        _LOGGER.warning("RTU CRC mismatch: recv=0x%04X calc=0x%04X", recv_crc, calc_crc)
    return list(struct.unpack(f">{byte_count // 2}H", raw))


# ---------------------------------------------------------------------------
# Async Modbus client
# ---------------------------------------------------------------------------


class EG4ModbusClient:
    """Async Modbus TCP / RTU-over-TCP client. Read-only."""

    CONNECT_TIMEOUT = 5.0
    READ_TIMEOUT = 5.0
    RECV_BUFFER = 4096

    def __init__(self, host: str, port: int, unit_id: int, protocol: str) -> None:
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self.protocol = protocol  # may be PROTOCOL_AUTO initially
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._tid = 0

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open TCP connection to the dongle."""
        _LOGGER.debug("Connecting to %s:%s", self.host, self.port)
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port),
            timeout=self.CONNECT_TIMEOUT,
        )
        _LOGGER.debug("Connected")

        if self.protocol == PROTOCOL_AUTO:
            self.protocol = await self._detect_protocol()
            _LOGGER.info("Auto-detected protocol: %s", self.protocol)

    async def disconnect(self) -> None:
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
            self._reader = None

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    # ------------------------------------------------------------------
    # Protocol detection
    # ------------------------------------------------------------------

    async def _detect_protocol(self) -> str:
        """
        Send a Modbus TCP read for 1 holding register; inspect the response.
        Falls back to RTU-over-TCP if TCP framing not recognised.
        """
        try:
            frame = _build_tcp_frame(self.unit_id, FC_READ_HOLDING, 0, 1, tid=1)
            self._writer.write(frame)
            await self._writer.drain()
            data = await asyncio.wait_for(
                self._reader.read(self.RECV_BUFFER), timeout=self.READ_TIMEOUT
            )
            if len(data) >= 6:
                _proto = struct.unpack(">H", data[2:4])[0]
                if _proto == 0x0000:
                    return PROTOCOL_TCP
                if data[0] == 0xA5:
                    _LOGGER.warning(
                        "0xA5 start byte suggests SolarmanV5 framing — "
                        "not yet implemented. Attempting TCP parse anyway."
                    )
                    return PROTOCOL_TCP
        except Exception as exc:
            _LOGGER.debug("TCP probe failed: %s", exc)

        # Try RTU probe on same connection
        try:
            frame = _build_rtu_frame(self.unit_id, FC_READ_HOLDING, 0, 1)
            self._writer.write(frame)
            await self._writer.drain()
            data = await asyncio.wait_for(
                self._reader.read(self.RECV_BUFFER), timeout=self.READ_TIMEOUT
            )
            if len(data) >= 5 and data[0] == self.unit_id:
                return PROTOCOL_RTU
        except Exception as exc:
            _LOGGER.debug("RTU probe failed: %s", exc)

        _LOGGER.warning(
            "Could not auto-detect protocol; defaulting to Modbus TCP. "
            "Capture traffic and set protocol explicitly if reads fail."
        )
        return PROTOCOL_TCP

    # ------------------------------------------------------------------
    # Register reads (the only public data-access method)
    # ------------------------------------------------------------------

    async def read_registers(
        self, fc: int, start: int, count: int
    ) -> list[int]:
        """
        Read `count` registers starting at `start` using function code `fc`.
        Returns a list of uint16 values.
        Raises ModbusError, ModbusFrameError, or asyncio.TimeoutError on failure.
        """
        if not self.connected:
            raise ConnectionError("Not connected")

        self._tid = (self._tid + 1) & 0xFFFF

        if self.protocol == PROTOCOL_TCP:
            frame = _build_tcp_frame(self.unit_id, fc, start, count, self._tid)
        else:
            frame = _build_rtu_frame(self.unit_id, fc, start, count)

        _LOGGER.debug("TX: %s", frame.hex())
        self._writer.write(frame)
        await self._writer.drain()

        # Expected response size (TCP: 9 + 2*count, RTU: 5 + 2*count)
        expected = (9 if self.protocol == PROTOCOL_TCP else 5) + count * 2
        data = await asyncio.wait_for(
            self._reader.read(max(expected, self.RECV_BUFFER)),
            timeout=self.READ_TIMEOUT,
        )
        _LOGGER.debug("RX: %s", data.hex())

        if self.protocol == PROTOCOL_TCP:
            return _parse_tcp(data)
        else:
            return _parse_rtu(data, self.unit_id)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def to_signed16(val: int) -> int:
        return val if val < 0x8000 else val - 0x10000

    @staticmethod
    def to_signed32(hi: int, lo: int) -> int:
        combined = ((hi & 0xFFFF) << 16) | (lo & 0xFFFF)
        return combined if combined < 0x80000000 else combined - 0x100000000

    @staticmethod
    def to_uint32(hi: int, lo: int) -> int:
        return ((hi & 0xFFFF) << 16) | (lo & 0xFFFF)
