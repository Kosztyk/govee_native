"""Boolean read-only property sensors; event-only capabilities are not polled."""

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PROPERTY
from .entity import GoveeEntity, readable

BOOLEAN_NAMES = {"motion", "presence", "waterleak", "leak", "door", "window", "smoke", "contact", "pumpfault"}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        GoveeBinarySensor(coordinator, d, kind, instance)
        for d in coordinator.devices.values()
        for kind, instance in d.capabilities
        if kind == PROPERTY and any(fragment in instance.lower() for fragment in BOOLEAN_NAMES)
    )


class GoveeBinarySensor(GoveeEntity, BinarySensorEntity):
    def __init__(self, coordinator, device, kind, instance):
        super().__init__(coordinator, device, f"binary_{instance}")
        self.kind, self.instance = kind, instance
        self._attr_name = readable(instance)

    @property
    def is_on(self):
        value = self.device.state(self.kind, self.instance)
        return bool(value) if isinstance(value, (bool, int)) else None
