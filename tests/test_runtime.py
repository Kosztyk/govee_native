"""Regressions for the observed broker-connected / command-unconfirmed fault.

Uses real Home Assistant 2026.11 entities and coordinator. Only network/GATT
boundaries are simulated; these tests do not imply physical hardware tests.
"""
import asyncio
import json
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace
import pytest

from custom_components.govee_native import coordinator as coordinator_module
from custom_components.govee_native.api import GoveeError, GoveeAuthError
from custom_components.govee_native.app_mqtt import GoveeAppMqtt
from custom_components.govee_native.bluetooth import GoveeBluetooth
from custom_components.govee_native.const import ON_OFF, COLOR, RANGE, MODE, TOGGLE, SEGMENT
from custom_components.govee_native.diagnostics import async_get_config_entry_diagnostics
from custom_components.govee_native.entity import GoveeEntity
from custom_components.govee_native.h6199 import frame
from custom_components.govee_native.lan import GoveeLan
from custom_components.govee_native.light import GoveeLight
from custom_components.govee_native.sensor import GoveeAppConnectionSensor
from conftest import make_device


class FakeMqtt:
    connected = True
    messages_received = 0
    commands_published = 0
    last_command_name = None
    last_message_at = None

    def __init__(self, c, *, confirm=True):
        self.c, self.confirm, self.calls = c, confirm, []

    async def publish(self, topic, cmd, data=None, **kwargs):
        self.calls.append((topic, cmd, data, kwargs))
        if self.confirm and cmd == "turn":
            self.c.mqtt_update({"device": next(iter(self.c.devices)), "state": {"onOff": data["val"]}})

    async def stop(self):pass


def cloud_device(c):
    d = next(iter(c.devices.values()))
    d.cloud_source = True
    d.cloud_capabilities = {(ON_OFF, "powerSwitch"), (COLOR, "colorRgb"), (RANGE, "brightness")}
    return d


async def test_fast_feedback_before_publish_returns_is_not_lost(coordinator):
    c = coordinator;d = next(iter(c.devices.values()));c.mqtt = FakeMqtt(c)
    await c.async_control(d, ON_OFF, "powerSwitch", 1)
    assert c.confirmed_commands == 1 and c.unconfirmed_commands == 0
    assert d.last_command_result == "confirmed"
    assert not c.pending_commands and not c._confirmation_waiters


async def test_writes_are_serialized_per_device(coordinator):
    c = coordinator;d = next(iter(c.devices.values()));c.mqtt = FakeMqtt(c)
    await asyncio.gather(c.async_control(d, ON_OFF, "powerSwitch", 1), c.async_control(d, ON_OFF, "powerSwitch", 0))
    assert [call[2]["val"] for call in c.mqtt.calls if call[1] == "turn"] == [1, 0]
    assert c.confirmed_commands == 2 and d.state(ON_OFF, "powerSwitch") == 0


async def test_broker_acceptance_is_not_device_confirmation(coordinator, monkeypatch):
    c = coordinator;d = next(iter(c.devices.values()));c.mqtt = FakeMqtt(c, confirm=False)
    monkeypatch.setattr(coordinator_module, "MQTT_CONFIRM_TIMEOUT", 0.01)
    with pytest.raises(GoveeError):await c.async_control(d, ON_OFF, "powerSwitch", 1)
    assert c.confirmed_commands == 0 and c.unconfirmed_commands == 1
    assert d.state(ON_OFF, "powerSwitch") is None
    assert not c.pending_commands and not c._confirmation_waiters


