"""Lighting including RGB, white temperature, brightness and dynamic scenes."""

from __future__ import annotations
import asyncio
from collections import defaultdict

from homeassistant.components.light import (
    ATTR_BRIGHTNESS, ATTR_COLOR_TEMP_KELVIN, ATTR_EFFECT, ATTR_RGB_COLOR,
    ColorMode, LightEntity, LightEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .api import GoveeError
from .const import COLOR, ON_OFF, RANGE, SEGMENT
from .entity import GoveeToggleEntity
from .models import Device
from .capabilities import segment_indices, segment_mode, MASKED_RING_MODELS, MAIN_PANEL_MODELS


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    coordinator = hass.data["govee_native"][entry.entry_id]
    entities = [GoveeLight(coordinator, d) for d in coordinator.devices.values() if d.kind == "light"]
    for device in coordinator.devices.values():
        indices = segment_indices(device)
        mode = segment_mode(entry.options, device.device_id)
        if mode in ("individual", "both"):
            entities.extend(GoveeSegmentLight(coordinator, device, index) for index in indices)
        if indices and mode in ("grouped", "both"):
            entities.append(GoveeSegmentGroup(coordinator, device))
        if device.sku.upper() in MASKED_RING_MODELS:
            zones = ("mainPanel", "ring") if device.sku.upper() == "H1252" else ("mainPanel",)
            entities.extend(GoveeZoneLight(coordinator, device, zone) for zone in zones)
        elif device.sku.upper() in MAIN_PANEL_MODELS:
            entities.append(GoveeMainPanelLight(coordinator, device))
    async_add_entities(entities)


class GoveeLight(GoveeToggleEntity, LightEntity):
    _attr_name = None

    def __init__(self, coordinator, device: Device):
        super().__init__(coordinator, device, "light")
        self._last_color = None

    @property
    def supported_color_modes(self):
        rgb = self.device.has(COLOR, "colorRgb")
        ct = self.device.has(COLOR, "colorTemperatureK")
        if rgb and ct:
            return {ColorMode.RGB, ColorMode.COLOR_TEMP}
        if rgb:
            return {ColorMode.RGB}
        if ct:
            return {ColorMode.COLOR_TEMP}
        if self.device.has(RANGE, "brightness"):
            return {ColorMode.BRIGHTNESS}
        return {ColorMode.ONOFF}

    @property
    def color_mode(self):
        # Confirmed status takes precedence over HA's last command.
        kelvin = self.device.state(COLOR, "colorTemperatureK")
        if isinstance(kelvin, int) and kelvin > 0 and ColorMode.COLOR_TEMP in self.supported_color_modes:
            return ColorMode.COLOR_TEMP
        rgb = self.device.state(COLOR, "colorRgb")
        if isinstance(kelvin, int) and kelvin == 0 and isinstance(rgb, int) and ColorMode.RGB in self.supported_color_modes:
            return ColorMode.RGB
        if self._last_color in self.supported_color_modes:
            return self._last_color
        # Govee doesn't always expose the active color mode. RGB is a fallback,
        # not a claim about the mode currently active on the physical light.
        for preferred in (ColorMode.RGB, ColorMode.COLOR_TEMP, ColorMode.BRIGHTNESS, ColorMode.ONOFF):
            if preferred in self.supported_color_modes:
                return preferred
        return ColorMode.ONOFF

    @property
    def brightness(self):
        value = self.device.state(RANGE, "brightness")
        if not isinstance(value, (int, float)):
            return None
        upper = self.device.range(RANGE, "brightness").get("max", 100)
        return round(value / upper * 255) if upper else None

    @property
    def rgb_color(self):
        rgb = self.device.state(COLOR, "colorRgb")
        if isinstance(rgb, int):
            return (rgb >> 16 & 255, rgb >> 8 & 255, rgb & 255)
        return None

    @property
    def color_temp_kelvin(self):
        value = self.device.state(COLOR, "colorTemperatureK")
        return value if isinstance(value, int) and 2000 <= value <= 9000 else None

    @property
    def min_color_temp_kelvin(self):
        return self.device.range(COLOR, "colorTemperatureK").get("min", 2000)

    @property
    def max_color_temp_kelvin(self):
        return self.device.range(COLOR, "colorTemperatureK").get("max", 9000)

    @property
    def supported_features(self):
        return LightEntityFeature.EFFECT if self.device.scene_labels else LightEntityFeature(0)

    @property
    def effect_list(self):
        return sorted(self.device.scene_labels) or None

    @property
    def effect(self):
        # The API does not consistently report the active effect. Unknown is preferable.
        return None

    async def async_turn_on(self, **kwargs):
        if kwargs.get(ATTR_BRIGHTNESS) == 0:
            await self.async_turn_off()
            return
        commands = []
        selected_color_mode = None
        if self.device.has(ON_OFF, "powerSwitch") and (
            not kwargs or self.device.state(ON_OFF, "powerSwitch") != 1
        ):
            # A bare light.turn_on must *always* issue a command, even when
            # stale API state claims the device is already on.
            commands.append((ON_OFF, "powerSwitch", 1))
        if ATTR_BRIGHTNESS in kwargs and self.device.has(RANGE, "brightness"):
            upper = self.device.range(RANGE, "brightness").get("max", 100)
            lower = self.device.range(RANGE, "brightness").get("min", 0)
            value = max(lower, min(upper, round(kwargs[ATTR_BRIGHTNESS] / 255 * upper)))
            commands.append((RANGE, "brightness", value))
        if ATTR_RGB_COLOR in kwargs and self.device.has(COLOR, "colorRgb"):
            r, g, b = kwargs[ATTR_RGB_COLOR]
            commands.append((COLOR, "colorRgb", (r << 16) | (g << 8) | b))
            selected_color_mode = ColorMode.RGB
        if ATTR_COLOR_TEMP_KELVIN in kwargs and self.device.has(COLOR, "colorTemperatureK"):
            commands.append((COLOR, "colorTemperatureK", round(kwargs[ATTR_COLOR_TEMP_KELVIN])))
            selected_color_mode = ColorMode.COLOR_TEMP
        if ATTR_EFFECT in kwargs:
            effect = self.device.scene_labels.get(kwargs[ATTR_EFFECT])
            if not effect:
                from homeassistant.exceptions import HomeAssistantError
                raise HomeAssistantError("Unknown Govee scene; refresh app catalogs or Developer API scenes")
            if effect.get("source") == "one_click":
                # Scene itself includes its intended device state; do not
                # pre-send power/brightness/color that can override it.
                if len(kwargs) > 1:
                    from homeassistant.exceptions import HomeAssistantError
                    raise HomeAssistantError("Set a Govee app scene separately from brightness or color")
                await self.coordinator.async_activate_one_click(effect["shortcut_id"])
                return
            commands.append((effect["type"], effect["instance"], effect["value"]))
        failures = []
        for kind, instance, value in commands:
            try:
                await self.coordinator.async_control(self.device, kind, instance, value, refresh=False)
            except (GoveeError, ValueError) as exc:
                failures.append(f"{instance}: {exc}")
        # The coordinator already handles MQTT device feedback. An immediate
        # cloud refresh can overwrite valid push state with stale/invalid API
        # data, and needless polling aggravates quota exhaustion.
        if failures:
            from homeassistant.exceptions import HomeAssistantError
            raise HomeAssistantError("Govee command(s) failed: " + "; ".join(failures))
        if selected_color_mode is not None:
            self._last_color = selected_color_mode
        if self.device.sku.upper() in MAIN_PANEL_MODELS:
            await self._restore_ring()

    async def async_turn_off(self, **kwargs):
        self.device.local_settings["power_off_pending"] = True
        try:
            await self.async_write(ON_OFF, "powerSwitch", 0)
        finally:
            self.device.local_settings.pop("power_off_pending", None)

    async def _restore_ring(self):
        by_color = defaultdict(list)
        for index, rgb in self.device.segment_colors.items():
            by_color[rgb].append(index)
        for rgb, indices in by_color.items():
            await self.async_write(SEGMENT, "segmentedColorRgb", {"segment": indices, "rgb": rgb})


class GoveeSegmentLight(GoveeToggleEntity, LightEntity):
    """One discovered colour region; local colours persist without faking reports."""
    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_color_mode = ColorMode.RGB
    _attr_entity_registry_enabled_default = True

    def __init__(self, coordinator, device, index):
        super().__init__(coordinator, device, f"segment_{index}")
        self.index = index
        self.indices = (index,)
        self._attr_name = f"Segment {index + 1}"

    @property
    def independent_brightness(self):
        return self.device.has(SEGMENT, "segmentedBrightness") or self.device.sku.upper() in MASKED_RING_MODELS

    @property
    def is_on(self):
        if self.device.state(ON_OFF, "powerSwitch") == 0:
            return False
        return any(self.device.segment_colors.get(i, 0) for i in self.indices)

    @property
    def rgb_color(self):
        values = {self.device.segment_colors.get(i) for i in self.indices}
        value = values.pop() if len(values) == 1 else None
        return (value >> 16 & 255, value >> 8 & 255, value & 255) if value is not None else None

    @property
    def brightness(self):
        if not self.independent_brightness:
            return None
        values = {self.device.local_settings.get("segment_brightness", {}).get(str(i)) for i in self.indices}
        value = values.pop() if len(values) == 1 else None
        return round(value * 255 / 100) if value is not None else None

    @property
    def extra_state_attributes(self):
        attributes = super().extra_state_attributes
        attributes.update(state_assumed=True, segments=list(self.indices),
                          independent_brightness=self.independent_brightness)
        return attributes

    async def async_turn_on(self, **kwargs):
        from homeassistant.exceptions import HomeAssistantError
        if ATTR_BRIGHTNESS in kwargs and not self.independent_brightness:
            raise HomeAssistantError("This device has no independent segment brightness; use the main light")
        if kwargs.get(ATTR_BRIGHTNESS) == 0:
            await self.async_turn_off()
            return
        rgb = kwargs.get(ATTR_RGB_COLOR)
        by_color = defaultdict(list)
        for index in self.indices:
            remembered = self.device.local_settings.get("segment_last_colors", {}).get(str(index), 0xFFFFFF)
            value = (rgb[0] << 16 | rgb[1] << 8 | rgb[2]) if rgb else remembered
            by_color[value].append(index)
        for value, indices in by_color.items():
            await self.async_write(SEGMENT, "segmentedColorRgb", {"segment": indices, "rgb": value})
        if ATTR_BRIGHTNESS in kwargs:
            brightness = max(1, min(100, round(kwargs[ATTR_BRIGHTNESS] * 100 / 255)))
            await self.async_write(SEGMENT, "segmentedBrightness", {"segment": list(self.indices), "brightness": brightness})

    async def async_turn_off(self, **kwargs):
        await asyncio.sleep(0)
        if self.device.state(ON_OFF, "powerSwitch") == 0 or self.device.local_settings.get("power_off_pending"):
            self.device.segment_colors.update({i: 0 for i in self.indices})
            self.coordinator.save_runtime_later()
            self.coordinator.async_set_updated_data(dict(self.coordinator.devices))
            return
        await self.async_write(SEGMENT, "segmentedColorRgb", {"segment": list(self.indices), "rgb": 0})


class GoveeSegmentGroup(GoveeSegmentLight):
    """One control for every discovered segment, with shared coordinator state."""
    def __init__(self, coordinator, device):
        GoveeToggleEntity.__init__(self, coordinator, device, "segments_group")
        self.indices = segment_indices(device)
        self._attr_name = "All segments"


class GoveeZoneLight(GoveeToggleEntity, LightEntity):
    """Independent ceiling-light main panel or ring, using a verified mask."""
    _attr_supported_color_modes = {ColorMode.RGB, ColorMode.COLOR_TEMP}
    _attr_min_color_temp_kelvin = 2700
    _attr_max_color_temp_kelvin = 6500

    def __init__(self, coordinator, device, zone):
        super().__init__(coordinator, device, f"zone_{zone}")
        self.zone = zone
        self._toggle_key = ("devices.capabilities.toggle", "mainLightToggle" if zone == "mainPanel" else "backgroundLightToggle")
        self._attr_name = "Main panel" if zone == "mainPanel" else "Ring"

    @property
    def is_on(self):
        return self.device.state(ON_OFF, "powerSwitch") != 0 and bool(self.device.state(*self._toggle_key))

    @property
    def color_mode(self):
        zone = self.device.local_settings.get("zones", {}).get(self.zone, {})
        return ColorMode.COLOR_TEMP if zone.get("kelvin") else ColorMode.RGB

    @property
    def color_temp_kelvin(self):
        return self.device.local_settings.get("zones", {}).get(self.zone, {}).get("kelvin")

    @property
    def rgb_color(self):
        value = self.device.local_settings.get("zones", {}).get(self.zone, {}).get("rgb")
        return (value >> 16 & 255, value >> 8 & 255, value & 255) if value is not None else None

    @property
    def brightness(self):
        value = self.device.local_settings.get("zones", {}).get(self.zone, {}).get("brightness")
        return round(value * 255 / 100) if value is not None else None

    async def async_turn_on(self, **kwargs):
        from .const import TOGGLE
        if kwargs.get(ATTR_BRIGHTNESS) == 0:
            await self.async_turn_off()
            return
        await self.async_write(TOGGLE, self._toggle_key[1], 1)
        if ATTR_RGB_COLOR in kwargs:
            r, g, b = kwargs[ATTR_RGB_COLOR]
            await self.async_write("govee_native.zone", self.zone,
                {"instance": "segmentedColorRgb", "rgb": r << 16 | g << 8 | b})
        elif ATTR_COLOR_TEMP_KELVIN in kwargs:
            await self.async_write("govee_native.zone", self.zone,
                {"instance": "segmentedColorTemperature", "kelvin": int(kwargs[ATTR_COLOR_TEMP_KELVIN])})
        if ATTR_BRIGHTNESS in kwargs:
            await self.async_write("govee_native.zone", self.zone,
                {"instance": "segmentedBrightness", "brightness": max(1, round(kwargs[ATTR_BRIGHTNESS] * 100 / 255))})

    async def async_turn_off(self, **kwargs):
        from .const import TOGGLE
        await self.async_write(TOGGLE, self._toggle_key[1], 0)


class GoveeMainPanelLight(GoveeLight):
    """H1270's main panel; keep the ring's segment overlay when setting it."""
    def __init__(self, coordinator, device):
        GoveeToggleEntity.__init__(self, coordinator, device, "main_panel")
        self._last_color = None
        self._attr_name = "Main panel"

    @property
    def supported_features(self):
        return LightEntityFeature(0)

    @property
    def is_on(self):
        if self.device.state(ON_OFF, "powerSwitch") == 0:
            return False
        if self.device.state(COLOR, "colorTemperatureK"):
            return True
        return bool(self.device.state(COLOR, "colorRgb"))

    async def async_turn_off(self, **kwargs):
        rgb = self.device.state(COLOR, "colorRgb")
        kelvin = self.device.state(COLOR, "colorTemperatureK")
        if rgb:
            self.device.local_settings["panel_last_rgb"] = rgb
        if kelvin:
            self.device.local_settings["panel_last_kelvin"] = kelvin
        await self.async_write(COLOR, "colorRgb", 0)
        await self._restore_ring()

    async def async_turn_on(self, **kwargs):
        if not any(k in kwargs for k in (ATTR_RGB_COLOR, ATTR_COLOR_TEMP_KELVIN)):
            rgb = self.device.local_settings.get("panel_last_rgb")
            if rgb:
                kwargs[ATTR_RGB_COLOR] = (rgb >> 16 & 255, rgb >> 8 & 255, rgb & 255)
            else:
                kwargs[ATTR_COLOR_TEMP_KELVIN] = self.device.local_settings.get("panel_last_kelvin", 4000)
        await super().async_turn_on(**kwargs)
