# 尼特利（Netlea）云端通信协议说明

> 适用客户端：尼特利 App v3.196（Android）
> 通信层：HTTPS + WebSocket，JSON 控制指令承载十六进制设备帧
> 所有结论均已用实机（SG-X-PRO-80w）逐帧验证；标注「推断」的条目尚待确认。

---

## 1. 应用结构

官方 App 采用跨端框架（uni-app）实现，业务逻辑以 JS 形式载入，原生层仅作运行时容器。
各业务模块按 `core/net/...` 这样的路径组织（日志里的 `at core/net/websocket/SkyClient.js:53`
之类），因此可以还原出模块划分：

| 源码文件 | 作用 |
| --- | --- |
| `core/net/http/NetRequest.js` | REST 封装，统一 200/401/409/500 处理 |
| `core/net/websocket/SkyWebSocket.js` | 原生 WebSocket 封装，心跳 60 s、自动重连 |
| `core/net/websocket/SkyClient.js` | 业务协议：`msgType` 1001/1002/2001 |
| `core/net/factory/sendFactory.js` | 帧 → 云端的发布（`topic: App/Control/<realAddress>`） |
| `core/net/utils/SkyRequestManager.js` | 按 `seq` 匹配设备应答、超时重试 |
| `core/net/ble/*` | BLE 直连（Service `0003CBBB-…`）、OTA |
| `api/ble/*Device.js` | 每种设备（灯、喂食器、加热棒、UV…）的指令构造 |
| `api/ble/BaseDeviceDecoder.js` | 按 `deviceType + cmd` 分发的状态解码器 |
| `core/utils/devicesUtils.js` | 设备列表组装、`statusLog` 解析 |
| `plugins/DeviceBus.js` | 把 WS 帧解码后广播给页面 |

家族常量（`$constants`）：

| `equipmentProductId` | 含义 | 帧内 `deviceType` |
| --- | --- | --- |
| 1 | 水草灯 / 灯具 | 1 |
| 2 | 水泵 | 2 |
| 3 | 气泵 | 3 |
| 4 | 过滤桶 | 4 |
| 5 | 喂食器 | 5 |
| 6 | 智能鱼缸 / 泵滤桶 | 6 |
| 7 | UV 杀菌灯 | 7 |
| 8 | 加热棒 | 8 |

---

## 2. 两套通道

### 2.1 HTTPS（登录 / 设备清单）

* Base：`https://v3api.netlea.com/prod-api`
* 鉴权：`Authorization: <token>`（**没有** `Bearer` 前缀）
* 响应：`{"code":200, "msg":..., "token":..., "rows":[...], "data":{...}}`
  * `code` 200 成功；401/409 = 会话过期；500 业务错误
  * 注意：部分接口把数据平铺在顶层（`rows`/`token`），部分是 `data` 包装，两种都存在

关键接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| **GET** | `/V3/Api/captchaMsg` | `?captchaMsgFlag=3&phonenumber=` → `{uuid}`，发短信（POST 会返回 500 `Request method 'POST' not supported`） |
| POST | `/V3/Api/appuser/loginBycode` | `{code, username, uuid, appVer}` → `{token, userId, userName, ...}` |
| POST | `/V3/Api/uniapp/login` | 运营商一键登录（`accessToken`+`openId`） |
| GET | `/V3/Api/getWifiCollabUserDeviceList` | `?pageNum&pageSize&userId` → `rows[]`，**含 `statusLog` 快照** |
| GET | `/V3/Api/getDeviceInfoByRealAddress` | `?realAddress=` → `data` |
| GET | `/V3/Api/getSysConfig` | 免鉴权，返回 MQTT broker 信息 |
| POST | `/V3/Api/batchSyncEquipmentUser` | App 本地设备表与云端同步 |

### 2.1.1 实测（2026-11）

```
GET /V3/Api/getSysConfig
→ {"msg":"操作成功","code":200,"mqttServerIp":"27.151.28.153",
   "mqttServerPort":1883,"mqttServerDomain":"mqtt.netlea.com"}

GET /V3/Api/getWifiCollabUserDeviceList?pageNum=1&pageSize=10   （无 token）
→ {"msg":"token认证失败","code":401}

POST /V3/Api/appuser/loginBycode {"code":"000000","username":"","uuid":""}
→ {"msg":"uuid不能为空","code":500}
```

