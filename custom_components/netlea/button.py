"""Button platform: force a status query."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
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
        lambda device: [NetleaRefreshButton(coordinator, device)],
    )


class NetleaRefreshButton(NetleaEntity, ButtonEntity):
    _attr_name = "查询状态"
    _attr_icon = "mdi:refresh"

    def __init__(self, coordinator: NetleaCoordinator, device) -> None:
        super().__init__(coordinator, device, "refresh")

    async def async_press(self) -> None:
        await self.coordinator._async_query_devices()  # noqa: SLF001 - internal on purpose
        await self.coordinator.async_request_refresh()
