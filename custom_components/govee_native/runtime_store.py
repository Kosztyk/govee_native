"""Persist last-known settings, colours and counters, never reachability."""
from __future__ import annotations

from copy import deepcopy

from homeassistant.helpers.storage import Store


class RuntimeStore:
    def __init__(self, hass, entry_id):
        self.store = Store(hass, 1, f"govee_native.{entry_id}.runtime")
        self.data = {}

    async def load(self):
        raw = await self.store.async_load()
        self.data = raw if isinstance(raw, dict) else {}
        for key in ("segments", "settings", "energy", "cloud_budget"):
            if not isinstance(self.data.get(key), dict):
                self.data[key] = {}
        return self.data

    def save_later(self, factory):
        self.store.async_delay_save(factory, 1)

    async def save(self, data):
        await self.store.async_save(data)


def restore_setting(device, kind, instance, value):
    """Seed a missing value only; restoring never sends or confirms a command."""
    if not device.valid_setting(kind, instance, value) or device.state(kind, instance) is not None:
        return False
    device.assume(kind, instance, deepcopy(value))
    # The command grace period belongs to newly sent commands. A live poll
    # should be able to replace this restored assumption immediately.
    device.state_times[(kind, instance)] = 0
    return True


def restore_settings(device, data):
    if not isinstance(data, dict) or data.get("sku") != device.sku:
        return
    values = data.get("values", [])
    if isinstance(values, list):
        for item in values:
            if not isinstance(item, dict):
                continue
            kind, instance = item.get("type"), item.get("instance")
            if isinstance(kind, str) and isinstance(instance, str):
                restore_setting(device, kind, instance, item.get("value"))
    scene = data.get("scene")
    if isinstance(scene, str) and "selected_scene" not in device.local_settings:
        device.local_settings["selected_scene"] = scene


def settings_snapshot(device):
    """Persist validated setting values, excluding readings and power state."""
    return {"sku": device.sku, "values": [
        {"type": kind, "instance": instance, "value": deepcopy(value)}
        for (kind, instance), value in device.states.items()
        if device.valid_setting(kind, instance, value)],
        "scene": device.local_settings.get("selected_scene")}


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
