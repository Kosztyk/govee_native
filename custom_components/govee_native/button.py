"""Device status and catalog refresh actions in the device page."""
from homeassistant.components.button import ButtonEntity, ButtonDeviceClass
from homeassistant.helpers.entity import EntityCategory
from .const import DOMAIN
from .entity import GoveeEntity


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(GoveeRefreshButton(coordinator, device, catalog)
        for device in coordinator.devices.values() for catalog in (False, True)
        if not catalog or device.kind == "light")


class GoveeRefreshButton(GoveeEntity, ButtonEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = ButtonDeviceClass.UPDATE

    def __init__(self, coordinator, device, catalog):
        super().__init__(coordinator, device, "refresh_catalog" if catalog else "refresh_status")
        self.catalog = catalog
        self._attr_name = "Refresh scenes" if catalog else "Request status"

    @property
    def available(self):
        return not self.coordinator._closed

    async def async_press(self):
        if self.catalog:
            await self.coordinator.async_load_scenes(self.device, refresh=True)
        else:
            if self.coordinator.lan:self.coordinator.lan.query_status()
            await self.coordinator.async_query_status(force=True)
            await self.coordinator.async_request_refresh()
