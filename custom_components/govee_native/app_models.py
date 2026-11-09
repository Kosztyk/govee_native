"""Translate private BFF device data to safe HA discovery and diagnostics."""
from __future__ import annotations
import json
from typing import Any

from .app_api import normalize_app_device


def likely_light(sku: str, raw: dict) -> bool:
    """Only populate generic light controls on recognizable lighting SKUs.

    Not all Govee SKUs are lighting; don't turn heaters/plugs into bulbs.
    """
    category = str(raw.get("deviceTypeName") or raw.get("category") or "").lower()
    if category in ("light", "lights", "lighting"):
        return True
    if "fan" in category or "heater" in category or "humid" in category:
        return False
    return sku.upper().startswith("H6") or (sku.upper().startswith("H70") and not sku.upper().startswith("H710"))


def discover_app_device(raw: dict) -> dict:
    """Make a Device.from_payload-compatible definition for private-only devices.

    MQTT power/brightness/color packets are proven shapes, but individual
    model support is not: advertise only recognizable WiFi lamps with topics.
    """
    values = normalize_app_device(raw)
    if not values:
        return {}
    is_light = likely_light(values["sku"], raw)
    is_plug = values["sku"].upper() in ("H5080", "H5083", "H5086", "H5160", "H5161")
    caps: list[dict] = []
    if (is_light or is_plug) and (values["topic"] or (values["sku"] == "H6199" and values.get("ble_address"))):
        caps = [
            {"type": "devices.capabilities.on_off", "instance": "powerSwitch", "parameters": {
                "dataType": "ENUM", "options": [{"name":"Off","value":0},{"name":"On","value":1}]}},
            {"type": "devices.capabilities.range", "instance": "brightness", "parameters": {
                "dataType":"INTEGER", "range":{"min":0,"max":100}}},
            {"type": "devices.capabilities.color_setting", "instance": "colorRgb", "parameters": {
                "dataType":"INTEGER", "range":{"min":0,"max":16777215}}},
        ]
        if is_plug:
            caps=caps[:1]
        elif values["sku"].upper() == "H6199":
            # Confirmed by the legacy Developer API for this exact SKU.
            caps.append({"type": "devices.capabilities.color_setting", "instance": "colorTemperatureK",
                         "parameters": {"dataType": "INTEGER", "range": {"min": 2000, "max": 9000}}})
    return {
        "sku":values["sku"], "device":values["device"], "deviceName":values["name"],
        "type":"devices.types.light" if is_light else ("devices.types.socket" if is_plug else "devices.types.private_app"),
        "capabilities":caps, "private":values,
    }


def app_readings(last: dict) -> dict[str, Any]:
    """Read key names that were observed in BFF lastDeviceData.

    Ambiguous raw temperature is exposed without claiming a Celsius unit.
    """
    if not isinstance(last,dict):
        return {}
    found = {}
    for k in ("tem", "hum", "temperature", "humidity", "battery", "onOff", "brightness"):
        v=last.get(k)
        if isinstance(v,(int,float)) and not isinstance(v,bool):
            found[k]=v
    return found


def mqtt_state_packet(raw: dict) -> tuple[str, dict]:
    """Decode *reported* Govee account AWS IoT state, never command echoes.

    Real replies put the state at top level: {device, sku, state:{onOff,...}}.
    Older devices can put the entire reply as a JSON string inside ``msg``.
    The sent-command envelope {msg:{cmd,data,...}} is NOT device feedback.
    """
    if not isinstance(raw, dict):
        return "", {}
    packet = raw
    wrapped = raw.get("msg")
    if "state" not in raw and wrapped is not None:
        if isinstance(wrapped, str):
            try:
                wrapped = json.loads(wrapped)
            except (ValueError, TypeError):
                return "", {}
        if isinstance(wrapped, dict) and "state" in wrapped:
            packet = wrapped
    dev = packet.get("device") or packet.get("deviceId") or raw.get("device")
    identity = str(dev) if isinstance(dev, str) else ""
    state = packet.get("state")
    if not isinstance(state, dict):
        # No state: ignore commands/ACKs and avoid false confirmations.
        return identity, {}
    values: dict[tuple[str, str], Any] = {}
    power = state.get("onOff")
    if isinstance(power, bool):
        values[("devices.capabilities.on_off", "powerSwitch")] = int(power)
    elif type(power) is int and power in (0, 1):
        values[("devices.capabilities.on_off", "powerSwitch")] = power
    brightness = state.get("brightness")
    if type(brightness) in (int, float) and 0 <= brightness <= 100:
        values[("devices.capabilities.range", "brightness")] = brightness
    kelvin = state.get("colorTemInKelvin")
    if type(kelvin) is int and 2000 <= kelvin <= 9000:
        values[("devices.capabilities.color_setting", "colorTemperatureK")] = kelvin
    elif kelvin == 0:
        # A confirmed color-mode report, not a bogus Kelvin readback.
        values[("devices.capabilities.color_setting", "colorTemperatureK")] = 0
    color = state.get("color")
    if isinstance(color, dict) and all(type(color.get(k)) is int and 0 <= color[k] <= 255 for k in ("r", "g", "b")):
        rgb = (color["r"] << 16) | (color["g"] << 8) | color["b"]
        # Govee reports RGB=(0,0,0) in white-temperature mode; that
        # sentinel should not overwrite the last actual color.
        if not (rgb == 0 and type(kelvin) is int and kelvin > 0):
            values[("devices.capabilities.color_setting", "colorRgb")] = rgb
    return identity, values