由此确认：响应是**扁平 JSON**、错误码在顶层 `code`、`getSysConfig` 不需要鉴权、
设备实际挂在 `mqtt.netlea.com:1883`（设备侧是裸 MQTT，App 侧才走 WebSocket 桥）。
未做任何未授权探测（例如匿名连该 broker），仅记录事实。

> 设备清单没有独立的"我的设备"接口：App 把设备存在本地
> `USER_DEVICE_DATA_`，登录后靠 `batchSyncEquipmentUser` 和云端对账。
>
> **正确姿势**（`ln()`，App 首页）：
>
> ```
> POST /V3/Api/batchSyncEquipmentUser
> {"userId": <userId>, "client": 0, "equipmentUserEntityList": []}
> → {"code":200, "rows":[{realAddress, mac, name, statusLog, equipmentType, …}]}
> ```
>
> 传空列表就等于"把我这边的空表跟云端对一次"，返回的是**账号全量设备**，
> 每条都带 `statusLog` 快照。这是新设备（新装 App/第三方客户端）重建清单的唯一入口。
>
> `getWifiCollabUserDeviceList` 是「联动触发设备选择器」的接口，
> 对部分账号会抛 500（`{"msg":"{upload.image.exception}","code":500}`），
> 不能当主路径用。

### 2.2 WebSocket（设备控制 / 状态上报）

* URL：`ws://v3grpc.netlea.com:8088/websocket`
* 握手头：`token: <token>`、`contentType: application/json`
* 心跳：每 60 s 发 `{"msgType":"ping"}`，服务端回 `pong`（客户端会过滤掉）
* 消息体统一 `{"msgType": "...", "msgBody": ...}`

| `msgType` | 方向 | 含义 |
| --- | --- | --- |
| `1001` | 上行 | 请求在线设备列表 |
| `1001` | 下行 | `[{realAddress, runState}]`，`runState=1` 在线 |
| `1002` | 上行 | 下发设备指令（见下） |
| `1002` | 下行 | 设备应答 / 主动上报，`code` 200 成功 |
| `2001` | 下行 | 设备在线状态变化 |

下发指令：

```json
{
  "msgType": "1002",
  "msgBody": {
    "time": 1762345678901,
    "all": "1",
    "topic": "App/Control/D4438A6633CF",
    "message": "5a08011b0000007e"
  }
}
```

* `topic` = `App/Control/<realAddress>`，底层是厂商 MQTT 桥
* `message` = 设备帧的 **小写十六进制字符串**（`ol()`：每字节 `toString(16)` 补足两位）

设备上报 / 应答：

```json
{
  "code": 200,
  "msgType": "1002",
  "msgBody": { "realAddress": "D4438A6633CF", "message": "<hex>" }
}
```

---

## 3. 设备帧格式

同一套帧在 BLE 与云端复用，由 `statusLog.protocolType` 决定用哪种（默认 90）。

### 3.1 协议 0x5A（90）—— 无序号

```
off 0   0x5A            魔数
off 1   LEN             整帧长度（含校验）
off 2   DEVICE_TYPE     设备类型
off 3   CMD             指令号
off 4   BYTE4           下行：cmdType（0=普通，0x10=设置态）
                        上行：(request_state << 4) | code
off 5   BYTE5           flash_wifi 标志（0x5A 构造器保留）
off 6   ...BODY...
off n   CHECKSUM        前面所有字节之和 & 0xFF
```

### 3.2 协议 0x6A（106）—— 带序号

```
off 0   0x6A
off 1   LEN
off 2   DEVICE_TYPE
off 3   0x07            扩展头长度
off 4-5 SEQ             2 字节大端，每设备递增，用于应答匹配
off 6   CMD
off 7   BYTE7           同 0x5A 的 byte4
off 8-9 0x00 0x00
off 10  ...BODY...
off n   CHECKSUM
```

### 3.3 字段编码

* 多字节字段 **小端**（App 读：切片 → `reverse()` → `getUint16/32`；写：`hex.padStart` → 反转）
* 校验：`sum(bytes) % 256`（`pn()`），**不是** CRC
* BLE 特征值写入时额外套一层：首字节 = 8 位 CRC（`mn()`），其余字节与首字节异或
  （仅 BLE 直连需要，云端不需要）

### 3.4 状态位

