"""Hybrid coordinator: private Govee app inventory/MQTT plus documented cloud API.

The private app is the primary discovery source when account login is provided.
OpenAPI is optional and may be used for additional supported device controls.
"""
from __future__ import annotations
import asyncio
import logging
import base64
from time import monotonic
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import GoveeAPI, GoveeError, GoveeAuthError
from .app_api import GoveeAppAPI, AppAPIError, AppAuthenticationError
from .app_mqtt import GoveeAppMqtt, mqtt_control
from .app_models import discover_app_device, mqtt_state_packet
from .const import SCENE, DIY_COLOR
from .models import Device, InvalidCapabilityValue
from .one_click import OneClickShortcut, parse_one_click_shortcuts
from .lan import GoveeLan
from .h6199 import add_capabilities, control_frames

_LOGGER = logging.getLogger(__name__)
MQTT_CONFIRM_TIMEOUT = 8


class GoveeCoordinator(DataUpdateCoordinator[dict[str, Device]]):
    def __init__(self, hass: HomeAssistant, api: GoveeAPI | None, poll_seconds: int,
                 app: GoveeAppAPI | None = None, config_entry=None):
        super().__init__(hass, _LOGGER, name="Govee Native", config_entry=config_entry,
                         update_interval=timedelta(seconds=poll_seconds))
        self.api=api
        self.app=app
        self.openapi_auth_error: str | None = None
        self._write_locks: dict[str, asyncio.Lock] = {}
        self.devices: dict[str,Device]={}
        self.app_rows: list[dict]=[]
        self.app_catalogs: dict[str, Any]={}
        self.one_clicks: dict[str, OneClickShortcut] = {}
        self.one_click_last_error: str | None = None
        self.one_click_runs = 0
        self.mqtt: GoveeAppMqtt | None=None
        self.lan: GoveeLan | None=None
        self.ble = None
        self.prefer_bluetooth = False
        self.h6199_profile = "classic"
        self._mqtt_retry_after: dict[str, float] = {}
        self._background: set[asyncio.Task] = set()
        self._closed = False
        self._last_status_query: dict[str, float] = {}
        self.inventory_cache = None
        self._discovered = False
        self.allow_cached_inventory = False
        self.app_auth_error: str | None = None
        self.app_unavailable = False
        self._scene_lock=asyncio.Lock()
        self._last_discovery=0.0
        self.confirmed_device_updates=0
        self.last_confirmed_device_update=None
        self.pending_commands: dict[str,dict[str,Any]]={}
        self._confirmation_waiters: dict[str,asyncio.Future] = {}
        self.confirmed_commands = 0
        self.unconfirmed_commands = 0

    def _disable_invalid_openapi(self) -> None:
        """Stop using a revoked Developer key without breaking private-app control.

        Never cache or log a key. The owner can restore it by updating the
        original integration's key and reloading Govee Native.
        """
        if self.api is not None:
            _LOGGER.warning("Govee Developer API key was rejected (401/403); disabling OpenAPI for this entry until reload")
        self.api = None
        self.openapi_auth_error = "invalid_api_key"
        for dev in self.devices.values():
            dev.last_error = None
        if self.devices:
            self.async_set_updated_data(dict(self.devices))

    def mqtt_connection_changed(self, connected: bool) -> None:
        """Keep HA entity availability and connection diagnostics current."""
        self.async_set_updated_data(dict(self.devices))
        if connected and not self._closed:
            self._mqtt_retry_after.clear()
            self._last_status_query.clear()
            task = self.hass.async_create_task(self.async_query_status(), "Govee MQTT initial status")
            self._background.add(task)
            task.add_done_callback(self._background.discard)

    async def async_query_status(self, *, force: bool = False) -> None:
        if not self.mqtt or not self.mqtt.connected or self._closed:
            return
        for dev in self.devices.values():
            topic = dev.app_data.get("topic")
            last_query = self._last_status_query.get(dev.device_id)
            if not topic or (not force and last_query is not None and monotonic() - last_query < 30):
                continue
            try:
                await self.mqtt.publish(topic, "status", query=True, cmd_version=2)
                self._last_status_query[dev.device_id] = monotonic()
            except Exception as exc:
                _LOGGER.debug("Govee MQTT status query failed: %s", type(exc).__name__)

    def _merge_device(self, incoming: Device) -> None:
        current = self.devices.get(incoming.device_id)
        if current:
            current.name, current.sku = incoming.name, incoming.sku
            current.capabilities.update(incoming.capabilities)
            current.scene_labels.update(incoming.scene_labels)
            if incoming.app_source:
                current.app_source = True
                current.app_data = incoming.app_data
            if incoming.cloud_source:
                current.cloud_source = True
                current.cloud_capabilities = incoming.cloud_capabilities
                current.kind = incoming.kind
        else:
            current = incoming
            self.devices[incoming.device_id] = current
        current.local_settings["protocol_profile"] = self.h6199_profile
        add_capabilities(current)

    async def async_discover(self) -> None:
        """Both sources are merged, preserving original entity identity."""
        successes=0
        if self.app:
            try:
                self.app_rows=await self.app.devices()
                self.app_unavailable = False
                for raw in self.app_rows:
                    payload=discover_app_device(raw)
                    if not payload:continue
                    dev=Device.from_payload(payload)
                    self._merge_device(dev)
                successes+=1
            except AppAuthenticationError as exc:
                if not self._keep_local_on_auth_error():
                    raise ConfigEntryAuthFailed("Govee mobile account login expired") from exc
            except AppAPIError as exc:
                self.app_unavailable = True
                _LOGGER.warning("Private app discovery unavailable: %s",exc)
        if self.api:
            try:
                rows=await self.api.devices()
                for raw in rows:
                    if not isinstance(raw,dict) or not raw.get("device") or not raw.get("sku"):
                        continue
                    dev=Device.from_payload(raw)
                    dev.cloud_source = True
                    dev.cloud_capabilities = set(dev.capabilities)
                    self._merge_device(dev)
                successes+=1
            except GoveeAuthError as exc:
                self._disable_invalid_openapi()
                if not successes and not (self.devices and self.allow_cached_inventory):
                    raise ConfigEntryAuthFailed("Govee OpenAPI key invalid") from exc
            except GoveeError as exc:
                _LOGGER.debug("OpenAPI discovery failed: %s",exc)
        if not successes:
            if self.devices and self.allow_cached_inventory:
                self._discovered = True
                self._last_discovery = monotonic()
                return
            raise UpdateFailed("Neither app account nor OpenAPI could discover devices")
        self._discovered = True
        self._last_discovery = monotonic()
        if self.inventory_cache:
            await self.inventory_cache.save(self.devices)

    def _keep_local_on_auth_error(self) -> bool:
        if not self.devices or not self.allow_cached_inventory:
            return False
        self.app_auth_error = "reauthentication_required"
        self.app_unavailable = True
        self.app = None
        if self.config_entry:
            self.config_entry.async_start_reauth(self.hass)
        return True

    async def _async_update_data(self) -> dict[str,Device]:
        discovered = False
        if not self.devices or not self._discovered or monotonic() - self._last_discovery >= 900:
            await self.async_discover()
            discovered = True
        if self.lan:self.lan.maybe_scan()
        if self.lan:self.lan.query_status()
        await self.async_query_status()
        # For app-only accounts, BFF is the primary source of latest device data.
        if self.app and not discovered:
            try:
                rows=await self.app.devices()
                recovered = self.app_unavailable
                self.app_unavailable = False
                self.app_rows=rows
                for raw in rows:
                    payload=discover_app_device(raw)
                    if not payload:continue
                    dev=self.devices.get(payload["device"])
                    if dev:
                        dev.app_data=payload["private"]
                        dev.app_source=True
                if recovered and self.config_entry:
                    # Rebuild transports/catalog entities skipped at an
                    # offline startup. HA serializes its scheduled reload.
                    self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
            except AppAuthenticationError as exc:
                if not self._keep_local_on_auth_error():
                    raise ConfigEntryAuthFailed("Govee account login expired") from exc
            except AppAPIError as exc:
                self.app_unavailable = True
                _LOGGER.debug("BFF state poll unavailable: %s",exc)
        if self.api:
            async def refresh(dev: Device) -> bool:
                # Private-only devices may not be visible through Developer API.
                if not dev.cloud_source:return False
                if monotonic() - dev.last_report_at < 90 and dev.last_transport in ("mqtt", "lan"):
                    return True
                try:
                    api = self.api
                    if api is None:return False
                    started = monotonic()
                    payload=await api.state(dev.sku,dev.device_id)
                    dev.update_state(payload, requested_at=started)
                    return True
                except GoveeAuthError as exc:
                    self._disable_invalid_openapi()
                    if not self.app:
                        raise ConfigEntryAuthFailed("Govee OpenAPI authorization failed") from exc
                    return False
                except GoveeError as exc:
                    # Cloud polling errors must not make an online app-MQTT
                    # device unavailable or invalidate its latest push state.
                    if not dev.app_source:
                        dev.last_error=str(exc)
                    return False
            await asyncio.gather(*(refresh(d) for d in self.devices.values()))
        return dict(self.devices)

    def mqtt_update(self, packet: dict) -> None:
        """Process only identifiable, known device status (never arbitrary state)."""
        device_id, states=mqtt_state_packet(packet)
        from .lan import normalize_id
        dev=self.devices.get(device_id) or next((d for d in self.devices.values()
            if device_id and normalize_id(d.device_id) == normalize_id(device_id)), None)
        if dev is None and isinstance(packet.get("_govee_topic"), str):
            topic=packet["_govee_topic"]
            dev=next((d for d in self.devices.values() if d.app_data.get("topic") == topic),None)
        if dev is None:return
        # Only ever accept state updates for capabilities advertised by this
        # device; status reports may carry other SKUs' fields.
        states = {key: val for key, val in states.items() if dev.has(*key)}
        if not states:
            return
        self._apply_report(dev, states, "mqtt")

    def _apply_report(self, dev: Device, states: dict, source: str) -> None:
        dev.report(states, source)
        self.confirmed_device_updates += 1
        self.last_confirmed_device_update = self.hass.loop.time()
        # Clear a pending write only when the device actually reports the
        # requested property with the requested value.
        pending = self.pending_commands.get(dev.device_id)
        if pending and states.get((pending["kind"], pending["instance"])) == pending["value"]:
            self.pending_commands.pop(dev.device_id, None)
            waiter=self._confirmation_waiters.get(dev.device_id)
            if waiter and not waiter.done():
                waiter.set_result(True)
            self.confirmed_commands += 1
            dev.last_command_result = "confirmed"
        self.async_set_updated_data(dict(self.devices))

    async def async_control(self, device: Device, kind: str, instance: str,
                            value: Any, *, refresh: bool = True) -> None:
        """Write once and confirm against a fresh device report.

        Register the waiter *before* publishing to avoid racing with fast
        replies. Serialize control per device to avoid overwriting waiters.
        A broker PUBACK is not a device acknowledgement.
        """
        try:
            device.validate(kind, instance, value)
        except InvalidCapabilityValue as exc:
            raise ValueError(str(exc)) from exc

        lock = self._write_locks.setdefault(device.device_id, asyncio.Lock())
        async with lock:
            device.last_command_result = "pending"
            if self.lan and await self.lan.async_control(device,kind,instance,value):
                device.last_command_transport = "lan"
                device.last_command_result = "confirmed"
                return
            frames = control_frames(device, kind, instance, value)
            if self.prefer_bluetooth and frames and await self._async_ble_control(device, kind, instance, value, frames):
                return
            if (self.mqtt and self.mqtt.connected and device.app_data.get("topic")
                    and monotonic() >= self._mqtt_retry_after.get(device.device_id, 0)):
                cmd = mqtt_control(kind,instance,value,device.sku)
                passthrough = cmd is None and frames is not None
                if passthrough:
                    cmd = ("ptReal", {"command": [base64.b64encode(f).decode() for f in frames],
                                      "device": device.device_id, "sku": device.sku}, 0)
                if cmd is not None:
                    loop = asyncio.get_running_loop()
                    waiter = loop.create_future()
                    self._confirmation_waiters[device.device_id] = waiter
                    self.pending_commands[device.device_id] = {
                        "kind":kind,"instance":instance,"value":value,
                        "sent_at":loop.time(),
                    }
                    try:
                        try:
                            await self.mqtt.publish(device.app_data["topic"],cmd[0],cmd[1],cmd_version=cmd[2])
                            device.last_command_transport = "mqtt"
                        except Exception as exc:
                            _LOGGER.warning("Native MQTT publish failed (%s)",type(exc).__name__)
                        else:
                            if passthrough:
                                # Most H6199 mode commands have no scalar status
                                # readback. State is explicitly assumed, and a
                                # PUBACK is never counted as device confirmation.
                                self._assume_write(device, kind, instance, value, "mqtt_published_unconfirmed")
                                await self.async_query_status(force=True)
                                return
                            if not waiter.done():
                                try:
                                    await self.mqtt.publish(device.app_data["topic"],"status",query=True,cmd_version=2)
                                except Exception as exc:
                                    _LOGGER.debug("Govee status query failed: %s",type(exc).__name__)
                            try:
                                await asyncio.wait_for(waiter,timeout=MQTT_CONFIRM_TIMEOUT)
                                self._mqtt_retry_after.pop(device.device_id, None)
                                return
                            except asyncio.TimeoutError:
                                self.unconfirmed_commands += 1
                                device.last_command_result = "mqtt_unconfirmed"
                                _LOGGER.warning("Govee MQTT command unconfirmed by device %s; broker accepted publication",device.sku)
                    finally:
                        self.pending_commands.pop(device.device_id,None)
                        if self._confirmation_waiters.get(device.device_id) is waiter:
                            self._confirmation_waiters.pop(device.device_id,None)
                        if not waiter.done():
                            waiter.cancel()
                        self.async_set_updated_data(dict(self.devices))
                    self._mqtt_retry_after[device.device_id] = monotonic() + 120
                    _LOGGER.debug("Govee MQTT command unavailable; trying alternate transport")
            if frames and await self._async_ble_control(device, kind, instance, value, frames):
                return
            if self.api and device.cloud_source and (kind, instance) in device.cloud_capabilities:
                try:
                    await self.api.control(device.sku,device.device_id,kind,instance,value)
                except GoveeAuthError as exc:
                    device.last_command_result = "failed"
                    self._disable_invalid_openapi()
                    raise GoveeError("Developer API key rejected (401/403); update the key before using cloud fallback") from exc
                except GoveeError:
                    device.last_command_result = "failed"
                    self.async_set_updated_data(dict(self.devices))
                    raise
                device.last_command_transport = "openapi"
                self._assume_write(device, kind, instance, value, "api_accepted_unconfirmed")
                if refresh:
                    await self.async_request_refresh()
                return
            device.last_command_result = "failed"
            self.async_set_updated_data(dict(self.devices))
            raise GoveeError("No working transport; enable app MQTT or LAN, or provide a valid Developer API key")

    def _assume_write(self, device, kind, instance, value, result) -> None:
        device.assume(kind, instance, value)
        device.last_command_result = result
        if kind == "devices.capabilities.color_setting" and instance == "colorRgb":
            device.assume(kind, "colorTemperatureK", 0)
        if device.sku == "H6199":
            device.local_settings[instance] = value
            from .const import MODE, TOGGLE, SEGMENT, COLOR
            video = instance in ("videoArea", "videoStyle", "videoSaturation") or (
                instance == "dreamViewToggle" and value == 1) or (instance == "backlightMode" and value == 1)
            music = instance in ("nativeMusicMode", "musicSensitivity") or (instance == "backlightMode" and value == 2)
            color = (kind == COLOR) or instance in ("segmentedColorRgb", "nativeScene") or (
                instance == "dreamViewToggle" and value == 0) or (instance == "backlightMode" and value == 0)
            if video or music or color:
                device.assume(TOGGLE, "dreamViewToggle", int(video))
            if video or music or (color and instance != "nativeScene"):
                device.assume(MODE, "backlightMode", 1 if video else 2 if music else 0)
            if kind == SEGMENT and instance == "segmentedColorRgb":
                device.segment_colors.update({i: value["rgb"] for i in value["segment"]})
        self.async_set_updated_data(dict(self.devices))

    async def _async_ble_control(self, device, kind, instance, value, frames) -> bool:
        if not self.ble or not await self.ble.async_write(device, frames):
            return False
        device.last_command_transport = "bluetooth"
        self._assume_write(device, kind, instance, value, "ble_written_unconfirmed")
        await self.async_query_status(force=True)
        return True

    async def async_close(self) -> None:
        self._closed = True
        for task in self._background:
            task.cancel()
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)
        self._background.clear()
        for waiter in self._confirmation_waiters.values():
            if not waiter.done():waiter.cancel()
        if self.mqtt:await self.mqtt.stop()
        if self.lan:await self.lan.async_stop()
        if self.ble:await self.ble.async_stop()

    async def async_load_scenes(self,device:Device) -> None:
        """Enumerate catalog from developer API and (separately) Android BFF."""
        async with self._scene_lock:
            if self.app and device.app_source:
                try:
                    self.app_catalogs[f"scenes_{device.device_id}"]=await self.app.lighting_scenes(
                        device.sku, device.device_id, device.app_data.get("goods_type", 0))
                except AppAPIError:
                    pass
            if not self.api:
                self.async_set_updated_data(dict(self.devices));return
            for diy in (False,True):
                try:payload=await self.api.scenes(device.sku,device.device_id,diy)
                except GoveeAuthError:
                    self._disable_invalid_openapi()
                    break
                except GoveeError:continue
                for cap in payload.get("capabilities",[]):
                    typ=cap.get("type");instance=cap.get("instance")
                    if typ not in (SCENE,DIY_COLOR) or not instance:continue
                    device.capabilities[(typ,instance)]=cap
                    device.cloud_capabilities.add((typ, instance))
                    for opt in cap.get("parameters",{}).get("options",[]):
                        value,name=opt.get("value"),opt.get("name")
                        if name is None or value is None:continue
                        label=f"{'DIY' if diy else 'Scene'}: {name}"
                        device.scene_labels[label]={"type":typ,"instance":instance,"value":value}
        self.async_set_updated_data(dict(self.devices))

    async def async_load_private_catalogs(self) -> None:
        """Load APK-defined Tap-to-Run actions and read-only app catalogs.

        Scene activation is only offered when Govee actually supplied a full
        command envelope for an account-owned device. A scene *name* alone is
        not sufficient evidence of a working private write protocol.
        """
        if not self.app:
            return
        try:
            response = await self.app.one_click_shortcuts()
            topics = {d.app_data["topic"]: d.device_id for d in self.devices.values()
                      if d.app_source and isinstance(d.app_data.get("topic"), str) and d.app_data["topic"]}
            new_shortcuts = parse_one_click_shortcuts(response, topics)
            self.one_clicks = new_shortcuts
            self.one_click_last_error = None
            self.app_catalogs["tap_to_run"] = {"available_actions": len(new_shortcuts)}
            for device in self.devices.values():
                # Clear only the app-derived shortcuts, never Developer API scenes.
                for label in list(device.scene_labels):
                    if device.scene_labels[label].get("source") == "one_click":
                        del device.scene_labels[label]
            for key, shortcut in new_shortcuts.items():
                if len(shortcut.device_ids) == 1:
                    device = self.devices.get(shortcut.device_ids[0])
                    if device and device.kind == "light":
                        device.scene_labels[f"App: {shortcut.name}"] = {
                            "source": "one_click", "shortcut_id": key,
                        }
        except (AppAPIError, ValueError, TypeError) as exc:
            self.one_click_last_error = type(exc).__name__
            _LOGGER.warning("Govee app Tap-to-Run catalog unavailable: %s", type(exc).__name__)
        try:
            self.app_catalogs["groups"] = await self.app.groups()
        except AppAPIError as exc:
            _LOGGER.debug("Govee app groups catalog unavailable: %s", type(exc).__name__)
        self.async_set_updated_data(dict(self.devices))

    async def async_activate_one_click(self, shortcut_id: str) -> None:
        """Execute the exact APK-stored IoT packets; no inferred scene IDs."""
        from homeassistant.exceptions import HomeAssistantError
        shortcut = self.one_clicks.get(shortcut_id)
        if not shortcut:
            raise HomeAssistantError("Govee app shortcut not found; refresh app catalogs")
        if not self.mqtt or not self.mqtt.connected:
            raise HomeAssistantError("Govee account MQTT disconnected; cannot activate app shortcut")
        try:
            for command in shortcut.commands:
                await self.mqtt.publish_app_packet(command.topic, command.packet)
        except Exception as exc:
            # Never log topic, arbitrary payload or private account ID.
            raise HomeAssistantError(f"Govee app shortcut publish failed ({type(exc).__name__})") from exc
        self.one_click_runs += 1
        for topic in dict.fromkeys(cmd.topic for cmd in shortcut.commands):
            try:
                await self.mqtt.publish(topic, "status", query=True, cmd_version=2)
            except Exception:
                _LOGGER.debug("Govee shortcut post-command status query unavailable")
        self.async_set_updated_data(dict(self.devices))
