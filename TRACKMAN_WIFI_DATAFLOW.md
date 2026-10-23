# TrackMan Performance Studio 如何从 WiFi 拿到击球数据

结论先行：**TPS 不解析雷达原始信号。TrackMan 设备本身就是一台带 HTTP + WebSocket 服务的嵌入式主机**，球被打中后由设备侧算完球杆/弹道参数，用一条 JSON WebSocket 消息推给 PCs。PC 端做的是「发现 → 取设备描述 → 登录 → 订阅 → 反序列化」。

---

## 0. 目标身份

| 文件 | 事实 |
|---|---|
| `Modules\TrackMan.Gui.Shell.exe` | PE x86-64，**无 CLI 头** → 只是 .NET apphost 启动器，本身不含业务代码 (ev_783355ba…) |
| `Modules\TrackMan.Gui.Shell.dll` | 真正的托管入口，`TargetFramework=.NETCoreApp,v10.0`，AssemblyVersion `10.4.100.0` (ev_2bfd86c9…) |

---

## 1. 设备发现（两条路并行）

### 1a. SSDP / UPnP 组播发现
- `TrackMan.Helpers.UPnP.UPnPDeviceFinder` 走 **Windows UPnP COM**（`IUPnPDeviceFinder`，CLSID `E2085F28-FEB7-404A-B8E7-E659BDEAAA02`）(ev_1425d5e4…)
- 搜索目标类型（ST）：
  ```
  urn:schemas-upnp-org:device:TrackMan:1
  ```
- 回调 `DeviceAdded / DeviceRemoved / SearchComplete` → `LanDeviceFactory.InvokeOnTrackManFound`
- 另有 `NetworkAddressChanged` 监听网卡变化自动重扫，`RescanInterval` 到点重扫 (ev_cb3b7d12…)

### 1b. 已知地址直连探测
`LanDeviceFactory` 构造时把两个写死的地址放进候选列表：

```
KnownWiFiAddress   = http://172.30.20.1:2869     // 雷达自己开 AP 时的网关地址
KnownUsbLanAddress = http://169.254.0.1:2869     // USB / 链路本地
```
- `172.30.20.1` / `169.254.0.1` / `:2869` 三个常量来自 `LanDeviceDescriptor..cctor`
- 还可通过 `app.config` 的 `KnownTrackManIpAddresses`（`<ip>`、`<ip:port>`、`<uri>`，分号分隔）追加
- `ProbeKnownIpAddressesLoop` 后台线程轮询探测，命中即 `DeviceAdded`

---

## 2. 抓设备描述文档

拿到 IP 后 `GET http://{ip}:2869/`，用 `XDocument` 解析 UPnP Device Description（命名空间 `urn:schemas-upnp-org:device-1-0`），读取的 XML 节点：

| XML 元素 | 用途 |
|---|---|
| `UDN` | UniqueDeviceName（唯一标识） |
| `serialNumber` | 设备序列号（也是 TPS 里选设备的 key） |
| `modelNumber` | `TMA4A` / `3.5` → 归为 TMA4A，否则 TMA3B（老型号） |
| `modelName` | 型号名 |
| **`webSocket`** | **击球数据推送的 WebSocket URI** |
| `api` | REST API 根地址 |
| `cameraApi` | 视觉/相机服务地址 |
| `presentationURL` | 展示页地址 |
| `ntpServer` | 对时服务器（设备侧时间同步） |
| `friendlyName` | 显示名 |

→ 解析结果就是 `LanDeviceDescriptor`：`{WebsocketUri, RestUri, VmsUri, PresentationUri, NtpServer, ApiVersion, …}`
→ `CanCreate` 要求 `ApiVersion.Major >= 2`，否则不建连接。 (ev_cb3b7d12…)

关键点：**端口和路径都是设备自己声明的**，2869 只是兜底默认值，不是协议硬约定。

---

## 3. 建立会话（REST 登录 + WS 订阅）

```
TrackerFactory.Create(descriptor)
      └─> TrackMan.Api.Devices.Tracker.Create(apiUri, websocketUri, cameraUri, presentationUri, …)
```
(ev_96233f94…)