`request_state`（byte4 高半字节）：`0` 查询应答、`1` 设置应答、`2` 主动上报、`4` 恢复设置应答
`code`（低半字节）：`0` 成功、`1` 失败、`2` 告警、`3` 无效、`7` token 失效

---

## 4. 灯具（deviceType 1）

构造器 `ml(1, CMD, body)`：4 字节 `on/off` 是**渐变时间**（不是位掩码），
`on=60/off=60` 是 App 里的默认值。

| CMD | 用途 | BODY |
| --- | --- | --- |
| 1 | 初始化（带 deviceIds/token） | 变长 |
| 4 | 时间同步 | `[0,0,tz,h,m,s,(y-2000,mon,day 由 mutator 写入)]` |
| 5 | 复位 / init | 空（mutator 写 byte5） |
| 6 | 写 token | `type, token(4), head` |
| **16** | **主灯设置（含开关）** | `lampState, lampMode, r1, r2, R, W, G, B, F, restoreActTime(2), modeId, grade(2), r3, r4` = **16 字节**（实机验证） |
| 17 | 射灯设置 | `lampState, lampMode, r1, r2, moodLamp, restoreActTime(2? 推断), grade, r3` |
| 18 | 主灯行程（10 段） | `sunMoonDisable, sunTime(4), moonTime(4)` + 10×`[tripDisable,weekCycle,startH,startM,endH,endM,R,G,W,B,F,modeId,grade(2)]` |
| 19 | 氛围灯行程 | 10×`[tripDisable,weekCycle,startH,startM,endH,endM,moodLamp,grade]` |
| 21 | 初始化后续 | `[0]` |
| 22 / 34 | 射灯/氛围灯跟随 | `followType, channel, grade` |
| 23 | 语音设置 | `voiceSwitch, powerPlay, prompt0..8` |
| 26 | 通道开关（带渐变） | `on(2), off(2), R, W, G, B, F, modeId, grade(2)` = 12 字节 |
| **27 / 29 / 33** | **查询 主灯 / 射灯 / 氛围灯** | `[0]` |
| 28 / 32 | 氛围灯/射灯快捷开关 | `on(4), off(4), moodLamp, grade` |
| 30 | 氛围灯设置 | `lampState, lampMode, r1, r2, moodLamp, restoreActTime(4), grade, r3` |
| 31 | 氛围灯行程 | 同 19 |
| 37 | 预留 | `[r]` |

### 状态解码

**CMD 20（三路聚合上报，最重要）**

| 偏移 | 字段 |
| --- | --- |
| 0-5 | `stateType, startH, startM, lampState, lampMode, pattern` |
| 6-10 | `R, W, G, B, F`（实测：38/31/55/34/0） |
| 11-12 | `reserve1, reserve2` |
| 13-21 | 射灯同结构（第 7 字节为 `moodLamp`，第 8 字节 `followType`） |
| 22-30 | 氛围灯同结构 |

**CMD 26**（12 字节，实测）：`on(2), off(2), R, W, G, B, F, modeId, grade(2)`
**CMD 28 / 32**（6 字节，实测）：`on(2), off(2), moodLamp, grade`

> 实测抓到的三帧：
> ```
> cmd=26 3c003c00251f372200000500      on=60 off=60 R=37 W=31 G=55 B=34 F=0 modeId=0 grade=5
> cmd=28 3c003c000004                    on=60 off=60 moodLamp=0 grade=4
> cmd=20 010000010003261f372200...      lampState=1 lampMode=0 pattern=3 R=38 W=31 G=55 B=34
> ```
> cmd 16 的应答是 **cmd 20**（全量三路状态），所以任何一次控制后状态都会立即刷新。
**CMD 18 / 19 / 31**：行程表（10 段）

