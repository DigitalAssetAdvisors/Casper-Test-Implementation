"""
DataUpdateCoordinator for EG4 GridBOSS.
Polls the dongle and makes data available to all sensor entities.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN,
    FC_READ_HOLDING,
    FC_READ_INPUT,
    MIN_POLL_INTERVAL,
)
from .modbus_client import EG4ModbusClient, ModbusError, ModbusFrameError

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Register read plan
# Each entry: (function_code, start_address, count, label)
#
# These blocks are chosen to cover all registers referenced by SENSOR_DEFS.
# Over-reading a small contiguous block is more efficient than one request per
# register and is safe — FC 03/04 reads are non-destructive.
#
# ALL addresses are SPECULATIVE until confirmed by packet capture analysis.
# ---------------------------------------------------------------------------
_READ_PLAN = [
    # Input registers: real-time operational data
    (FC_READ_INPUT, 0,   20, "input_0_19"),    # status, PV1, PV2, grid, output
    (FC_READ_INPUT, 17,   4, "input_17_20"),   # load power
    (FC_READ_INPUT, 70,  10, "input_70_79"),   # battery voltage, current, power
    (FC_READ_INPUT, 100, 10, "input_100_109"), # SOC, temperatures

    # Holding registers: a small block for device type / firmware / status
    (FC_READ_HOLDING, 0, 10, "holding_0_9"),
]

# Maximum registers per Modbus request
_MAX_REGS_PER_REQUEST = 125

# Inter-request pause to be gentle on the dongle
_INTER_REQUEST_DELAY = 0.1  # seconds


class EG4DataCoordinator(DataUpdateCoordinator):
    """Manages periodic polling and caches all register values."""

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        port: int,
        unit_id: int,
        protocol: str,
        poll_interval: int,
    ) -> None:
        self._client = EG4ModbusClient(host, port, unit_id, protocol)
        self._poll_interval = max(MIN_POLL_INTERVAL, poll_interval)

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=self._poll_interval),
        )

    # ------------------------------------------------------------------
    # Public helpers used by sensor entities
    # ------------------------------------------------------------------

    def get_input(self, addr: int) -> int | None:
        """Return a raw uint16 value from the last input register poll."""
        if self.data is None:
            return None
        return self.data.get((FC_READ_INPUT, addr))

    def get_holding(self, addr: int) -> int | None:
        """Return a raw uint16 value from the last holding register poll."""
        if self.data is None:
            return None
        return self.data.get((FC_READ_HOLDING, addr))

    # ------------------------------------------------------------------
    # Internal polling logic
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[tuple[int, int], int]:
        """Fetch all configured register blocks. Called by the coordinator."""
        cache: dict[tuple[int, int], int] = {}

        try:
            if not self._client.connected:
                await self._client.connect()
        except (OSError, asyncio.TimeoutError) as exc:
            raise UpdateFailed(f"Cannot connect to EG4 dongle: {exc}") from exc

        errors: list[str] = []
        for fc, start, count, label in _READ_PLAN:
            # Chunk into <= 125-register requests
            for chunk_start in range(start, start + count, _MAX_REGS_PER_REQUEST):
                chunk_count = min(_MAX_REGS_PER_REQUEST, start + count - chunk_start)
                try:
                    regs = await self._client.read_registers(fc, chunk_start, chunk_count)
                    for i, val in enumerate(regs):
                        cache[(fc, chunk_start + i)] = val
                except (ModbusError, ModbusFrameError, asyncio.TimeoutError) as exc:
                    _LOGGER.warning(
                        "Read error FC%02X @%d (block %s): %s", fc, chunk_start, label, exc
                    )
                    errors.append(str(exc))
                    # Don't abort — partial data is better than none
                except (OSError, ConnectionError) as exc:
                    # TCP connection broken; mark disconnected and raise
                    await self._client.disconnect()
                    raise UpdateFailed(f"Connection lost: {exc}") from exc

                await asyncio.sleep(_INTER_REQUEST_DELAY)

        if not cache:
            raise UpdateFailed(
                "All register reads failed. Check host/port/unit_id. "
                f"Errors: {'; '.join(errors)}"
            )

        if errors:
            _LOGGER.debug("Partial poll: %d registers ok, %d errors", len(cache), len(errors))

        return cache

    async def async_shutdown(self) -> None:
        """Disconnect cleanly when HA unloads the integration."""
        await self._client.disconnect()
