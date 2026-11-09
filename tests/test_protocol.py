import json
import math
from pathlib import Path
import pytest

from custom_components.govee_native.app_api import normalize_app_device, parse_response, AppAuthenticationError
from custom_components.govee_native.app_models import mqtt_state_packet
from custom_components.govee_native.app_mqtt import mqtt_packet, subscription_refused, mqtt_control
from custom_components.govee_native.bluetooth import valid_address
from custom_components.govee_native.const import COLOR, MODE, ON_OFF, RANGE, SEGMENT, TOGGLE
from custom_components.govee_native.h6199 import frame, valid_frame, control_frames
from custom_components.govee_native.models import valid_value
from conftest import make_device


def test_modern_write_envelope_has_account_reply_route():
    msg = mqtt_packet("turn", {"val": 1}, account_topic="GA/test")["msg"]
    assert msg["accountTopic"] == "GA/test"
    assert msg["origin"] == 32
    assert msg["data"] == {"val": 1}
    assert msg["type"] == 1
    assert msg["transaction"].startswith("v_")


def test_query_is_not_a_write():
    msg = mqtt_packet("status", query=True, cmd_version=2, account_topic="GA/test")["msg"]
    assert (msg["type"], msg["cmdVersion"]) == (0, 2)
    assert "data" not in msg and "origin" not in msg


@pytest.mark.parametrize("bad", [True, -1, 101, "50", None])
def test_invalid_brightness_is_not_published(bad):
    with pytest.raises(ValueError):mqtt_control(RANGE, "brightness", bad, "H6199")


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, True, 1.5, "1"])
def test_integer_schema_rejects_nonfinite_or_fractional_values(bad):
    assert not valid_value({"dataType": "INTEGER", "range": {"min": 0, "max": 100}}, bad)


@pytest.mark.parametrize("grant", [128, [1, 128]])
def test_subscription_denial(grant):
    assert subscription_refused(grant)


@pytest.mark.parametrize("grant", [0, 1, [1], None])
def test_subscription_acceptance(grant):
    assert not subscription_refused(grant)


@pytest.mark.parametrize("packet", [
    {"msg": {"cmd": "turn", "data": {"val": 1}, "device": "id", "type": 1}},
    {"device": "id", "cmd": "turn", "data": {"val": 1}},
    {"msg": "not json"}, [], None])
def test_command_echo_is_never_device_feedback(packet):
    assert mqtt_state_packet(packet)[1] == {}


@pytest.mark.parametrize("wrapped", [False, True, "json"])
def test_reported_power_and_color(wrapped):
    raw = {"device": "id", "state": {"onOff": 1, "brightness": 29,
           "color": {"r": 255, "g": 32, "b": 20}, "colorTemInKelvin": 0}}
    if wrapped:raw = {"msg": json.dumps(raw) if wrapped == "json" else raw}
    identity, states = mqtt_state_packet(raw)
    assert identity == "id"
    assert states[(ON_OFF, "powerSwitch")] == 1
    assert states[(RANGE, "brightness")] == 29
    assert states[(COLOR, "colorRgb")] == 0xFF2014


def test_white_temperature_report_does_not_replace_rgb_with_black():
    _, values = mqtt_state_packet({"device": "id", "state": {
        "color": {"r": 0, "g": 0, "b": 0}, "colorTemInKelvin": 4500}})
    assert (COLOR, "colorRgb") not in values
    assert values[(COLOR, "colorTemperatureK")] == 4500


def test_preserve_bluetooth_and_firmware_metadata():
    values = normalize_app_device({"device": "id", "sku": "H6199", "deviceExt": {
        "deviceSettings": '{"bleAddress":"AA:BB:CC:DD:EE:FF","bleName":"Govee_test","versionSoft":"2.0","topic":"GD/test"}'}})
    assert values["ble_address"] == "AA:BB:CC:DD:EE:FF"
    assert values["firmware"] == "2.0"


@pytest.mark.parametrize("address", ["10:20:30:40:50:60:70:80", "", None, "Govee_H6199", "AA-BB-CC-DD-EE-FF"])
def test_never_guess_bluetooth_mac_from_cloud_id(address):
    assert not valid_address(address)


def test_exact_captured_power_packets():
    d = make_device()
    assert control_frames(d, ON_OFF, "powerSwitch", 1)[0].hex() == "3301010000000000000000000000000000000033"
    assert control_frames(d, ON_OFF, "powerSwitch", 0)[0].hex() == "3301000000000000000000000000000000000032"


