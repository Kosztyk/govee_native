"""Model-gated native frames; no assumptions based on an advertising name.

Masked ceiling-light protocol adapted from lasswellt/govee-homeassistant.
See THIRD_PARTY_NOTICES.md for attribution and the MIT license.
"""
from .const import COLOR, ON_OFF, RANGE, SEGMENT, TOGGLE
from .h6199 import control_frames as h6199_frames, frame
from .capabilities import MASKED_RING_MODELS

BLE_MODELS = {"H6199", "H1270"}


def control_frames(device, kind, instance, value):
    if device.sku.upper() == "H6199":
        return h6199_frames(device, kind, instance, value)
    if device.sku.upper() in MASKED_RING_MODELS:
        if device.sku.upper() == "H1252" and kind == TOGGLE and instance in ("nativeMainPanel", "nativeRing", "mainLightToggle", "backgroundLightToggle"):
            return [frame(0x33, 0x30, 0 if instance in ("nativeMainPanel", "mainLightToggle") else 1, int(bool(value)))]
        if kind == SEGMENT and instance in ("segmentedColorRgb", "segmentedBrightness"):
            indices = value["segment"]
            if not indices or any(type(i) is not int or not 0 <= i < 16 for i in indices):
                raise ValueError("Ring segments must be 0..15")
            mask = sum(1 << i for i in set(indices))
            return masked_frame(instance, value, mask)
        if kind == "govee_native.zone" and instance in ("mainPanel", "ring"):
            mask = 1 << 16 if instance == "mainPanel" else 0xFFFF
            return masked_frame(value["instance"], value, mask)
    if device.sku.upper() == "H1270":
        if kind == ON_OFF and instance == "powerSwitch":
            return [frame(0x33, 1, int(bool(value)))]
        if kind == RANGE and instance == "brightness":
            return [frame(0x33, 4, round(value * 255 / 100))]
        if kind == COLOR and instance == "colorRgb":
            return [frame(0x33, 5, 0x15, 1, value >> 16 & 255, value >> 8 & 255, value & 255,
                          0, 0, 0, 0, 0, 0xFF, 0x7F)]
    return None


def masked_frame(instance, value, mask):
    if instance == "segmentedBrightness":
        return [frame(0x33, 5, 0x15, 2, value["brightness"], mask & 255, mask >> 8 & 255, mask >> 16 & 255)]
    if instance == "segmentedColorTemperature":
        from homeassistant.util.color import color_temperature_to_rgb
        kelvin = value["kelvin"]
        white = [round(c) for c in color_temperature_to_rgb(kelvin)]
        return [frame(0x33, 5, 0x15, 1, 255, 255, 255, kelvin >> 8, kelvin & 255,
                      *white, mask & 255, mask >> 8 & 255, mask >> 16 & 255)]
    rgb = value["rgb"]
    return [frame(0x33, 5, 0x15, 1, rgb >> 16 & 255, rgb >> 8 & 255, rgb & 255,
                  0, 0, 0, 0, 0, mask & 255, mask >> 8 & 255, mask >> 16 & 255)]
