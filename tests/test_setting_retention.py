"""Selections survive missing readback and coordinator recreation, without writes."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.core import State

from custom_components.govee_native.const import MODE, RANGE, ON_OFF, SCENE, TOGGLE, WORK_MODE
from custom_components.govee_native.coordinator import GoveeCoordinator
from custom_components.govee_native.h6199 import control_frames
from custom_components.govee_native.number import GoveeNumber
from custom_components.govee_native.runtime_store import RuntimeStore, restore_settings, settings_snapshot
from custom_components.govee_native.select import GoveeSelect, GoveeSceneSelect, GoveeWorkModeSelect
from conftest import make_device


async def test_selected_fields_survive_saved_runtime_and_new_coordinator(coordinator):
    c = coordinator
    device = next(iter(c.devices.values()))
    c.ble = SimpleNamespace(async_write=AsyncMock(return_value=True), async_stop=AsyncMock())
    await GoveeSelect(c, device, MODE, "videoArea").async_select_option("Partial")
    await GoveeSelect(c, device, MODE, "videoStyle").async_select_option("Game")
    await GoveeNumber(c, device, RANGE, "videoSaturation").async_set_native_value(39)
    await c.async_control(device, TOGGLE, "gradientToggle", 1)
    store = RuntimeStore(c.hass, "retention-test")
    await store.save(c.runtime_snapshot())

    restored = GoveeCoordinator(c.hass, None, 60)
    restored._runtime_data = await RuntimeStore(c.hass, "retention-test").load()
    restored._merge_device(make_device())
    fresh = restored.devices[device.device_id]
    try:
        assert GoveeSelect(restored, fresh, MODE, "videoArea").current_option == "Partial"
        assert GoveeSelect(restored, fresh, MODE, "videoStyle").current_option == "Game"
        assert GoveeNumber(restored, fresh, RANGE, "videoSaturation").native_value == 39
        assert fresh.state(TOGGLE, "gradientToggle") == 1
        assert fresh.local_settings["videoArea"] == 0
        assert control_frames(fresh, RANGE, "videoSaturation", 70)[0][3:6] == bytes([0, 1, 70])
        assert (MODE, "videoArea") in fresh.assumed_keys
        assert fresh.online is None and fresh.last_report_at == 0
        assert fresh.last_transport is None and fresh.last_command_result is None
        assert not restored._background and restored.confirmed_commands == 0
        c.ble.async_write.assert_awaited()
        assert c.ble.async_write.await_count == 4
    finally:
        await restored.async_close()


@pytest.mark.parametrize("bad", [None, "", -1, "unknown", 7])
def test_missing_or_invalid_reports_do_not_clear_last_selection(bad):
    device = make_device()
    device.assume(MODE, "videoArea", 0)
    device.state_times[(MODE, "videoArea")] = 0
    device.update_state({"capabilities": [{"type": MODE, "instance": "videoArea", "state": {"value": bad}}]}, requested_at=1)
    assert device.state(MODE, "videoArea") == 0
    device.report({(MODE, "videoArea"): bad}, "mqtt")
    assert device.state(MODE, "videoArea") == 0
    assert (MODE, "videoArea") in device.assumed_keys


async def test_restored_settings_accept_live_feedback_immediately(coordinator):
    c = coordinator
    device = next(iter(c.devices.values()))
    c._assume_write(device, MODE, "videoArea", 0, "ble_written_unconfirmed")
    fresh_coordinator = GoveeCoordinator(c.hass, None, 60)
    fresh_coordinator._runtime_data = c.runtime_snapshot()
    fresh_coordinator._merge_device(make_device())
    fresh = fresh_coordinator.devices[device.device_id]
    try:
        fresh.update_state({"capabilities": [{"type": MODE, "instance": "videoArea", "state": {"value": 1}}]}, requested_at=1)
        assert fresh.state(MODE, "videoArea") == 1
        assert fresh.local_settings["videoArea"] == 1
        assert (MODE, "videoArea") not in fresh.assumed_keys
    finally:
        await fresh_coordinator.async_close()


async def test_failed_selection_does_not_replace_saved_value(coordinator):
    c = coordinator
    device = next(iter(c.devices.values()))
    c._assume_write(device, MODE, "videoArea", 0, "ble_written_unconfirmed")
    selector = GoveeSelect(c, device, MODE, "videoArea")
    with pytest.raises(HomeAssistantError, match="No responsive transport"):
        await selector.async_select_option("All")
    assert selector.current_option == "Partial"


async def test_combined_scene_selector_remembers_successful_selection(coordinator):
    c = coordinator
    device = next(iter(c.devices.values()))
    c.ble = SimpleNamespace(async_write=AsyncMock(return_value=True), async_stop=AsyncMock())
    selector = GoveeSceneSelect(c, device)
    await selector.async_select_option("Native: Movie")
    assert selector.current_option == "Native: Movie"
    fresh_coordinator = GoveeCoordinator(c.hass, None, 60)
    fresh_coordinator._runtime_data = c.runtime_snapshot()
    fresh_coordinator._merge_device(make_device())
    try:
        fresh = fresh_coordinator.devices[device.device_id]
        assert GoveeSceneSelect(fresh_coordinator, fresh).current_option == "Native: Movie"
        assert fresh.state(ON_OFF, "powerSwitch") is None
    finally:
        await fresh_coordinator.async_close()


def test_snapshot_unknown_readback_keeps_zero_value():
    device = make_device()
    device.capabilities[(SCENE, "snapshot")] = {"parameters": {"dataType": "ENUM", "options": [{"name": "TV", "value": 0}]}}
    device.assume(SCENE, "snapshot", 0)
    device.report({(SCENE, "snapshot"): ""}, "mqtt")
    assert device.state(SCENE, "snapshot") == 0


@pytest.mark.parametrize(("field", "previous", "expected"), [
    ("videoArea", "Partial", 0), ("videoStyle", "Game", 1),
    ("videoSaturation", "39", 39), ("videoArea", "unknown", None),
    ("videoSaturation", "unavailable", None), ("videoSaturation", "101", None),
])
async def test_ha_restore_fallback_validates_prior_option_or_number(coordinator, field, previous, expected):
    c = coordinator
    device = next(iter(c.devices.values()))
    c.ble = SimpleNamespace(available=lambda d: True, async_stop=AsyncMock())
    entity = GoveeNumber(c, device, RANGE, field) if field == "videoSaturation" else GoveeSelect(c, device, MODE, field)
    entity.hass = c.hass
    entity.entity_id = ("number." if field == "videoSaturation" else "select.") + "retained_setting"
    entity._no_platform_reported = True
    with patch.object(entity, "async_get_last_state", AsyncMock(return_value=State(entity.entity_id, previous))):
        await entity.async_added_to_hass()
    entity.async_write_ha_state()
    assert device.state(RANGE if field == "videoSaturation" else MODE, field) == expected
    if expected is not None:
        assert c.hass.states.get(entity.entity_id).state == previous
        assert c.hass.states.get(entity.entity_id).attributes["state_assumed"] is True
        assert device.online is None


def test_stored_settings_are_revalidated_and_do_not_replace_live_state():
    device = make_device()
    device.report({(MODE, "videoArea"): 1}, "mqtt")
    restore_settings(device, {"sku": device.sku, "values": [
        {"type": MODE, "instance": "videoArea", "value": 0},
        {"type": MODE, "instance": "videoStyle", "value": 44},
        {"type": MODE, "instance": "removedField", "value": 1},
        {"type": RANGE, "instance": "videoSaturation", "value": 101},
        {"type": ON_OFF, "instance": "powerSwitch", "value": 1},
        {"type": ["invalid"], "instance": "bad", "value": 0}, None,
    ]})
    assert device.states == {(MODE, "videoArea"): 1}
    assert not device.assumed_keys
    restore_settings(device, {"sku": "another-model", "values": [
        {"type": MODE, "instance": "videoStyle", "value": 1}]})
    assert device.state(MODE, "videoStyle") is None


async def test_device_feedback_is_saved_as_the_latest_setting(coordinator):
    c = coordinator
    device = next(iter(c.devices.values()))
    c._assume_write(device, MODE, "videoArea", 0, "ble_written_unconfirmed")
    c._apply_report(device, {(MODE, "videoArea"): 1}, "mqtt")
    saved = c.runtime_snapshot()["settings"][device.device_id]
    fresh = make_device()
    restore_settings(fresh, saved)
    assert fresh.state(MODE, "videoArea") == 1
    assert fresh.local_settings["videoArea"] == 1
    assert fresh.assumed_keys and fresh.last_report_at == 0


async def test_page_recreation_and_unrelated_updates_keep_selection(coordinator):
    c = coordinator
    device = next(iter(c.devices.values()))
    c.mqtt = SimpleNamespace(connected=True, stop=AsyncMock())
    c._assume_write(device, MODE, "videoArea", 0, "ble_written_unconfirmed")
    c._apply_report(device, {(ON_OFF, "powerSwitch"): 1}, "mqtt")
    for _ in range(3):
        selector = GoveeSelect(c, device, MODE, "videoArea")
        selector.hass = c.hass
        selector.entity_id = "select.retained_area"
        selector._no_platform_reported = True
        selector.async_write_ha_state()
        assert c.hass.states.get(selector.entity_id).state == "Partial"
        c._merge_device(make_device())


async def test_structured_work_mode_retention(coordinator):
    c = coordinator
    device = make_device()
    capability = {"parameters": {"dataType": "STRUCT", "fields": [
        {"fieldName": "workMode", "dataType": "ENUM", "required": True,
         "options": [{"name": "Auto", "value": 1}]},
        {"fieldName": "modeValue", "dataType": "ENUM", "required": True,
         "options": [{"name": "Auto", "defaultValue": 50}]}]}}
    device.capabilities[(WORK_MODE, "workMode")] = capability
    device.assume(WORK_MODE, "workMode", {"workMode": 1, "modeValue": 50})
    fresh = make_device()
    fresh.capabilities[(WORK_MODE, "workMode")] = capability
    restore_settings(fresh, settings_snapshot(device))
    assert GoveeWorkModeSelect(c, fresh).current_option == "Auto"
    assert fresh.state(WORK_MODE, "workMode") == {"workMode": 1, "modeValue": 50}
