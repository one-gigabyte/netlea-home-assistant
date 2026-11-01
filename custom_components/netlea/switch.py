"""Switch platform: lamp / UV power."""

from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, PRODUCT_LAMP, PRODUCT_UV
from .coordinator import NetleaCoordinator
from .entity import NetleaEntity, async_setup_dynamic_entities

_LOGGER = logging.getLogger(__name__)

SUPPORTED = {PRODUCT_LAMP: "总开关", PRODUCT_UV: "UV 开关"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: NetleaCoordinator = hass.data[DOMAIN][entry.entry_id]

    def _build(device):
        if device.product_id in SUPPORTED:
            return [NetleaPowerSwitch(coordinator, device, SUPPORTED[device.product_id])]
        return []

    async_setup_dynamic_entities(coordinator, async_add_entities, _build)


class NetleaPowerSwitch(NetleaEntity, SwitchEntity):
    """Power switch for a lamp or the UV steriliser."""

    def __init__(self, coordinator: NetleaCoordinator, device, name: str) -> None:
        super().__init__(coordinator, device, "power")
        self._attr_name = name

    @property
    def is_on(self) -> bool | None:
        return self.device.is_on

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_set_power(self.device, True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_set_power(self.device, False)
