from aiohttp import web

from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import (
    DOMAIN,
    PLATFORMS,
    CONF_HOST,
    CONF_PORT,
    CONF_PASSWORD,
    CONF_NAME,
    CONF_SCAN_INTERVAL,
    CONF_WIEGAND_REVERSE,
    EVENT_LAURENT_CAT_PUSH,
    DEFAULT_PORT,
    DEFAULT_PASSWORD,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_WIEGAND_REVERSE,
)
from .coordinator import LaurentCoordinator
from .laurent_client import LaurentClient

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Laurent device from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    host = entry.data[CONF_HOST]
    port = entry.data.get(CONF_PORT, DEFAULT_PORT)
    password = entry.data.get(CONF_PASSWORD, DEFAULT_PASSWORD)
    name = entry.data.get(CONF_NAME, f"Laurent {host}")
    scan_interval = entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    wiegand_reverse = entry.options.get(CONF_WIEGAND_REVERSE, entry.data.get(CONF_WIEGAND_REVERSE, DEFAULT_WIEGAND_REVERSE))

    client = LaurentClient(
        host=host,
        port=port,
        password=password,
        wiegand_reverse=wiegand_reverse,
    )

    coordinator = LaurentCoordinator(
        hass=hass,
        client=client,
        scan_interval_sec=scan_interval,
        name=name,
    )

    # Initial connection
    connected = await client.connect()
    if not connected:
        _LOGGER.warning("Could not connect to Laurent (%s:%s) on startup. Will retry in background.", host, port)

    # Initial data fetch
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = coordinator

    # 1. Register Webhook for Laurent CAT ($KE,URL,RUN)
    webhook_id = f"laurent_{entry.entry_id}"

    async def handle_laurent_webhook(hass: HomeAssistant, hook_id: str, request: web.Request) -> web.Response:
        """Handle incoming HTTP GET/POST from Laurent CAT system."""
        # Query parameters like ?UST=VZU&value=12.3
        query_params = dict(request.query)
        _LOGGER.info("Received CAT URL_RUN request from Laurent (%s): %s", host, query_params)

        # Update client custom state and fire bus event
        for k, v in query_params.items():
            try:
                coordinator.client.cat_custom_data[k] = float(v)
            except ValueError:
                coordinator.client.cat_custom_data[k] = v

        hass.bus.async_fire(
            EVENT_LAURENT_CAT_PUSH,
            {
                "device": name,
                "host": host,
                **query_params,
            },
        )
        coordinator.async_set_updated_data(coordinator._build_state_data())
        return web.Response(text="#OK\r\n", content_type="text/plain")

    try:
        webhook.async_register(
            hass=hass,
            domain=DOMAIN,
            name=f"Laurent CAT ({name})",
            webhook_id=webhook_id,
            handler=handle_laurent_webhook,
            local_only=True,
        )
        _LOGGER.info("Registered CAT webhook endpoint: /api/webhook/%s", webhook_id)
    except Exception as err:
        _LOGGER.warning("Could not register webhook %s: %s", webhook_id, err)

    # Forward entry setups to platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register update listener for options
    entry.async_on_unload(entry.add_update_listener(async_update_options))

    return True


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    coordinator: LaurentCoordinator = hass.data[DOMAIN][entry.entry_id]
    wiegand_reverse = entry.options.get(CONF_WIEGAND_REVERSE, DEFAULT_WIEGAND_REVERSE)
    coordinator.client.wiegand_reverse = wiegand_reverse
    _LOGGER.info("Updated Laurent options: wiegand_reverse=%s", wiegand_reverse)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    webhook_id = f"laurent_{entry.entry_id}"
    try:
        webhook.async_unregister(hass, webhook_id)
    except Exception:
        pass

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: LaurentCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()

    return unload_ok
