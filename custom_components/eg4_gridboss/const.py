"""Constants for the EG4 GridBOSS integration."""

DOMAIN = "eg4_gridboss"

# Config entry keys
CONF_HOST = "host"
CONF_PORT = "port"
CONF_UNIT_ID = "unit_id"
CONF_POLL_INTERVAL = "poll_interval"
CONF_PROTOCOL = "protocol"

# Defaults
DEFAULT_HOST = "10.10.10.1"
DEFAULT_PORT = 8000
DEFAULT_UNIT_ID = 1
DEFAULT_POLL_INTERVAL = 30
DEFAULT_PROTOCOL = "auto"

# Minimum safe poll interval in seconds
MIN_POLL_INTERVAL = 10

# Protocol choices
PROTOCOL_AUTO = "auto"
PROTOCOL_TCP = "modbus_tcp"
PROTOCOL_RTU = "modbus_rtu_tcp"

# Modbus function codes
FC_READ_HOLDING = 0x03
FC_READ_INPUT = 0x04

# Device info
DEVICE_MANUFACTURER = "EG4"
DEVICE_MODEL = "GridBOSS"
