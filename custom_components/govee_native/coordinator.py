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
from .h6199 import add_capabilities
from .native_protocol import control_frames
from .capabilities import add_model_capabilities, native_outlet_value, segment_indices, segment_batch_size
from .confirmation import CommandConfirmation
from .reports import extra_report, state_envelope, app_thermometer_report
from .runtime_store import RuntimeStore, restore_segments, segment_snapshot

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
        self.confirmation = CommandConfirmation(self)
        self.pending_commands = self.confirmation.pending
        self._mqtt_seen: dict[str, float] = {}
        self._local_skips: dict[str, int] = {}
        self._poll_base = poll_seconds
        self._status_unsub = None
        self._app_poll_unsub = None
        self._app_lock = asyncio.Lock()
        self._last_app_refresh = None
        self.runtime_store = RuntimeStore(hass, config_entry.entry_id) if config_entry else None
        self._runtime_data = {}
        self._scene_loaded_at: dict[str, float] = {}
        self._scene_locks: dict[str, asyncio.Lock] = {}
        self._last_status_sent = None
        self._query_strikes: dict[str, int] = {}
        self._query_quarantine: set[str] = set()
        self._query_refused_skus: set[str] = set()
        self._queued_writes: dict[tuple, int] = {}
        self.superseded_commands = 0

    def async_set_updated_data(self, data):
        """Publish a push without postponing polls of unrelated cloud devices."""
        self.data = data
        self.last_update_success = True
        if self._listeners and self._unsub_refresh is None and not self._closed:
            self._schedule_refresh()
        self.async_update_listeners()

    def create_background(self, coro, name):
        task = self.hass.async_create_task(coro, name)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    async def async_start(self):
        from homeassistant.helpers.event import async_track_time_interval
        if self._status_unsub is None:
            async def status_tick(_now):
                await self.async_query_status()
            self._status_unsub = async_track_time_interval(self.hass, status_tick, timedelta(seconds=30))
        if self._app_poll_unsub is None:
            async def app_tick(_now):
                try:
                    await self.async_refresh_private()
                except ConfigEntryAuthFailed:
                    if self.config_entry:
                        self.config_entry.async_start_reauth(self.hass)
            self._app_poll_unsub = async_track_time_interval(self.hass, app_tick, timedelta(seconds=self._poll_base))

    async def async_restore_runtime(self):
        if self.runtime_store:
            self._runtime_data = await self.runtime_store.load()
        if self.api:
            self.api.restore_budget(self._runtime_data.get("cloud_budget", {}))
            self.api.on_request = self.save_runtime_later

    def runtime_snapshot(self):
        return {"segments": {d.device_id: segment_snapshot(d) for d in self.devices.values() if segment_indices(d)},
                "energy": {d.device_id: d.local_settings["energy_counter"] for d in self.devices.values()
                           if "energy_counter" in d.local_settings},
                "cloud_budget": self.api.budget_snapshot() if self.api else self._runtime_data.get("cloud_budget", {})}

    def save_runtime_later(self):
        if self.runtime_store and not self._closed:
            self.runtime_store.save_later(self.runtime_snapshot)

    def mqtt_healthy(self, device):
        last = self._mqtt_seen.get(device.device_id)
        return bool(self.mqtt and self.mqtt.connected and device.app_data.get("topic")
                    and last is not None and monotonic() - last < 900
                    and monotonic() >= self._mqtt_retry_after.get(device.device_id, 0))

    def mark_mqtt_unresponsive(self, device):
        self._mqtt_retry_after[device.device_id] = monotonic() + 120

    def has_pending(self, device):
        return any(key[0] == device.device_id for key in self.pending_commands)

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
        if not connected and self._last_status_sent:
            device_id, sent_at = self._last_status_sent
            device = self.devices.get(device_id)
            if device and monotonic() - sent_at < 2 and self._mqtt_seen.get(device_id, -1) < sent_at:
                sku = device.sku.upper()
                self._query_strikes[sku] = self._query_strikes.get(sku, 0) + 1
                if self._query_strikes[sku] >= 2:
                    self._query_quarantine.add(sku)
                    self._query_refused_skus.add(sku)
            self._last_status_sent = None
        if connected and not self._closed:
            self._mqtt_seen.clear()
            self._mqtt_retry_after.clear()
            self._last_status_query.clear()
            task = self.hass.async_create_task(self.async_query_status(), "Govee MQTT initial status")
            self._background.add(task)
            task.add_done_callback(self._background.discard)

    async def async_query_status(self, *, force: bool = False, device=None) -> None:
        if not self.mqtt or not self.mqtt.connected or self._closed:
            return
        for dev in ([device] if device else self.devices.values()):
            from .capabilities import NO_STATUS_QUERY_MODELS
            if dev.sku.upper() in NO_STATUS_QUERY_MODELS | self._query_quarantine or len(self._query_refused_skus) >= 3:
                continue
            topic = dev.app_data.get("topic")
            last_query = self._last_status_query.get(dev.device_id)
            if not topic or (not force and last_query is not None and monotonic() - last_query < 30):
                continue
            try:
                self._last_status_sent = (dev.device_id, monotonic())
                from .capabilities import PROBE_MODELS
                from .h6199 import frame
                probe_count = PROBE_MODELS.get(dev.sku.upper(), 0)
                if probe_count:
                    for probe in range(1, probe_count + 1):
                        command = base64.b64encode(frame(0xAA, 0x24, probe)).decode()
                        await self.mqtt.publish(topic, "ptReal", {"command": [command], "device": dev.device_id,
                            "sku": dev.sku}, read_only=True, cmd_version=0)
                else:
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
        add_model_capabilities(current)
        if current is incoming:
            restore_segments(current, self._runtime_data.get("segments", {}).get(current.device_id))
            energy = self._runtime_data.get("energy", {}).get(current.device_id)
            if isinstance(energy, dict):
                current.local_settings["energy_counter"] = energy
        readings = app_thermometer_report(current)
        if readings:
            current.report(readings, "app")

    async def async_discover(self) -> None:
        """Both sources are merged, preserving original entity identity."""
        before = self.discovery_shape() if self._discovered else None
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
        if before is not None and before != self.discovery_shape() and self.config_entry:
            self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)

    def discovery_shape(self):
        return {key: (device.kind, frozenset(device.capabilities), segment_indices(device))
                for key, device in self.devices.items()}

    def _keep_local_on_auth_error(self) -> bool:
        if not self.devices or not self.allow_cached_inventory:
            return False
        self.app_auth_error = "reauthentication_required"
        self.app_unavailable = True
        self.app = None
        if self.config_entry:
            self.config_entry.async_start_reauth(self.hass)
        return True

    async def async_refresh_private(self):
        if not self.app or self._closed or self._app_lock.locked():
            return
        async with self._app_lock:
            if self._last_app_refresh is not None and monotonic() - self._last_app_refresh < self._poll_base:
                return
            self._last_app_refresh = monotonic()
            try:
                rows=await self.app.devices()
                recovered = self.app_unavailable
                self.app_unavailable = False
                self.app_rows=rows
                for raw in rows:
                    payload=discover_app_device(raw)
                    if not payload:continue
                    dev=self.devices.get(payload["device"])
                    if dev is None:
                        self._merge_device(Device.from_payload(payload))
                        if self.config_entry:
                            self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
                    if dev:
                        dev.app_data=payload["private"]
                        dev.app_source=True
                        readings = app_thermometer_report(dev)
                        if readings:
                            dev.report(readings, "app")
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
            self.async_set_updated_data(dict(self.devices))

    async def _async_update_data(self) -> dict[str,Device]:
        discovered = False
        if not self.devices or not self._discovered or monotonic() - self._last_discovery >= 900:
            await self.async_discover()
            discovered = True
        if self.lan:self.lan.maybe_scan()
        if self.lan:self.lan.query_status()
        await self.async_query_status()
        if not discovered:
            await self.async_refresh_private()
        if self.api:
            pollable = [d for d in self.devices.values() if d.cloud_source and not self.all_entities_disabled(d)]
            self.update_interval = timedelta(seconds=self.api.poll_interval(self._poll_base, len(pollable)))
            async def refresh(dev: Device) -> bool:
                # Private-only devices may not be visible through Developer API.
                if not dev.cloud_source or self.all_entities_disabled(dev):return False
                skips = self._local_skips.get(dev.device_id, 0)
                if monotonic() - dev.last_report_at < 90 and dev.last_transport in ("mqtt", "lan") and skips < 5:
                    self._local_skips[dev.device_id] = skips + 1
                    return True
                self._local_skips[dev.device_id] = 0
                if not self.api.can_poll():
                    return False
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

    def all_entities_disabled(self, device):
        if not self.config_entry:
            return False
        from homeassistant.helpers import entity_registry as er
        registry = er.async_get(self.hass)
        prefix = device.device_id.lower().replace(":", "") + "_"
        entries = [entry for entry in er.async_entries_for_config_entry(registry, self.config_entry.entry_id)
                   if entry.unique_id.startswith(prefix)]
        return bool(entries) and all(entry.disabled for entry in entries)

    def mqtt_update(self, packet: dict) -> None:
        """Process only identifiable, known device status (never arbitrary state)."""
        device_id, states=mqtt_state_packet(packet)
        envelope = state_envelope(packet)
        device_id = device_id or envelope.get("device") or envelope.get("deviceId")
        from .lan import normalize_id
        dev=self.devices.get(device_id) or next((d for d in self.devices.values()
            if device_id and normalize_id(d.device_id) == normalize_id(device_id)), None)
        if dev is None and isinstance(packet.get("_govee_topic"), str):
            topic=packet["_govee_topic"]
            dev=next((d for d in self.devices.values() if d.app_data.get("topic") == topic),None)
        if dev is None:return
        states.update(extra_report(dev, packet))
        # Only ever accept state updates for capabilities advertised by this
        # device; status reports may carry other SKUs' fields.
        states = {key: val for key, val in states.items() if dev.has(*key)}
        if not states:
            return
        self._apply_report(dev, states, "mqtt")

    def _apply_report(self, dev: Device, states: dict, source: str) -> None:
        if source == "mqtt":
            self._mqtt_seen[dev.device_id] = monotonic()
            self._mqtt_retry_after.pop(dev.device_id, None)
        dev.report(states, source)
        self.confirmed_device_updates += 1
        self.last_confirmed_device_update = self.hass.loop.time()
        # Clear a pending write only when the device actually reports the
        # requested property with the requested value.
        self.confirmation.on_report(dev, states)
        self.async_set_updated_data(dict(self.devices))

    async def async_control(self, device: Device, kind: str, instance: str,
                            value: Any, *, refresh: bool = True) -> None:
        """Serialize sending only; confirmation never holds the command lock."""
        zone = kind == "govee_native.zone"
        try:
            if zone:
                from .capabilities import MASKED_RING_MODELS
                if device.sku.upper() not in MASKED_RING_MODELS or instance not in ("mainPanel", "ring"):
                    raise InvalidCapabilityValue("Unknown native light zone")
                if device.sku.upper() == "H1232" and instance != "mainPanel":
                    raise InvalidCapabilityValue("Use the discovered H1232 ring segments")
                if not isinstance(value, dict) or value.get("instance") not in ("segmentedColorRgb", "segmentedBrightness", "segmentedColorTemperature"):
                    raise InvalidCapabilityValue("Unknown native zone command")
                field = {"segmentedColorRgb": "rgb", "segmentedBrightness": "brightness", "segmentedColorTemperature": "kelvin"}[value["instance"]]
                low, limit = (2700, 6500) if field == "kelvin" else (0, 0xFFFFFF if field == "rgb" else 100)
                if set(value) != {"instance", field} or type(value.get(field)) is not int or not low <= value[field] <= limit:
                    raise InvalidCapabilityValue("Invalid native zone value")
            elif kind == "devices.capabilities.segment_color_setting":
                from .capabilities import MASKED_RING_MODELS
                native_ring = device.sku.upper() in MASKED_RING_MODELS
                if not isinstance(value, dict) or not isinstance(value.get("segment"), list):
                    raise InvalidCapabilityValue("Segment command requires indices")
                indices = segment_indices(device)
                if not value["segment"] or any(type(i) is not int or i not in indices for i in value["segment"]):
                    raise InvalidCapabilityValue("Segment index is not addressable on this device")
                if native_ring:
                    field = "rgb" if instance == "segmentedColorRgb" else "brightness" if instance == "segmentedBrightness" else None
                    if field is None or set(value) != {"segment", field} or type(value.get(field)) is not int or not 0 <= value[field] <= (0xFFFFFF if field == "rgb" else 100):
                        raise InvalidCapabilityValue("Invalid native segment value")
                else:
                    # A whole-group request may exceed the API's batch size;
                    # each batch is validated and sent separately below.
                    size = segment_batch_size(device, instance)
                    for offset in range(0, len(value["segment"]), size):
                        device.validate(kind, instance, {**value, "segment": value["segment"][offset:offset + size]})
            else:
                device.validate(kind, instance, value)
        except InvalidCapabilityValue as exc:
            raise ValueError(str(exc)) from exc

        lock = self._write_locks.setdefault(device.device_id, asyncio.Lock())
        queue_key = None
        from .const import COLOR, RANGE, SEGMENT
        if device.kind == "light" and (kind == COLOR or (kind == RANGE and instance == "brightness") or kind == SEGMENT):
            channel = "color_channel" if kind == COLOR else instance
            indices = tuple(sorted(set(value["segment"]))) if kind == SEGMENT else ()
            queue_key = device.device_id, channel, indices
            self._queued_writes[queue_key] = self._queued_writes.get(queue_key, 0) + 1
            ticket = self._queued_writes[queue_key]
        async with lock:
            if queue_key and ticket != self._queued_writes[queue_key]:
                self.superseded_commands += 1
                return
            generation = self.confirmation.supersede(device, kind, instance)
            device.last_command_result = "pending"
            frames = control_frames(device, kind, instance, value)
            if self.prefer_bluetooth and frames and await self._async_ble_control(device, kind, instance, value, frames):
                return
            if self.lan and await self.lan.async_control(device, kind, instance, value,
                    on_send=lambda: self._assume_write(device, kind, instance, value, "lan_sent_unconfirmed")):
                device.last_command_transport = "lan"
                device.last_command_result = "confirmed"
                self.confirmed_commands += 1
                self.async_set_updated_data(dict(self.devices))
                return
            if self.mqtt_healthy(device):
                outlet = native_outlet_value(device, instance, value)
                cmd = ("turn", {"val": outlet}, 0) if outlet is not None else mqtt_control(kind, instance, value, device.sku)
                passthrough = frames is not None and (cmd is None or device.sku.upper() in {"H1232", "H1252"})
                if passthrough:
                    cmd = ("ptReal", {"command": [base64.b64encode(f).decode() for f in frames],
                                      "device": device.device_id, "sku": device.sku}, 0)
                if cmd is not None:
                    pending = None if passthrough or outlet is not None else self.confirmation.prepare(device, kind, instance, value, generation)
                    try:
                        await self.mqtt.publish(device.app_data["topic"], cmd[0], cmd[1], cmd_version=cmd[2])
                    except Exception as exc:
                        self.confirmation.discard(pending)
                        self.mark_mqtt_unresponsive(device)
                        _LOGGER.debug("MQTT send failed (%s); trying another transport", type(exc).__name__)
                    else:
                        device.last_command_transport = "mqtt"
                        # A report can arrive before publish returns. Preserve
                        # that real state rather than re-marking it assumed.
                        if pending is None or self.pending_commands.get((device.device_id, kind, instance)) is pending:
                            self._assume_write(device, kind, instance, value, "mqtt_published_unconfirmed")
                        self.confirmation.start(pending)
                        if pending is None:
                            self.create_background(self.async_query_status(device=device), "Govee native readback")
                        return
            elif self.mqtt and self.mqtt.connected and device.app_data.get("topic"):
                self.create_background(self.async_query_status(device=device), "Govee MQTT health query")
            if frames and await self._async_ble_control(device, kind, instance, value, frames):
                return
            if self.api and device.cloud_source and (kind, instance) in device.cloud_capabilities:
                # Native extended indices must never leak into a REST schema
                # that advertised fewer addressable sections.
                values = [value]
                if kind == "devices.capabilities.segment_color_setting":
                    size = segment_batch_size(device, instance)
                    values = [{**value, "segment": value["segment"][i:i + size]}
                              for i in range(0, len(value["segment"]), size)]
                for batch in values:
                    device.validate(kind, instance, batch)
                pending = self.confirmation.prepare(device, kind, instance, value, generation) if self.mqtt_healthy(device) else None
                try:
                    for batch in values:
                        await self.api.control(device.sku, device.device_id, kind, instance, batch)
                        if kind == SEGMENT:
                            device.last_command_transport = "openapi"
                            self._assume_write(device, kind, instance, batch, "api_accepted_unconfirmed")
                except GoveeAuthError as exc:
                    self.confirmation.discard(pending)
                    device.last_command_result = "failed"
                    self._disable_invalid_openapi()
                    raise GoveeError("Developer API key rejected; update the key before using cloud fallback") from exc
                except GoveeError:
                    self.confirmation.discard(pending)
                    device.last_command_result = "failed"
                    self.async_set_updated_data(dict(self.devices))
                    raise
                device.last_command_transport = "openapi"
                if pending is None or self.pending_commands.get((device.device_id, kind, instance)) is pending:
                    self._assume_write(device, kind, instance, value, "api_accepted_unconfirmed")
                self.confirmation.start(pending)
                if refresh and self.api.can_poll():
                    self.create_background(self.async_request_refresh(), "Govee cloud readback")
                return
            device.last_command_result = "failed"
            self.async_set_updated_data(dict(self.devices))
            raise GoveeError("No responsive transport; enable Bluetooth/LAN or provide a valid Developer API key")

    async def async_retry_confirmed_miss(self, pending):
        """Retry once only when a fresh report still contains the old value."""
        device = pending.device
        async with self._write_locks.setdefault(device.device_id, asyncio.Lock()):
            if not self.confirmation.current(pending) or not self.api or not self.api.can_poll():
                return
            if not device.cloud_source or (pending.kind, pending.instance) not in device.cloud_capabilities:
                return
            try:
                await self.api.control(device.sku, device.device_id, pending.kind, pending.instance, pending.value)
            except GoveeError:
                return
            self._assume_write(device, pending.kind, pending.instance, pending.value, "retried_unconfirmed")

    def _assume_write(self, device, kind, instance, value, result) -> None:
        device.assume(kind, instance, value)
        device.last_command_result = result
        from .const import SEGMENT
        from .capabilities import MULTI_OUTLET_MODELS
        if kind == "devices.capabilities.on_off" and instance == "powerSwitch":
            for index in range(MULTI_OUTLET_MODELS.get(device.sku.upper(), 0)):
                device.assume("devices.capabilities.toggle", f"socketToggle{index + 1}", int(bool(value)))
        if kind == "govee_native.zone":
            device.local_settings.setdefault("zones", {}).setdefault(instance, {}).update(
                {k: v for k, v in value.items() if k in ("rgb", "brightness", "kelvin")})
            if "rgb" in value:
                device.local_settings["zones"][instance]["kelvin"] = 0
            if instance == "ring" and "rgb" in value:
                device.segment_colors.update({i: value["rgb"] for i in segment_indices(device)})
            self.save_runtime_later()
        if kind == SEGMENT:
            if instance == "segmentedColorRgb":
                device.segment_colors.update({i: value["rgb"] for i in value["segment"]})
                remembered = device.local_settings.setdefault("segment_last_colors", {})
                if value["rgb"]:
                    remembered.update({str(i): value["rgb"] for i in value["segment"]})
                if device.sku.upper() == "H1252":
                    zone = device.local_settings.setdefault("zones", {}).setdefault("ring", {})
                    zone["kelvin"] = 0
                    colors = {device.segment_colors.get(i) for i in segment_indices(device)}
                    zone.pop("rgb", None)
                    if len(colors) == 1 and None not in colors:
                        zone["rgb"] = colors.pop()
            elif instance == "segmentedBrightness":
                device.local_settings.setdefault("segment_brightness", {}).update(
                    {str(i): value["brightness"] for i in value["segment"]})
            self.save_runtime_later()
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
        self.create_background(self.async_query_status(device=device), "Govee Bluetooth readback")
        return True

    async def async_close(self) -> None:
        self._closed = True
        self.confirmation.close()
        if self._app_poll_unsub:
            self._app_poll_unsub()
            self._app_poll_unsub = None
        if self._status_unsub:
            self._status_unsub()
            self._status_unsub = None
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
        if self.runtime_store:
            await self.runtime_store.save(self.runtime_snapshot())

    async def async_load_scenes(self,device:Device, *, refresh=False) -> None:
        """Enumerate catalog from developer API and (separately) Android BFF."""
        async with self._scene_locks.setdefault(device.device_id, asyncio.Lock()):
            loaded = self._scene_loaded_at.get(device.device_id)
            if not refresh and loaded is not None and monotonic() - loaded < 21600:
                return
            success = False
            if self.app and device.app_source:
                try:
                    self.app_catalogs[f"scenes_{device.device_id}"]=await self.app.lighting_scenes(
                        device.sku, device.device_id, device.app_data.get("goods_type", 0))
                    success = True
                except AppAPIError:
                    pass
            if not self.api:
                if success:
                    self._scene_loaded_at[device.device_id] = monotonic()
                self.async_set_updated_data(dict(self.devices));return
            for diy in (False,True):
                try:payload=await self.api.scenes(device.sku,device.device_id,diy)
                except GoveeAuthError:
                    self._disable_invalid_openapi()
                    break
                except GoveeError:continue
                success = True
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
            if success:
                self._scene_loaded_at[device.device_id] = monotonic()
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
