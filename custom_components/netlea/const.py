"""Constants for the Netlea (尼特利) integration."""

from __future__ import annotations

DOMAIN = "netlea"
MANUFACTURER = "Netlea"
CONF_TOKEN = "token"
CONF_PHONE = "phone"
CONF_CODE = "code"
CONF_UUID = "uuid"
CONF_USER_ID = "user_id"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_LAMP_MODE = "lamp_mode"

# --------------------------------------------------------------------------- #
# cloud endpoints (observed on the app's network traffic)
# --------------------------------------------------------------------------- #
API_BASE = "https://v3api.netlea.com/prod-api"
IMG_BASE = "https://v3img.netlea.com/prod-api"

EP_CAPTCHA = "/V3/Api/captchaMsg"
EP_LOGIN_BY_CODE = "/V3/Api/appuser/loginBycode"
EP_LOGIN_UNIAPP = "/V3/Api/uniapp/login"
EP_SYS_CONFIG = "/V3/Api/getSysConfig"
EP_DEVICE_LIST = "/V3/Api/getWifiCollabUserDeviceList"
EP_DEVICE_INFO = "/V3/Api/getDeviceInfoByRealAddress"
EP_SYNC_DEVICES = "/V3/Api/batchSyncEquipmentUser"

WS_URL = "ws://v3grpc.netlea.com:8088/websocket"

# websocket message types (core/net/websocket/SkyClient.js)
MSG_ONLINE_LIST = "1001"
MSG_CONTROL = "1002"
MSG_STATUS_CHANGE = "2001"
MSG_PING = "ping"

WS_TOPIC_PREFIX = "App/Control/"
WS_PING_INTERVAL = 45  # app uses 60s; a bit faster keeps NAT alive

DEFAULT_SCAN_INTERVAL = 30
MIN_SCAN_INTERVAL = 10

# --------------------------------------------------------------------------- #
# device families (equipmentProductId, see the app's `$constants`)
# --------------------------------------------------------------------------- #
PRODUCT_LAMP = 1
PRODUCT_PUMP = 2
PRODUCT_AIR_PUMP = 3
PRODUCT_FILTER_BUCKET = 4
PRODUCT_FEEDER = 5
PRODUCT_PUMP_FILTER = 6
PRODUCT_SMART_TANK = 6
PRODUCT_UV = 7
PRODUCT_HEATER = 8

PRODUCT_NAMES = {
    PRODUCT_LAMP: "水草灯",
    PRODUCT_PUMP: "水泵",
    PRODUCT_AIR_PUMP: "气泵",
    PRODUCT_FILTER_BUCKET: "过滤桶",
    PRODUCT_FEEDER: "喂食器",
    PRODUCT_SMART_TANK: "智能鱼缸",
    PRODUCT_UV: "UV 杀菌灯",
    PRODUCT_HEATER: "加热棒",
}

# protocol device type used inside the frame header
DEVTYPE_LAMP = 1
DEVTYPE_UV = 7

# lampMode used when Home Assistant drives the lamp (see protocol.LAMP_MODE_*):
# 1 = 跟随行程 (same as the app home card), 0 = 手动/临时 (always applies,
# paired with restoreActTime), 2 = 临时开关.
DEFAULT_LAMP_MODE = "auto"
# (value, label) — values are strings because Home Assistant selectors only
# accept strings; the coordinator converts them back to int where needed.
LAMP_MODE_OPTIONS: list[tuple[str, str]] = [
    ("auto", "自动：沿用设备当前模式（推荐，不改动行程）"),
    ("1", "跟随行程 lampMode=1：和 App 首页灯卡一致"),
    ("0", "手动/临时 lampMode=0：立即生效，约 1000 秒后回落行程"),
    ("2", "临时开关 lampMode=2"),
]
