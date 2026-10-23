# 实时抓取 TrackMan 每一次击球数据

> 结论：**能拿到，而且是全字段原始 JSON。** 不需要抓包、不需要 hook 进程、不需要登录设备。
> TPS 自己在 `localhost:21581` 开了个 WebSocket，每产生一次击球就主动广播一次。

配套工具：`trackman_live.py`（零依赖，仅标准库）

---

## 1. 三个入口，选一个

| 入口 | 地址 | 需要什么 | 能否拿到每杆 | 评价 |
|---|---|---|---|---|
| **A. TPS 本地 WS** | `ws://localhost:21581/ws` | TPS 正在运行 | ✅ 每杆实时 | **推荐**。免鉴权、不碰设备、不影响 TPS 正常使用 |
| B. 直连设备 WS | `ws://<设备IP>:<port>/ws` | 设备在线 + 先 `/login` 占 session | ✅ 每杆实时 | 不装 TPS 时用。会抢占设备 session，可能把 TPS 踹下线 |
| C. TPS GraphQL | `http://localhost:21581/graphql` | Bearer token | ❌ 没有击球订阅 | schema 里只有 lobby / installation / activity / utilities，**不是数据通道** |

方案 C 可以彻底排除。真正拿球的只有 A、B，而 A 是 B 的超集——TPS 已经把设备那边的一切都转发了。

---

## 2. 方案 A：订阅 TPS 本地 WebSocket

### 2.1 端点

```
ws://localhost:21581/ws
```

- 端口来自 `TrackMan.Cache.LocalApiConfig.ListenUrl`，默认值硬编码为 `localhost:21581`；被 `app.config` / AppSettings 的 `LocalApi:EndPoint` 覆盖时以配置为准。
- 路径来自 `[HttpGet("/ws")]`，标在 `TrackMan.LocalApi.Controllers.WebSocketController.Get` 上。

### 2.2 不需要 token

`LocalApiHost.LocalTokenAuth` 中间件的实际逻辑是：

```csharp
var path = context.Request.Path;
string scope =
    path.StartsWithSegments("/cloud/graphql") ? "https://auth.trackman.com/tps/cloud" :
    path.StartsWithSegments("/graphql")       ? "https://auth.trackman.com/tps/graphql" : null;

if (LocalApiAuth && scope != null) {           // ← 只有 graphql 路径才校验
    var token = Request.Headers.Authorization.ToString().StartsWith("Bearer ") ? ... : null;
    if (LocalTokenService?.ValidateToken(token, scope) != true) {
        Response.StatusCode = 401;
        return;
    }
}
await next(context);
```

`/ws` 的 `scope` 是 `null`，直接放行。所以**连 WS 不用任何 header、不用 token**。

### 2.3 握手后的完整报文序列

```jsonc
// 客户端 → 服务端，连上立刻发
{"Type":"Subscribe","Payload":{"MessageList":["ALL!"]}}

// 服务端 → 客户端
{"Type":"Acknowledge","SubType":"Subscribe"}

// 紧接着会补发当前状态快照（State 字典里已有的值），所以连上就能看到最近一次读数
{"Type":"DeviceStatus","Payload":{"IsConnected":true,"IsMeasuring":false}}

// 每打一杆
{"Type":"Measurement","Payload": { ...设备原始 JSON... }}

// 应用层心跳，必须回，否则被踢
{"Type":"Ping"}          →  {"Type":"Pong"}
```

**`MessageList` 的取值**：服务端按 `"ALL"`、`"ALL!"`、`<topic名>` 三者匹配。`["ALL!"]` = 订阅全部。想只听击球可写 `["Measurement"]`，加状态就写 `["Measurement","DeviceStatus","Measuring"]`。

**心跳**：服务端 `PingLoop` 定期下发应用层 `{"Type":"Ping"}`（JSON，不是 RFC6455 控制帧），收到后必须回 `{"Type":"Pong"}`，否则 `_pingPongTimeout` 后连接被关闭。`trackman_live.py` 已处理，另外每 30 秒静默会主动补一帧 Pong。

