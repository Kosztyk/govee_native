"""Discovered numerical ranges: target humidity, volume, and other settings."""

from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, RANGE
from .entity import GoveeEntity, readable
from .runtime_store import restore_setting


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities = []
    for device in coordinator.devices.values():
        for kind, instance in device.capabilities:
            if kind != RANGE:
                continue
            if instance == "brightness" and device.kind == "light":
                continue
            if instance == "humidity" and device.kind in ("humidifier", "dehumidifier"):
                continue
            bounds = device.range(kind, instance)
            if isinstance(bounds.get("min"), (int, float)) and isinstance(bounds.get("max"), (int, float)):
                entities.append(GoveeNumber(coordinator, device, kind, instance))
    async_add_entities(entities)


class GoveeNumber(GoveeEntity, RestoreEntity, NumberEntity):
    def __init__(self, coordinator, device, kind, instance):
        super().__init__(coordinator, device, f"number_{kind}_{instance}")
        self.kind, self.instance = kind, instance
        self._attr_name = readable(instance)
        bounds = device.range(kind, instance)
        self._attr_native_min_value = bounds["min"]
        self._attr_native_max_value = bounds["max"]
        self._attr_native_step = bounds.get("precision") or 1

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        if self.device.state(self.kind, self.instance) is not None:
            return
        previous = await self.async_get_last_state()
        if previous:
            try:
                value = float(previous.state)
            except ValueError:
                return
            if value.is_integer():
                value = int(value)
            if restore_setting(self.device, self.kind, self.instance, value):
                self.coordinator.save_runtime_later()

    @property
    def native_value(self):
        value = self.device.state(self.kind, self.instance)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    async def async_set_native_value(self, value: float):
        if float(value).is_integer():
            value = int(value)
        await self.async_write(self.kind, self.instance, value)
