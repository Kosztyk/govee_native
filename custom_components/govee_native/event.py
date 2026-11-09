"""Event-only Govee capabilities, delivered by the documented MQTT feed."""

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import DOMAIN, EVENT, CONF_MQTT
from .entity import GoveeEntity, readable


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    if not entry.options.get(CONF_MQTT, False):
        async_add_entities([])
        return
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        GoveeEvent(coordinator, d, instance)
        for d in coordinator.devices.values()
        for kind, instance in d.capabilities
        if kind == EVENT
    )


class GoveeEvent(GoveeEntity, EventEntity):
    _attr_event_types = ["triggered"]

    def __init__(self, coordinator, device, instance):
        super().__init__(coordinator, device, f"event_{instance}")
        self.instance = instance
        self._attr_name = readable(instance)

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(
            self.hass, f"{DOMAIN}_{self.device.device_id}_{self.instance}", self._handle_event
        ))

    @property
    def available(self):
        # Event subscriptions are independent of device/state polling and
        # Bluetooth sensors may report an offline Wi-Fi state.
        return self.coordinator.last_update_success

    def _handle_event(self, state):
        self._trigger_event("triggered", {k: v for k, v in state.items() if k in ("name", "value", "message")})
        self.async_write_ha_state()
