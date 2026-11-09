"""Read-only, SKU-gated state decoders adapted from Govee Cloud Integration.

Field layouts and model gates: lasswellt/govee-homeassistant 2026.10.2.
See THIRD_PARTY_NOTICES.md. Unknown opcodes never become sensor readings.
"""
from __future__ import annotations

import base64
import binascii
import math

from .capabilities import PROBE_MODELS, THERMOMETER_MODELS, apply_outlet_report
from .const import PROPERTY, TOGGLE


def state_envelope(packet):
    """Read a status envelope, including older JSON-in-msg firmware."""
    import json
    wrapped = packet.get("msg")
    if isinstance(wrapped, str):
        try:
            wrapped = json.loads(wrapped)
        except (ValueError, TypeError):
            wrapped = None
    return wrapped if isinstance(wrapped, dict) and ("state" in wrapped or "op" in wrapped) else packet


def frames_from(packet):
    op = packet.get("op", {})
    commands = op.get("command", []) if isinstance(op, dict) else []
    if not isinstance(commands, list) or len(commands) > 200:
        return []
    frames = []
    for block in commands:
        if not isinstance(block, str) or len(block) > 8192:
            continue
        try:
            frames.append(base64.b64decode(block, validate=True))
        except (ValueError, binascii.Error):
            pass
    return frames


def extra_report(device, packet):
    sku = device.sku.upper()
    packet = state_envelope(packet)
    result = {}
    state = packet.get("state", {})
    if isinstance(state, dict):
        result.update(apply_outlet_report(device, state.get("onOff")))
        # Common read-only values are accepted only when the device advertises
        # the corresponding property. No temperature unit is guessed here.
        for name in ("sensorTemperature", "sensorHumidity", "battery", "airQuality", "pm25", "filterLifeTime"):
            value = state.get(name)
            if type(value) in (int, float) and math.isfinite(value) and device.has(PROPERTY, name):
                result[(PROPERTY, name)] = value
    frames = frames_from(packet)
    if sku in PROBE_MODELS:
        # Some probe records span multiple MQTT base64 blocks.
        joined = b"".join(frames)
        candidates = [joined] if joined else []
        for raw in candidates:
            if len(raw) >= 26 and raw[1] == 0x24:
                probe = raw[2]
                if 1 <= probe <= PROBE_MODELS[sku]:
                    for offset, name in ((22, "Core"), (24, "Ambient")):
                        value = probe_temperature(raw, offset)
                        if value is not None:
                            result[(PROPERTY, f"probe{probe}{name}Temperature")] = value
            elif len(raw) >= 40 and raw[1] == 0x0F:
                for probe in range(1, PROBE_MODELS[sku] + 1):
                    offset = 10 + (probe - 1) * 16
                    for delta, name in ((0, "Core"), (6, "Ambient")):
                        value = probe_temperature(raw, offset + delta)
                        if value is not None:
                            result[(PROPERTY, f"probe{probe}{name}Temperature")] = value
    for raw in frames:
        if sku == "H7152":
            if len(raw) >= 6 and raw[:3] == b"\xaa\x10\x81":
                packed = int.from_bytes(raw[3:6], "big")
                temp, hum = (packed // 1000) / 10, (packed % 1000) / 10
                if -40 <= temp <= 125 and 0 <= hum <= 100:
                    result.update({(PROPERTY, "sensorTemperature"): temp, (PROPERTY, "sensorHumidity"): hum})
            elif len(raw) >= 13 and raw[:2] == b"\xaa\x17":
                result[(PROPERTY, "pumpFault")] = int(raw[12] == 1)
            elif len(raw) >= 10 and raw[:2] == b"\xaa\x19":
                result[(PROPERTY, "drainMode")] = "pump" if raw[9] == 1 else "tank"
        elif sku == "H5086" and len(raw) >= 16 and raw[:2] == b"\xaa\x19":
            # Replies to other app clients can contain an unrelated counter.
            from .energy_counter import answers_other_client, uptime_from_sta
            if answers_other_client(packet.get("sta")):
                continue
            result.update({(PROPERTY, "onTime"): int.from_bytes(raw[2:5], "big"),
                           (PROPERTY, "energyToday"): int.from_bytes(raw[5:8], "big") / 10000,
                           (PROPERTY, "voltage"): int.from_bytes(raw[8:10], "big") / 100,
                           (PROPERTY, "current"): int.from_bytes(raw[10:12], "big") / 100,
                           (PROPERTY, "power"): int.from_bytes(raw[12:15], "big") / 100,
                           (PROPERTY, "powerFactor"): raw[15]})
            uptime = uptime_from_sta(packet.get("sta"))
            if uptime is not None:
                device.local_settings["reported_uptime"] = uptime
        elif sku == "H5106" and len(raw) == 20 and raw[:2].hex() not in {
            "0000", "0003", "0100", "0101", "0102", "0103", "331a", "3315", "aa0d", "aa0e"}:
            temp = int.from_bytes(raw[:2], "big", signed=True) / 100
            hum = int.from_bytes(raw[9:11], "big") / 100
            if -40 <= temp <= 125 and 0 <= hum <= 100:
                result.update({(PROPERTY, "sensorTemperature"): round(temp, 1),
                               (PROPERTY, "sensorHumidity"): round(hum, 1),
                               (PROPERTY, "pm25"): int.from_bytes(raw[18:20], "big")})
        elif sku == "H1252" and len(raw) == 20 and raw[:2] == b"\xaa\x30":
            if raw[2] in (0, 1) and raw[3] in (0, 1):
                from .h6199 import valid_frame
                if valid_frame(raw):
                    result[(TOGGLE, "nativeMainPanel")] = raw[2]
                    result[(TOGGLE, "nativeRing")] = raw[3]
                    result[(TOGGLE, "mainLightToggle")] = raw[2]
                    result[(TOGGLE, "backgroundLightToggle")] = raw[3]
    return {k: v for k, v in result.items() if device.has(*k)}


def probe_temperature(raw, offset):
    if offset + 2 > len(raw) or raw[offset:offset + 2] == b"\xff\xff":
        return None
    value = int.from_bytes(raw[offset:offset + 2], "big", signed=True) / 100
    return value if -100 <= value <= 400 else None


def app_thermometer_report(device):
    """BFF stores the known thermometer family's tem/hum in hundredths."""
    if device.sku.upper() not in THERMOMETER_MODELS:
        return {}
    last = device.app_data.get("last", {})
    if not isinstance(last, dict):
        return {}
    result = {}
    fields = (("tem", "temperature", "sensorTemperature", (-150, 300)),
              ("hum", "humidity", "sensorHumidity", (0, 100)),
              ("tem2", "temperature2", "sensorTemperature2", (-150, 300)))
    for key, plain, name, bounds in fields:
        raw = last.get(key, last.get(plain))
        if type(raw) in (int, float) and math.isfinite(raw) and raw not in (65535, 32767, -32768, -1):
            value = raw / 100 if key in last and type(raw) is int else raw
            if bounds[0] <= value <= bounds[1] and device.has(PROPERTY, name):
                result[(PROPERTY, name)] = round(value, 2)
    return result