def test_exact_captured_red_packet():
    assert control_frames(make_device(), COLOR, "colorRgb", 0xFF0000)[0].hex() == "33050bff00000000ff7f00000000000000000042"


def test_exact_captured_gradient_packet():
    assert control_frames(make_device(), TOGGLE, "gradientToggle", 1)[0].hex() == "33a3010000000000000000000000000000000091"


def test_region_mask_spans_both_bytes():
    value = {"segment": [0, 8, 14], "rgb": 0x102030}
    data = control_frames(make_device(), SEGMENT, "segmentedColorRgb", value)[0]
    assert data[3:6] == bytes([16, 32, 48])
    assert data[8:10] == bytes([1, 65])
    assert valid_frame(data)


@pytest.mark.parametrize("segments", [[15], [-1], [True], []])
def test_invalid_regions_are_rejected(segments):
    with pytest.raises(ValueError):
        control_frames(make_device(), SEGMENT, "segmentedColorRgb", {"segment": segments, "rgb": 0})


def test_device_modes_and_settings_are_combined():
    d = make_device()
    d.local_settings.update(videoArea=0, videoStyle=1, videoSaturation=75)
    data = control_frames(d, TOGGLE, "dreamViewToggle", 1)[0]
    assert data[:6] == bytes([0x33, 5, 0, 0, 1, 75])
    assert valid_frame(data)


def test_dreamview_off_restores_color_instead_of_sunset():
    d = make_device();d.states[(COLOR, "colorRgb")] = 0x123456
    data = control_frames(d, TOGGLE, "dreamViewToggle", 0)[0]
    assert data[:6] == bytes([0x33, 5, 0x0B, 0x12, 0x34, 0x56])


def test_h6199_frames_are_not_used_for_another_model():
    d = make_device();d.sku = "H6601"
    assert control_frames(d, ON_OFF, "powerSwitch", 1) is None


def test_unknown_kelvin_encoding_uses_alternate_transport():
    assert control_frames(make_device(), COLOR, "colorTemperatureK", 4500) is None


def test_checksum_and_length_validation():
    assert valid_frame(frame(0x33, 1, 1))
    assert not valid_frame(frame(0x33, 1, 1)[:-1])
    bad = bytearray(frame(0x33, 1, 1));bad[2] = 0
    assert not valid_frame(bad)


def test_http_200_auth_error_is_not_empty_inventory():
    with pytest.raises(AppAuthenticationError):parse_response({"status": 401})


def test_empty_cloud_values_do_not_clear_valid_push_state():
    d = make_device();d.states[(RANGE, "brightness")] = 29
    d.update_state({"capabilities": [{"type": RANGE, "instance": "brightness", "state": {"value": ""}}]})
    assert d.state(RANGE, "brightness") == 29


def test_poll_started_before_push_cannot_overwrite_new_state():
    from time import monotonic
    d = make_device();started = monotonic()
    d.report({(ON_OFF, "powerSwitch"): 1}, "mqtt")
    d.update_state({"capabilities": [{"type": ON_OFF, "instance": "powerSwitch", "state": {"value": 0}}]}, requested_at=started)
    assert d.state(ON_OFF, "powerSwitch") == 1


def test_report_clears_assumed_flag_for_reported_property():
    d = make_device();d.assume(ON_OFF, "powerSwitch", 1)
    assert d.assumed_keys
    d.report({(ON_OFF, "powerSwitch"): 0}, "mqtt")
    assert not d.assumed_keys and d.state(ON_OFF, "powerSwitch") == 0


def test_translation_files_match():
    root = Path(__file__).parents[1] / "custom_components/govee_native"
    assert json.loads((root / "strings.json").read_text()) == json.loads((root / "translations/en.json").read_text())


def test_updated_firmware_uses_v3_rgb_frame():
    d = make_device();d.local_settings["protocol_profile"] = "updated"
    data = control_frames(d, COLOR, "colorRgb", 0x102030)[0]
    assert data[:7] == bytes([0x33, 5, 0x15, 1, 16, 32, 48])
    assert data[12:14] == bytes([255, 127]) and valid_frame(data)


def test_updated_firmware_music_uses_app_v3_submode():
    from custom_components.govee_native.h6199 import add_capabilities
    d = make_device();d.local_settings["protocol_profile"] = "updated"
    add_capabilities(d)
    d.validate(MODE, "nativeMusicMode", 49)
    data = control_frames(d, MODE, "nativeMusicMode", 49)[0]
    assert data[:5] == bytes([0x33, 5, 0x13, 49, 50]) and valid_frame(data)
