"""Nonblocking confirmation of light writes against fresh device reports."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from .const import COLOR, ON_OFF, RANGE

QUERY_DELAY = 1.5
CONFIRM_WINDOW = 6.0


def matches(kind, instance, actual, wanted):
    if actual is None:
        return False
    if kind == COLOR and instance == "colorRgb" and type(actual) is int and type(wanted) is int:
        return all(abs((actual >> shift & 255) - (wanted >> shift & 255)) <= 2 for shift in (0, 8, 16))
    if type(actual) in (int, float) and type(wanted) in (int, float):
        tolerance = 150 if instance == "colorTemperatureK" else 2 if instance == "brightness" else 0
        return abs(actual - wanted) <= tolerance
    return actual == wanted


@dataclass
class Pending:
    device: Any
    kind: str
    instance: str
    value: Any
    previous: Any
    generation: int
    task: asyncio.Task | None = None
    event: asyncio.Event = field(default_factory=asyncio.Event)
    reported: Any = None
    has_report: bool = False


class CommandConfirmation:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.pending: dict[tuple[str, str, str], Pending] = {}
        self.generations: dict[tuple[str, str, str], int] = {}

    def key(self, device, kind, instance):
        return device.device_id, kind, instance

    def supersede(self, device, kind, instance):
        """Invalidate old retries before a newer write is sent, on any transport."""
        target = self.key(device, kind, instance)
        keys = {target}
        if kind not in (COLOR, RANGE):
            keys.update(k for k in self.generations if k[0] == device.device_id)
        elif kind == COLOR:
            keys.update((device.device_id, COLOR, i) for i in ("colorRgb", "colorTemperatureK"))
        for key in keys:
            self.generations[key] = self.generations.get(key, 0) + 1
            old = self.pending.pop(key, None)
            if old and old.task and old.task is not asyncio.current_task():
                old.task.cancel()
        return self.generations[target]

    def prepare(self, device, kind, instance, value, generation):
        # Write-only modes and segments cannot be confirmed by scalar status.
        if not (device.kind == "light" and (kind, instance) in {
            (ON_OFF, "powerSwitch"), (RANGE, "brightness"),
            (COLOR, "colorRgb"), (COLOR, "colorTemperatureK")}):
            return None
        if matches(kind, instance, device.state(kind, instance), value):
            return None
        pending = Pending(device, kind, instance, value, device.state(kind, instance), generation)
        self.pending[self.key(device, kind, instance)] = pending
        return pending

    def current(self, pending):
        key = self.key(pending.device, pending.kind, pending.instance)
        return self.generations.get(key) == pending.generation

    def start(self, pending):
        if pending is None or self.pending.get(self.key(pending.device, pending.kind, pending.instance)) is not pending:
            return
        pending.task = self.coordinator.create_background(self._confirm(pending), "Govee command confirmation")

    def discard(self, pending):
        if pending is None:
            return
        key = self.key(pending.device, pending.kind, pending.instance)
        if self.pending.get(key) is pending:
            self.pending.pop(key)
        if pending.task and pending.task is not asyncio.current_task():
            pending.task.cancel()

    def on_report(self, device, states):
        for key, pending in list(self.pending.items()):
            if key[0] != device.device_id or (pending.kind, pending.instance) not in states:
                continue
            pending.reported = states[(pending.kind, pending.instance)]
            pending.has_report = True
            pending.event.set()
            if matches(pending.kind, pending.instance, pending.reported, pending.value):
                self.pending.pop(key)
                self.coordinator.confirmed_commands += 1
                device.last_command_result = "confirmed"
                if pending.task:
                    pending.task.cancel()

    async def _wait(self, pending, timeout):
        try:
            await asyncio.wait_for(pending.event.wait(), timeout)
        except TimeoutError:
            pass

    async def _confirm(self, pending):
        try:
            await self._wait(pending, QUERY_DELAY)
            if not self.current(pending):
                return
            if not pending.has_report:
                await self.coordinator.async_query_status(device=pending.device)
            pending.event.clear()
            await self._wait(pending, max(0, CONFIRM_WINDOW - QUERY_DELAY))
            if not self.current(pending):
                return
            self.coordinator.unconfirmed_commands += 1
            pending.device.last_command_result = "unconfirmed"
            if not pending.has_report:
                self.coordinator.mark_mqtt_unresponsive(pending.device)
            elif (pending.previous is not None
                  and matches(pending.kind, pending.instance, pending.reported, pending.previous)
                  and not (pending.kind == ON_OFF and pending.value == 1)):
                # A contradictory report is evidence to retry once. Silence is
                # not: never resend a heater/appliance command or a power-on.
                await self.coordinator.async_retry_confirmed_miss(pending)
            self.coordinator.async_set_updated_data(dict(self.coordinator.devices))
        finally:
            self.discard(pending)

    def close(self):
        for pending in list(self.pending.values()):
            self.discard(pending)
