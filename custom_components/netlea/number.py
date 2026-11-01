"""Number platform: WRGB channel levels, brightness / grade and UV power."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, PRODUCT_LAMP, PRODUCT_UV
from .coordinator import NetleaCoordinator
from .entity import NetleaEntity, async_setup_dynamic_entities
from .protocol import CHANNEL_MAX, CHANNEL_ORDER, GRADE_MAX

MODE = NumberMode.SLIDER

CHANNEL_LABELS = {
    "R": ("红色通道", "mdi:alpha-r-circle"),
    "G": ("绿色通道", "mdi:alpha-g-circle"),
    "W": ("白色通道", "mdi:alpha-w-circle"),
    "B": ("蓝色通道", "mdi:alpha-b-circle"),
    "F": ("F 通道", "mdi:alpha-f-circle"),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: NetleaCoordinator = hass.data[DOMAIN][entry.entry_id]

    def _build(device):
        if device.product_id == PRODUCT_LAMP:
            entities: list[NumberEntity] = [NetleaGradeNumber(coordinator, device)]
            entities.extend(
                NetleaChannelNumber(coordinator, device, key) for key in CHANNEL_ORDER
            )
            return entities
        if device.product_id == PRODUCT_UV:
            return [NetleaUvPwmNumber(coordinator, device)]
        return []

    async_setup_dynamic_entities(coordinator, async_add_entities, _build)


class NetleaGradeNumber(NetleaEntity, NumberEntity):
    """Spectrum preset index (`grade`, 档位 1-10) — not a percentage."""

    _attr_name = "光谱档位"
    _attr_icon = "mdi:stairs-up"
    _attr_native_min_value = 0
    _attr_native_max_value = GRADE_MAX
    _attr_native_step = 1
    _attr_mode = MODE

    def __init__(self, coordinator: NetleaCoordinator, device) -> None:
        super().__init__(coordinator, device, "grade")

    @property
    def native_value(self) -> float | None:
        value = self.device.grade
        return None if value is None else float(value)

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_brightness(self.device, int(value))


class NetleaChannelNumber(NetleaEntity, NumberEntity):
    """One spectrum channel (R / G / W / B / F) of the lamp, 0-100 %."""

    _attr_native_min_value = 0
    _attr_native_max_value = CHANNEL_MAX
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "%"
    _attr_mode = MODE

    def __init__(self, coordinator: NetleaCoordinator, device, key: str) -> None:
        super().__init__(coordinator, device, f"channel_{key.lower()}")
        self._key = key
        label, icon = CHANNEL_LABELS.get(key, (f"{key} 通道", "mdi:tune"))
        self._attr_name = label
        self._attr_icon = icon

    @property
    def native_value(self) -> float | None:
        value = self.device.channel_map().get(self._key)
        return None if value is None else float(value)

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_channel(self.device, self._key, int(value))


class NetleaUvPwmNumber(NetleaEntity, NumberEntity):
    """UV lamp PWM duty (0-255)."""

    _attr_name = "功率"
    _attr_native_min_value = 0
    _attr_native_max_value = 255
    _attr_native_step = 1
    _attr_mode = MODE

    def __init__(self, coordinator: NetleaCoordinator, device) -> None:
        super().__init__(coordinator, device, "pwm")

    @property
    def native_value(self) -> float | None:
        value = self.device.merged_state().get("pwm")
        return None if value is None else float(value)

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_brightness(self.device, int(value))
