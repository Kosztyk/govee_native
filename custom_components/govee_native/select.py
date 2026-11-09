"""Capability enums, music presets and discovered dynamic scenes."""

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.restore_state import RestoreEntity
from .const import DOMAIN, SCENE, DIY_COLOR, MODE, MUSIC, WORK_MODE
from .entity import GoveeEntity, readable
from .runtime_store import restore_setting


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities = []
    for device in coordinator.devices.values():
        for kind, instance in device.capabilities:
            if kind in (MODE, MUSIC, SCENE, DIY_COLOR):
                options = device.options(kind, instance)
                if options and all("value" in opt and "name" in opt for opt in options):
                    entities.append(GoveeSelect(coordinator, device, kind, instance))
        if device.has(WORK_MODE, "workMode"):
            work = GoveeWorkModeSelect(coordinator, device)
            if work.options:
                entities.append(work)
        if device.kind == "light" and (coordinator.api is not None or device.scene_labels):
            entities.append(GoveeSceneSelect(coordinator, device))
    async_add_entities(entities)


class GoveeSelect(GoveeEntity, RestoreEntity, SelectEntity):
    def __init__(self, coordinator, device, kind, instance):
        super().__init__(coordinator, device, f"select_{kind}_{instance}")
        self.kind, self.instance = kind, instance
        self._attr_name = readable(instance)

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        if self.device.state(self.kind, self.instance) is None:
            # Migrate existing installations which predate the settings store.
            previous = await self.async_get_last_state()
            if previous:
                match = next((opt for opt in self.device.options(self.kind, self.instance)
                              if str(opt.get("name")) == previous.state), None)
                if match and restore_setting(self.device, self.kind, self.instance, match["value"]):
                    self.coordinator.save_runtime_later()

    @property
    def options(self):
        return list(dict.fromkeys(str(opt["name"]) for opt in self.device.options(self.kind, self.instance)))

    @property
    def current_option(self):
        value = self.device.state(self.kind, self.instance)
        return next((str(opt["name"]) for opt in self.device.options(self.kind, self.instance) if opt.get("value") == value), None)

    async def async_select_option(self, option):
        match = next((opt for opt in self.device.options(self.kind, self.instance) if str(opt.get("name")) == option), None)
        if match is None:
            raise ValueError(f"Unknown option: {option}")
        await self.async_write(self.kind, self.instance, match["value"])


class GoveeSceneSelect(GoveeEntity, RestoreEntity, SelectEntity):
    _attr_name = "Scene and DIY"

    def __init__(self, coordinator, device):
        super().__init__(coordinator, device, "scene")

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        if "selected_scene" not in self.device.local_settings:
            previous = await self.async_get_last_state()
            if previous and previous.state in self.options:
                self.device.local_settings["selected_scene"] = previous.state
                self.coordinator.save_runtime_later()
        # APK Tap-to-Run selectors are populated before setup. OpenAPI scenes
        # may still need a separate fetch after entity creation.
        if self.coordinator.api:
            self.hass.async_create_task(self.coordinator.async_load_scenes(self.device))

    @property
    def options(self):
        return sorted(self.device.scene_labels)

    @property
    def current_option(self):
        # Govee often cannot report the active scene. Retain the last choice
        # instead of returning unknown after every successful activation.
        selected = self.device.local_settings.get("selected_scene")
        return selected if selected in self.device.scene_labels else None

    @property
    def assumed_state(self):
        return self.current_option is not None

    @property
    def extra_state_attributes(self):
        return {**super().extra_state_attributes, "selection_source": "last_selected",
                "state_assumed": self.assumed_state}

    async def async_select_option(self, option):
        scene = self.device.scene_labels.get(option)
        if scene is None:
            raise ValueError("Scene not discovered; run govee_native.refresh_scenes")
        if scene.get("source") == "one_click":
            await self.coordinator.async_activate_one_click(scene["shortcut_id"])
        else:
            await self.async_write(scene["type"], scene["instance"], scene["value"])
        self.device.local_settings["selected_scene"] = option
        self.coordinator.save_runtime_later()
        self.coordinator.async_set_updated_data(dict(self.coordinator.devices))


class GoveeWorkModeSelect(GoveeEntity, RestoreEntity, SelectEntity):
    """Structured workMode selector built from advertised workMode/modeValue fields."""

    _attr_name = "Work mode"

    def __init__(self, coordinator, device):
        super().__init__(coordinator, device, "select_workMode")
        cap = device.capability(WORK_MODE, "workMode") or {}
        fields = cap.get("parameters", {}).get("fields", [])
        primary = next((f for f in fields if f.get("fieldName") == "workMode"), {})
        secondary = next((f for f in fields if f.get("fieldName") == "modeValue"), {})
        self._choices = {}
        for mode in primary.get("options", []):
            if "value" not in mode:
                continue
            name = str(mode.get("name", mode["value"]))
            sub = next((o for o in secondary.get("options", []) if o.get("name") == mode.get("name")), None)
            if not sub and secondary.get("required", False):
                continue
            if sub and sub.get("options"):
                for variant in sub["options"]:
                    if "value" not in variant:
                        continue
                    label = f"{name}: {variant.get('name', variant['value'])}"
                    self._choices[label] = {"workMode": mode["value"], "modeValue": variant["value"]}
            elif sub and "defaultValue" in sub:
                self._choices[name] = {"workMode": mode["value"], "modeValue": sub["defaultValue"]}
            elif not secondary.get("required", False):
                self._choices[name] = {"workMode": mode["value"]}

    @property
    def options(self):
        return list(self._choices)

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        if self.device.state(WORK_MODE, "workMode") is None:
            previous = await self.async_get_last_state()
            if previous and previous.state in self._choices and restore_setting(
                    self.device, WORK_MODE, "workMode", self._choices[previous.state]):
                self.coordinator.save_runtime_later()

    @property
    def current_option(self):
        current = self.device.state(WORK_MODE, "workMode")
        if not isinstance(current, dict):
            return None
        return next((label for label, value in self._choices.items() if value == current), None)

    async def async_select_option(self, option):
        if option not in self._choices:
            raise ValueError("Unknown work mode")
        await self.async_write(WORK_MODE, "workMode", self._choices[option])