async def test_timeout_falls_back_and_next_write_skips_broken_mqtt(coordinator, monkeypatch):
    c = coordinator;d = cloud_device(c);c.mqtt = FakeMqtt(c, confirm=False)
    c.api = SimpleNamespace(control=AsyncMock())
    monkeypatch.setattr(coordinator_module, "MQTT_CONFIRM_TIMEOUT", 0.01)
    await c.async_control(d, ON_OFF, "powerSwitch", 1, refresh=False)
    await c.async_control(d, ON_OFF, "powerSwitch", 0, refresh=False)
    assert sum(call[1] == "turn" for call in c.mqtt.calls) == 1
    assert c.api.control.await_count == 2
    assert c.confirmed_commands == 0
    assert d.last_command_result == "api_accepted_unconfirmed"


async def test_native_mode_is_not_sent_to_unknown_openapi_endpoint(coordinator):
    c = coordinator;d = cloud_device(c);c.api = SimpleNamespace(control=AsyncMock())
    with pytest.raises(GoveeError):await c.async_control(d, MODE, "videoStyle", 1, refresh=False)
    c.api.control.assert_not_awaited()


async def test_ble_write_is_explicitly_assumed(coordinator):
    c = coordinator;d = next(iter(c.devices.values()))
    c.ble = SimpleNamespace(async_write=AsyncMock(return_value=True), async_stop=AsyncMock())
    await c.async_control(d, ON_OFF, "powerSwitch", 1)
    assert d.last_command_result == "ble_written_unconfirmed"
    assert d.last_command_transport == "bluetooth" and d.assumed_keys
    assert c.confirmed_commands == 0


async def test_native_video_uses_ptreal_and_does_not_claim_confirmation(coordinator):
    c = coordinator;d = next(iter(c.devices.values()));c.mqtt = FakeMqtt(c, confirm=False)
    await c.async_control(d, TOGGLE, "dreamViewToggle", 1)
    command = next(call for call in c.mqtt.calls if call[1] == "ptReal")
    assert command[2]["device"] == d.device_id
    assert d.state(TOGGLE, "dreamViewToggle") == 1
    assert d.last_command_result == "mqtt_published_unconfirmed" and c.confirmed_commands == 0


async def test_region_write_updates_only_requested_regions(coordinator):
    c = coordinator;d = next(iter(c.devices.values()));c.mqtt = FakeMqtt(c, confirm=False)
    d.segment_colors[5] = 0x112233
    await c.async_control(d, SEGMENT, "segmentedColorRgb", {"segment": [0, 14], "rgb": 0xFF0000})
    assert d.segment_colors == {0: 0xFF0000, 5: 0x112233, 14: 0xFF0000}


@pytest.mark.parametrize("uptime", [0.0, 20.0, 1000000.0])
async def test_periodic_status_queries_are_throttled(coordinator, monkeypatch, uptime):
    monkeypatch.setattr(coordinator_module, "monotonic", lambda: uptime)
    c = coordinator;c.mqtt = FakeMqtt(c)
    await c.async_query_status();await c.async_query_status()
    assert len(c.mqtt.calls) == 1
    await c.async_query_status(force=True)
    assert len(c.mqtt.calls) == 2


async def test_known_device_case_normalization(coordinator):
    c = coordinator;d = next(iter(c.devices.values()))
    c.mqtt_update({"device": d.device_id.lower(), "state": {"onOff": 1}})
    assert d.state(ON_OFF, "powerSwitch") == 1


async def test_unknown_device_feedback_is_ignored(coordinator):
    c = coordinator
    c.mqtt_update({"device": "unowned", "state": {"onOff": 1}})
    assert c.confirmed_device_updates == 0


async def test_discovery_merge_keeps_entity_device_object(coordinator):
    c = coordinator;d = next(iter(c.devices.values()))
    d.assume(ON_OFF, "powerSwitch", 1)
    fresh = make_device();fresh.name = "Renamed backlight"
    c._merge_device(fresh)
    assert c.devices[d.device_id] is d
    assert d.name == "Renamed backlight" and d.state(ON_OFF, "powerSwitch") == 1


