# Netlea 尼特利水族 · Home Assistant 集成

把尼特利（Netlea）App 里的水族设备接进 Home Assistant。协议基于对 App 与云端
通信的分析，细节见 [docs/netlea-protocol.md](docs/netlea-protocol.md)。

> **⚠️ 免责声明**
>
> 本项目为个人开发的**非官方**项目，与尼特利（Netlea）品牌方及其关联公司无任何隶属、
> 合作、赞助或授权关系。**仅供技术交流与学习使用**，不提供任何商业服务与技术支持。
> 集成完全在本地运行，**不收集、不上传任何用户信息**。请在使用前自行评估风险，
> 完整条款见文末[免责声明](#免责声明)。

当前版本覆盖 **灯具（水草灯，deviceType 1）** 与 **UV 杀菌灯（deviceType 7）**，
两者都走厂商云端（HTTPS + WebSocket），不需要 root、不需要额外硬件、不依赖手机蓝牙。

## 能力

| 实体 | 灯具 | UV 灯 |
| --- | --- | --- |
| `light` WRGB 灯光（RGBW 取色 + 亮度） | ✅ cmd 16 | — |
| `number` R / W / G / B / F 五路通道（0-100%） | ✅ | — |
| `switch` 总开关 / UV 开关 | ✅ cmd 16 | ✅ cmd 48 |
| `number` 光谱档位（grade 1-10） | ✅ | — |
| `number` 功率（pwm 0-255） | — | ✅ |
| `sensor` 灯组状态 / 固件 / 最近帧 | ✅ | ✅ |
| `binary_sensor` 在线 | ✅ | ✅ |
| `button` 立即查询 | ✅ | ✅ |
| 服务 `netlea.send_frame` | ✅ 任意帧 | ✅ |

未映射的家族（喂食器、智能鱼缸、加热棒、水泵）在协议文档里有完整命令表，
用 `netlea.send_frame` 可以直接下发。

## 安装

```bash
# 1. 拷贝到 HA 配置目录
cp -r netlea_ha/custom_components/netlea <HA配置>/custom_components/

# 2. 重启 Home Assistant
# 3. 设置 → 设备与服务 → 添加集成 → 搜 "Netlea"
```

无第三方依赖（只用 HA 自带的 aiohttp）。

## 配置

1. 输入注册尼特利 App 的**手机号**，点提交 → 会收到短信验证码
2. 填入 6 位验证码 → 完成

也可以直接粘贴已有的 `userToken`（可从 App 本地存储中取得），跳过短信。

> 登录接口只支持短信验证码和运营商一键登录，**没有密码登录**，
> 所以首次配置必须走一次短信。

## 服务

```yaml
# 直接下发完整帧（十六进制）
service: netlea.send_frame
data:
  real_address: D4438A6633CF
  frame: "5a1b01100000010100003c32281e14e8030000006400000000009f"

# 或者只给命令，让集成拼帧
service: netlea.send_frame
data:
  real_address: D4438A6633CF
  device_type: 1
  cmd: 27
  body: "00"

# 只算不发，方便先在开发者工具里核对
service: netlea.build_frame
data:
  device_type: 1
  cmd: 16
  body: "010100003c32281e14e803000000640000000000"
```

## 排障

### `Netlea setup failed: {upload.image.exception}`（v0.1.0 会出现，v0.1.1 已修）

`{upload.image.exception}` 是服务端 i18n 没填的占位符，来自
`GET /V3/Api/getWifiCollabUserDeviceList` ——这个接口是「联动设备选择器」用的，
部分账号（或有某些型号设备时）会直接抛 500。

v0.1.1 起改成 App 首页真正用的那条路：

```
POST /V3/Api/batchSyncEquipmentUser
{"userId": <userId>, "client": 0, "equipmentUserEntityList": []}
```

返回的 `rows` 就是账号下全部设备（含 `statusLog` 快照）。同时：

* 首次同步失败**不再阻断**集成加载，WS 和轮询继续跑，实体等设备出现后自动创建
* 登录时会把 `userId` 存进配置项；老的配置项在下次成功同步后自动补上
* 仍拿不到设备时日志会给出明确提示，不再只有一行英文异常

### 升级到 v0.1.1 后仍看不到实体

1. 设置 → 设备与服务 → Netlea → 配置，把 `userId` 填上（可在 App 内或下面探针里查看）
2. 或者删掉集成重新添加一次（走短信登录会自动带上 userId）
3. 用探针直接看服务端到底返回了什么：

```bash
python3 tools/netlea_probe.py devices
# 有 userId 时：
python3 tools/netlea_probe.py devices 123456
```

## 调试与验证

先用命令行探针确认账号能登、设备地址对不对（只依赖标准库）：

```bash
python3 tools/netlea_probe.py config          # 免鉴权的 getSysConfig，验证网络与接口
python3 tools/netlea_probe.py login           # 手机号 + 短信验证码，token 存到 ~/.netlea_token
python3 tools/netlea_probe.py devices         # 打印设备清单与 statusLog
python3 tools/netlea_probe.py frame 1 27 00   # 构造查询帧
```

HA 侧打开 debug 日志：

```yaml
logger:
  logs:
    custom_components.netlea: debug
```

日志里能看到每一帧的收发（`-> <addr> <hex>` 与 `cmd=.. state=.. code=.. body=..`）。
实体的「最近帧」传感器保存最后一次原始帧，方便对照。

## 已实测确认的事实

* `getSysConfig` 免鉴权返回 `mqttServerDomain: mqtt.netlea.com:1883`
* 未带 token 请求设备清单 → `{"msg":"token认证失败","code":401}`
* `captchaMsg` 只接受 **GET**，POST 返回 500
* 登录后 `App/Control/<realAddress>` 的十六进制帧格式与协议分析结论一致（见协议文档）

## 已知限制

* 依赖厂商云，断网/服务器故障时不可用
* 通道顺序 `R,W,G,B,F` 与 16 字节帧结构已用真机（SG-X-PRO-80w）+ 云端 `lastControlHex` 双向验证，
  重建出来的帧与 App 下发的帧逐字节一致
* `grade` 是光谱档位 1-10（不是百分比）；通道值是 0-101%
* `lampMode` 决定下发的通道值是否立即生效：集成「配置」里可选「跟随行程（默认）」或「手动（临时）」。
  实测 `lampMode=0` 会立刻生效，但约 1000 秒后设备自动回到行程（`restoreActTime`）
* 设备清单来自 `POST /V3/Api/batchSyncEquipmentUser`，需要 `userId`；缺失时集成会尝试匿名调用并自动学习，仍失败请按「排障」操作
* 蓝牙（BLE）直连未实现，见协议文档第 7 节

## 目录

```
netlea_ha/
├── custom_components/netlea/
│   ├── __init__.py        集成入口
│   ├── api.py             HTTPS + WebSocket 传输层
│   ├── coordinator.py     设备清单、在线状态、帧状态、指令
│   ├── protocol.py        0x5A / 0x6A 帧编解码 + 字段表
│   ├── config_flow.py     手机号 + 短信验证码
│   ├── entity.py          实体基类
│   ├── switch.py / number.py / sensor.py / binary_sensor.py / button.py
│   ├── services.py        send_frame / build_frame
│   └── translations/      中英
└── docs/netlea-protocol.md  协议说明
```

## 免责声明

**1. 非官方项目**

本项目由个人开发者独立维护，与尼特利（Netlea）品牌方及其关联公司、经销商、代理商
**无任何隶属、合作、赞助、认可或授权关系**。本项目不是官方固件、官方 App 或官方插件，
也不代表厂商立场。文中出现的商标、产品名称、图形标识归其各自权利人所有，仅用于描述
兼容性，不构成任何形式的商标许可。

**2. 仅供技术交流与学习**

本项目用于研究家庭自动化与设备互操作性，面向个人学习、研究与自用场景，
**不提供任何商业服务、付费支持或可用性承诺**。作者不保证功能完整性、稳定性、
兼容性或持续维护。请勿将其用于任何商业分发或对第三方的有偿服务。

**3. 不收集任何用户信息**

集成完全在你自己的 Home Assistant 实例内运行：

* **无遥测**：不含任何统计上报、崩溃上报、远程日志或使用行为采集
* **无服务端**：本项目没有服务器，也没有任何数据回传通道
* **凭据留在本地**：手机号、短信验证码、登录 token、设备地址仅保存在你的
  Home Assistant 配置中（`.storage/core.config_entries`），不会离开你的设备
* **网络请求范围**：仅发往尼特利官方云端（`v3api.netlea.com`、`v3grpc.netlea.com`）
  与你自己的设备，不经过任何第三方中转

**4. 使用风险自行甄别**

本项目通过厂商云端接口与设备通信，这些接口没有公开文档，**可能随时变更、限流或关闭**。
由此可能产生但不限于以下风险，均**由使用者自行承担**：

* 设备控制失效、状态不同步、自动化误动作
* 云端接口变更导致集成完全不可用
* 厂商账号受到限制或风控
* 设备固件升级后协议不再兼容

请在安装前自行确认：你的使用方式符合所处地区的法律法规，以及你与厂商之间适用的
服务条款。是否使用、如何使用，由你自行判断并承担相应后果。

**5. 无担保与责任限制**

本项目按「现状」（AS IS）提供，不附带任何明示或暗示的担保，包括但不限于适销性、
特定用途适用性与非侵权担保。在法律允许的最大范围内，作者不对因使用或无法使用本项目
而产生的任何直接、间接、附带或后果性损失承担责任。

**6. 权利与下架**

若相关权利人认为本项目存在不当之处，请通过
[Issues](https://github.com/one-gigabyte/netlea-home-assistant/issues) 联系，
作者将在核实后及时调整或移除相关内容。

---

**使用本项目即表示你已阅读、理解并同意上述全部条款。如不同意，请立即停止使用并删除本项目。**