1. **登录** — `POST {RestUri}/login`
   ```json
   { "Username": "...", "Password": "", "Operator": "...",
     "Software": "TrackMan Performance Studio", "ClientId": "...", "Force": false }
   ```
   → 返回 `AuthToken`。注意 `Password` 传空字符串：局域网内靠「设备占用/Operator 抢占」而不是账号密码做授权，`Force=true` 可以抢别人的 session（"Could not login. Device already in use"）。
2. **连 WebSocket** — `ClientWebSocket` 连 `{WebsocketUri}`
3. **WS 一打开就发订阅**：
   ```json
   { "Type": "Subscribe", "Payload": ["ALL!"] }
   ```
   设备回 `{"Type":"Acknowledge","SubType":"Subscribe"}`；随后 `BindAuthTokenToWebsocket` 把 AuthToken 绑进这条通道，回 `Acknowledge/AuthToken`。
4. **保活**：设备发 `Ping` → 客户端回 `Pong`；超时/错误 → `CloseWebsocket` 并重连。

---

## 4. 数据推送 —— 真正的击球数据在这里

设备**主动推** JSON 文本帧，客户端只做分发（`Tracker.WebSocketOnMessage` → `LanDevice.ProcessMessage`）：

| Type | SubType | 处理 |
|---|---|---|
| `Measurement` | `LaunchData` / `Measurement` | `Deserialize<TrackMan.Api.Data.Golf.Measurement>` ← **击球数据主通道** |
| `Measurement` | `MeasurementData` | 追加文件（视频/数据文件落盘） |
| `LiveTrajectory` | — | 实时弹道点列 `PositionList` / `FullPositionList` |
| `Setup` | — | 设备设置变更（目标、球位、环境…） |
| `TrackerState` / `SystemState` | — | 状态机 |
| `Files` | — | 设备文件列表 |
| `SessionOpened` / `SessionClosed` | — | 占用状态 |
| `goodbye` | — | 断线 |

`LanDevice.ProcessMessage` 进一步按 `Measurement.Kind` 分流：

```
Kind == "LaunchData"  → LaunchDataReceived(...)          // 只发了发射段参数
否则                   → MeasurementReceived(...)         // 完整弹道
                        + OnApiMeasurement(...)           // 给 API/第三方 SDK 用
```

若 `ShouldReprocessIncomingDataWithSharedCalculator` 为真，还会先过一遍
`TrackMan.SharedCalculator.Instance.NormalizeMeasurement(...)` 做二次归一化，再往下发。

除推送外还有轮询兜底：`GET {RestUri}/system/status`，连续多次失败即判定失联（"Lost contact with {0} after {1} consecutive failed status polls"）。

---

## 5. 落到业务模型

`TrackMan.Model.MeasurementConverter.ToGolfMeasurement` → `TrackMan.Data.Golf.GolfMeasurement`
→ `TrackMan.Data.CS.Modules.RadarInterfaceModule.RadarModule` 抛 `ApiMeasurementEvent` / `FinalDataEvent`
→ GUI/报表/Unity 端消费。 (ev_f776e8bd…)

数据字段（`TrackMan.Api.Data.Golf.Measurement`，ev_fcb82a11…）：
- 球杆：`ClubSpeed` `AttackAngle` `ClubPath` `DynamicLoft` `FaceAngle` `SpinLoft` `FaceToPath` `SwingPlane` `SwingDirection` `SwingRadius` `LowPoint*` `DPlaneTilt` `ImpactOffset` `ImpactHeight` `DynamicLie` `Tempo` `BackswingTime` `ForwardswingTime`
- 球：`BallSpeed` `SmashFactor` `LaunchAngle` `LaunchDirection` `SpinRate` `SpinAxis` `GyroSpinAngle` `SkidDistance`
- 弹道：`MaxHeight` `Carry` `Total` `CarrySide` `TotalSide` `LandingAngle` `LandingHeight` `HangTime` `Curve`
- 推杆：`RollSpeed` `RollPercentage` `Break` `TotalBreak` `EffectiveStimp` `FlatStimp` `SlopePercentage*` `Bounces`
- 原始轨迹：`BallTrajectory` / `ClubTrajectory`

---

## 6. 控制面 REST 接口（同一台设备）

`login`、`system/status`、`Setup`(POST)、`systemtime`、`diagnostics`、`Firmware?test=1`、`camera/api/info`、`statemanager/pause`、`target`、`debug/shotsimulator`、`license/keys`、`files/logs.zip`

