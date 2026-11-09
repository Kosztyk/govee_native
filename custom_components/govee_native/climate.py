"""Conservative heater control, only with an advertised temperature schema."""

from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature, HVACMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import UnitOfTemperature

from .const import DOMAIN, ON_OFF, TEMP_SETTING
from .entity import GoveeEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        GoveeHeater(coordinator, d) for d in coordinator.devices.values()
        if d.kind == "heater" and d.has(ON_OFF, "powerSwitch") and d.has(TEMP_SETTING, "targetTemperature")
    )


class GoveeHeater(GoveeEntity, ClimateEntity):
    _attr_name = None
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator, device):
        super().__init__(coordinator, device, "climate")
        cap = device.capability(TEMP_SETTING, "targetTemperature") or {}
        fields = cap.get("parameters", {}).get("fields", [])
        bounds = next((f.get("range", {}) for f in fields if f.get("fieldName") == "temperature"), {})
        self._attr_min_temp = bounds.get("min", 5)
        self._attr_max_temp = bounds.get("max", 35)
        self._attr_target_temperature_step = bounds.get("precision", 1)

    @property
    def hvac_mode(self):
        value = self.device.state(ON_OFF, "powerSwitch")
        if value is None:
            return None
        return HVACMode.HEAT if value else HVACMode.OFF

    @property
    def native_target_temperature(self):
        value = self.device.state(TEMP_SETTING, "targetTemperature")
        return value.get("temperature") if isinstance(value, dict) else None

    @property
    def native_current_temperature(self):
        # Govee documents sensorTemperature in Fahrenheit for some SKUs.
        # An unknown unit must not be presented as Celsius.
        return None

    async def async_set_hvac_mode(self, hvac_mode):
        if hvac_mode not in self.hvac_modes:
            raise ValueError("Unsupported heating mode")
        await self.async_write(ON_OFF, "powerSwitch", int(hvac_mode == HVACMode.HEAT))

    async def async_set_temperature(self, **kwargs):
        if (value := kwargs.get("temperature")) is None:
            return
        payload = {"temperature": round(value), "unit": "Celsius"}
        await self.async_write(TEMP_SETTING, "targetTemperature", payload)
