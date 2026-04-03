"""
Sensor platform for EG4 GridBOSS.
Each entry in SENSOR_DEFS becomes a native HA sensor entity.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DEVICE_MANUFACTURER,
    DEVICE_MODEL,
    DOMAIN,
    FC_READ_HOLDING,
    FC_READ_INPUT,
)
from .coordinator import EG4DataCoordinator
from .modbus_client import EG4ModbusClient

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sensor definition dataclass
# ---------------------------------------------------------------------------

@dataclass
class EG4SensorDescription(SensorEntityDescription):
    """
    Extends SensorEntityDescription with register-extraction metadata.

    value_fn:
        Callable[(coordinator) -> float | int | None]
        All logic for reading + scaling a value lives here.
        Returns None if data is unavailable.

    confidence:
        "confirmed" | "likely" | "speculative"
        Marks how well-validated the register address is.
        Speculative sensors still work; they're just labeled in the log.
    """
    value_fn: Callable[[EG4DataCoordinator], float | int | None] = field(
        default=lambda c: None
    )
    confidence: str = "speculative"


# ---------------------------------------------------------------------------
# Helper lambdas used in SENSOR_DEFS
# ---------------------------------------------------------------------------

def _input(addr: int) -> Callable:
    """Read a single uint16 input register, return raw value."""
    def fn(c: EG4DataCoordinator) -> int | None:
        return c.get_input(addr)
    return fn


def _input_scaled(addr: int, scale: float) -> Callable:
    def fn(c: EG4DataCoordinator) -> float | None:
        v = c.get_input(addr)
        return None if v is None else round(v * scale, 3)
    return fn


def _input_signed(addr: int, scale: float = 1.0) -> Callable:
    """Read uint16, interpret as signed int16, apply scale."""
    def fn(c: EG4DataCoordinator) -> float | None:
        v = c.get_input(addr)
        if v is None:
            return None
        return round(EG4ModbusClient.to_signed16(v) * scale, 3)
    return fn


def _input_u32(hi_addr: int, lo_addr: int, scale: float = 1.0) -> Callable:
    """Combine two consecutive input registers as unsigned 32-bit, apply scale."""
    def fn(c: EG4DataCoordinator) -> float | None:
        hi = c.get_input(hi_addr)
        lo = c.get_input(lo_addr)
        if hi is None or lo is None:
            return None
        return round(EG4ModbusClient.to_uint32(hi, lo) * scale, 1)
    return fn


def _input_s32(hi_addr: int, lo_addr: int, scale: float = 1.0) -> Callable:
    """Combine two consecutive input registers as signed 32-bit, apply scale."""
    def fn(c: EG4DataCoordinator) -> float | None:
        hi = c.get_input(hi_addr)
        lo = c.get_input(lo_addr)
        if hi is None or lo is None:
            return None
        return round(EG4ModbusClient.to_signed32(hi, lo) * scale, 1)
    return fn


def _holding(addr: int) -> Callable:
    def fn(c: EG4DataCoordinator) -> int | None:
        return c.get_holding(addr)
    return fn


# ---------------------------------------------------------------------------
# Sensor definitions
# ---------------------------------------------------------------------------
# ALL register addresses are SPECULATIVE until confirmed by packet capture.
# Update value_fn addresses and scales after capture analysis confirms them.
#
# To disable a sensor: remove its entry or set entity_registry_enabled_default=False
# ---------------------------------------------------------------------------

SENSOR_DEFS: list[EG4SensorDescription] = [

    # --- PV Generation ---
    EG4SensorDescription(
        key="pv_power",
        name="PV Power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        icon="mdi:solar-power",
        # Growatt-family: 32-bit unsigned at input[3:4], raw unit = 0.1 W
        # SPECULATIVE — verify by shading a panel and watching this change
        value_fn=_input_u32(3, 4, scale=0.1),
        confidence="speculative",
    ),
    EG4SensorDescription(
        key="pv1_voltage",
        name="PV1 Voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        entity_registry_enabled_default=False,  # advanced; hidden by default
        value_fn=_input_scaled(1, 0.1),
        confidence="speculative",
    ),
    EG4SensorDescription(
        key="pv1_current",
        name="PV1 Current",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        entity_registry_enabled_default=False,
        value_fn=_input_scaled(2, 0.1),
        confidence="speculative",
    ),

    # --- Battery ---
    EG4SensorDescription(
        key="battery_soc",
        name="Battery SOC",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:battery",
        # Growatt-family: raw % value at input[103]
        # SPECULATIVE — compare directly to app display to confirm
        value_fn=_input(103),
        confidence="speculative",
    ),
    EG4SensorDescription(
        key="battery_voltage",
        name="Battery Voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        # 0.01V scaling; for 48V system expect 44–58V range
        # SPECULATIVE — confirm with multimeter
        value_fn=_input_scaled(70, 0.01),
        confidence="speculative",
    ),
    EG4SensorDescription(
        key="battery_current",
        name="Battery Current",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        # Signed: positive = charging, negative = discharging
        # SPECULATIVE — sign convention not confirmed
        value_fn=_input_signed(71, 0.1),
        confidence="speculative",
    ),
    EG4SensorDescription(
        key="battery_power",
        name="Battery Power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        icon="mdi:battery-charging",
        # Signed 32-bit at input[74:75]: positive = charge, negative = discharge
        # SPECULATIVE
        value_fn=_input_s32(74, 75),
        confidence="speculative",
    ),
    EG4SensorDescription(
        key="battery_temperature",
        name="Battery Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_registry_enabled_default=False,
        value_fn=_input_signed(105, 0.1),
        confidence="speculative",
    ),

    # --- Grid ---
    EG4SensorDescription(
        key="grid_power",
        name="Grid Power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        icon="mdi:transmission-tower",
        # Signed 32-bit: positive = importing, negative = exporting
        # SPECULATIVE — toggle a known load to verify sign convention
        value_fn=_input_s32(11, 12),
        confidence="speculative",
    ),
    EG4SensorDescription(
        key="grid_voltage",
        name="Grid Voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        entity_registry_enabled_default=False,
        value_fn=_input_scaled(9, 0.1),
        confidence="speculative",
    ),
    EG4SensorDescription(
        key="grid_frequency",
        name="Grid Frequency",
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        entity_registry_enabled_default=False,
        value_fn=_input_scaled(10, 0.01),
        confidence="speculative",
    ),

    # --- Load ---
    EG4SensorDescription(
        key="load_power",
        name="Load Power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        icon="mdi:home-lightning-bolt",
        # Unsigned 32-bit at input[17:18]
        # SPECULATIVE — toggle a known load to verify
        value_fn=_input_u32(17, 18),
        confidence="speculative",
    ),

    # --- Inverter ---
    EG4SensorDescription(
        key="inverter_status",
        name="Inverter Status",
        device_class=None,
        state_class=None,
        native_unit_of_measurement=None,
        icon="mdi:information-outline",
        value_fn=_input(0),
        confidence="speculative",
    ),
    EG4SensorDescription(
        key="inverter_temperature",
        name="Inverter Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_registry_enabled_default=False,
        value_fn=_input_signed(106, 0.1),
        confidence="speculative",
    ),
]


# ---------------------------------------------------------------------------
# Platform setup
# ---------------------------------------------------------------------------


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EG4DataCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [EG4SensorEntity(coordinator, entry, desc) for desc in SENSOR_DEFS]
    async_add_entities(entities)


# ---------------------------------------------------------------------------
# Sensor entity
# ---------------------------------------------------------------------------


class EG4SensorEntity(CoordinatorEntity[EG4DataCoordinator], SensorEntity):
    """A single sensor entity backed by the EG4 coordinator."""

    entity_description: EG4SensorDescription

    def __init__(
        self,
        coordinator: EG4DataCoordinator,
        entry: ConfigEntry,
        description: EG4SensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="EG4 GridBOSS",
            manufacturer=DEVICE_MANUFACTURER,
            model=DEVICE_MODEL,
            configuration_url=f"http://{entry.data['host']}",
        )
        if description.confidence == "speculative":
            _LOGGER.debug(
                "Sensor '%s' uses speculative register address — "
                "verify after packet capture analysis.",
                description.key,
            )

    @property
    def native_value(self) -> float | int | str | None:
        try:
            return self.entity_description.value_fn(self.coordinator)
        except Exception as exc:
            _LOGGER.debug("Error extracting value for %s: %s", self.entity_description.key, exc)
            return None