async def test_recent_push_avoids_cloud_state_poll(coordinator):
    c = coordinator;d = cloud_device(c)
    d.report({(ON_OFF, "powerSwitch"): 1}, "mqtt")
    c.api = SimpleNamespace(state=AsyncMock())
    await c._async_update_data()
    c.api.state.assert_not_awaited()


async def test_invalid_optional_key_keeps_app_inventory(coordinator):
    c = coordinator
    c.app = SimpleNamespace(devices=AsyncMock(return_value=[{"device": "new", "sku": "H6199", "topic": "GD/owned"}]))
    c.api = SimpleNamespace(devices=AsyncMock(side_effect=GoveeAuthError()))
    await c.async_discover()
    assert c.api is None and c.openapi_auth_error == "invalid_api_key"
    assert "new" in c.devices


async def test_light_zero_brightness_is_power_off(coordinator):
    c = coordinator;d = next(iter(c.devices.values()));c.async_control = AsyncMock()
    entity = GoveeLight(c, d)
    await entity.async_turn_on(brightness=0)
    c.async_control.assert_awaited_once_with(d, ON_OFF, "powerSwitch", 0)


@pytest.mark.parametrize("platform,instance", [
    ("light", "powerSwitch"),
    ("switch", "powerSwitch"),
    ("switch", "dreamViewToggle"),
    ("switch", "gradientToggle"),
    ("fan", "powerSwitch"),
    ("humidifier", "powerSwitch"),
])
async def test_slider_survives_startup_unconfirmed_commands_and_feedback(
    coordinator, platform, instance
):
    """Check HA's published state, including both conditions selecting its buttons."""
    from custom_components.govee_native.fan import GoveeFan
    from custom_components.govee_native.humidifier import GoveeHumidifier
    from custom_components.govee_native.switch import GoveeSwitch

    c = coordinator
    d = next(iter(c.devices.values()))
    kind = TOGGLE if instance != "powerSwitch" else ON_OFF
    c.ble = SimpleNamespace(
        available=lambda device: True,
        async_write=AsyncMock(return_value=True),
        async_stop=AsyncMock(),
    )
    if platform == "switch":
        entity = GoveeSwitch(c, d, kind, instance)
    else:
        factory = {"light": GoveeLight, "fan": GoveeFan, "humidifier": GoveeHumidifier}
        entity = factory[platform](c, d)
    entity.hass = c.hass
    entity.entity_id = f"{platform}.govee_slider"
    entity._no_platform_reported = True  # Detached entity; use real HA state serialization.

    def published_state(expected, assumed):
        entity.async_write_ha_state()
        state = c.hass.states.get(entity.entity_id)
        assert state.state == expected
        assert not state.attributes.get("assumed_state", False)
        assert state.attributes["state_assumed"] is assumed

    # A visual off fallback must not fabricate a device value or confirmation.
    published_state("off", True)
    assert d.state(kind, instance) is None
    await entity.async_turn_on()
    published_state("on", True)
    await entity.async_turn_off()
    published_state("off", True)
    assert c.confirmed_commands == 0
    assert d.last_command_result == "ble_written_unconfirmed"

    d.report({(kind, instance): 1}, "mqtt")
    assert (kind, instance) not in d.assumed_keys
    # DreamView also writes the video mode; its uncertainty must be retained.
    published_state("on", bool(d.assumed_keys))
    d.report({(kind, instance): 0}, "mqtt")
    published_state("off", bool(d.assumed_keys))


async def test_segment_slider_retains_write_only_diagnostics(coordinator):
    from custom_components.govee_native.light import GoveeSegmentLight

    c = coordinator
    d = next(iter(c.devices.values()))
    c.mqtt = FakeMqtt(c, confirm=False)
    d.report({(ON_OFF, "powerSwitch"): 1}, "mqtt")
    entity = GoveeSegmentLight(c, d, 0)
    entity.hass = c.hass
    entity.entity_id = "light.govee_segment_slider"
    entity._no_platform_reported = True

    for command, expected in [(None, "off"), (entity.async_turn_on, "on"),
                              (entity.async_turn_off, "off")]:
        if command:
            await command()
        entity.async_write_ha_state()
        state = c.hass.states.get(entity.entity_id)
        assert state.state == expected
        assert not state.attributes.get("assumed_state", False)
        assert state.attributes["state_assumed"] is True
    assert c.confirmed_commands == 0