---

## 7. 连接形态枚举（解释了「怎么算从 WiFi」）

```
ConnectionType = None, Usb, UsbLan, Lan, WiFi, LinkLocal, LanLan, LanWifi, WifiLan, WifiWifi
WirelessMode   = AP, STA, OFF
```
- **AP 模式**：PC 连雷达自己的热点 → 命中 `172.30.20.1:2869`
- **STA 模式**：雷达接入场馆 WiFi → 只能靠 SSDP 组播发现
- **USB / 链路本地**：`169.254.0.1:2869`

---

## 8. FastDDS 是不是也传击球数据？

**不是**，容易误判。`TrackMan.FastDDS.dll` 是 eProsima **Fast DDS** 的 SWIG 封装（`Publication/Subscription`、`udp/tcp`、`DiscoveryServer`、`SHM`，以及 1MB/1.5MB/2MB/5MB 的 `FrameData*Subscriber`），说明架构里具备 DDS 局域网数据分发能力 (ev_55449881…)。

但在 TPS 10.4 的 GUI 程序集里，DDS 唯一使用点是
`TrackMan.Gui.CS.Modules.HardwareUpdater.TmioMessageHandler`：
参与方名 `TPS`，订阅字符串主题 `General/PreInstallation/Status` —— 那是 **iO 硬件安装/固件流程的状态广播**，与击球数据无关。

> 注：`TrackMan.Gui.CS.Modules.dll` 41MB，超出 REA 元数据表示预算（64MB representation limit），该程序集改用本地元数据 + IL 读取器定向扫描；其余程序集均由 REA 直接产出证据。

---

## 9. 一句话数据流

```
雷达设备 (TrackMan 4 / iO)                        PC (TrackMan Performance Studio)
┌────────────────────┐                        ┌──────────────────────────────────┐
│ 雷达 + 视觉 + 算法  │  ① SSDP 239.255.255.250 │ LanDeviceFactory                 │
│ 算出球杆/弹道参数    │ ──── NOTIFY/SEARCH ───> │  UPnPDeviceFinder (COM)          │
│                    │ <── M-SEARCH TrackMan:1─ │  + 探测 172.30.20.1:2869         │
│ HTTP :2869         │  ② GET / (Device Desc)  │ LanDeviceDescriptor              │
│  ├ /login          │ <────────────────────── │  解析 webSocket / api URI        │
│  ├ /system/status  │  ③ POST /login → token  │ Tracker.Login                    │
│  └ /Setup          │ <────────────────────── │                                  │
│ WS  {webSocket}    │  ④ WS 订阅 ["ALL!"]     │ Tracker.Connect                  │
│                    │ ──────────────────────> │                                  │
│                    │  ⑤ 推送 Measurement JSON│ ProcessMessage →              │
│                    │ ──────────────────────> │   Deserialize<Measurement>       │
│                    │  ⑥ LaunchData / Full    │   → GolfMeasurement → 界面/报表  │
└────────────────────┘                        └──────────────────────────────────┘
```

---

## 10. 证据与方法边界

- REA 7.x 静态托管分析：`inspect_managed_artifact` / `inspect-managed-members`
  - ev_783355ba… `TrackMan.Gui.Shell.exe`（非托管 apphost）
  - ev_2bfd86c9… `TrackMan.Gui.Shell.dll`
  - ev_cb3b7d12… `TrackMan.Device.dll`（发现 / LanDevice / 消息分发）
  - ev_96233f94… `TrackMan.Api.Devices.dll`（Tracker / SSDP / WS 客户端）
  - ev_1425d5e4… `TrackMan.Helpers.dll`（UPnP COM 封装）
  - ev_f776e8bd… `TrackMan.Data.CS.Modules.RadarInterfaceModule.dll`
  - ev_55449881… `TrackMan.FastDDS.dll`
  - ev_fcb82a11… `TrackMan.Api.Data.dll`（Measurement 字段）
- **静态分析，未抓实时流量**：报文格式来自 IL 里的字段名与类型字符串；WS 实际端口由设备描述文档在运行时决定。
- REST 路由名（`login` / `system/status` / `Setup` …）经 `ApiRouteWithNamingStrategy` 映射，完整 URL 前缀取决于设备固件版本。
