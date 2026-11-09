"""Behavioral checks at simulated network boundaries, not hardware claims."""
import asyncio
import base64
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.govee_native import confirmation
from custom_components.govee_native.api import GoveeAPI, GoveeRateLimit
from custom_components.govee_native.capabilities import add_model_capabilities, segment_indices
from custom_components.govee_native.const import COLOR, ON_OFF, PROPERTY, RANGE, SEGMENT, TOGGLE
from custom_components.govee_native.energy_counter import EnergyCounter
from custom_components.govee_native.h6199 import frame, valid_frame
from custom_components.govee_native.light import GoveeSegmentLight, GoveeSegmentGroup, GoveeZoneLight
from custom_components.govee_native.models import Device
from custom_components.govee_native.native_protocol import control_frames
from custom_components.govee_native.reports import extra_report, app_thermometer_report
from custom_components.govee_native.runtime_store import restore_segments, segment_snapshot
from custom_components.govee_native.sensor import GoveeEnergySensor
from homeassistant.util import dt as dt_util
from test_runtime import FakeMqtt, make_mqtt_responsive


def segmented(sku="TEST-RGBIC", count=12, batch=4, brightness=False):
    fields = [{"fieldName": "segment", "dataType": "ARRAY", "required": True,
               "size": {"min": 1, "max": batch}, "elementRange": {"min": 0, "max": count - 1}},
              {"fieldName": "rgb", "dataType": "INTEGER", "required": True,
               "range": {"min": 0, "max": 0xFFFFFF}}]
    caps = [{"type": SEGMENT, "instance": "segmentedColorRgb", "parameters": {"dataType": "STRUCT", "fields": fields}}]
    if brightness:
        caps.append({"type": SEGMENT, "instance": "segmentedBrightness", "parameters": {"dataType": "STRUCT", "fields": [
            fields[0], {"fieldName": "brightness", "dataType": "INTEGER", "required": True,
                        "range": {"min": 0, "max": 100}}]}})
    device = Device.from_payload({"sku": sku, "device": "segment-test", "type": "devices.types.light", "capabilities": caps})
    device.cloud_source = True
    device.cloud_capabilities = set(device.capabilities)
    add_model_capabilities(device)
    return device


def cloud_api():
    return SimpleNamespace(control=AsyncMock(), can_poll=lambda: False, requests_today=0,
                           poll_interval=lambda base, devices: base, state=AsyncMock(return_value={"capabilities": []}))


async def test_rapid_commands_do_not_wait_for_reports(coordinator):
    c = coordinator;d = next(iter(c.devices.values()))
    c.mqtt = FakeMqtt(c, confirm=False);make_mqtt_responsive(c)
    await asyncio.wait_for(asyncio.gather(
        c.async_control(d, ON_OFF, "powerSwitch", 1),
        c.async_control(d, RANGE, "brightness", 25),
        c.async_control(d, COLOR, "colorRgb", 0x123456)), timeout=0.3)
    assert d.state(ON_OFF, "powerSwitch") == 1
    assert d.state(RANGE, "brightness") == 25
    assert d.state(COLOR, "colorRgb") == 0x123456
    assert c.confirmed_commands == 0 and c.has_pending(d)
    c.mqtt_update({"device": d.device_id, "state": {"onOff": 1, "brightness": 25, "color": {"r": 18, "g": 52, "b": 86}}})
    assert c.confirmed_commands == 3 and not c.has_pending(d)
    assert not ({(ON_OFF, "powerSwitch"), (RANGE, "brightness"), (COLOR, "colorRgb")} & d.assumed_keys)


async def test_unrelated_report_does_not_confirm_another_field(coordinator):
    c = coordinator;d = next(iter(c.devices.values()))
    c.mqtt = FakeMqtt(c, confirm=False);make_mqtt_responsive(c)
    await c.async_control(d, RANGE, "brightness", 25)
    c.mqtt_update({"device": d.device_id, "state": {"onOff": 1}})
    assert c.has_pending(d) and c.confirmed_commands == 0
    c.mqtt_update({"device": d.device_id, "state": {"brightness": 26}})
    assert not c.has_pending(d) and c.confirmed_commands == 1