@pytest.mark.parametrize("uptime", [0.0, 60.0, 1000000.0])
async def test_connection_sensor_does_not_equate_broker_to_device(coordinator, monkeypatch, uptime):
    monkeypatch.setattr("custom_components.govee_native.sensor.monotonic", lambda: uptime)
    monkeypatch.setattr("custom_components.govee_native.models.monotonic", lambda: uptime)
    c = coordinator;d = next(iter(c.devices.values()));c.mqtt = FakeMqtt(c)
    sensor = GoveeAppConnectionSensor(c, d)
    assert sensor.native_value == "waiting_for_device"
    assert sensor.extra_state_attributes["last_device_report_age"] is None
    c.mqtt_update({"device": d.device_id, "state": {"onOff": 1}})
    assert sensor.native_value == "mqtt"
    assert sensor.extra_state_attributes["last_device_report_age"] == 0


@pytest.mark.parametrize("uptime", [0.0, 60.0, 1000000.0])
async def test_stale_broker_device_is_not_available(coordinator, monkeypatch, uptime):
    monkeypatch.setattr("custom_components.govee_native.entity.monotonic", lambda: uptime)
    monkeypatch.setattr("custom_components.govee_native.models.monotonic", lambda: uptime)
    c = coordinator;d = next(iter(c.devices.values()));c.mqtt = FakeMqtt(c)
    assert not GoveeEntity(c, d, "test").available
    c.mqtt_update({"device": d.device_id, "state": {"onOff": 1}})
    assert GoveeEntity(c, d, "test").available


async def test_diagnostic_catalog_ids_are_hashed(coordinator):
    c = coordinator;d = next(iter(c.devices.values()))
    c.hass.data["govee_native"] = {"entry": c}
    c.app_catalogs[f"scenes_{d.device_id}"] = {"data": []}
    result = await async_get_config_entry_diagnostics(c.hass, SimpleNamespace(entry_id="entry"))
    assert d.device_id not in json.dumps(result)
    assert result["version"] == "0.4.2"


@pytest.mark.parametrize("uptime", [0.0, 60.0, 1000000.0])
def test_lan_status_updates_temperature_mode_and_freshness(monkeypatch, uptime):
    monkeypatch.setattr("custom_components.govee_native.lan.monotonic", lambda: uptime)
    monkeypatch.setattr("custom_components.govee_native.models.monotonic", lambda: uptime)
    d = make_device();lan = GoveeLan({d.device_id: d}, lambda: None)
    lan.transport = SimpleNamespace(sendto=lambda *args: None)
    lan._packet(json.dumps({"msg": {"cmd": "scan", "data": {"ip": "192.168.1.10", "device": d.device_id, "sku": "H6199"}}}).encode(), ("192.168.1.10", 4001))
    assert not lan.available(d)
    d.states[(COLOR, "colorTemperatureK")] = 4500
    lan._packet(b'{"msg":{"cmd":"devStatus","data":{"onOff":1,"brightness":29,"colorTemInKelvin":0,"color":{"r":255,"g":0,"b":0}}}}', ("192.168.1.10", 4003))
    assert lan.available(d) and d.state(COLOR, "colorTemperatureK") == 0
    assert d.state(COLOR, "colorRgb") == 0xFF0000


