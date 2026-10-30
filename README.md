# TrackMan Helper

[![version](https://img.shields.io/github/v/tag/szpack/trackman-helper?label=version&color=3fb950)](https://github.com/szpack/trackman-helper/releases)
[![Auto Release](https://github.com/szpack/trackman-helper/actions/workflows/release.yml/badge.svg)](https://github.com/szpack/trackman-helper/actions/workflows/release.yml)

从 TrackMan Performance Studio 实时抓取每一次击球数据的工具集。

核心原理：**TPS 在 `localhost:21581` 开了个 WebSocket，每产生一次击球就把设备原始 JSON 原样广播给所有订阅者。** 不用抓包、不用 hook 进程、不用登录设备。

协议细节见 [`LIVE_CAPTURE.md`](LIVE_CAPTURE.md)，设备侧完整链路见 [`TRACKMAN_WIFI_DATAFLOW.md`](TRACKMAN_WIFI_DATAFLOW.md)。

---

## 快速开始

### Windows：双击 `启动看板.bat`

不需要装任何第三方库，不需要配环境变量，不需要开命令行。

1. 确认 TrackMan Performance Studio 已经启动
2. 双击 `启动看板.bat`
3. 浏览器自动打开 <http://127.0.0.1:8770>，可以开始打了

脚本自己会做这几件事：

- 找一个能用的 Python（依次试 `py -3`、`python`、以及 WorkBuddy 自带的解释器）
- 探测 21581 端口，判断 TPS 是不是在运行
- 没检测到 TPS 时会问一句要不要看演示数据（8 秒不选自动进演示）
- 启动后自动打开浏览器，并绕开系统代理

**停止**：关掉那个黑色命令行窗口，或者在里面按 `Ctrl+C`。

**注意**：那个命令行窗口要一直开着。它不是后台服务，关掉看板就停了。

### 实时看板（手动）

```bash
python trackman_dashboard.py
```

浏览器打开 <http://127.0.0.1:8770>。TPS 一开打，页面自动出数，不用刷新。

没有设备想先看效果：

```bash
python trackman_dashboard.py --demo
```

### 命令行采集

```bash
python trackman_live.py --probe     # 自检：收满 5 杆退出
python trackman_live.py             # 常驻：实时打印 + 落盘
```

### 自检 / 联调

```bash
python mock_tps_test.py 21599        # 起一个假 TPS
python trackman_live.py --port 21599 --probe
```

---

## 看板功能

| 区域 | 内容 |
|---|---|
| 顶部 | TPS 连接状态、设备状态、总杆数、最快球速、平均落点 |
| 最新一杆 | 球速 / 杆头速度 / 落点 三个大字，下挂效率、发射角、后旋、总距、最大高度、滞空、侧旋、杆头轨迹、杆面角、杆面/轨迹差、攻击角、动态杆面角 |
| **AI 教练分析** | 每杆自动分析：发现了什么 / 问题 / 怎么改 |
| 球速趋势 | 最近 24 杆折线，最新点高亮 |
| 落点分布 | 横轴侧旋（左曲←→右曲）、纵轴落点距离，越新的点越亮 |
| 击球历史 | 最近 60 杆明细表，新行闪绿，点某一行可切换分析对象 |

交互：

- **全部球杆**下拉：按球杆筛选，图表统计同步过滤
- **英制 / 公制**切换：mph↔km/h、yd↔m，选择记在 localStorage
- 数据落盘 `dashboard_shots.jsonl`，重启看板自动接回历史

### 只在设备算完之后才显示

TrackMan 对一次击球是**渐进推送**的：先给球速和发射角，再补杆头参数，最后才算距离和弹道，同一个 `Id` 一共 7 帧。

看板不等这些半成品：先攒在内存里，等拿到 `Carry` + `Total` + `BallTrajectory` 之后，再等 0.9 秒确认没有后续帧，才正式入库并广播。所以页面上出现的每一杆都是**设备的最终结果**，不会先跳一个 43 mph 再变成 96 mph。

等待期间页面显示「第 N 杆正在计算…」。如果 9 秒还收不齐（比如打偏了设备算不出距离），也会强制提交并标注数据不全。

---

## AI 教练分析

每一杆算完，后端会把完整数据和「规则诊断结论」一起交给大模型，产出三段式分析：**发现了什么 / 什么问题 / 怎么改**。

### 为什么分两层

规则层先按各球杆的公开基准逐项对照（smash、发射角、后旋、攻击角、动态杆面角、杆面/轨迹差、旋转轴、击球点），算出客观结论。大模型只负责把这些结论组织成教练口吻的因果关系和训练方案，**不让它碰数字**——否则很容易把 1.29 说成 1.45。

这样做的好处是：没有 API Key、断网、模型超时，你依然能拿到有依据的诊断，只是表达没那么"活"。

界面上会标明来源：紫色 `AI 生成` 或灰色 `规则诊断`。

### 配置（三种方式，任选一种）

**① 页面里配（推荐）**：右上角 `AI 设置` → 填接口地址、模型名、API Key → 点「测试」验证 → 保存。Key 只写在本机 `ai_config.json`。

**② 配置文件**：在项目目录建 `ai_config.json`（**改完不用重启**，下次分析自动读新值）：

```json
{
  "base_url": "https://api.deepseek.com/v1",
  "api_key": "sk-...",
  "model": "deepseek-flash"
}
```

**③ 环境变量**：`TM_AI_BASE_URL`、`TM_AI_API_KEY`、`TM_AI_MODEL`

任何 **OpenAI 兼容**接口都能用：

| 服务 | base_url | model |
|---|---|---|
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-flash`（强推理用 `deepseek-v4-pro`） |
| Kimi | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` |
| 通义 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| 本地 Ollama | `http://127.0.0.1:11434/v1` | `qwen2.5:7b` |

> **DeepSeek 模型名注意**：`deepseek-chat` 和 `deepseek-reasoner` 这两个旧别名已于 2026-07-24 退役，
> 现在官方模型 ID 是 `deepseek-flash`（V4.1-Flash）和 `deepseek-v4-pro`。
> `deepseek-flash` 默认带思考模式，本程序会自动加 `thinking: {type: disabled}` 关掉它——
> 否则一杆分析要多等十几秒，且思考 token 也计费。

本机地址（Ollama）会自动绕开系统代理。

不想用 AI：`--no-ai`，只出规则诊断。

### 球杆会影响判断

payload 里**没有球杆字段**，所以球杆是按球速推断的（96 mph → 7 号铁）。推断错了基准就错了：同样 1.30 的效率，对 7 号铁算及格，对一号木就是严重损失。

AI 卡片右上角的下拉可以选真实球杆，选完会自动重新分析，选择记在 localStorage。标题里出现"球杆按球速推断"就是在提醒你确认一下。

---

## 参数

### trackman_dashboard.py

| 参数 | 默认 | 说明 |
|---|---|---|
| `--port` | 8770 | 看板端口 |
| `--host` | 127.0.0.1 | TPS 地址 |
| `--tps-port` | 21581 | TPS 本地 API 端口 |
| `--tps-path` | /ws | TPS WebSocket 路径 |
| `--persist` | dashboard_shots.jsonl | 历史文件，传 `""` 关闭 |
| `--demo` | 关 | 模拟数据演示 |
| `--no-ai` | 关 | 关闭 AI，只出规则诊断 |
| `--open` | 关 | 启动后自动打开浏览器 |

### trackman_live.py

| 参数 | 默认 | 说明 |
|---|---|---|
| `--probe` | 关 | 收满 5 杆退出，测连通性 |
| `--device IP` | — | 绕过 TPS 直连设备（会抢占设备 session，把 TPS 踢下线） |
| `--out` | shots.jsonl | JSONL 落盘 |
| `--raw-dir` | shots/ | 每杆原始 JSON 目录 |
| `--no-save` | 关 | 只看不存 |

---

## 数据格式

`dashboard_shots.jsonl` 每行一条：

```json
{
  "seq": 1,
  "ts": "2026-10-23T18:46:46.156",
  "kind": "Measurement",
  "m": {
    "clubSpeed": 74.55, "ballSpeed": 96.45, "smash": 1.2937, "launch": 22.417,
    "spin": 3064.73, "spinAxis": -8.202, "clubPath": 3.6, "faceAngle": -0.786,
    "attack": -2.0, "loft": 25.561, "carry": 133.5, "total": 149.5,
    "height": 65.35, "hang": 5.0004, "side": -9.59
  },
  "raw": { }
}
```

`m` 是归一化后的字段（缺失的不出现），`raw` 是设备原始 payload，含 `BallTrajectory` / `ClubTrajectory` 多项式系数。

### 单位：设备发 SI，看板内部统一用英制

这是**必须知道的一条**——`BallSpeed` / `ClubSpeed` 设备给的是 **m/s**，`Carry` / `Total` / `MaxHeight` 给的是 **米**。
后端 `_is_si()` 会按量级判断（球速 < 100 视为 m/s），再统一换算成 mph / yd / ft，前端只负责英制↔公制切换：

| 字段 | 设备单位 | `m` 里的单位 |
|---|---|---|
| `ClubSpeed` `BallSpeed` | m/s | mph（×2.2369） |
| `Carry` `Total` `CarrySide` `Curve` | m | yd（×1.0936） |
| `MaxHeight` | m | ft（×3.2808） |
| `LaunchAngle` `SpinAxis` `ClubPath` `FaceAngle` `AttackAngle` 等 | 度 | 度（不换算） |
| `SpinRate` | rpm | rpm（不换算） |
| `HangTime` | 秒 | 秒（不换算） |

角度 / 转速 / 效率 / 滞空这些直接就是 TPS 界面上的数，不需要换算也精确一致。

### 同一杆会推送 7 次

设备对一次击球是**渐进推送**的：第 1 次只有 `BallSpeed` + `LaunchAngle`，之后逐步补齐杆头参数、旋转、再到 `Carry` / `Total` / 弹道，同一个 `Id` 共 7 条。
所以后端按 `Id` 合并（新值覆盖、老值兜底），前端按 `seq` 原地替换，最终一杆只占一行。

原始字段名对不上时不会丢数据，`trackman_dashboard.py` 里的 `MAP` 表按候选名递归匹配，加一行就能支持新固件。

---

## 版本号与发布

版本号只有一个真相：仓库根目录的 **`VERSION`** 文件（形如 `1.2.3`）。看板右上角会显示当前版本，鼠标悬停可以看到提交号和工作区是否干净。

**自动递增**：推送到 `main` 后，GitHub Actions（[`.github/workflows/release.yml`](.github/workflows/release.yml)）会按提交信息决定下一个版本，改 `VERSION`、打 tag、发 Release，全自动。

| 提交信息 | 递增 | 例子 |
|---|---|---|
| 含 `BREAKING CHANGE` 或标题带 `!` | **major** | `feat!: 换掉推送协议` → `2.0.0` |
| `feat:` / `feat(scope):` | **minor** | `feat: 加杆面角趋势图` → `1.3.0` |
| 其它（fix/docs/chore/...） | **patch** | `fix: 修 8 号铁基准` → `1.2.4` |

所以提交信息最好按 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/) 写，否则默认只递增 patch。

**本地预览版本号，不会改动任何东西**：

```bash
python .github/scripts/bump_version.py
python version.py            # 看完整版本（本地开发会带 -dev.N+g<sha>）
```

需要强制某个递增类型时，可以在 Actions 页面手动触发 `Auto Release` 并选择 `major` / `minor` / `patch`。

**本地开发的版本号长这样**：`0.1.0-dev.7+g3f2a1bc`，含义是「基于 0.1.0，比 tag 多 7 个提交，当前提交 3f2a1bc」。工作区有未提交改动时末尾再加 `-dirty`。这样从看板上就能分清跑的是发版代码还是半成品。

## 可选依赖

核心功能零依赖。下面两项缺了也能跑，只是对应功能降级：

```bash
pip install -r requirements.txt
```

- `pyodbc` — 识别球杆（读 TPS 本地 LocalDB）
- `uiautomation` + `Pillow` — 识别 TPS 的 Normalize 开关（读按钮底色）

**装在哪不重要，但必须是"启动看板的那个" Python**。为此做了两层兜底，避免出现
"明明装了却显示未识别"：

1. `pydeps.py` 会在本机找装了该依赖的解释器，自己换过去重启（`os.execv`），
   换成功就是 20ms / 0.9s 的原生速度；
2. 换不过去也不放弃：`tps_club` 借那个解释器跑 `--json` 模式查库（约 0.6s），
   `tps_norm` 同样借它读按钮状态，最后才退到 PowerShell / layout 文件。

Normalize 徽章有三种可信度，别搞混：

| 徽章 | 含义 |
| --- | --- |
| `TPS 归一化中` / `实测（Normalize 关）` | UIA 实时读的按钮底色，可信 |
| `Normalize 待确认`（虚线边框） | 从 layout 文件推测的。layout 只在保存布局时写盘，运行中切开关不反映 |
| `Normalize 未识别` | 两条路都没读到（TPS 不在击球分析界面 / 没启动） |


## 常见问题

**页面显示"未连接 TPS"**
先确认 TPS 正在运行，且端口没错：`netstat -ano | findstr 21581`。端口被改过就 `--tps-port 新端口`。

**浏览器打开是 502 / 白屏**
本机可能配了系统代理。Chrome/Edge 一般自动绕过 localhost，若不行在代理设置里把 `127.0.0.1` 加进例外。

**端口 8770 被占用**
`--port 9000` 换一个。

**看板能连但一直没数据**
TPS 连上设备了吗（看顶部设备状态）？以及是否有人在打。`Measuring` 状态说明设备正在测量。

**球杆显示"未识别"，但 TPS 界面上明明有**
球杆不在设备推送里，只能按 `MeasurementId` 去 TPS 本地库 `Strokes` 表反查。两种常见原因：
TPS 落库比推送慢（看板会在 6s / 18s / 45s 自动重试补上），或者启动看板的 Python 没装 `pyodbc`
（见上面「可选依赖」——现在会自动换解释器，不会静默降级了）。
自检：`curl http://127.0.0.1:8770/api/status` 看 `clubDb.driver` 是不是 `pyodbc`。

**手动选过球杆，结果新的一杆也被按那根杆分析**
已修：手动指定只作用于当前这一杆，换杆不再记忆。TPS 里记录的球杆是权威值，
残留的手动选择会被作废（日志里能看到「作废残留的手动球杆」）。

---

## 已验证 / 未验证

- ✅ 协议结论全部来自静态分析（REA 元数据 + dnfile/dncil 定向 IL 反汇编），见 `LIVE_CAPTURE.md` 末尾的证据表
- ✅ **真实 TPS 上验证通过**（2026-10-23）：连上 `ws://127.0.0.1:21581/ws`、订阅被确认、收到 `DeviceStatus` 且设备已连接——确认 `/ws` 确实免 token
- ✅ **真实击球数据已抓到并与 TPS 界面逐项对照**（2026-10-23）：
  - 精确一致：球速、杆头速度、效率、发射角、后旋、侧旋、杆头轨迹、杆面角、杆面/轨迹差、攻击角、动态杆面角（差值均在 UI 四舍五入范围内）
  - 差 3~4%：`Carry` / `Total` / `MaxHeight` —— raw 是雷达实测值，TPS 界面在 `Normalized` 模式下显示的是环境归一化后的值，不是同一个口径
- ⚠️ **payload 里没有球杆字段**（`Club` / `ClubId` 都不存在），所以看板显示"未识别球杆"。TPS 界面上的球杆来自它自己的 session 记录，不在这条数据流里

### AI 分析的验证情况

- ✅ 规则诊断在**真实 7 杆数据**上跑通，结论与 TPS 数据一致（效率 1.28/1.35、后旋 4213/6600-7500、动态杆面角 26.8°/28-33°）
- ✅ LLM 链路用本地假 OpenAI 服务端到端验证：配置 → 调用 → JSON 解析 → 前端渲染，`source` 正确标为 `ai`
- ✅ 失败降级验证：无 Key、接口报错、返回非法 JSON 都会退到规则诊断并标明原因
- ⚠️ **没有用真实大模型 API Key 跑过**（当前机器没有配置），首次配 Key 后建议先点「测试」
- ⚠️ 真实击球的 `pending → 算完 → 分析` 链路：时序逻辑用录制的 7 帧数据单测通过（0.73 秒内 7 帧全部不提交，第 8 帧后 0.9 秒提交），但没有在设备实时推流下验证

### 关于距离的 3~4% 差异

同一杆 `f0b154ee`：

| 项 | 设备 raw | 换算后 | TPS 界面 |
|---|---|---|---|
| Carry | 122.07 m | 133.5 yd | 138.8 yd |
| Total | 136.705 m | 149.5 yd | 154.5 yd |
| MaxHeight | 19.919 m | 65.35 ft | 68 ft |
| 差值 Carry→Total | 14.64 m | 16.0 yd | 15.7 yd |

两个值内部都自洽（`BallTrajectory` 的 `XFit` 多项式在 `HangTime=5.0004s` 处积分得 121.7 m，正好等于 raw 的 `Carry`），差的这一层是 TPS 叠加了 `Normalized`（归一化到标准温度/气压/海拔/无风）。

**要验证**：把 TPS 右上角的 `Normalized` 关掉，同一环境再打一杆，看 raw 的 Carry 是否与界面一致。另外 `ReducedAccuracy` 字段里列出了 `Carry` / `Total` / `CarrySide` / `TotalSide` / `LandingAngle` / `HangTime`，说明设备自己也把这几个标为低置信度估算值。

看板默认显示 **raw 实测值**，不做任何归一化——需要和 TPS 对齐时，以角度和速度为准。

第一次接真机，建议先 `python trackman_live.py --probe`，确认端口与字段名。
