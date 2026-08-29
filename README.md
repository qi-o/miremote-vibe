# miremote-vibe

**把 65 块的小米蓝牙遥控器 2 Pro，变成 Windows 上的 vibe coding 遥控器。**

躺在沙发上，按住遥控器语音键对 AI 编程助手说话，松手后文字自动打进终端；
方向键翻阅 AI 的输出、OK 批准、返回键打断、音量键调音量。

**Turn a $10 Xiaomi Bluetooth Remote 2 Pro into a vibe coding controller for Windows.**

Lean back on your couch, hold the remote's voice button to talk to your AI
coding assistant, release, and the transcribed text lands in your terminal.
Arrow keys scroll the AI's output, OK approves, Back interrupts, volume keys
adjust volume.

**软件界面**（控制台 / 按键映射可视化编辑 / 语音模式切换）：

| 控制台 / Console | 按键映射 / Key Mapping |
|---|---|
| ![控制台](assets/ui-control.png) | ![按键映射](assets/ui-mapping.png) |

> [!IMPORTANT]
> **项目现状**：这是一个 vibe Coding 项目——在一天之内由 AI agent 与作者
> 协作完成，代码比较粗糙，不是一个完善的产品。它在我的机器上完整跑通，
> 但没有经过多设备、多环境的测试。
>
> **发布目的**：发布出来只是希望给大家一个参考，尤其是给 Windows 系统下
> 想要使用小米蓝牙遥控器硬件做类似事情的人提供参考——这里的设备协议逆向、
> 蓝牙语音解码、被系统丢弃按键的救回方案，全网目前没有现成的 Windows 实现。
>
> **交流意愿**：这是我自己 vibe coding 出来的一个小玩具，分享出来纯粹是
> 希望它记录的技术方案能帮到遇到同样问题的人。**大概率不会有后期维护**——
> 如果哪里跑不通，README 的踩坑记录和 `docs/` 里的技术档案写得很细，
> 建议直接把源码拿去自己改，不必等我。祝玩得开心。
>
> **Project status**: This is a vibe-coding project — built in a single day by
> an AI agent working with its author. The code is rough; it is **not** a
> polished product. It works end-to-end on my machine but has not been tested
> across different hardware or environments.
>
> **Why published**: Shared purely as a reference, especially for people on
> Windows who want to hack on this Xiaomi remote hardware — the protocol
> reverse engineering, Bluetooth voice decoding, and recovery of keys silently
> dropped by the Windows driver have no existing open-source Windows
> implementation that we know of.
>
> **Community**: This is a little toy I vibe-coded for myself, shared purely
> in the hope that the documented solutions help someone hitting the same
> problems. **It will most likely not be maintained.** If something breaks,
> the pitfalls log in this README and the archive under `docs/` are quite
> detailed — feel free to take the source and adapt it yourself rather than
> waiting on me. Happy hacking.

---

## 中文目录

