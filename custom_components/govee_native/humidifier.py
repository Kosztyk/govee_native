"""Humidifier or dehumidifier with target humidity and on/off."""

from homeassistant.components.humidifier import HumidifierEntity, HumidifierDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, ON_OFF, RANGE, PROPERTY
from .entity import GoveeToggleEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        GoveeHumidifier(coordinator, d) for d in coordinator.devices.values()
        if d.kind in ("humidifier", "dehumidifier") and d.has(ON_OFF, "powerSwitch") and d.has(RANGE, "humidity")
    )


class GoveeHumidifier(GoveeToggleEntity, HumidifierEntity):
    _attr_name = None

    def __init__(self, coordinator, device):
        super().__init__(coordinator, device, "humidifier")
        self._attr_device_class = (HumidifierDeviceClass.DEHUMIDIFIER if device.kind == "dehumidifier" else HumidifierDeviceClass.HUMIDIFIER)
        bounds = device.range(RANGE, "humidity")
        self._attr_min_humidity = bounds.get("min", 0)
        self._attr_max_humidity = bounds.get("max", 100)

    @property
    def target_humidity(self):
        return self.device.state(RANGE, "humidity")

    @property
    def current_humidity(self):
        return self.device.state(PROPERTY, "sensorHumidity")

    async def async_turn_on(self, **kwargs):
        await self.async_write(ON_OFF, "powerSwitch", 1)

    async def async_turn_off(self, **kwargs):
        await self.async_write(ON_OFF, "powerSwitch", 0)

    async def async_set_humidity(self, humidity):
        await self.async_write(RANGE, "humidity", round(humidity))