async def test_new_scene_supersedes_old_scalar_retry(coordinator):
    c = coordinator;d = next(iter(c.devices.values()))
    c.mqtt = FakeMqtt(c, confirm=False);make_mqtt_responsive(c)
    await c.async_control(d, COLOR, "colorRgb", 0xFF0000)
    pending = next(iter(c.pending_commands.values()))
    await c.async_control(d, TOGGLE, "dreamViewToggle", 1)
    assert not c.confirmation.current(pending) and not c.pending_commands


async def test_silence_marks_mqtt_unresponsive_without_resending_power_on(coordinator, monkeypatch):
    monkeypatch.setattr(confirmation, "QUERY_DELAY", 0.01)
    monkeypatch.setattr(confirmation, "CONFIRM_WINDOW", 0.025)
    c = coordinator;d = next(iter(c.devices.values()))
    c.mqtt = FakeMqtt(c, confirm=False);make_mqtt_responsive(c)
    c.api = cloud_api();d.cloud_source = True;d.cloud_capabilities = set(d.capabilities)
    await c.async_control(d, ON_OFF, "powerSwitch", 1)
    pending = next(iter(c.pending_commands.values()))
    await asyncio.wait_for(pending.task, 0.3)
    assert c.unconfirmed_commands == 1 and not c.mqtt_healthy(d)
    c.api.control.assert_not_awaited()
    await c.async_control(d, ON_OFF, "powerSwitch", 0, refresh=False)
    c.api.control.assert_awaited_once()


async def test_contradictory_light_report_allows_one_budgeted_retry(coordinator, monkeypatch):
    monkeypatch.setattr(confirmation, "QUERY_DELAY", 0.01)
    monkeypatch.setattr(confirmation, "CONFIRM_WINDOW", 0.025)
    c = coordinator;d = next(iter(c.devices.values()))
    c.mqtt = FakeMqtt(c, confirm=False);make_mqtt_responsive(c)
    c.mqtt_update({"device": d.device_id, "state": {"brightness": 10}})
    c.api = cloud_api();c.api.can_poll = lambda: True
    d.cloud_source = True;d.cloud_capabilities = set(d.capabilities)
    await c.async_control(d, RANGE, "brightness", 25)
    pending = next(iter(c.pending_commands.values()))
    c.mqtt_update({"device": d.device_id, "state": {"brightness": 10}})
    await asyncio.wait_for(pending.task, 0.3)
    c.api.control.assert_awaited_once_with(d.sku, d.device_id, RANGE, "brightness", 25)
    assert d.last_command_result == "retried_unconfirmed"


async def test_reconnect_requires_fresh_device_evidence(coordinator):
    c = coordinator;d = next(iter(c.devices.values()))
    c.mqtt = FakeMqtt(c);make_mqtt_responsive(c)
    assert c.mqtt_healthy(d)
    c.mqtt_connection_changed(True)
    assert not c.mqtt_healthy(d)
    c.mqtt_update({"device": d.device_id, "state": {"onOff": 0}})
    assert c.mqtt_healthy(d)


async def test_push_does_not_rearm_an_existing_poll(coordinator):
    from homeassistant.core import is_callback
    assert is_callback(coordinator.async_set_updated_data)
    c = coordinator;d = next(iter(c.devices.values()))
    c._listeners["test"] = (lambda: None, None)
    c._unsub_refresh = lambda: None
    with patch.object(c, "_schedule_refresh") as schedule, patch.object(c._debounced_refresh, "async_cancel") as cancel:
        c.mqtt_update({"device": d.device_id, "state": {"onOff": 1}})
        schedule.assert_not_called();cancel.assert_not_called()
    c._listeners.clear();c._unsub_refresh = None


async def test_local_reports_still_get_periodic_cloud_reconciliation(coordinator):
    c = coordinator;d = next(iter(c.devices.values()));d.cloud_source = True
    d.report({(ON_OFF, "powerSwitch"): 1}, "mqtt")
    c.api = cloud_api();c.api.can_poll = lambda: True
    for _ in range(6):
        await c._async_update_data()
    c.api.state.assert_awaited_once()


@pytest.mark.parametrize("sku,expected", [("H7075", 3), ("H7076", 4), ("H7026", 16), ("H1232", 16), ("TEST-RGBIC", 20)])
def test_segment_count_keeps_batch_limit_separate(sku, expected):
    assert len(segment_indices(segmented(sku, count=20, batch=3))) == expected


