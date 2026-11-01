"""Shared entity base for the Netlea integration."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import NetleaCoordinator, NetleaDevice


def async_setup_dynamic_entities(
    coordinator: NetleaCoordinator,
    async_add_entities: AddEntitiesCallback,
    factory: Callable[[NetleaDevice], list],
) -> None:
    """Create entities now and again whenever a new device shows up.

    The cloud inventory can be empty at setup time (server hiccup or a brand
    new account) and fill in on a later poll, so entity creation cannot be a
    one-off snapshot.
    """
    known: set[str] = set()

    def _sync() -> None:
        new_entities: list = []
        for address, device in coordinator.devices.items():
            if address in known:
                continue
            known.add(address)
            new_entities.extend(factory(device) or [])
        if new_entities:
            async_add_entities(new_entities)

    _sync()
    coordinator.async_add_device_listener(_sync)


class NetleaEntity(CoordinatorEntity[NetleaCoordinator]):
    """Base class binding an entity to one cloud device record."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: NetleaCoordinator,
        device: NetleaDevice,
        key: str,
    ) -> None:
        super().__init__(coordinator)
        self.device = device
        self._attr_unique_id = f"{device.real_address}_{key}"

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.device.real_address)},
            name=self.device.name,
            manufacturer=MANUFACTURER,
            model=self.device.product_name,
            sw_version=self.device.firmware,
            serial_number=self.device.real_address,
        )

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "real_address": self.device.real_address,
            "product": self.device.product_name,
            "protocol": f"0x{self.device.protocol:02X}",
            "last_frame": self.device.last_frame,
        }
