#!/usr/bin/env python3
"""
mqtt_bridge.py — Path A: EG4 GridBOSS → Home Assistant MQTT bridge
====================================================================
Reads telemetry from the EG4 monitoring dongle via Modbus TCP/RTU-over-TCP
and publishes to an MQTT broker for Home Assistant auto-discovery.

SAFETY: Read-only. No write or command Modbus frames are sent.

Usage:
    python3 mqtt_bridge.py [--config config.yaml]
    python3 mqtt_bridge.py --host 10.10.10.1 --port 8000 \
                            --mqtt-host 192.168.1.10 --mqtt-port 1883

Configuration (command-line or config.yaml):
    dongle:
      host: 10.10.10.1
      port: 8000
      unit_id: 1
      protocol: auto        # auto | tcp | rtu
      poll_interval: 30     # seconds (minimum 10 recommended)
      timeout: 5
    mqtt:
      host: localhost
      port: 1883
      username: ""
      password: ""
      client_id: eg4_gridboss_bridge
      topic_prefix: eg4/gridboss
      ha_discovery_prefix: homeassistant
      retain: true
    logging:
      level: INFO
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import math
import signal
import struct
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Optional imports — fail gracefully with helpful messages
# ---------------------------------------------------------------------------

try:
    import paho.mqtt.client as mqtt_client
except ImportError:
    print("ERROR: paho-mqtt not installed. Run: pip install paho-mqtt", file=sys.stderr)
    sys.exit(1)

try:
    import yaml  # type: ignore
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False

# Re-use protocol logic from eg4_probe — same directory assumed
import os, importlib.util

def _import_probe():
    probe_path = os.path.join(os.path.dirname(__file__), "eg4_probe.py")
    spec = importlib.util.spec_from_file_location("eg4_probe", probe_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

try:
    probe = _import_probe()
    DongleTransport   = probe.DongleTransport
    detect_protocol   = probe.detect_protocol
    read_registers    = probe.read_registers
    PROTOCOL_TCP      = probe.PROTOCOL_TCP
    PROTOCOL_RTU      = probe.PROTOCOL_RTU
    PROTOCOL_UNKNOWN  = probe.PROTOCOL_UNKNOWN
    FC_READ_HOLDING   = probe.FC_READ_HOLDING
    FC_READ_INPUT     = probe.FC_READ_INPUT
    reg_as_signed     = probe.reg_as_signed
except Exception as e:
    print(f"ERROR: Could not load eg4_probe.py: {e}\nMake sure eg4_probe.py is in the same directory.",
          file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Telemetry field definitions
# ---------------------------------------------------------------------------
# These define what we publish to MQTT.
# All register addresses and scaling are SPECULATIVE until confirmed by capture.
# Confidence: confirmed | likely | speculative
#
# To disable a field: set enabled=False
# To fix an address: update register_fc and register_addr after capture analysis.

@dataclass
class TelemetryField:
    name: str                        # MQTT sub-topic (eg4/gridboss/<name>)
    description: str                 # Human-readable label
    register_fc: int                 # Modbus function code (3 or 4)
    register_addr: int               # Register address
    scale: float = 1.0               # Multiply raw value by this
    signed: bool = False             # Treat as int16 (two's complement)
    unit: str = ""                   # Unit of measurement for HA
    device_class: str = ""           # HA device class (power, energy, voltage, etc.)
    state_class: str = "measurement" # HA state class
    confidence: str = "speculative"  # confirmed | likely | speculative
    enabled: bool = True
    hi_word_addr: Optional[int] = None  # If set, value = (hi << 16) | lo
    notes: str = ""


TELEMETRY_FIELDS: list[TelemetryField] = [
    # --- PV Power ---
    TelemetryField(
        name="pv_power_w",
        description="Total PV Power",
        register_fc=FC_READ_INPUT,
        register_addr=3,          # Hi word; lo word at 4
        hi_word_addr=None,        # Will be combined from regs 3+4 in logic below
        scale=1.0,
        unit="W",
        device_class="power",
        confidence="speculative",
        notes="Growatt-family: 32-bit value at input[3:4]. Divide by 10 if units are 0.1W.",
    ),

    # --- Battery ---
    TelemetryField(
        name="battery_soc_pct",
        description="Battery State of Charge",
        register_fc=FC_READ_INPUT,
        register_addr=103,
        scale=1.0,
        unit="%",
        device_class="battery",
        state_class="measurement",
        confidence="speculative",
        notes="Growatt-family address 103. May be at different address on EG4.",
    ),
    TelemetryField(
        name="battery_voltage_v",
        description="Battery Voltage",
        register_fc=FC_READ_INPUT,
        register_addr=70,
        scale=0.01,
        unit="V",
        device_class="voltage",
        confidence="speculative",
        notes="0.01V scaling is common; verify against displayed value.",
    ),
    TelemetryField(
        name="battery_power_w",
        description="Battery Power (positive=charge, negative=discharge)",
        register_fc=FC_READ_INPUT,
        register_addr=74,
        scale=1.0,
        signed=True,
        unit="W",
        device_class="power",
        confidence="speculative",
        notes="Sign convention not confirmed. May need inverting.",
    ),

    # --- Grid ---
    TelemetryField(
        name="grid_power_w",
        description="Grid Power (positive=import, negative=export)",
        register_fc=FC_READ_INPUT,
        register_addr=11,
        scale=1.0,
        signed=True,
        unit="W",
        device_class="power",
        confidence="speculative",
        notes="32-bit at regs 11+12 in Growatt layout; sign convention to verify.",
    ),
    TelemetryField(
        name="grid_voltage_v",
        description="Grid Voltage",
        register_fc=FC_READ_INPUT,
        register_addr=9,
        scale=0.1,
        unit="V",
        device_class="voltage",
        confidence="speculative",
    ),

    # --- Load ---
    TelemetryField(
        name="load_power_w",
        description="Load / Consumption Power",
        register_fc=FC_READ_INPUT,
        register_addr=17,
        scale=1.0,
        unit="W",
        device_class="power",
        confidence="speculative",
        notes="32-bit at regs 17+18. Always positive.",
    ),

    # --- Status ---
    TelemetryField(
        name="inverter_status",
        description="Inverter Status Code",
        register_fc=FC_READ_INPUT,
        register_addr=0,
        scale=1.0,
        unit="",
        device_class="",
        state_class="",
        confidence="speculative",
        notes="Raw status bitfield or enum. Meaning requires capture analysis.",
    ),
]


# ---------------------------------------------------------------------------
# Register read plan derived from TELEMETRY_FIELDS
# ---------------------------------------------------------------------------

def build_read_plan(fields: list[TelemetryField]) -> dict[int, list[tuple[int, int]]]:
    """
    Group enabled fields by function code.
    Returns {fc: [(start, count), ...]} with minimal contiguous blocks.
    """
    from itertools import groupby

    by_fc: dict[int, set[int]] = {}
    for f in fields:
        if not f.enabled:
            continue
        by_fc.setdefault(f.register_fc, set()).add(f.register_addr)
        if f.hi_word_addr is not None:
            by_fc[f.register_fc].add(f.hi_word_addr)
        # For 32-bit fields using two consecutive registers, include the next reg
        # We over-read slightly and that's safe.
        by_fc[f.register_fc].add(f.register_addr + 1)

    plan: dict[int, list[tuple[int, int]]] = {}
    for fc, addrs in by_fc.items():
        sorted_addrs = sorted(addrs)
        # Merge into contiguous blocks (gap <= 5 registers is still one read)
        blocks: list[tuple[int, int]] = []
        if not sorted_addrs:
            continue
        block_start = sorted_addrs[0]
        block_end   = sorted_addrs[0]
        for addr in sorted_addrs[1:]:
            if addr - block_end <= 5:
                block_end = addr
            else:
                blocks.append((block_start, block_end - block_start + 2))
                block_start = addr
                block_end   = addr
        blocks.append((block_start, block_end - block_start + 2))
        plan[fc] = blocks

    return plan


# ---------------------------------------------------------------------------
# Value extractor
# ---------------------------------------------------------------------------

def extract_field_value(
    f: TelemetryField,
    register_cache: dict[tuple[int, int], int],
) -> Optional[float]:
    """Extract a field's value from the register cache. Returns None if missing."""
    raw = register_cache.get((f.register_fc, f.register_addr))
    if raw is None:
        return None

    # 32-bit value: combine hi word (f.register_addr) and lo word (f.register_addr+1)
    # This is the Growatt convention for power values; update if EG4 differs.
    lo_raw = register_cache.get((f.register_fc, f.register_addr + 1))
    if lo_raw is not None and f.register_addr in {3, 7, 11, 15, 17, 74}:
        # Treat as 32-bit unsigned, then optionally sign
        combined = ((raw & 0xFFFF) << 16) | (lo_raw & 0xFFFF)
        if f.signed and combined >= 0x80000000:
            combined -= 0x100000000
        return combined * f.scale

    value = reg_as_signed(raw) if f.signed else raw
    return value * f.scale