@pytest.mark.parametrize("mode,individual,grouped", [("individual", 12, 0), ("grouped", 0, 1), ("both", 12, 1), ("disabled", 0, 0)])
async def test_segment_modes_create_the_selected_entities(coordinator, mode, individual, grouped):
    from custom_components.govee_native.light import async_setup_entry
    c = coordinator;d = segmented();c.devices = {d.device_id: d};c.hass.data["govee_native"] = {"test": c}
    entities = []
    await async_setup_entry(c.hass, SimpleNamespace(entry_id="test", options={"segment_mode_by_device": {d.device_id: mode}}), entities.extend)
    assert sum(type(e) is GoveeSegmentLight for e in entities) == individual
    assert sum(type(e) is GoveeSegmentGroup for e in entities) == grouped
    assert len({e.unique_id for e in entities}) == len(entities)


async def test_group_splits_to_the_advertised_batch_size_and_updates_individuals(coordinator):
    c = coordinator;d = segmented();c.devices[d.device_id] = d;c.api = cloud_api()
    group = GoveeSegmentGroup(c, d)
    await group.async_turn_on(rgb_color=(12, 34, 56))
    assert c.api.control.await_count == 3
    assert [call.args[4]["segment"] for call in c.api.control.await_args_list] == [list(range(4)), list(range(4, 8)), list(range(8, 12))]
    assert all(GoveeSegmentLight(c, d, i).rgb_color == (12, 34, 56) for i in range(12))
    assert group.rgb_color == (12, 34, 56)


async def test_segment_colour_restores_after_off_and_restart(coordinator):
    c = coordinator;d = segmented();c.devices[d.device_id] = d;c.api = cloud_api()
    first = GoveeSegmentLight(c, d, 0)
    await first.async_turn_on(rgb_color=(10, 20, 30));await first.async_turn_off()
    saved = json.loads(json.dumps(segment_snapshot(d)))
    restored = segmented();restore_segments(restored, saved)
    assert restored.segment_colors[0] == 0 and restored.last_transport is None
    await GoveeSegmentLight(c, restored, 0).async_turn_on()
    assert restored.segment_colors[0] == 0x0A141E


async def test_unsupported_segment_brightness_cannot_be_a_fake_slider(coordinator):
    c = coordinator;d = segmented();c.api = cloud_api()
    with pytest.raises(HomeAssistantError, match="no independent segment brightness"):
        await GoveeSegmentLight(c, d, 0).async_turn_on(brightness=50)
    c.api.control.assert_not_awaited()


async def test_supported_segment_brightness_is_actually_transmitted(coordinator):
    c = coordinator;d = segmented(brightness=True);c.api = cloud_api()
    entity = GoveeSegmentLight(c, d, 0)
    await entity.async_turn_on(rgb_color=(1, 2, 3), brightness=128)
    assert c.api.control.await_args.args[3:] == ("segmentedBrightness", {"segment": [0], "brightness": 50})
    assert entity.brightness == 128


@pytest.mark.parametrize("indices", [[-1], [12], [True], []])
async def test_invalid_segments_never_reach_the_network(coordinator, indices):
    c = coordinator;d = segmented();c.api = cloud_api()
    with pytest.raises(ValueError):
        await c.async_control(d, SEGMENT, "segmentedColorRgb", {"segment": indices, "rgb": 0})
    c.api.control.assert_not_awaited()


def test_restore_rejects_unknown_indices_and_bad_colours():
    d = segmented()
    restore_segments(d, {"colors": {"0": 0x123456, "999": 1, "2": -1, "3": True, "bad": 1}})
    assert d.segment_colors == {0: 0x123456} and d.online is None


@pytest.mark.parametrize("sku", ["H1232", "H1252"])
def test_ring_mask_never_touches_main_panel(sku):
    d = segmented(sku, count=13)
    packet = control_frames(d, SEGMENT, "segmentedColorRgb", {"segment": list(range(16)), "rgb": 0x123456})[0]
    assert packet[12:15] == bytes((255, 255, 0)) and valid_frame(packet)
    panel = control_frames(d, "govee_native.zone", "mainPanel", {"instance": "segmentedColorRgb", "rgb": 0x123456})[0]
    assert panel[12:15] == bytes((0, 0, 1)) and valid_frame(panel)
    if sku == "H1252":
        assert control_frames(d, TOGGLE, "nativeMainPanel", 0)[0][:4] == bytes((0x33, 0x30, 0, 0))
    else:
        assert control_frames(d, TOGGLE, "nativeMainPanel", 0) is None