async def test_actual_mqtt_publish_injects_account_topic_and_rate_limits():
    client = GoveeAppMqtt({}, "GA/test-account", "123", "stable-id", lambda p: None)
    client.connected = True;client._client = SimpleNamespace(publish=AsyncMock())
    await client.publish("GD/owned", "turn", {"val": 1})
    await client.publish("GD/owned", "status", query=True, cmd_version=2)
    sent = [json.loads(c.args[1])["msg"] for c in client._client.publish.await_args_list]
    assert all(p["accountTopic"] == "GA/test-account" for p in sent)
    assert client.commands_published == 1 and sent[1]["type"] == 0


async def test_ble_does_not_connect_to_stale_advertisement(coordinator):
    c = coordinator;d = next(iter(c.devices.values()));ble = GoveeBluetooth(c.hass)
    with patch("custom_components.govee_native.bluetooth.bluetooth.async_address_present", return_value=False), patch("custom_components.govee_native.bluetooth.establish_connection", new_callable=AsyncMock) as connect:
        assert not await ble.async_write(d, [frame(0x33, 1, 1)])
        connect.assert_not_awaited()


async def test_ble_write_disconnects_and_does_not_claim_device_confirmation(coordinator):
    c = coordinator;d = next(iter(c.devices.values()));ble = GoveeBluetooth(c.hass)
    client = SimpleNamespace(services=SimpleNamespace(get_characteristic=lambda _: SimpleNamespace(properties=["write"])),
                             write_gatt_char=AsyncMock(), disconnect=AsyncMock())
    with patch("custom_components.govee_native.bluetooth.bluetooth.async_address_present", return_value=True), patch("custom_components.govee_native.bluetooth.bluetooth.async_ble_device_from_address", return_value=object()), patch("custom_components.govee_native.bluetooth.establish_connection", new_callable=AsyncMock, return_value=client):
        # Real Bleak clients are hashable; SimpleNamespace is not.
        class Client:
            pass
        instance = Client();instance.__dict__.update(client.__dict__)
        with patch("custom_components.govee_native.bluetooth.establish_connection", new_callable=AsyncMock, return_value=instance):
            assert await ble.async_write(d, [frame(0x33, 1, 1)])
            instance.disconnect.assert_awaited_once()
            assert instance.write_gatt_char.await_count == 2  # keepalive + command
    assert ble.writes == 1 and not d.states


async def test_all_platforms_construct_h6199_entities(coordinator):
    import importlib
    from custom_components.govee_native.const import PLATFORMS
    c = coordinator
    c.hass.data["govee_native"] = {"test": c}
    entry = SimpleNamespace(entry_id="test", options={})
    entities = []
    for name in PLATFORMS:
        module = importlib.import_module("custom_components.govee_native." + name)
        await module.async_setup_entry(c.hass, entry, lambda additions: entities.extend(additions))
    assert sum(isinstance(e, GoveeLight) for e in entities) == 1
    assert len([e for e in entities if type(e).__name__ == "GoveeSegmentLight"]) == 15
    assert len([e for e in entities if type(e).__name__ == "GoveeRefreshButton"]) == 2
    ids = [e.unique_id for e in entities]
    assert len(ids) == len(set(ids))


async def test_local_discovery_survives_expired_account(coordinator):
    from custom_components.govee_native.app_api import AppAuthenticationError
    c = coordinator;c.allow_cached_inventory = True
    c.app = SimpleNamespace(devices=AsyncMock(side_effect=AppAuthenticationError()))
    await c.async_discover()
    assert c.devices and c.app is None
    assert c.app_auth_error == "reauthentication_required"


async def test_local_discovery_survives_network_outage(coordinator):
    from custom_components.govee_native.app_api import AppAPIError
    c = coordinator;c.allow_cached_inventory = True
    c.app = SimpleNamespace(devices=AsyncMock(side_effect=AppAPIError()))
    await c.async_discover()
    assert c.devices and c._discovered