# ---------------------------------------------------------------------------
# MQTT publisher
# ---------------------------------------------------------------------------

class HAMqttPublisher:
    """Publishes telemetry and HA auto-discovery payloads to MQTT."""

    DEVICE_INFO = {
        "identifiers": ["eg4_gridboss"],
        "name": "EG4 GridBOSS",
        "model": "GridBOSS",
        "manufacturer": "EG4",
        "sw_version": "path-a-bridge-0.1",
    }

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        client_id: str,
        topic_prefix: str,
        ha_discovery_prefix: str,
        retain: bool,
        logger: logging.Logger,
    ) -> None:
        self.topic_prefix = topic_prefix
        self.ha_prefix    = ha_discovery_prefix
        self.retain       = retain
        self.log          = logger

        self._client = mqtt_client.Client(
            client_id=client_id,
            protocol=mqtt_client.MQTTv311,
        )
        if username:
            self._client.username_pw_set(username, password)

        self._client.on_connect    = self._on_connect
        self._client.on_disconnect = self._on_disconnect

        self._connected = False
        self._host = host
        self._port = port

    def connect(self) -> None:
        self._client.connect_async(self._host, self._port, keepalive=60)
        self._client.loop_start()
        # Wait up to 10s for connection
        deadline = time.monotonic() + 10
        while not self._connected and time.monotonic() < deadline:
            time.sleep(0.1)
        if not self._connected:
            raise ConnectionError(f"Could not connect to MQTT broker at {self._host}:{self._port}")

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self._connected = True
            self.log.info("MQTT connected to %s:%s", self._host, self._port)
        else:
            self.log.error("MQTT connection failed: rc=%s", rc)

    def _on_disconnect(self, client, userdata, rc):
        self._connected = False
        if rc != 0:
            self.log.warning("MQTT disconnected unexpectedly: rc=%s", rc)

    def publish_discovery(self, fields: list[TelemetryField]) -> None:
        """Publish HA MQTT auto-discovery payloads for all enabled fields."""
        for f in fields:
            if not f.enabled:
                continue
            state_topic = f"{self.topic_prefix}/{f.name}"
            config_topic = (
                f"{self.ha_prefix}/sensor/eg4_gridboss/{f.name}/config"
            )
            payload: dict[str, Any] = {
                "name": f.description,
                "unique_id": f"eg4_gridboss_{f.name}",
                "state_topic": state_topic,
                "device": self.DEVICE_INFO,
            }
            if f.unit:
                payload["unit_of_measurement"] = f.unit
            if f.device_class:
                payload["device_class"] = f.device_class
            if f.state_class:
                payload["state_class"] = f.state_class

            self._client.publish(
                config_topic,
                json.dumps(payload),
                qos=1,
                retain=True,
            )
            self.log.debug("Published discovery for %s", f.name)

        # Publish availability topic
        avail_topic = f"{self.topic_prefix}/availability"
        avail_config = f"{self.ha_prefix}/sensor/eg4_gridboss/availability/config"
        self._client.publish(
            avail_config,
            json.dumps({
                "name": "EG4 GridBOSS Bridge Availability",
                "unique_id": "eg4_gridboss_availability",
                "state_topic": avail_topic,
                "device": self.DEVICE_INFO,
            }),
            qos=1,
            retain=True,
        )

    def publish_telemetry(
        self,
        fields: list[TelemetryField],
        register_cache: dict[tuple[int, int], int],
    ) -> int:
        """Publish all field values. Returns count of successfully published fields."""
        count = 0
        for f in fields:
            if not f.enabled:
                continue
            val = extract_field_value(f, register_cache)
            if val is None:
                self.log.debug("Field %s: no data in cache", f.name)
                continue
            if math.isnan(val) or math.isinf(val):
                self.log.warning("Field %s: non-finite value %s, skipping", f.name, val)
                continue
            topic = f"{self.topic_prefix}/{f.name}"
            payload = f"{val:.2f}" if isinstance(val, float) else str(int(val))
            self._client.publish(topic, payload, qos=0, retain=self.retain)
            count += 1
        return count

    def set_available(self, available: bool) -> None:
        topic = f"{self.topic_prefix}/availability"
        self._client.publish(topic, "online" if available else "offline",
                             qos=1, retain=True)

    def disconnect(self) -> None:
        self.set_available(False)
        self._client.loop_stop()
        self._client.disconnect()