async def test_extended_ring_segments_cannot_leak_into_rest(coordinator):
    c = coordinator;d = segmented("H1232", count=13);c.api = cloud_api()
    with pytest.raises(ValueError):
        await c.async_control(d, SEGMENT, "segmentedColorRgb", {"segment": [15], "rgb": 0x123456})
    c.api.control.assert_not_awaited()


async def test_zone_updates_have_separate_state(coordinator):
    c = coordinator;d = segmented("H1252", count=16);d.app_data["topic"] = "GD/owned"
    c.devices = {d.device_id: d};c.mqtt = FakeMqtt(c, confirm=False)
    c._apply_report(d, {(TOGGLE, "nativeMainPanel"): 0}, "mqtt")
    main, ring = GoveeZoneLight(c, d, "mainPanel"), GoveeZoneLight(c, d, "ring")
    await main.async_turn_on(rgb_color=(255, 0, 0), brightness=128)
    await ring.async_turn_on(rgb_color=(0, 0, 255))
    assert main.rgb_color == (255, 0, 0) and ring.rgb_color == (0, 0, 255)
    assert main.brightness == 128


@pytest.mark.parametrize("sku", ["H5160", "H5161"])
async def test_outlets_use_addressed_bitmasks(coordinator, sku):
    c = coordinator;d = segmented(sku);d.app_data["topic"] = "GD/owned";c.devices = {d.device_id: d}
    c.mqtt = FakeMqtt(c, confirm=False)
    c._apply_report(d, {(TOGGLE, "socketToggle1"): 0}, "mqtt")
    await c.async_control(d, TOGGLE, "socketToggle2", 1)
    assert next(call[2] for call in c.mqtt.calls if call[1] == "turn") == {"val": 34}
    values = extra_report(d, {"state": {"onOff": 5}})
    assert [values[(TOGGLE, f"socketToggle{i}")] for i in (1, 2, 3)] == [1, 0, 1]


def packet(raw, sta=None):
    return {"op": {"command": [base64.b64encode(raw).decode()]}, "sta": sta}


def test_energy_frame_is_gated_to_the_outlet_model(coordinator):
    d = segmented("H5086");raw = bytearray(20);raw[:2] = b"\xaa\x19"
    raw[2:5] = (3000).to_bytes(3, "big");raw[5:8] = (1234).to_bytes(3, "big")
    raw[8:10] = (23000).to_bytes(2, "big");raw[10:12] = (150).to_bytes(2, "big");raw[12:15] = (34500).to_bytes(3, "big")
    report = packet(raw, {"stc": "19_0_0_4000_1"})
    values = extra_report(d, report)
    assert values[(PROPERTY, "voltage")] == 230 and values[(PROPERTY, "power")] == 345
    assert values[(PROPERTY, "energyToday")] == 0.1234
    d.report(values, "mqtt")
    energy = GoveeEnergySensor(coordinator, d)
    assert energy.native_value == 0.1234 and energy.last_reset is not None
    assert not extra_report(segmented("TEST-RGBIC"), report)
    assert not extra_report(d, packet(raw, {"stc": "0_0_0_4000_1"}))


def test_energy_reboot_restore_does_not_start_a_second_cycle():
    now = dt_util.utcnow()
    counter = EnergyCounter.new(now, 1234, 3000, 4000)
    counter.update(now + timedelta(seconds=60), 0, 20, 20)
    assert counter.energy_deciwatt_hours == 1234 and counter.reboot_seen is not None
    counter.update(now + timedelta(seconds=120), 1230, 3010, 80)
    restored = EnergyCounter.from_dict(counter.as_dict())
    assert restored.last_reset == now and restored.energy_deciwatt_hours == 1230


