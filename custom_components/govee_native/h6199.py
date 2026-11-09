"""H6199 Immersion protocol, scoped to the device used in this installation.

20-byte frames and mode values follow Obi2000's H6199 hardware captures.
They can be sent directly over GATT or base64-encoded in MQTT ptReal.
This is independent protocol evidence; the protected 7.7.11 lighting code
was NOT decompiled. See APK_AUDIT.md for exact sources and limitations.
"""
from __future__ import annotations

from functools import reduce
from operator import xor
from typing import Any

from .const import COLOR, MODE, ON_OFF, RANGE, SEGMENT, TOGGLE

SKU = "H6199"
WRITE_UUID = "00010203-0405-0607-0809-0a0b0c0d2b11"
NOTIFY_UUID = "00010203-0405-0607-0809-0a0b0c0d2b10"
NATIVE_SCENES = {"Sunrise": 0, "Sunset": 1, "Movie": 4, "Dating": 5,
                 "Romantic": 7, "Twinkle": 8, "Candlelight": 9, "Snowflake": 15}
MUSIC_V3 = {"Rhythm": 3, "Spectrum": 4, "Energetic": 5, "Rolling": 6,
            "Bloom": 48, "Shiny": 49, "Separation": 50, "Hopping": 51,
            "Piano keys": 52, "Fountain": 53, "Day and night": 55}


def frame(*payload: int) -> bytes:
    if not 1 <= len(payload) <= 19 or any(type(x) is not int or not 0 <= x <= 255 for x in payload):
        raise ValueError("A Govee frame requires 1..19 bytes")
    data = bytes(payload).ljust(19, b"\0")
    return data + bytes([reduce(xor, data)])


def valid_frame(data: bytes) -> bool:
    return len(data) == 20 and reduce(xor, data) == 0


def add_capabilities(device) -> None:
    if device.sku != SKU or device.kind != "light":
        return

    def enum(kind, instance, options):
        device.capabilities.setdefault((kind, instance), {"type": kind, "instance": instance,
            "parameters": {"dataType": "ENUM", "options": [
                {"name": name, "value": value} for name, value in options.items()]}})

    enum(TOGGLE, "gradientToggle", {"Off": 0, "On": 1})
    enum(TOGGLE, "dreamViewToggle", {"Off": 0, "On": 1})
    enum(MODE, "backlightMode", {"Color": 0, "Video": 1, "Music": 2})
    enum(MODE, "videoArea", {"Partial": 0, "All": 1})
    enum(MODE, "videoStyle", {"Movie": 0, "Game": 1})
    music = MUSIC_V3 if device.local_settings.get("protocol_profile") == "updated" else {f"Mode {n}": n for n in (0, 2, 3, 5)}
    device.capabilities.pop((MODE, "nativeMusicMode"), None)
    enum(MODE, "nativeMusicMode", music)
    enum(MODE, "nativeScene", NATIVE_SCENES)
    for instance, maximum in (("videoSaturation", 100), ("musicSensitivity", 99)):
        device.capabilities.setdefault((RANGE, instance), {"type": RANGE, "instance": instance,
            "parameters": {"dataType": "INTEGER", "range": {"min": 0, "max": maximum}}})
    if not device.has(SEGMENT, "segmentedColorRgb"):
        device.capabilities[(SEGMENT, "segmentedColorRgb")] = {
            "type": SEGMENT, "instance": "segmentedColorRgb", "parameters": {
                "dataType": "STRUCT", "fields": [
                    {"fieldName": "segment", "dataType": "ARRAY", "required": True,
                     "size": {"min": 1, "max": 15}, "options": [{"value": i} for i in range(15)]},
                    {"fieldName": "rgb", "dataType": "INTEGER", "required": True,
                     "range": {"min": 0, "max": 16777215}}]}}
    for name, value in NATIVE_SCENES.items():
        device.scene_labels[f"Native: {name}"] = {"type": MODE, "instance": "nativeScene", "value": value}


def control_frames(device, kind: str, instance: str, value: Any) -> list[bytes] | None:
    """Only explicitly sourced H6199 controls; never reuse on another SKU."""
    if device.sku != SKU:
        return None
    settings = device.local_settings
    updated = settings.get("protocol_profile") == "updated"

    def color(rgb, mask):
        r, g, b = rgb >> 16 & 255, rgb >> 8 & 255, rgb & 255
        if updated:
            return frame(0x33, 5, 0x15, 1, r, g, b, 0, 0, 0, 0, 0, mask & 255, mask >> 8)
        return frame(0x33, 5, 0x0B, r, g, b, 0, 0, mask & 255, mask >> 8)
    if kind == ON_OFF and instance == "powerSwitch":
        return [frame(0x33, 0x01, int(bool(value)))]
    if kind == RANGE and instance == "brightness":
        upper = 255 if updated else 254
        return [frame(0x33, 0x04, max(0, min(upper, round(value * upper / 100))))]
    if kind == COLOR and instance == "colorRgb":
        return [color(value, 0x7FFF)]
    if kind == SEGMENT and instance == "segmentedColorRgb":
        segments, rgb = value["segment"], value["rgb"]
        if not segments or any(type(i) is not int or not 0 <= i < 15 for i in segments):
            raise ValueError("H6199 segment indices must be 0..14")
        mask = sum(1 << i for i in set(segments))
        return [color(rgb, mask)]
    if kind == TOGGLE and instance == "gradientToggle":
        return [frame(0x33, 0xA3, int(bool(value)))]
    if kind == MODE and instance == "nativeScene":
        return [frame(0x33, 0x05, 0x04, value)]
    # There is no video-off opcode: leave Video by setting a solid color.
    if (kind == TOGGLE and instance == "dreamViewToggle" and not value) or (
        kind == MODE and instance == "backlightMode" and value == 0
    ):
        rgb = device.state(COLOR, "colorRgb")
        return control_frames(device, COLOR, "colorRgb", rgb if type(rgb) is int else 0xFFFFFF)
    video = ((kind == TOGGLE and instance == "dreamViewToggle" and value)
             or (kind == MODE and instance in ("videoArea", "videoStyle"))
             or (kind == MODE and instance == "backlightMode" and value == 1)
             or (kind == RANGE and instance == "videoSaturation"))
    if video:
        values = {"videoArea": 1, "videoStyle": 0, "videoSaturation": 50, **settings, instance: value}
        return [frame(0x33, 0x05, 0, values["videoArea"], values["videoStyle"], values["videoSaturation"])]
    music = ((kind == MODE and instance == "nativeMusicMode")
             or (kind == MODE and instance == "backlightMode" and value == 2)
             or (kind == RANGE and instance == "musicSensitivity"))
    if music:
        values = {"nativeMusicMode": 3 if updated else 0, "musicSensitivity": 50, **settings, instance: value}
        return [frame(0x33, 0x05, 0x13 if updated else 0x0C, values["nativeMusicMode"], values["musicSensitivity"])]
    # Kelvin encodings include a device-specific white-balance lookup. Keep
    # white temperature on native MQTT/LAN/OpenAPI rather than inventing RGB.
    return None