async def test_inventory_cache_excludes_credentials_and_state(coordinator):
    from custom_components.govee_native.inventory import InventoryCache
    c = coordinator;d = next(iter(c.devices.values()))
    d.app_data.update(token="must-not-store", password="must-not-store", last={"onOff": 1})
    d.assume(ON_OFF, "powerSwitch", 1)
    cache = InventoryCache(c.hass, "test")
    cache.store = SimpleNamespace(async_save=AsyncMock(), async_load=AsyncMock())
    await cache.save(c.devices)
    data = cache.store.async_save.call_args.args[0]
    assert "must-not-store" not in json.dumps(data)
    cache.store.async_load.return_value = data
    restored = await cache.load()
    assert restored[d.device_id].app_data["ble_address"] == d.app_data["ble_address"]
    assert not restored[d.device_id].states


async def test_real_setup_and_unload_registers_and_cleans_services(coordinator):
    import custom_components.govee_native as integration
    from custom_components.govee_native.app_api import GoveeAppAPI
    from homeassistant.config_entries import ConfigEntry, ConfigEntries, ConfigEntryState
    from types import MappingProxyType
    c = coordinator;hass = c.hass
    hass.config_entries = ConfigEntries(hass, {})
    entry = ConfigEntry(domain="govee_native", version=2, minor_version=1, source="user", title="Test",
        data={"email": "test@example.invalid", "app_token": "test", "app_account_topic": "GA/test", "app_account_id": "test"},
        options={"app_catalogs": False, "app_mqtt": False}, unique_id="test", subentries_data=[], discovery_keys=MappingProxyType({}), state=ConfigEntryState.SETUP_IN_PROGRESS)
    with patch.object(integration, "async_get_clientsession", return_value=object()), patch.object(GoveeAppAPI, "devices", new_callable=AsyncMock, return_value=[{"device": "id", "sku": "H6199", "topic": "GD/test"}]), patch.object(hass.config_entries, "async_forward_entry_setups", new_callable=AsyncMock), patch.object(hass.config_entries, "async_unload_platforms", new_callable=AsyncMock, return_value=True):
        assert await integration.async_setup_entry(hass, entry)
        assert hass.services.has_service("govee_native", "set_segments")
        assert entry.entry_id in hass.data["govee_native"]
        assert await integration.async_unload_entry(hass, entry)
        assert not hass.services.has_service("govee_native", "set_segments")


async def test_bluetooth_options_keep_existing_settings(coordinator):
    from custom_components.govee_native.config_flow import GoveeNativeOptionsFlow
    c = coordinator;d = next(iter(c.devices.values()))
    entry = SimpleNamespace(entry_id="test", options={"poll_seconds": 60, "private_mutations": True})
    c.hass.data["govee_native"] = {"test": c}
    flow = GoveeNativeOptionsFlow();flow.hass = c.hass
    with patch.object(GoveeNativeOptionsFlow, "config_entry", property(lambda self: entry)):
        result = await flow.async_step_init({"bluetooth_control": True, "configure_bluetooth": True})
        assert result["step_id"] == "bluetooth"
        result = await flow.async_step_bluetooth({"device_id": d.device_id, "address": "AA:BB:CC:DD:EE:FF"})
    assert result["type"] == "create_entry"
    assert result["data"]["private_mutations"] is True
    assert result["data"]["bluetooth_addresses"][d.device_id] == "AA:BB:CC:DD:EE:FF"


async def test_bluetooth_options_reject_eight_byte_cloud_id(coordinator):
    from custom_components.govee_native.config_flow import GoveeNativeOptionsFlow
    c = coordinator;d = next(iter(c.devices.values()))
    entry = SimpleNamespace(entry_id="test", options={})
    c.hass.data["govee_native"] = {"test": c}
    flow = GoveeNativeOptionsFlow();flow.hass = c.hass;flow._pending_options = {}
    with patch.object(GoveeNativeOptionsFlow, "config_entry", property(lambda self: entry)):
        result = await flow.async_step_bluetooth({"device_id": d.device_id, "address": d.device_id})
    assert result["errors"]["base"] == "invalid_bluetooth_address"