> **通道顺序是 `R, W, G, B, F`**。依据（实机 + 云端交叉验证）：
>
> 1. 型号配置 `mcustomConfig[0].custom` 里 `custom1_text=R`、`custom2_text=W`、
>    `custom3_text=G`、`custom4_text=B`、`custom5_text=F`（App 的 `Ei()` 就是按这个顺序取值）
> 2. 云端 `statusLog.lastControlHex` 存着 App 上次下发的真帧
>    `5a17 0110 0000 0101 0000 251f3722 00 e803 00 0500 0000 11`，
>    五个通道字节 `25 1f 37 22 00` = R=37 W=31 G=55 B=34
> 3. 这组数字正好等于该型号 `zhudengConfig` 里「水草/正白」第 5 档
>    `{W:31,R:37,G:55,B:34}`，完全吻合
>
> 注意：官方 App 的解码器把 CMD 20/26 的通道标成了 `R,G,W,B,F`，**这个标注是错的**
> （W 和 G 反了），以实机为准。
>
> `grade` 是**光谱档位 1-10**，不是百分比；通道值是 0-101%（`custom1Max=101`）。
>
> `lampMode`（`mode_light_*`）：`0` 手动/临时（配 `restoreActTime` 用），
> `1` 跟随行程（App 首页灯卡在 `state==0` 时发的就是 1），`2` 临时开关。
> **实测**：`lampMode=0` 立刻生效，约 `restoreActTime`（1000 秒）后设备自动回落到行程。
> App 里 `ti()` 的取值逻辑是 `state==0 → 1；state==3/4 → 0；其余 → 1`。
> 想强制手动生效就发 `lampMode=0`。

---

## 5. UV 杀菌灯（deviceType 7）

| CMD | 用途 | BODY |
| --- | --- | --- |
| 34 | 按键/蜂鸣/童锁 | `reserve1, buzzMode, lockMode, reserve2` |
| 48 | 设置（byte4=0x10） | `mode, onOff, state, r1, pwm, lv, r2, r3, r4, r5` |
| 49 | 行程（byte4=0x10 设置 / 0 查询） | `config(2) + 10×[enable,week,startH,startM,endH,endM,channel1,grade,r3,r4]` |
| 50 | 查询状态（byte4=0） | 空 |
| 51 | 快捷设置（byte4=0x10） | `onOff, openMin(2), closeMin(2), pwm, lv, priorityOnOff, second(2)` |

解码：48 → `runMode, onOff, automatic, r1, pwm, lv, restoreActTime(2), r4`；
50 → `controlMode, runMode, onOff, automatic, isAutomaticSet, stateH, stateM, pwm, lv, restoreActTime(2), TProtect`。

---

## 6. 其它家族（本集成暂未做实体映射，帧格式已备查）

* **喂食器 dt=5**：`cmd 17` 手动喂食（`modeSwitch`）、`18` 设置份数、`26/27/28` 指示灯/震动、
  `22/23` 换水/换滤材提醒、`19` 喂食记录
* **智能鱼缸 dt=6**：`129` **总开关**（BODY 就 1 字节 `totalSwitch`）、`32/40/48/56/88` 各通道
  （主灯/泵/造浪）查询与设置、`74` 通道开关、`50` 温度、`58/90` 造浪剩余喂食时间、`131` 告警列表、
  `135` 温度校准、`136` 24 小时温度曲线
* **加热棒 dt=8**：`32` 设置温度、`33` 状态、`36` 定时、`136` 温度曲线
* **水泵/过滤桶 dt=2/4**：定流/波浪/喂食/暂停（`pump_filtr_bucket_*` 常量）

---

## 7. BLE 本地直连（备选方案，未实现）

| 项 | 值 |
| --- | --- |
| Service | `0003CBBB-0000-1000-8000-00805F9BFFF0` |
| 特征值 | `FFF1..FFF9`、`FFE1..FFE5`、`DDD1..DDD6`、`EEE1..EEEE` |

* 写特征值前要套 CRC+XOR 壳（`tl(buf, true)`）
* 设备靠广播名/MAC 识别，`mac` 可能是逗号分隔的多台（一个"设备"由多个灯体组成）
* 想不依赖云端，需要把 `BaseDeviceDecoder` 那套策略整体移植到 ESPHome/HA 蓝牙代理上

---

## 8. 尚未确认的点

1. `statusLog` 里 `controlType=1` 表示 WiFi 控制；BLE 设备是 0。部分老设备 `protocolType=106`。
2. CMD 16 的通道顺序（见第 4 节警告）需要实机验证。
3. 云端 `getWifiCollabUserDeviceList` 是否对所有账号都返回全量设备（本集成已做空列表兜底：
   拉不到设备时不会有实体，可在日志里看到 `No devices`）。
4. `grade` 语义是每光谱通道的 PWM 百分比（0-100），由 App 的 `li()` 推出，未在实机验证上限。
