"""Persist only command-derived colours and counters, never reachability."""
from __future__ import annotations

from homeassistant.helpers.storage import Store


class RuntimeStore:
    def __init__(self, hass, entry_id):
        self.store = Store(hass, 1, f"govee_native.{entry_id}.runtime")
        self.data = {}

    async def load(self):
        raw = await self.store.async_load()
        self.data = raw if isinstance(raw, dict) else {}
        for key in ("segments", "energy", "cloud_budget"):
            if not isinstance(self.data.get(key), dict):
                self.data[key] = {}
        return self.data

    def save_later(self, factory):
        self.store.async_delay_save(factory, 1)

    async def save(self, data):
        await self.store.async_save(data)


def restore_segments(device, data):
    """Restored local colours remain assumed; they do not prove a live device."""
    from .capabilities import segment_indices
    if not isinstance(data, dict):
        return
    zones = data.get("zones", {})
    if isinstance(zones, dict):
        device.local_settings["zones"] = {
            zone: {key: value for key, value in values.items()
                   if key in ("rgb", "brightness", "kelvin") and type(value) is int
                   and 0 <= value <= (0xFFFFFF if key == "rgb" else 6500 if key == "kelvin" else 100)}
            for zone, values in zones.items() if zone in ("mainPanel", "ring") and isinstance(values, dict)}
    allowed = set(segment_indices(device))
    for key, rgb in data.get("colors", {}).items() if isinstance(data.get("colors"), dict) else ():
        try:
            index = int(key)
        except (ValueError, TypeError):
            continue
        if index in allowed and type(rgb) is int and 0 <= rgb <= 0xFFFFFF:
            device.segment_colors[index] = rgb
    remembered = data.get("remembered", {})
    if isinstance(remembered, dict):
        device.local_settings["segment_last_colors"] = {
            str(i): rgb for i in allowed if type(rgb := remembered.get(str(i))) is int and 0 < rgb <= 0xFFFFFF}
    brightness = data.get("brightness", {})
    if isinstance(brightness, dict):
        device.local_settings["segment_brightness"] = {
            str(i): v for i in allowed if type(v := brightness.get(str(i))) is int and 0 <= v <= 100}


def segment_snapshot(device):
    return {"colors": {str(k): v for k, v in device.segment_colors.items()},
            "remembered": device.local_settings.get("segment_last_colors", {}),
            "brightness": device.local_settings.get("segment_brightness", {}),
            "zones": device.local_settings.get("zones", {})}
