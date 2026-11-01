"""Sensor platform: diagnostics and decoded lamp channels."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PRODUCT_LAMP
from .coordinator import NetleaCoordinator
from .entity import NetleaEntity, async_setup_dynamic_entities

CHANNELS = ("R", "G", "W", "B", "F")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: NetleaCoordinator = hass.data[DOMAIN][entry.entry_id]

    def _build(device):
        ents: list[SensorEntity] = [
            NetleaFirmwareSensor(coordinator, device),
            NetleaLastFrameSensor(coordinator, device),
        ]
        if device.product_id == PRODUCT_LAMP:
            ents.append(NetleaLampStateSensor(coordinator, device))
        return ents

    async_setup_dynamic_entities(coordinator, async_add_entities, _build)


class NetleaFirmwareSensor(NetleaEntity, SensorEntity):
    _attr_name = "固件版本"
    _attr_icon = "mdi:chip"

    def __init__(self, coordinator: NetleaCoordinator, device) -> None:
        super().__init__(coordinator, device, "firmware")

    @property
    def native_value(self) -> str | None:
        return self.device.firmware or self.device.raw_status_log.get("firmwareVersion")


class NetleaLastFrameSensor(NetleaEntity, SensorEntity):
    """Raw hex of the most recent frame — handy while validating commands."""

    _attr_name = "最近帧"
    _attr_icon = "mdi:code-json"

    def __init__(self, coordinator: NetleaCoordinator, device) -> None:
        super().__init__(coordinator, device, "last_frame")

    @property
    def native_value(self) -> str | None:
        return self.device.last_frame


class NetleaLampStateSensor(NetleaEntity, SensorEntity):
    """Human readable summary of the three lamp channels."""

    _attr_name = "灯组状态"
    _attr_icon = "mdi:lightbulb-group"

    def __init__(self, coordinator: NetleaCoordinator, device) -> None:
        super().__init__(coordinator, device, "lamp_state")

    @property
    def native_value(self) -> str | None:
        merged = self.device.merged_state()
        if not merged:
            return None
        parts = []
        for label, key in (("主灯", "lampState"), ("射灯", "spot_lampState"), ("氛围灯", "atm_lampState")):
            if key in merged:
                parts.append(f"{label}{'开' if merged[key] else '关'}")
        return " ".join(parts) or None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        attrs = dict(super().extra_state_attributes)
        merged = self.device.merged_state()
        attrs["channels"] = {key: merged.get(key) for key in CHANNELS if key in merged}
        attrs["lampMode"] = merged.get("lampMode")
        attrs["grade"] = merged.get("grade")
        attrs["decoded"] = {str(cmd): payload for cmd, payload in self.device.state.items()}
        return attrs
