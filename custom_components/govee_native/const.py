"""Constants for Govee Native, by Kosztyk."""

from datetime import timedelta

DOMAIN = "govee_native"
NAME = "Govee Native"
VERSION = "0.4.2"
API_BASE = "https://openapi.api.govee.com/router/api/v1"
CONF_API_KEY = "api_key"
CONF_POLL_SECONDS = "poll_seconds"
DEFAULT_POLL_SECONDS = 60
MIN_POLL_SECONDS = 30
MAX_POLL_SECONDS = 600
DEFAULT_UPDATE_INTERVAL = timedelta(seconds=DEFAULT_POLL_SECONDS)

PLATFORMS = ["light", "switch", "select", "number", "sensor", "binary_sensor", "fan", "humidifier", "climate", "event", "scene", "button"]
TYPE_PREFIX = "devices.types."
CAP_PREFIX = "devices.capabilities."
ON_OFF = CAP_PREFIX + "on_off"
TOGGLE = CAP_PREFIX + "toggle"
RANGE = CAP_PREFIX + "range"
MODE = CAP_PREFIX + "mode"
COLOR = CAP_PREFIX + "color_setting"
SEGMENT = CAP_PREFIX + "segment_color_setting"
SCENE = CAP_PREFIX + "dynamic_scene"
DIY_COLOR = CAP_PREFIX + "diy_color_setting"
MUSIC = CAP_PREFIX + "music_setting"
WORK_MODE = CAP_PREFIX + "work_mode"
TEMP_SETTING = CAP_PREFIX + "temperature_setting"
PROPERTY = CAP_PREFIX + "property"
EVENT = CAP_PREFIX + "event"
ONLINE = CAP_PREFIX + "online"

CONF_MQTT = "mqtt_events"
CONF_LAN = "lan_control"

# Android private API credentials are optional; app transport is preferred when present.
CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_APP_TOKEN = "app_token"
CONF_APP_ACCOUNT_ID = "app_account_id"
CONF_APP_ACCOUNT_TOPIC = "app_account_topic"
CONF_APP_MQTT = "app_mqtt"
CONF_APP_CATALOGS = "app_catalogs"
CONF_PRIVATE_MUTATIONS = "private_mutations"

# Explicit opt-in: consume a key already configured in the legacy integration.
CONF_USE_LEGACY_API = "use_legacy_api_key"
CONF_BLUETOOTH = "bluetooth_control"
CONF_PREFER_BLUETOOTH = "prefer_bluetooth"
CONF_BLUETOOTH_ADDRESSES = "bluetooth_addresses"
CONF_H6199_PROFILE = "h6199_profile"
