"""Config flow for EG4 GridBOSS — UI-driven setup inside Home Assistant."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_HOST,
    CONF_POLL_INTERVAL,
    CONF_PORT,
    CONF_PROTOCOL,
    CONF_UNIT_ID,
    DEFAULT_HOST,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_PROTOCOL,
    DEFAULT_UNIT_ID,
    DOMAIN,
    MIN_POLL_INTERVAL,
    PROTOCOL_AUTO,
    PROTOCOL_RTU,
    PROTOCOL_TCP,
)
from .modbus_client import EG4ModbusClient

_LOGGER = logging.getLogger(__name__)

STEP_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST, default=DEFAULT_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=65535)
        ),
        vol.Required(CONF_UNIT_ID, default=DEFAULT_UNIT_ID): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=247)
        ),
        vol.Required(CONF_POLL_INTERVAL, default=DEFAULT_POLL_INTERVAL): vol.All(
            vol.Coerce(int), vol.Range(min=MIN_POLL_INTERVAL, max=3600)
        ),
        vol.Required(CONF_PROTOCOL, default=DEFAULT_PROTOCOL): vol.In(
            [PROTOCOL_AUTO, PROTOCOL_TCP, PROTOCOL_RTU]
        ),
    }
)


async def _test_connection(hass: HomeAssistant, data: dict[str, Any]) -> str | None:
    """
    Try to connect to the dongle and read one register.
    Returns None on success, or an error key string on failure.
    """
    client = EG4ModbusClient(
        host=data[CONF_HOST],
        port=data[CONF_PORT],
        unit_id=data[CONF_UNIT_ID],
        protocol=data[CONF_PROTOCOL],
    )
    try:
        await asyncio.wait_for(client.connect(), timeout=10)
        # Try reading one holding register as a connectivity check
        await asyncio.wait_for(
            client.read_registers(0x03, 0, 1), timeout=5
        )
        return None
    except asyncio.TimeoutError:
        return "cannot_connect"
    except OSError:
        return "cannot_connect"
    except Exception:  # noqa: BLE001
        # Connection worked but register read failed (e.g. wrong unit ID,
        # or device returned an exception). That still means we reached it.
        _LOGGER.debug("Connection test: register read failed, but host is reachable")
        return None
    finally:
        await client.disconnect()


class EG4GridBOSSConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial setup UI flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            # Prevent duplicate entries for the same host
            await self.async_set_unique_id(
                f"{user_input[CONF_HOST]}:{user_input[CONF_PORT]}"
            )
            self._abort_if_unique_id_configured()

            error_key = await _test_connection(self.hass, user_input)
            if error_key:
                errors["base"] = error_key
            else:
                return self.async_create_entry(
                    title=f"EG4 GridBOSS ({user_input[CONF_HOST]})",
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_SCHEMA,
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(config_entry):
        return EG4OptionsFlow(config_entry)


class EG4OptionsFlow(config_entries.OptionsFlow):
    """Allow updating poll interval and protocol after initial setup."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self._config_entry.data
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_POLL_INTERVAL,
                    default=current.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
                ): vol.All(vol.Coerce(int), vol.Range(min=MIN_POLL_INTERVAL, max=3600)),
                vol.Required(
                    CONF_PROTOCOL,
                    default=current.get(CONF_PROTOCOL, DEFAULT_PROTOCOL),
                ): vol.In([PROTOCOL_AUTO, PROTOCOL_TCP, PROTOCOL_RTU]),
                vol.Required(
                    CONF_UNIT_ID,
                    default=current.get(CONF_UNIT_ID, DEFAULT_UNIT_ID),
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=247)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
