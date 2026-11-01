"""Binary sensor platform: online state."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import NetleaCoordinator
from .entity import NetleaEntity, async_setup_dynamic_entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: NetleaCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_setup_dynamic_entities(
        coordinator,
        async_add_entities,
        lambda device: [NetleaOnlineSensor(coordinator, device)],
    )


class NetleaOnlineSensor(NetleaEntity, BinarySensorEntity):
    _attr_name = "在线"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: NetleaCoordinator, device) -> None:
        super().__init__(coordinator, device, "online")

    @property
    def is_on(self) -> bool:
        return self.device.online
