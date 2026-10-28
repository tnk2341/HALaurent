"""Config flow and Options flow for Laurent integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
import homeassistant.helpers.config_validation as cv

from .const import (
    DOMAIN,
    CONF_HOST,
    CONF_PORT,
    CONF_PASSWORD,
    CONF_NAME,
    CONF_SCAN_INTERVAL,
    CONF_WIEGAND_REVERSE,
    CONF_DEBOUNCE_MS,
    DEFAULT_PORT,
    DEFAULT_PASSWORD,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_WIEGAND_REVERSE,
    DEFAULT_DEBOUNCE_MS,
)
from .laurent_client import LaurentClient

_LOGGER = logging.getLogger(__name__)


class LaurentConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Laurent."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = user_input.get(CONF_PORT, DEFAULT_PORT)
            password = user_input.get(CONF_PASSWORD, DEFAULT_PASSWORD)
            name = user_input.get(CONF_NAME, f"Laurent {host}")

            # Check if already configured
            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured()

            # Test connection
            client = LaurentClient(
                host=host,
                port=port,
                password=password,
            )
            success = await client.connect()
            await client.disconnect()

            if success:
                return self.async_create_entry(
                    title=name,
                    data={
                        CONF_HOST: host,
                        CONF_PORT: port,
                        CONF_PASSWORD: password,
                        CONF_NAME: name,
                        CONF_SCAN_INTERVAL: user_input.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                    },
                    options={
                        CONF_WIEGAND_REVERSE: user_input.get(CONF_WIEGAND_REVERSE, DEFAULT_WIEGAND_REVERSE),
                        CONF_DEBOUNCE_MS: user_input.get(CONF_DEBOUNCE_MS, DEFAULT_DEBOUNCE_MS),
                    },
                )
            else:
                errors["base"] = "cannot_connect"

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST): cv.string,
                vol.Optional(CONF_PORT, default=DEFAULT_PORT): cv.port,
                vol.Optional(CONF_PASSWORD, default=DEFAULT_PASSWORD): cv.string,
                vol.Optional(CONF_NAME, default="Laurent"): cv.string,
                vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): vol.All(
                    cv.positive_int, vol.Range(min=1, max=300)
                ),
                vol.Optional(CONF_WIEGAND_REVERSE, default=DEFAULT_WIEGAND_REVERSE): cv.boolean,
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
        """Create the options flow."""
        return LaurentOptionsFlowHandler(config_entry)


class LaurentOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle Laurent options changes."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_WIEGAND_REVERSE,
                    default=options.get(CONF_WIEGAND_REVERSE, DEFAULT_WIEGAND_REVERSE),
                ): cv.boolean,
                vol.Optional(
                    CONF_DEBOUNCE_MS,
                    default=options.get(CONF_DEBOUNCE_MS, DEFAULT_DEBOUNCE_MS),
                ): vol.All(cv.positive_int, vol.Range(min=0, max=2000)),
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema)