# ---------------------------------------------------------------------------
# Bridge orchestrator
# ---------------------------------------------------------------------------

class EG4MqttBridge:
    MIN_POLL_INTERVAL = 10   # seconds — don't hammer the dongle

    def __init__(self, config: dict[str, Any], logger: logging.Logger) -> None:
        self.log = logger
        self.config = config

        dongle_cfg = config.get("dongle", {})
        mqtt_cfg   = config.get("mqtt", {})

        self.dongle_host     = dongle_cfg.get("host", "10.10.10.1")
        self.dongle_port     = int(dongle_cfg.get("port", 8000))
        self.unit_id         = int(dongle_cfg.get("unit_id", 1))
        self.protocol_pref   = dongle_cfg.get("protocol", "auto")
        self.poll_interval   = max(
            self.MIN_POLL_INTERVAL,
            float(dongle_cfg.get("poll_interval", 30))
        )

        self.transport = DongleTransport(
            self.dongle_host, self.dongle_port, verbose=False
        )
        self.protocol: Optional[str] = None

        self.publisher = HAMqttPublisher(
            host                = mqtt_cfg.get("host", "localhost"),
            port                = int(mqtt_cfg.get("port", 1883)),
            username            = mqtt_cfg.get("username", ""),
            password            = mqtt_cfg.get("password", ""),
            client_id           = mqtt_cfg.get("client_id", "eg4_gridboss_bridge"),
            topic_prefix        = mqtt_cfg.get("topic_prefix", "eg4/gridboss"),
            ha_discovery_prefix = mqtt_cfg.get("ha_discovery_prefix", "homeassistant"),
            retain              = bool(mqtt_cfg.get("retain", True)),
            logger              = logger,
        )

        self._running = False
        self._consecutive_errors = 0
        self.MAX_CONSECUTIVE_ERRORS = 5

    def _detect_protocol(self) -> bool:
        if self.protocol_pref == "tcp":
            self.protocol = PROTOCOL_TCP
            return True
        if self.protocol_pref == "rtu":
            self.protocol = PROTOCOL_RTU
            return True
        self.protocol = detect_protocol(self.transport, self.unit_id)
        if self.protocol == PROTOCOL_UNKNOWN:
            self.log.error("Cannot detect protocol. Capture and manual analysis needed.")
            return False
        return True

    def _poll(self, read_plan: dict[int, list[tuple[int, int]]]) -> dict[tuple[int, int], int]:
        """Execute one full poll cycle. Returns register cache."""
        cache: dict[tuple[int, int], int] = {}
        for fc, blocks in read_plan.items():
            for start, count in blocks:
                result = read_registers(
                    self.transport, fc, start, count,
                    self.unit_id, self.protocol
                )
                if result.error:
                    self.log.warning("Read FC%02X @%d: %s", fc, start, result.error)
                    continue
                for i, val in enumerate(result.registers):
                    cache[(fc, start + i)] = val
                time.sleep(0.1)   # gentle inter-request gap
        return cache

    def run(self) -> None:
        self._running = True

        # Detect protocol
        if not self._detect_protocol():
            sys.exit(1)
        self.log.info("Protocol: %s", self.protocol)

        # Connect MQTT
        self.publisher.connect()
        self.publisher.publish_discovery(TELEMETRY_FIELDS)
        self.log.info("HA auto-discovery payloads published.")

        # Build read plan
        read_plan = build_read_plan(TELEMETRY_FIELDS)
        self.log.info("Read plan: %s", read_plan)

        # Signal handler for clean shutdown
        def _shutdown(signum, frame):
            self.log.info("Shutdown signal received.")
            self._running = False

        signal.signal(signal.SIGINT,  _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)

        self.publisher.set_available(True)

        while self._running:
            cycle_start = time.monotonic()
            try:
                cache = self._poll(read_plan)
                if cache:
                    published = self.publisher.publish_telemetry(TELEMETRY_FIELDS, cache)
                    self.log.info(
                        "[%s] Published %d fields (%d registers read)",
                        datetime.datetime.now().strftime("%H:%M:%S"),
                        published,
                        len(cache),
                    )
                    self._consecutive_errors = 0
                else:
                    self.log.warning("Poll returned empty cache.")
                    self._consecutive_errors += 1
            except Exception as e:
                self._consecutive_errors += 1
                self.log.error("Poll error (%d/%d): %s",
                               self._consecutive_errors,
                               self.MAX_CONSECUTIVE_ERRORS, e)
                if self._consecutive_errors >= self.MAX_CONSECUTIVE_ERRORS:
                    self.log.error("Too many consecutive errors. Marking offline.")
                    self.publisher.set_available(False)

            # Back-off: if errors accumulate, slow down polling
            effective_interval = self.poll_interval
            if self._consecutive_errors > 0:
                effective_interval = min(
                    self.poll_interval * (2 ** self._consecutive_errors),
                    300  # cap at 5 min
                )
                self.log.info("Backing off to %.0fs interval.", effective_interval)

            elapsed = time.monotonic() - cycle_start
            sleep_time = max(0, effective_interval - elapsed)
            if self._running:
                time.sleep(sleep_time)

        # Clean shutdown
        self.publisher.set_available(False)
        self.publisher.disconnect()
        self.log.info("Bridge stopped.")


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict[str, Any] = {
    "dongle": {
        "host": "10.10.10.1",
        "port": 8000,
        "unit_id": 1,
        "protocol": "auto",
        "poll_interval": 30,
        "timeout": 5,
    },
    "mqtt": {
        "host": "localhost",
        "port": 1883,
        "username": "",
        "password": "",
        "client_id": "eg4_gridboss_bridge",
        "topic_prefix": "eg4/gridboss",
        "ha_discovery_prefix": "homeassistant",
        "retain": True,
    },
    "logging": {
        "level": "INFO",
    },
}