### 2.4 会不会影响 TPS 使用

不会。`WebsocketServer.Connections` 是字典，每个连接各自持有 `Topics`，`SendMessage` 遍历发给所有订阅该 topic 的连接。多挂一个客户端只是多一个订阅者。

### 2.5 一杆是怎么变成一条消息的

```
设备推 WS 帧
  → Tracker.WebSocketOnMessage
  → LanDevice.ProcessMessage            （按 Type/Kind 分发）
  → OnApiMeasurement(measurement)
  → DeviceHandler.OnApiMeasurement：     ← LocalApi 在这里插入
        new WebSocketMessage {
            Type    = "Measurement",
            Payload = JToken.Parse(measurement.OriginalString)   // 原样，未加工
        }
  → WebsocketServer.SendMessage          → 广播给所有 /ws 订阅者
```

注意 `Payload` 是 `OriginalString` 直接解析的，**TPS 没有二次加工**——你拿到的和设备发给 TPS 的完全一致。

---

## 3. 用法

```bash
# 抓本地 TPS，实时打印 + 落盘
python trackman_live.py

# 连通性自检：收满 5 杆自动退出
python trackman_live.py --probe

# 只看不存
python trackman_live.py --no-save

# TPS 装了但端口被改过
python trackman_live.py --port 21581

# 不装 TPS，直连设备（端口要从设备 UPnP 描述文档的 <webSocket> 字段取）
python trackman_live.py --device 172.30.20.1 --port 2869
```

输出示例：

```
[已连] 发送订阅：{"Type":"Subscribe","Payload":{"MessageList":["ALL!"]}}
[已订] Acknowledge SubType=Subscribe
[设备] {"IsConnected": true, "IsMeasuring": false}
[心跳] Ping -> Pong

=== 第 1 杆  18:42:06  [LaunchData] ===
  杆头速度 84.2  球速 118.4  效率 1.41  发射角 18.2  后旋 6320  侧旋 -3.1
  杆头轨迹 1.20  杆面角 -0.80  攻击角 -3.40  落点 152.3  总距离 161.8  最大高度 27.5  滞空 6.20
  原始 -> shots/20261023-184206_0001_launch.json
```

### 落盘格式

`shots.jsonl` 每行一条：

```json
{"seq":1, "ts":"2026-10-23T18:42:06.120", "summary":{"球速":118.4, "...":0}, "payload":{ ...原始完整 JSON... }}
```

同时 `shots/<时间>_<序号>_<kind>.json` 存每杆的原始 payload（含 `BallTrajectory` / `ClubTrajectory` 点列，未经裁剪）。

### 字段提取

`trackman_live.py` 里的 `FIELDS` 表用「候选名 + 大小写不敏感 + 递归查找」匹配，字段名对不上时不会静默丢数据，而是打印提示并保留原始 JSON。不同固件/软件版本的字段名如果不一样，改 `FIELDS` 加候选名即可。

常见字段：`ClubSpeed` `BallSpeed` `SmashFactor` `LaunchAngle` `SpinRate` `SpinAxis` `ClubPath` `FaceAngle` `FaceToPath` `AttackAngle` `DynamicLoft` `Carry` `Total` `CarrySide` `Curve` `MaxHeight` `HangTime` `LandingAngle` `SwingDirection`。

**单位（已用真机数据核对，2026-10-23）**：`ClubSpeed` / `BallSpeed` 是 **m/s**，`Carry` / `Total` / `CarrySide` / `Curve` / `MaxHeight` 是 **米**；角度类为度，`SpinRate` 为 rpm，`HangTime` 为秒。真机样本：`BallSpeed 43.1156` × 2.23694 = 96.45 mph，TPS 界面显示 96.4——换算关系确认无误。

**payload 里没有球杆字段**。`Club` / `ClubId` / `Player` 都不存在，TPS 界面上的球杆信息来自它自己的 session 记录，不随 `Measurement` 推送。

