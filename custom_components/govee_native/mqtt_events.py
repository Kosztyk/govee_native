"""Optional official OpenAPI MQTT event listener (not the Android app's AWS IoT session)."""

from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
import paho.mqtt.client as mqtt

from .const import DOMAIN, EVENT
from .coordinator import GoveeCoordinator

_LOGGER = logging.getLogger(__name__)


class GoveeEvents:
    def __init__(self, hass: HomeAssistant, coordinator: GoveeCoordinator, api_key: str):
        self.hass = hass
        self.coordinator = coordinator
        self._api_key = api_key
        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            protocol=mqtt.MQTTv311,
            client_id="govee-native-" + __import__("uuid").uuid4().hex[:12],
        )
        self._client.username_pw_set(api_key, api_key)
        self._client.tls_set()
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect
        self._running = False

    async def async_start(self) -> None:
        self._running = True
        # Nonblocking connect; the broker is not required for ordinary polling.
        self._client.connect_async("mqtt.openapi.govee.com", 8883, keepalive=60)
        self._client.loop_start()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code != 0:
            _LOGGER.warning("Govee MQTT connection rejected (code %s)", reason_code)
            return
        # Documentation lists GA/<API key> while its JavaScript sample
        # subscribes directly to <API key>; support both published forms.
        client.subscribe([(f"GA/{self._api_key}", 0), (self._api_key, 0)])

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        if self._running:
            _LOGGER.debug("Govee MQTT disconnected (code %s), reconnecting", reason_code)

    def _on_message(self, client, userdata, message):
        try:
            payload: Any = json.loads(message.payload.decode("utf-8"))
            if not isinstance(payload, dict) or not isinstance(payload.get("capabilities"), list):
                return
            device_id = payload.get("device")
            if device_id not in self.coordinator.devices:
                return
            for cap in payload["capabilities"]:
                if cap.get("type") != EVENT:
                    continue
                instance = cap.get("instance")
                if not instance or not self.coordinator.devices[device_id].has(EVENT, instance):
                    continue
                states = cap.get("state")
                if not isinstance(states, list):
                    continue
                for state in states:
                    if isinstance(state, dict):
                        self.hass.loop.call_soon_threadsafe(
                            async_dispatcher_send, self.hass,
                            f"{DOMAIN}_{device_id}_{instance}", state,
                        )
        except (UnicodeDecodeError, ValueError, TypeError):
            _LOGGER.debug("Ignoring invalid Govee MQTT event data")

    async def async_stop(self) -> None:
        self._running = False
        await self.hass.async_add_executor_job(self._client.disconnect)
        await self.hass.async_add_executor_job(self._client.loop_stop)