def test_dehumidifier_and_air_quality_decoders_are_distinct():
    d = segmented("H7152");packed = 209 * 1000 + 532
    values = extra_report(d, packet(b"\xaa\x10\x81" + packed.to_bytes(3, "big")))
    assert values[(PROPERTY, "sensorTemperature")] == 20.9 and values[(PROPERTY, "sensorHumidity")] == 53.2
    fault = bytearray(20);fault[:2] = b"\xaa\x17";fault[12] = 1
    assert extra_report(d, packet(fault))[(PROPERTY, "pumpFault")] == 1
    aqi = segmented("H5106");raw = bytearray(20);raw[:2] = (2510).to_bytes(2, "big");raw[9:11] = (5480).to_bytes(2, "big");raw[18:20] = (3).to_bytes(2, "big")
    values = extra_report(aqi, packet(raw))
    assert values[(PROPERTY, "sensorTemperature")] == 25.1 and values[(PROPERTY, "pm25")] == 3


@pytest.mark.parametrize("sku,probe", [("H5192", 2), ("H5194", 4)])
def test_probe_reads_handle_negative_temperatures_and_missing_ambient(sku, probe):
    d = segmented(sku);raw = bytearray(26);raw[:3] = bytes((0xAA, 0x24, probe))
    raw[22:24] = (-500).to_bytes(2, "big", signed=True);raw[24:26] = b"\xff\xff"
    values = extra_report(d, packet(raw))
    assert values[(PROPERTY, f"probe{probe}CoreTemperature")] == -5
    assert (PROPERTY, f"probe{probe}AmbientTemperature") not in values


def test_thermometer_sentinels_do_not_hide_the_second_probe():
    d = segmented("H5112");d.app_data["last"] = {"tem": -1, "tem2": -500, "hum": 65535}
    values = app_thermometer_report(d)
    assert (PROPERTY, "sensorTemperature") not in values
    assert values[(PROPERTY, "sensorTemperature2")] == -5
    assert (PROPERTY, "sensorHumidity") not in values


def test_daily_budget_paces_a_fleet_and_resets_at_midnight(monkeypatch):
    from custom_components.govee_native import api as api_module
    monkeypatch.setattr(api_module.time, "time", lambda: 864000.0)
    api = GoveeAPI(None, "test")
    assert api.poll_interval(60, 20) == 193
    api.requests_today = 9000
    assert not api.can_poll() and api.poll_interval(60, 20) == 86405
    monkeypatch.setattr(api_module.time, "time", lambda: 950401.0)
    assert api.can_poll() and api.requests_today == 0


def test_budget_cannot_be_reset_by_reloading_the_entry():
    api = GoveeAPI(None, "test");api.requests_today = 123
    reloaded = GoveeAPI(None, "test");reloaded.restore_budget(json.loads(json.dumps(api.budget_snapshot())))
    assert reloaded.requests_today == 123
    reloaded.restore_budget({"day": reloaded._request_day, "requests": -1})
    assert reloaded.requests_today == 123


async def test_http_rate_limit_backoff_stops_further_requests():
    response = SimpleNamespace(status=429, headers={"Retry-After": "120"})
    context = AsyncMock();context.__aenter__.return_value = response
    session = SimpleNamespace(request=Mock(return_value=context));api = GoveeAPI(session, "test")
    with pytest.raises(GoveeRateLimit):
        await api.devices()
    with pytest.raises(GoveeRateLimit):
        await api.devices()
    session.request.assert_called_once()
    assert 118 < api.retry_after <= 120 and not api.can_poll()


async def test_concurrent_scene_fetches_share_a_cache(coordinator):
    c = coordinator;d = next(iter(c.devices.values()))
    c.app = SimpleNamespace(lighting_scenes=AsyncMock(return_value={"data": []}))
    await asyncio.gather(c.async_load_scenes(d), c.async_load_scenes(d))
    c.app.lighting_scenes.assert_awaited_once()
    await c.async_load_scenes(d, refresh=True)
    assert c.app.lighting_scenes.await_count == 2


