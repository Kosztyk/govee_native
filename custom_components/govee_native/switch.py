"""Discovered toggles and power switches for simple appliances and sockets."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, ON_OFF, TOGGLE
from .entity import GoveeToggleEntity, readable


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities = []
    for d in coordinator.devices.values():
        if d.has(ON_OFF, "powerSwitch") and d.kind not in ("light", "fan", "air_purifier", "humidifier", "dehumidifier", "heater"):
            entities.append(GoveeSwitch(coordinator, d, ON_OFF, "powerSwitch", primary=True))
        for kind, instance in d.capabilities:
            if kind == TOGGLE:
                entities.append(GoveeSwitch(coordinator, d, kind, instance))
    async_add_entities(entities)


class GoveeSwitch(GoveeToggleEntity, SwitchEntity):
    def __init__(self, coordinator, device, kind, instance, primary=False):
        super().__init__(coordinator, device, f"switch_{kind}_{instance}")
        self.kind, self.instance = kind, instance
        self._toggle_key = (kind, instance)
        self._attr_name = None if primary else readable(instance)

    async def async_turn_on(self, **kwargs):
        await self.async_write(self.kind, self.instance, 1)

    async def async_turn_off(self, **kwargs):
        await self.async_write(self.kind, self.instance, 0)