def deep_merge(base: dict, override: dict) -> dict:
    result = base.copy()
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_config(path: Optional[str]) -> dict[str, Any]:
    config = DEFAULT_CONFIG.copy()
    if path:
        if not _YAML_AVAILABLE:
            print("ERROR: PyYAML not installed. Run: pip install pyyaml", file=sys.stderr)
            sys.exit(1)
        with open(path) as f:
            user_config = yaml.safe_load(f) or {}
        config = deep_merge(config, user_config)
    return config


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="EG4 GridBOSS MQTT bridge for Home Assistant")
    p.add_argument("--config",        help="Path to YAML config file")
    p.add_argument("--host",          help="Dongle host (overrides config)")
    p.add_argument("--port",          type=int, help="Dongle port (overrides config)")
    p.add_argument("--unit-id",       type=int, help="Modbus unit ID (overrides config)")
    p.add_argument("--protocol",      choices=["auto", "tcp", "rtu"],
                                      help="Force protocol (overrides config)")
    p.add_argument("--poll-interval", type=float, help="Poll interval in seconds")
    p.add_argument("--mqtt-host",     help="MQTT broker host")
    p.add_argument("--mqtt-port",     type=int, help="MQTT broker port")
    p.add_argument("--mqtt-user",     help="MQTT username")
    p.add_argument("--mqtt-pass",     help="MQTT password")
    p.add_argument("--topic-prefix",  help="MQTT topic prefix (default: eg4/gridboss)")
    p.add_argument("--log-level",     default="INFO",
                                      choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)

    # Command-line overrides
    if args.host:          config["dongle"]["host"]          = args.host
    if args.port:          config["dongle"]["port"]          = args.port
    if args.unit_id:       config["dongle"]["unit_id"]       = args.unit_id
    if args.protocol:      config["dongle"]["protocol"]      = args.protocol
    if args.poll_interval: config["dongle"]["poll_interval"] = args.poll_interval
    if args.mqtt_host:     config["mqtt"]["host"]            = args.mqtt_host
    if args.mqtt_port:     config["mqtt"]["port"]            = args.mqtt_port
    if args.mqtt_user:     config["mqtt"]["username"]        = args.mqtt_user
    if args.mqtt_pass:     config["mqtt"]["password"]        = args.mqtt_pass
    if args.topic_prefix:  config["mqtt"]["topic_prefix"]   = args.topic_prefix

    log_level = args.log_level or config.get("logging", {}).get("level", "INFO")
    logging.basicConfig(
        level=getattr(logging, log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logger = logging.getLogger("eg4_bridge")

    logger.info("EG4 GridBOSS MQTT Bridge starting...")
    logger.info("Dongle: %s:%s  Unit ID: %s  Protocol: %s",
                config["dongle"]["host"], config["dongle"]["port"],
                config["dongle"]["unit_id"], config["dongle"]["protocol"])
    logger.info("MQTT: %s:%s  Topic prefix: %s",
                config["mqtt"]["host"], config["mqtt"]["port"],
                config["mqtt"]["topic_prefix"])
    logger.info("Poll interval: %ss", config["dongle"]["poll_interval"])
    logger.warning("NOTE: All register addresses are SPECULATIVE until confirmed by capture.")

    bridge = EG4MqttBridge(config, logger)
    bridge.run()


if __name__ == "__main__":
    main()