async def test_slider_backlog_keeps_only_the_latest_queued_value(coordinator):
    c = coordinator;d = next(iter(c.devices.values()));d.cloud_source = True;d.cloud_capabilities = set(d.capabilities)
    c.api = cloud_api();entered, release = asyncio.Event(), asyncio.Event()
    values = []

    async def write(_sku, _device, _kind, _instance, value):
        values.append(value)
        if len(values) == 1:
            entered.set();await release.wait()

    c.api.control.side_effect = write
    first = asyncio.create_task(c.async_control(d, RANGE, "brightness", 10, refresh=False))
    await entered.wait()
    second = asyncio.create_task(c.async_control(d, RANGE, "brightness", 20, refresh=False))
    third = asyncio.create_task(c.async_control(d, RANGE, "brightness", 30, refresh=False))
    await asyncio.sleep(0);release.set()
    await asyncio.gather(first, second, third)
    assert values == [10, 30] and d.state(RANGE, "brightness") == 30
    assert c.superseded_commands == 1


@pytest.mark.parametrize("main,ring", [(0, 0), (1, 0), (0, 1), (1, 1)])
def test_zone_reports_decode_both_state_bytes_and_require_checksum(main, ring):
    d = segmented("H1252", 16)
    raw = frame(0xAA, 0x30, main, ring)
    values = extra_report(d, packet(raw))
    assert values[(TOGGLE, "mainLightToggle")] == main
    assert values[(TOGGLE, "backgroundLightToggle")] == ring
    assert not extra_report(d, packet(raw[:-1]))
    assert not extra_report(d, packet(raw[:-1] + bytes((raw[-1] ^ 1,))))
    assert not extra_report(segmented("H1232"), packet(raw))


async def test_zone_white_temperature_is_transmitted_and_retained(coordinator):
    c = coordinator;d = segmented("H1252", 16);d.app_data["topic"] = "GD/owned"
    c.devices = {d.device_id: d};c.mqtt = FakeMqtt(c, confirm=False)
    c._apply_report(d, {(TOGGLE, "mainLightToggle"): 0}, "mqtt")
    zone = GoveeZoneLight(c, d, "mainPanel")
    await zone.async_turn_on(color_temp_kelvin=2700)
    assert zone.color_temp_kelvin == 2700 and zone.color_mode == "color_temp"
    command = [call for call in c.mqtt.calls if call[1] == "ptReal"][-1]
    raw = base64.b64decode(command[2]["command"][0])
    assert raw[7:9] == bytes((2700 >> 8, 2700 & 255)) and raw[12:15] == b"\0\0\1"


async def test_segment_options_preserve_other_devices_and_settings(coordinator):
    from custom_components.govee_native.config_flow import GoveeNativeOptionsFlow
    c = coordinator;d = next(iter(c.devices.values()));c.hass.data["govee_native"] = {"test": c}
    entry = SimpleNamespace(entry_id="test", options={"private_mutations": True, "segment_mode_by_device": {"other": "grouped"}})
    flow = GoveeNativeOptionsFlow();flow.hass = c.hass
    with patch.object(GoveeNativeOptionsFlow, "config_entry", property(lambda self: entry)):
        result = await flow.async_step_init({"configure_segments": True})
        assert result["step_id"] == "segments"
        result = await flow.async_step_segments({"device_ids": [d.device_id], "mode": "both"})
    assert result["type"] == "create_entry"
    assert result["data"]["private_mutations"] is True
    assert result["data"]["segment_mode_by_device"] == {"other": "grouped", d.device_id: "both"}


async def test_segment_registry_preserves_explicit_user_disabled_entities(coordinator):
    from custom_components.govee_native import _update_segment_registry
    from homeassistant.helpers import entity_registry as er
    c = coordinator;d = next(iter(c.devices.values()));prefix = d.device_id.lower().replace(":", "") + "_"
    entries = [SimpleNamespace(unique_id=prefix + f"segment_{i}", entity_id=f"light.segment_{i}",
                              disabled_by=disabler) for i, disabler in enumerate(
        (er.RegistryEntryDisabler.INTEGRATION, er.RegistryEntryDisabler.USER, None))]
    registry = SimpleNamespace(entities=SimpleNamespace(get_entries_for_config_entry_id=lambda _: entries), async_update_entity=Mock())
    with patch.object(er, "async_get", return_value=registry):
        _update_segment_registry(c.hass, SimpleNamespace(entry_id="test", options={}), c)
    registry.async_update_entity.assert_called_once_with("light.segment_0", disabled_by=None)


