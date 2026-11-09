"""Fan power plus enumerated fan-speed presets when supported by the device."""

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, ON_OFF, MODE
from .entity import GoveeToggleEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(GoveeFan(coordinator, d) for d in coordinator.devices.values() if d.kind in ("fan", "air_purifier") and d.has(ON_OFF, "powerSwitch"))


class GoveeFan(GoveeToggleEntity, FanEntity):
    _attr_name = None

    def __init__(self, coordinator, device):
        super().__init__(coordinator, device, "fan")
        self._speed_instance = next((name for typ, name in device.capabilities if typ == MODE and name in ("fanSpeed", "gearMode")), None)
        self._attr_supported_features = FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF
        if self._speed_instance:
            self._attr_supported_features |= FanEntityFeature.PRESET_MODE

    @property
    def preset_modes(self):
        if not self._speed_instance:
            return None
        return [str(o["name"]) for o in self.device.options(MODE, self._speed_instance)]

    @property
    def preset_mode(self):
        if not self._speed_instance:
            return None
        value = self.device.state(MODE, self._speed_instance)
        return next((str(o["name"]) for o in self.device.options(MODE, self._speed_instance) if o.get("value") == value), None)

    async def async_set_preset_mode(self, preset_mode):
        option = next((o for o in self.device.options(MODE, self._speed_instance) if str(o.get("name")) == preset_mode), None)
        if not option:
            raise ValueError("Unsupported fan preset")
        await self.async_write(MODE, self._speed_instance, option["value"])

    async def async_turn_on(self, percentage=None, preset_mode=None, **kwargs):
        if percentage is not None:
            from homeassistant.exceptions import HomeAssistantError
            raise HomeAssistantError("This fan does not advertise percentage-based speed control")
        await self.async_write(ON_OFF, "powerSwitch", 1)
        if preset_mode:
            await self.async_set_preset_mode(preset_mode)

    async def async_turn_off(self, **kwargs):
        await self.async_write(ON_OFF, "powerSwitch", 0)