- [功能一览](#功能一览全部真机验证)
- [它是怎么工作的](#它是怎么工作的)
- [快速开始](#快速开始)
- [踩坑记录](#踩坑记录本文档最有价值的部分)
- [已知限制](#已知限制)
- [Roadmap](#roadmap想折腾的方向)
- [致谢与许可](#致谢与许可)

## English Contents

- [Features](#features-verified-on-real-hardware)
- [How It Works](#how-it-works)
- [Quick Start](#quick-start)
- [Pitfalls Log](#pitfalls-log-the-most-valuable-part-of-this-doc)
- [Known Limitations](#known-limitations)
- [Roadmap](#roadmap-ideas-if-you-want-to-hack-on-it)
- [Credits & License](#credits--license)

---

# 中文

## 功能一览（全部真机验证）

| 功能 | 说明 |
|---|---|
| 按键捕获 | 方向/OK/主页/菜单/TV/电源/语音键，Raw Input 按设备过滤（不影响物理键盘） |
| **哑键救回** | **返回/音量±的报文被 Windows 驱动丢弃**，本项目用 Frida Gadget 注入 WUDFHost 取回（tap4 协议：并发连接防护 + 控制握手，macOS 项目也没有的能力） |
| 本地语音 | 按住说话→松手→ATVV 蓝牙协议解码→faster-whisper 本地转写→文字粘贴（全离线） |
| 微信语音模式 | 松手后整段音频桥接给微信输入法识别（自动去语气词、整理语句）；需 VB-CABLE + 在微信输入法里把语音麦克风设为 CABLE Output、"按住说话"快捷键设为 Ctrl+Alt+V |
| Qt GUI | 按键映射可视化编辑（点遥控器图绑键）+ 语音模式切换 + 日志 + 系统托盘 |
| 回归测试 | 27 项 pytest 覆盖哑键协议解析/Gadget 版本协商/语音链路（`tests/`） |
| 开机自启 | 静默后台启动 + 自动启动守护 |

> 状态：2026-08-29 tap4 稳定版。语音链路大坑已修（会话开始**不再主动发
> MIC_OPEN**——固件 2671 上主动开麦会触发一条无声的"主机流"，把按住语音键
> 的真实物理流堵死，表现为每段固定 1.9 秒无效音频）；MIC_CLOSE 后音频通知
> 失效也已按言灵的 REOPEN RESET 序列修复。

## 它是怎么工作的

```
① 按键：遥控器 HID → Raw Input（VID/PID 过滤）→ 动作系统（SendInput/剪贴板/…）
② 语音：遥控器 ATVV(GATT) → ADPCM 解码 → whisper 转写（本地）或 CABLE 桥接（微信）
③ 哑键：WUDFHost 内 Gadget 钩子 → localhost:30685 → 解码成按键边沿
```

三条链路的关键技术细节：

- **ATVV 协议**：遥控器麦克风走 Google ATVV 私有 GATT 服务（`AB5E0001-...`），
  Windows 不认它为麦克风，本项目从握手（`GET_CAPS`）到 IMA ADPCM 解码全链路实现
- **哑键机制**：返回键 usage=0xF1、音量=0x80/0x81，HidOverGatt 驱动收到报文但
  翻译不成键盘事件直接丢弃；frida-gadget DLL（官方发布物，双 SHA-256 锁定）
  注入 WUDFHost 钩 `NtDeviceIoControlFile` 取回报文
- **微信桥接的架构教训**：遥控器语音键=F5 透传，按住期间注入热键会被微信
  输入法的"防误触"拒绝（它只认硬件按键状态，注入的假松开骗不过）。最终采用
  **松手播放**架构：录音期间零注入缓冲 → 松手后按住 Ctrl+Alt+V（微信输入法
  "按住说话"快捷键）→ 整段音频播放进 VB-CABLE → 松开 → 输入法识别上屏。
  想做"按下实时出字"？两条硬约束卡死，详见下面踩坑记录的新增章节

## 快速开始

### 从源码运行

```bat
:: 依赖：Python 3.10+（在 3.14 上开发测试）
pip install PySide6-Essentials faster-whisper nvidia-cublas-cu12 nvidia-cudnn-cu12 ^
            winrt-windows-devices-bluetooth winrt-windows-storage-streams sounddevice pyinstaller

python -m miremote app
```

蓝牙配对遥控器（长按 TV 键进入配对模式；电脑上它是键盘设备，重连同样长按 TV 键），启动守护即可。
详细步骤见 [docs/使用说明.md](docs/使用说明.md)。

### 打包 exe

```bat
:: 可选：哑键拦截需要 Frida Gadget（约 7MB，仓库不含二进制）
curl -L -o assets\frida-gadget-17.15.3-windows-x86_64.dll.xz ^
  https://github.com/frida/frida/releases/download/17.15.3/frida-gadget-17.15.3-windows-x86_64.dll.xz

pyinstaller miremote.spec --noconfirm
```

## 踩坑记录（本文档最有价值的部分）

一天开发里实测踩过的坑，每一条都是真实翻车后修的：

### ctypes 四连坑（同一类问题踩了四次）

**所有返回 HANDLE/指针的 Win32 API 必须显式声明 `restype=ctypes.c_void_p`**，
否则 64 位句柄被截断成 32 位，症状五花八门：

| API | 截断后的症状 |
|---|---|
| `HWND` | WinError 1400 无效窗口句柄 |
| `GetCurrentProcess` | WinError 6 句柄无效 |
| `GlobalLock` | memmove 写 NULL → access violation |
| `GetClipboardData` | 同上 |

### SendInput 静默失效（潜伏一个月的 bug）

`INPUT` 结构体的 union 必须包含 **MOUSEINPUT + KEYBDINPUT + HARDWAREINPUT 全部三种**。
只写 KEYBDINPUT 时 `sizeof(INPUT)=32`（x64 应为 40），`SendInput` 因 cbSize
不匹配**静默返回 0**——所有按键动作全部失效但不报任何错。方向键走透传所以
一直没暴露。

### 遥控器语音键 = F5 透传会干扰微信热键

按住遥控器语音键期间，F5 持续透传到焦点窗口，微信的组合键检测（应该是低级
钩子自绘状态机）被额外按键干扰，浮窗不出现。实测复现后改为切换式架构。

### Qt 窗口不能被外部 ShowWindow 显示

从另一个进程 `ShowWindow` 硬显示 Qt 窗口 → Qt 不知道 → 不重绘 → **窗口空白**。
跨进程唤回必须让 Qt 亲自 `show()`（本项目用 TEMP 目录信号文件 + 500ms 轮询）。

### whisper 子进程必须干净环境

宿主 shell 的配置文件（starship/nvm 等）会污染环境变量，导致 ctranslate2 在
CUDA 模型加载时原生崩溃（access violation）。解法：转写放干净环境子进程
（只传必要变量 + `HF_HUB_OFFLINE=1` + 显式 UTF-8 解码）。
另一个坑：空字符串环境变量不能传（`HF_HOME=""` 会让缓存定位失败）。

### 打包（PyInstaller）相关

- 直接打包包内 `__main__.py` 会相对导入失败——用顶层 `launcher.py` 入口
- `faulthandler.enable()` 在 frozen 下 `sys.stderr is None` 直接崩——加 frozen 判断
- faster-whisper 的 `silero_vad_v6.onnx` 不会自动打包——spec 显式 collect
- frozen 子进程要用 `[sys.executable, "子命令", ...]` 而不是脚本路径
- **exe 的自定义参数必须在 `__main__.main()` 登记**（`--silent` 被当未知命令
  打印帮助后秒退，造成开机自启"没反应"的假象）

### 遥控器固件 ATVV 模块会卡死（软重启可修）

症状：按键全部正常（SoftDevice 活着），但语音完全收不到音频帧，或只在松开语音键
的瞬间收到几帧"尾巴"（0-8 帧 = 0~120ms）。日志特征是主机流打不开、固件反复报
"物理流未释放"。**大概率由异常退出残留的悬挂麦克风会话引发**（进程被杀时没发
MIC_CLOSE）。修复：**长按遥控器 TV 键重连**（电脑把它当键盘用，重连键是 TV 键，
不是电视场景的主页+菜单）；顽固时在 Windows 蓝牙设置里删除设备重新配对。

### 电源键的真实身份：VK_NONE（不是哑键）

RC003 电源键（键盘页 usage 0x66）的报文 Windows 能收到，但 HidOverGatt 不为它
映射任何虚拟键——Raw Input 里表现为 VK=0（VK_NONE）。所以电源键**天然可用且
无需注入**，本项目把它按 `VK_NONE` 分发（见 `service.py` 默认配置）。

### "静音键"是个幽灵

协议 usage 表里有 0x7F（静音），早期版本也配置过它，但 RC003 实体上只有音量
+/- 两个侧键——0x7F 在真机上**从未出现过**。已从默认配置移除，避免误导后来者。

### 与笔记本键盘的冲突边界（为什么本项目不受影响）

RC003 的确认/Home/TV 键与笔记本 Enter/Home/~ 的 VK+扫描码完全相同，低层键盘
钩子（SetWindowsHookEx）**无法区分事件来源**——基于钩子的方案必然互相干扰
（实测过把笔记本 Enter 改写成空格的惨案）。本项目按键引擎走 **Raw Input 按设备
过滤**（VID 2717/PID 32B8），笔记本键盘事件根本进不了分发，这是架构上免疫。

### 二轮攻坚新增坑（2026-08-28/29，语音桥接与实时输入）

- **主动 MIC_OPEN 会堵死物理流**：会话开始就发 `0C 00` 开麦，在固件 2671 上
  会打开一条无声的"主机流"，与按住语音键触发的物理流互斥——症状是每段录音
  固定收到约 1.9 秒与说话内容无关的帧。修复：**绝不在 begin 时主动开麦**，
  只被动收物理流（言灵源码 `physical_stream_must_release_before_host_open`
  印证）。注意别和另一个坑搞混：完全不响应固件的 `0x08` 开麦请求也不行，
  响应式开麦（收到请求才回 `0C 00`）是必要的
- **MIC_CLOSE 后音频通知订阅悄悄失效**：第二段起收不到任何帧。修复=按言灵
  REOPEN RESET 序列重订阅（取消订阅 → CCCD=None → 停 180ms → 重新 NOTIFY）
- **PortAudio/WASAPI 堆损坏（0xc0000374 连崩）**：15ms 小块高频 write +
  每段会话开关流会踩崩 libportaudio。修复=虚拟声卡流**常开** + ~60ms 批量
  大块写
- **AUDCLNT_E_OUT_OF_ORDER**：多个线程并发写同一条 CABLE 流触发。播放必须
  互斥（含"上一段没播完就丢弃下一段"的兜底）
- **微信输入法防误触只认硬件按键**：只要有真实硬件键处于按下状态（比如按住
  的语音键 F5），它就拒绝响应热键；SendInput 注入的按键不会触发防误触，
  但注入"假松开"也清不掉它。这意味着**"按住语音键的同时唤起输入法"在热键
  路径上被物理堵死**
- **本机 LL 钩子先于 Raw Input 分发**（实测快约 1.2ms），且钩子里吞掉的键
  Raw Input 也收不到——"在低层钩子吞 F5"会直接饿死依赖 Raw Input 的语音
  触发。要做设备区分只能把触发源一起搬进钩子层（参考 `miremote/llhook.py`）
- **PyInstaller 错收 Qt DLL**：某些桌面软件会把自有运行时目录塞进 PATH，
  打包时被错收导致 `ImportError: DLL load failed ... QtCore`。spec 里按
  关键字剔除污染的 PATH 条目（见 `miremote.spec`）

### 实时输入（按下即出字）：已尝试、暂未攻克

两个 AI Agent 各自独立攻了两天，全部路线汇总失败。核心死结是两条物理约束
的交集：**遥控器固件只在语音键按住期间才发音频流** × **输入法在硬件键按住
期间拒绝热键**——想在"按住期间"唤起输入法，热键路径走不通；Gadget 报文
级抹除 F5、低层钩子吞键、80ms 切换式热键、pre-roll 缓冲等都试过，输入法
面板在按住期间始终不稳定出现。源码里保留了整套 dormant 实验实现（环境变量
`MIREMOTE_REALTIME_DEV=1` 启用，正式包不启用），`tools/` 里有三个诊断探针。
细节档案（两边的完整尝试清单）见作者博客/交接文档，欢迎带着新思路来挑战。

### 更多

完整坑清单和调试方法论见 [docs/AGENT_GUIDE.md](docs/AGENT_GUIDE.md) §5-§7，
开发全过程记录见 [docs/开发说明.md](docs/开发说明.md)（中文）。

## 已知限制

- 返回键拦截需要 UAC 提权（开机自启场景会弹窗，计划任务方案可规避，见 roadmap）
- 语音键/方向键透传有副作用（焦点在浏览器时 F5=刷新）
- 微信语音模式是"松手后出字"（约 0.45s + 说话时长 + 0.35s 的固有延迟）；
  "按下实时出字"两个 Agent 攻了两天未攻克，原因见上面踩坑记录
- 只在 RC003（固件 2671）+ 一台 RTX 4060 机器上验证过
- 转写准确率：whisper medium 约 90%+，说"登录"偶尔变"灯露"

## Roadmap（想折腾的方向）

- [ ] 开机自启 UAC 优化（计划任务以最高权限运行）
- [ ] FunASR/SenseVoice 替换 whisper（中文 CER 约一半、快 12 倍）
- [ ] 语音子进程常驻池（降低松手→出字延迟）
- [x] ~~连续听写（松手不断流）~~ → **11 个版本攻坚后放弃，最终回退松手播放**
      （2026-08，两个独立 AI Agent + 三个复盘 AI + 主开发共四轮）：固件侧
      主机流无音频且与物理流互斥、60s 单段上限；输入法侧防误触/面板累积/
      通信模式静音等闭源行为无法从系统外保证。**完整复盘（四层根因/全部
      尝试时间线/方法论教训/可复用遗产）见
      [docs/实时输入攻坚复盘-LIVE2_POSTMORTEM.md](docs/实时输入攻坚复盘-LIVE2_POSTMORTEM.md)**；
      dormant 代码（`MIREMOTE_REALTIME_DEV`）与诊断探针在仓库内，欢迎新思路
- [ ] 多遥控器/其他型号支持（需 learn 模式采集 usage 表）

## 致谢与许可

### 参考项目

- **[richlearntodo-debug/vibe-flow（言灵 Vibe Flow）](https://github.com/richlearntodo-debug/vibe-flow)**（GPL-3.0）
  同代际的 Windows 遥控器 vibe coding 工具（C#/.NET），功能更完整、有安装器和持续维护：
  连续听写（主机流 + 8 秒 MIC_EXTEND 心跳，实测通过 15 分钟长听写）、微信/系统语音/
  Typeless 等多转写客户端、默认麦克风三角色自动路由、七段自检。**本项目后期的很多
  验证是在其源码协助下完成的**（连续听写协议细节、哑键失败原因分析），推荐想要
  "开箱即用完整体验"的用户优先尝试言灵；两个项目对 RC003 的逆向结论互相印证。
- **[xxb26553663-star/remote-bridge-hub](https://github.com/xxb26553663-star/remote-bridge-hub)**（GPL-3.0）
  哑键救回方案（Frida Gadget 注入 WUDFHost、IOCTL 过滤、usage 表、注入器结构）源自该项目。
- **[fanxeon/mi-ao](https://github.com/fanxeon/mi-ao)**
  ATVV 语音协议（UUID、握手字节、opcode、ADPCM 帧格式）参考其协议文档（docs/PROTOCOL.md）。
- **[nijez/open-voice-bridge](https://github.com/nijez/open-voice-bridge)**
  Windows UI（PySide6）参考；其文档确认了返回键被 Windows 驱动丢弃的问题。
- **[godarrenw/mi_remote_control](https://github.com/godarrenw/mi_remote_control)**
  macOS 同类项目，桥接架构参考。

### 依赖

[faster-whisper](https://github.com/SYSTRAN/faster-whisper) ·
[whisper](https://github.com/openai/whisper) ·
[PySide6/Qt](https://www.qt.io) ·
[Frida](https://frida.re) ·
[PyInstaller](https://pyinstaller.org) ·
[sounddevice/PortAudio](https://python-sounddevice.readthedocs.io) ·
[pywinrt](https://github.com/pywinrt/pywinrt)

### 许可

- 本项目代码采用 **MIT** 许可（见 [LICENSE](LICENSE)）；
  例外：`miremote/tapinject.py` 与 `miremote/backkey.py` 衍生自
  GPL-3.0 项目 remote-bridge-hub，这两个文件以 **GPL-3.0** 提供。
- "Frida"、"VB-CABLE"、"微信输入法/WeType"、"小米/Xiaomi"为各自所有者的
  商标/产品，本项目与它们无隶属关系。

---

# English

## Features (verified on real hardware)

| Feature | Notes |
|---|---|
| Button capture | D-pad / OK / Home / Menu / TV / Power / Voice via Raw Input, filtered by device (physical keyboard untouched) |
| **Dead-key recovery** | **Back / Volume± HID reports are silently dropped by the Windows driver** — recovered by injecting a Frida Gadget into WUDFHost (tap4 protocol: concurrent-connection guard + control handshake; not even the macOS projects do this) |
| Local voice | Hold-to-talk → release → ATVV Bluetooth decode → faster-whisper local transcription → paste (fully offline) |
| WeChat voice mode | After release, the whole utterance is bridged to WeType IME recognition (auto-removes filler words); requires VB-CABLE, WeType mic set to CABLE Output, and WeType's hold-to-talk hotkey set to Ctrl+Alt+V |
| Qt GUI | Visual key remapping (click the remote picture) + voice mode switch + log + system tray |
| Regression tests | 27 pytest cases covering the dead-key protocol / Gadget version negotiation / voice chain (`tests/`) |
| Boot autostart | Silent background start + service auto-launch |

> Status: tap4 stable, 2026-08-29. The big voice-chain bug is fixed: sessions
> **no longer send MIC_OPEN proactively** — on firmware 2671 a proactive
> opens a silent "host stream" that blocks the real physical stream (symptom:
> every utterance was a fixed ~1.9 s of useless audio). The post-MIC_CLOSE
> audio-notify subscription loss is fixed too, via vibe-flow's REOPEN RESET
> sequence.

## How It Works

```
① Buttons: remote HID → Raw Input (VID/PID filter) → action system (SendInput/clipboard/…)
② Voice:   remote ATVV (GATT) → ADPCM decode → whisper (local) or CABLE bridge (WeChat)
③ Dead keys: Gadget hook inside WUDFHost → localhost:30685 → decoded key edges
```

Key technical details:

- **ATVV protocol**: the remote's microphone streams over Google's ATVV private
  GATT service (`AB5E0001-...`). Windows does not expose it as a microphone, so
  this project implements the full chain — from the `GET_CAPS` handshake to
  IMA ADPCM decoding.
- **Dead keys**: the Back key usage (0xF1) and volume usages (0x80/0x81) arrive
  at the HidOverGatt driver but cannot be translated into keyboard events, so
  Windows drops them. A frida-gadget DLL (official release, dual SHA-256
  pinned) is injected into WUDFHost to hook `NtDeviceIoControlFile` and
  recover the reports.
- **Architecture lesson from the WeChat bridge**: the remote's voice key
  passes through as F5, and WeType's anti-mistouch logic **rejects hotkeys
  while any real hardware key is held** (injected keys don't trigger the
  guard, but a fake "release" can't clear it either). The final design is
  release-playback: zero injection while recording → after release, hold
  Ctrl+Alt+V (WeType's hold-to-talk hotkey) → play the utterance into
  VB-CABLE → release → WeType types the text. Want press-to-text in real
  time? Two hard constraints block it — see the new pitfalls section below.

## Quick Start

### Run from source

```bat
:: Requires Python 3.10+ (developed and tested on 3.14)
pip install PySide6-Essentials faster-whisper nvidia-cublas-cu12 nvidia-cudnn-cu12 ^
            winrt-windows-devices-bluetooth winrt-windows-storage-streams sounddevice pyinstaller

python -m miremote app
```

Pair the remote via Bluetooth (hold **Home + Menu** to enter pairing mode),
then start the service. Full walkthrough in [docs/使用说明.md](docs/使用说明.md) (Chinese).

### Build the exe

```bat
:: Optional: dead-key recovery needs the Frida Gadget (~7MB, not committed)
curl -L -o assets\frida-gadget-17.15.3-windows-x86_64.dll.xz ^
  https://github.com/frida/frida/releases/download/17.15.3/frida-gadget-17.15.3-windows-x86_64.dll.xz

pyinstaller miremote.spec --noconfirm
```

## Pitfalls Log (the most valuable part of this doc)

Every pitfall below was hit for real during a single day of development:

### The ctypes quadruple trap (same class of bug, four times)

**Every Win32 API that returns a HANDLE/pointer must declare
`restype=ctypes.c_void_p` explicitly** — otherwise the 64-bit handle is
truncated to 32 bits with wildly varying symptoms:

| API | Symptom after truncation |
|---|---|
| `HWND` | WinError 1400 invalid window handle |
| `GetCurrentProcess` | WinError 6 invalid handle |
| `GlobalLock` | memmove writes to NULL → access violation |
| `GetClipboardData` | same |

### SendInput failing silently (a month-long latent bug)

The `INPUT` struct's union must contain **all three of MOUSEINPUT +
KEYBDINPUT + HARDWAREINPUT**. With only KEYBDINPUT, `sizeof(INPUT)` is 32
(x64 expects 40) and `SendInput` **silently returns 0** — every key action
dead with zero errors. Arrow keys pass through natively, which is why it
went unnoticed.

### The voice key passes through as F5 and breaks WeChat hotkeys

While the remote's voice button is held, F5 keeps passing through to the
focused window, and WeChat's hotkey detection (a hand-rolled state machine,
likely on a low-level hook) refuses to trigger with extra keys held. Confirmed
by experiment; fixed with the toggle-style architecture.

### Never ShowWindow a Qt window from outside its process

Calling `ShowWindow` on a Qt window from another process → Qt never learns
about it → no repaint → **blank window**. Cross-process revival must let Qt
call `show()` itself (this project uses a TEMP-dir flag file + 500ms polling).

### whisper subprocesses need a clean environment

The host shell's profile (starship/nvm …) pollutes env vars and crashes
ctranslate2 during CUDA model load (access violation). Fix: run transcription
in a clean-env subprocess (minimal vars + `HF_HUB_OFFLINE=1` + explicit UTF-8
decoding). Related trap: never pass empty-string env vars
(`HF_HOME=""` breaks cache resolution).

### PyInstaller notes

- Packaging a package's `__main__.py` directly breaks relative imports —
  use a top-level `launcher.py` entry
- `faulthandler.enable()` crashes under frozen (`sys.stderr is None`) —
  guard with a frozen check
- faster-whisper's `silero_vad_v6.onnx` is not collected automatically —
  collect it explicitly in the spec
- frozen subprocesses must use `[sys.executable, "subcommand", ...]`,
  not script paths
- **Custom exe arguments must be registered in `__main__.main()`** — an
  unregistered `--silent` printed help and exited instantly, faking a broken
  boot autostart

### The remote's ATVV firmware module can wedge (soft-recoverable)

Symptoms: all buttons keep working (the SoftDevice is alive) but voice receives
zero audio frames — or only a few "tail" frames (0–8 frames = 0–120 ms) at the
moment you release the voice key. Log signature: host stream fails to open and
the firmware keeps reporting "physical stream not released". Most likely caused
by a leftover suspended mic session from an unclean exit (process killed without
MIC_CLOSE). Fix: **long-press the remote's TV button to reconnect** (on a PC it
is used as a keyboard, so the reconnect key is TV — not Home+Menu, which is the
TV-scenario combo). For stubborn cases, remove and re-pair the device in
Windows Bluetooth settings.

### The power key's true identity: VK_NONE (not a dead key)

The RC003 power key (keyboard-page usage 0x66) does reach Windows, but
HidOverGatt maps it to no virtual key at all — it shows up in Raw Input as
VK=0 (VK_NONE). So the power key works natively without any injection; this
project dispatches it as `VK_NONE` (see `service.py` default config).

### The "mute key" is a ghost

The HID usage table lists 0x7F (mute), and early configs included it, but the
RC003 hardware only has two volume rocker keys — 0x7F has never been observed
on a real device. Removed from the default config to avoid misleading others.

### Why this project never fights the laptop keyboard

RC003's OK/Home/TV keys share exact VK+scancode with laptop Enter/Home/~, and a
low-level keyboard hook (SetWindowsHookEx) **cannot tell which device an event
came from** — hook-based designs inevitably cross-fire (we reproduced a laptop
Enter turning into Space). This project's key engine runs on **Raw Input with
per-device filtering** (VID 2717/PID 32B8): laptop keyboard events never enter
the dispatch. Architectural immunity.

### Round-two pitfalls (2026-08-28/29, WeChat bridge & realtime input)

- **Proactive MIC_OPEN blocks the physical stream**: sending `0C 00` at session
  start opens a silent "host stream" on firmware 2671 that is mutually
  exclusive with the physical stream — every utterance arrived as a fixed
  ~1.9 s of frames unrelated to speech. Fix: **never open the mic proactively
  in `begin`**; consume the physical stream passively (vibe-flow's
  `physical_stream_must_release_before_host_open` confirms the exclusivity).
  Don't overcorrect: you DO still need to answer the firmware's `0x08` mic
  request — reactive open, not proactive, not none.
- **Audio-notify subscription silently dies after MIC_CLOSE**: from the second
  utterance on, no frames arrive. Fix: resubscribe per vibe-flow's REOPEN
  RESET (unsubscribe → CCCD=None → wait 180 ms → re-notify).
- **PortAudio/WASAPI heap corruption (0xc0000374, twice)**: 15 ms high-frequency
  small writes plus per-session stream open/close crashes libportaudio. Fix:
  keep the virtual-cable stream **open for the process lifetime** and write in
  ~60 ms batches.
- **AUDCLNT_E_OUT_OF_ORDER**: two threads writing the same CABLE stream.
  Playback must be serialized (drop the new utterance if the previous one is
  still playing).
- **WeType anti-mistouch only looks at hardware keys**: with any real hardware
  key held (e.g. the voice key = F5), WeType refuses hotkeys. SendInput keys
  don't trip the guard, but an injected fake "release" can't clear it either.
  So **waking the IME while holding the voice key is physically blocked on the
  hotkey path**.
- **On this machine the LL hook fires BEFORE Raw Input** (~1.2 ms earlier,
  measured), and a key swallowed in the LL hook never reaches Raw Input —
  swallowing F5 in a low-level hook starves any Raw-Input-based voice trigger.
  Device discrimination then requires moving the trigger into the hook layer
  itself (see `miremote/llhook.py`).
- **PyInstaller picking up wrong Qt DLLs**: some desktop apps put their own
  runtime dirs on PATH; the packager collects their DLLs and Qt breaks with
  `ImportError: DLL load failed ... QtCore`. Filter polluted PATH entries in
  the spec (see `miremote.spec`).

### Realtime input (press-to-text): attempted, not solved

Two AI agents attacked this independently for two days; every route failed.
The core deadlock is the intersection of two physical constraints: **the
firmware only streams audio while the voice key is held** × **the IME rejects
hotkeys while a hardware key is held**. Gadget-level report suppression,
LL-hook swallowing, an 80 ms toggle hotkey, and pre-roll buffering were all
tried — the IME panel never appeared reliably during the hold. The full
dormant experimental implementation is preserved in the source (enable with
`MIREMOTE_REALTIME_DEV=1`; release builds don't) together with three
diagnostic probes under `tools/`.

### More

Full pitfall list and debugging methodology in
[docs/AGENT_GUIDE.md](docs/AGENT_GUIDE.md) §5–§7 (Chinese);
the complete development story in [docs/开发说明.md](docs/开发说明.md) (Chinese).

## Known Limitations

- Dead-key recovery requires UAC elevation (a dialog appears on boot;
  a scheduled-task approach can avoid it — see roadmap)
- Voice/d-pad keys pass through with side effects (F5 refreshes a focused browser)
- WeChat voice mode is release-to-text (~0.45 s + utterance length + 0.35 s
  inherent latency); press-to-text realtime was attacked by two agents for two
  days and remains unsolved — see the pitfalls section above
- Only verified on RC003 (firmware 2671) + one RTX 4060 laptop
- Transcription accuracy: whisper medium ≈ 90%+; "登录" (login) occasionally
  becomes "灯露"

## Roadmap (ideas if you want to hack on it)

- [ ] Boot autostart without UAC prompt (scheduled task with highest privileges)
- [ ] Replace whisper with FunASR/SenseVoice (half the CER, 12× faster for Chinese)
- [ ] Persistent transcription worker pool (lower release-to-text latency)
- [x] ~~Continuous dictation (no stream break on release)~~ → **attempted
      across 11 builds, abandoned, rolled back to release-playback**
      (2026-08, two independent AI agents + three reviewer AIs + the lead
      developer): the host-owned stream carries no audio and is mutually
      exclusive with the physical stream on this 2671 unit; the IME's
      anti-mistouch, panel accumulation and communication-mode muting are
      unverifiable from outside. **Full postmortem (root causes per layer,
      complete attempt timeline, methodology lessons, surviving assets) in
      [docs/实时输入攻坚复盘-LIVE2_POSTMORTEM.md](docs/实时输入攻坚复盘-LIVE2_POSTMORTEM.md)**
      (Chinese). Dormant code (`MIREMOTE_REALTIME_DEV`) and diagnostic probes
      are in the repo — new ideas welcome
- [ ] Support more remotes (requires usage-table collection via learn mode)

## Credits & License

### Referenced projects

- **[richlearntodo-debug/vibe-flow (言灵 Vibe Flow)](https://github.com/richlearntodo-debug/vibe-flow)** (GPL-3.0)
  A same-generation Windows remote vibe-coding tool (C#/.NET) that is more
  complete and actively maintained: continuous dictation (host stream + 8s
  MIC_EXTEND heartbeats, regression-tested to 15 minutes), multiple dictation
  clients (WeChat / Windows voice typing / Typeless), automatic default-mic
  role routing, and a seven-part self-check. **Much of this project's later
  verification was done with its source as reference** (continuous-dictation
  protocol details, dead-key failure analysis). If you want a polished
  out-of-the-box experience, try vibe-flow first; the two projects
  cross-validate each other's RC003 reverse-engineering.
- **[xxb26553663-star/remote-bridge-hub](https://github.com/xxb26553663-star/remote-bridge-hub)** (GPL-3.0)
  Source of the dead-key recovery approach (Frida Gadget injection into
  WUDFHost, IOCTL filtering, usage table, injector structure).
- **[fanxeon/mi-ao](https://github.com/fanxeon/mi-ao)**
  ATVV voice protocol (UUIDs, handshake bytes, opcodes, ADPCM frame format)
  per their protocol notes (docs/PROTOCOL.md).
- **[nijez/open-voice-bridge](https://github.com/nijez/open-voice-bridge)**
  Windows UI (PySide6) reference; their docs confirmed the driver-dropped
  Back key.
- **[godarrenw/mi_remote_control](https://github.com/godarrenw/mi_remote_control)**
  macOS sibling; bridging architecture reference.

### Dependencies

[faster-whisper](https://github.com/SYSTRAN/faster-whisper) ·
[whisper](https://github.com/openai/whisper) ·
[PySide6/Qt](https://www.qt.io) ·
[Frida](https://frida.re) ·
[PyInstaller](https://pyinstaller.org) ·
[sounddevice/PortAudio](https://python-sounddevice.readthedocs.io) ·
[pywinrt](https://github.com/pywinrt/pywinrt)

### License

- This project's code is released under the **MIT** license (see [LICENSE](LICENSE));
  exception: `miremote/tapinject.py` and `miremote/backkey.py` derive from
  the GPL-3.0 project remote-bridge-hub and are provided under **GPL-3.0**.
- "Frida", "VB-CABLE", "WeType/微信输入法" and "Xiaomi/小米" are trademarks or
  products of their respective owners; this project is not affiliated with them.
