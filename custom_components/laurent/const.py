"""Constants for the Laurent integration."""

DOMAIN = "laurent"

# Default configuration values
DEFAULT_PORT = 2424
DEFAULT_PASSWORD = "Laurent"
DEFAULT_SCAN_INTERVAL = 5  # seconds for polling temperature, ADC, etc.
DEFAULT_DEBOUNCE_MS = 50
DEFAULT_WIEGAND_REVERSE = True

# Configuration & Options keys
CONF_HOST = "host"
CONF_PORT = "port"
CONF_PASSWORD = "password"
CONF_NAME = "name"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_WIEGAND_REVERSE = "wiegand_reverse"
CONF_DEBOUNCE_MS = "debounce_ms"

# Events
EVENT_LAURENT_WIEGAND = f"{DOMAIN}_wiegand_event"
EVENT_LAURENT_INPUT_CHANGED = f"{DOMAIN}_input_changed"

# Supported Platforms
PLATFORMS = [
    "switch",
    "binary_sensor",
    "sensor",
    "button",
    "event",
]

# Models & Hardware capabilities
MODEL_LAURENT_4 = "Laurent-4"
MODEL_LAURENT_5 = "Laurent-5"
MODEL_LAURENT_2 = "Laurent-2"
MODEL_LAURENT_128 = "Laurent-128"
MODEL_GENERIC = "Laurent Generic"
