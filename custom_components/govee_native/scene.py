"""Executable Govee Home Tap-to-Run shortcuts as native HA scenes."""
from __future__ import annotations

from homeassistant.components.scene import Scene
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        GoveeOneClickScene(coordinator, entry.entry_id, shortcut)
        for shortcut in coordinator.one_clicks.values()
    )


class GoveeOneClickScene(CoordinatorEntity, Scene):
    """Runs the exact APK-returned command sequence; no guessed scene packet."""
    _attr_icon = "mdi:palette"

    def __init__(self, coordinator, entry_id, shortcut):
        super().__init__(coordinator)
        self.shortcut_id = shortcut.key
        self._attr_name = f"Govee {shortcut.name}"
        self._attr_unique_id = f"govee_native_{entry_id}_one_click_{shortcut.key}"

    @property
    def available(self) -> bool:
        return bool(self.coordinator.mqtt and self.coordinator.mqtt.connected)

    async def async_activate(self, **kwargs):
        await self.coordinator.async_activate_one_click(self.shortcut_id)
