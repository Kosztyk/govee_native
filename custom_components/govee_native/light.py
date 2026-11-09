"""Lighting including RGB, white temperature, brightness and dynamic scenes."""

from __future__ import annotations

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


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    coordinator = hass.data["govee_native"][entry.entry_id]
    entities = [GoveeLight(coordinator, d) for d in coordinator.devices.values() if d.kind == "light"]
    entities.extend(GoveeSegmentLight(coordinator, d, index)
        for d in coordinator.devices.values() if d.kind == "light" and d.sku == "H6199"
        for index in range(15))
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

    async def async_turn_off(self, **kwargs):
        await self.async_write(ON_OFF, "powerSwitch", 0)


class GoveeSegmentLight(GoveeToggleEntity, LightEntity):
    """H6199's 15 color regions; disabled by default to keep the device page tidy.

    Segment power and brightness are not independently controlled by this
    protocol. An RGB-only entity uses black for off and remembers its color.
    """
    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_color_mode = ColorMode.RGB
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator, device, index):
        super().__init__(coordinator, device, f"segment_{index}")
        self.index = index
        self._attr_name = f"Segment {index + 1}"
        self._last_rgb = 0xFFFFFF

    @property
    def is_on(self):
        value = self.device.segment_colors.get(self.index)
        if self.device.state(ON_OFF, "powerSwitch") == 0:
            return False
        return bool(value)

    @property
    def rgb_color(self):
        value = self.device.segment_colors.get(self.index)
        return ((value >> 16 & 255), (value >> 8 & 255), (value & 255)) if value is not None else None

    @property
    def extra_state_attributes(self):
        attributes = super().extra_state_attributes
        # H6199 does not return independent region color status.
        attributes["state_assumed"] = True
        return attributes

    async def async_turn_on(self, **kwargs):
        if ATTR_BRIGHTNESS in kwargs:
            from homeassistant.exceptions import HomeAssistantError
            raise HomeAssistantError("H6199 segment brightness is not independent; use the main light")
        rgb = kwargs.get(ATTR_RGB_COLOR)
        value = (rgb[0] << 16 | rgb[1] << 8 | rgb[2]) if rgb else self._last_rgb
        await self.async_write(SEGMENT, "segmentedColorRgb", {"segment": [self.index], "rgb": value})
        self._last_rgb = value or self._last_rgb

    async def async_turn_off(self, **kwargs):
        await self.async_write(SEGMENT, "segmentedColorRgb", {"segment": [self.index], "rgb": 0})