**一次击球会推送 7 次**，同一个 `Id`，字段逐步补齐：第 1 条只有 `Id/Time/Kind/LaunchDirection/BallSpeed/LaunchAngle`，第 4 条加入杆头参数，第 5 条加入旋转，第 6 条才出现 `Carry`/`Total` 和 `BallTrajectory`。消费端必须按 `Id` 合并，否则一次击球会记成 7 杆。

---

## 4. 方案 B：直连设备（不装 TPS）

同一套 `Type/Payload` 协议，设备端和 TPS 本地端是**同构**的，差别只在接入前要多两步：

1. **发现**：SSDP 搜 `urn:schemas-upnp-org:device:TrackMan:1`，或直接探 `http://172.30.20.1:2869`（设备自开 AP 的网关）、`http://169.254.0.1:2869`。
2. **取描述文档**：读 `serialNumber`、`modelNumber`、**`webSocket`**、`api`。真实端口以 `<webSocket>` 为准，2869 只是兜底。
3. **登录**：`POST {api}/login`，`Password` 传空串，靠 `Operator` / `ClientId` 占位。`Force=true` 会抢走别人的 session——设备同一时刻只能被一个 client 占用，**所以这一步会把 TPS 踹下线**。
4. 再按 §2.3 的报文订阅。

`--device` 目前只做第 4 步（直连 WS）。第 1~3 步（UPnP 发现 + 登录）需要在真实设备上验证报文细节后再补，仓库里还没实现。

---

## 5. 已知不确定项

- **端口可能被配置覆盖**：`LocalApi:EndPoint` 若改了，`21581` 就不对。先用 `netstat -ano | findstr 21581` 或看 TPS 的 `app.config` 确认。
- **2026-10-23 实测环境没有在线设备**：`172.30.20.1` / `169.254.0.1` 均不可达，本机也没有 TPS 进程在监听，所以以上结论全部来自静态分析 + 本地模拟服务端验证，**没有对真实设备跑过端到端**。
- **`Measurement` 的 payload 外层结构**取决于设备固件版本。`trackman_live.py` 用递归提取，对 `{"Kind":"LaunchData","Data":{...}}` 和扁平结构都能吃，但字段名仍需按实际数据核对一次。
- `.exe` 是 apphost，真正逻辑在 `TrackMan.Gui.Shell.dll`（.NET 10 / v10.4.100.0）。

---

## 6. 证据来源

| 结论 | 依据 |
|---|---|
| 监听 `localhost:21581` | `TrackMan.Cache.LocalApiConfig::get_ListenUrl` → `"http://"` + `"localhost:21581"` |
| 端点 `/ws` | `WebSocketController.Get` 上 `HttpGetAttribute` 的 blob：`01 00 03 "/ws"` |
| `/ws` 免鉴权 | `LocalApiHost.LocalTokenAuth` 的 `<LocalTokenAuth>d__9::MoveNext`，scope 仅对 graphql 路径赋值 |
| 订阅报文格式 | `WebsocketServer::Read`：取 `Payload.MessageList` → `conn.Topics` → `SendAck` |
| 匹配 `ALL` / `ALL!` | 同方法内 `newarr String[3]` 装入 `"ALL"`、`"ALL!"`、`key` |
| 每杆广播 | `DeviceHandler.OnApiMeasurement`：`Type="Measurement"`、`Payload=JToken.Parse(OriginalString)` |
| 心跳 | `WebsocketServer::.ctor` 建 `_pingMessage = {"Type":"Ping"}`；`PingLoop` 下发；`Read` 匹配 `"Pong"` 更新 `LastSeenTime` |
| 超时踢人 | 日志串 `"Client WS didn't respond to ping in {TimeoutSecs} seconds, closing connection"` |
| GraphQL 无击球订阅 | `TrackMan.LocalApi` 全部 Subscription 类型：仅 `LobbySubscriptions.playersChanged` |

方法：REA 元数据投影 + `dnfile`/`dncil` 定向 IL 反汇编（41MB 的 `TrackMan.Gui.CS.Modules.dll` 超出 REA 预算，改用本地读取器）。全程静态分析。