async def test_failed_status_queries_are_quarantined_without_marking_the_broker_healthy(coordinator):
    c = coordinator;d = next(iter(c.devices.values()));c.mqtt = FakeMqtt(c, confirm=False)
    for _ in range(2):
        await c.async_query_status(force=True)
        c.mqtt_connection_changed(False)
    assert d.sku in c._query_quarantine and not c.mqtt_healthy(d)
    await c.async_query_status(force=True)
    assert len(c.mqtt.calls) == 2


async def test_silent_mqtt_session_gets_only_one_automatic_restart(monkeypatch):
    import sys
    from custom_components.govee_native.app_mqtt import GoveeAppMqtt
    client = GoveeAppMqtt({}, "GA/account", "test", "id", lambda _: None)
    connections = []

    class Messages:
        def __aiter__(self):
            return self

        async def __anext__(self):
            client.status_queries += 3
            if len(connections) == 2:
                client._stopping = True
            raise TimeoutError

    class Connection:
        def __init__(self, **kwargs):
            self.messages = Messages();self.subscribe = AsyncMock(return_value=[1])
            connections.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    monkeypatch.setitem(sys.modules, "aiomqtt", SimpleNamespace(Client=Connection))
    with patch("custom_components.govee_native.app_mqtt.asyncio.sleep", new_callable=AsyncMock):
        await asyncio.wait_for(client._run(), 0.3)
    assert len(connections) == 2 and client.silent and client._silent_reconnect_attempted


def test_encrypted_frames_have_distinct_counters_and_round_trip():
    from custom_components.govee_native.ble_crypto import GoveeBLESession
    session = GoveeBLESession(bytes(16), bytes(8), bytes(8))
    first, second = session.wrap(frame(0x33, 1, 1)), session.wrap(frame(0x33, 1, 0))
    assert first[:4] == b"\0\0\0\1" and second[:4] == b"\0\0\0\2"
    assert session.unwrap(first) == frame(0x33, 1, 1)
    assert session.unwrap(second) == frame(0x33, 1, 0)


def test_header_budget_recovers_even_without_a_reset_header(monkeypatch):
    from custom_components.govee_native import api as api_module
    clock = [1000000.0]
    monkeypatch.setattr(api_module.time, "time", lambda: clock[0])
    api = GoveeAPI(None, "test");api._response_limits({"X-RateLimit-Remaining": "0"})
    assert not api.can_poll()
    clock[0] += 61
    assert api.can_poll()


def test_duration_reset_headers_are_relative_to_receipt(monkeypatch):
    from custom_components.govee_native import api as api_module
    monkeypatch.setattr(api_module.time, "time", lambda: 1000000.0)
    api = GoveeAPI(None, "test")
    api._response_limits({"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "120"})
    assert api.rate_reset == 1000120.0 and api.poll_interval(60, 2) == 120


async def test_probe_query_keeps_the_read_frame_in_the_wire_envelope():
    from custom_components.govee_native.app_mqtt import GoveeAppMqtt
    client = GoveeAppMqtt({}, "GA/account", "test", "id", lambda _: None)
    client.connected = True;client._client = SimpleNamespace(publish=AsyncMock())
    read_frame = base64.b64encode(frame(0xAA, 0x24, 1)).decode()
    await client.publish("GD/device", "ptReal", {"command": [read_frame]}, read_only=True)
    message = json.loads(client._client.publish.await_args.args[1])["msg"]
    assert message["type"] == 1 and message["data"]["command"] == [read_frame]
    assert client.status_queries == 1 and client.commands_published == 0


@pytest.mark.parametrize("sku", ["H5160", "H5161"])
@pytest.mark.parametrize("power,wire", [(0, 112), (1, 119)])
async def test_outlet_master_addresses_all_sockets(coordinator, sku, power, wire):
    c = coordinator;d = next(iter(c.devices.values()));d.sku = sku
    add_model_capabilities(d);c.mqtt = FakeMqtt(c, confirm=False)
    make_mqtt_responsive(c)
    await c.async_control(d, ON_OFF, "powerSwitch", power)
    assert next(call[2] for call in c.mqtt.calls if call[1] == "turn") == {"val": wire}
    assert all(d.state(TOGGLE, f"socketToggle{i}") == power for i in (1, 2, 3))
