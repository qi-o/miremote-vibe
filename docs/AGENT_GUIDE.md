# AGENT GUIDE：项目技术档案与改进指南

> **English note**: This is the Chinese agent/engineer handoff guide — code
> structure, hard-coded protocol facts (ATVV UUIDs, opcodes, HID usage table,
> registry-based device discovery), the must-read pitfall list (§5), build/test
> commands (§4), and a priority-ranked improvement roadmap (§6). Machine
> translation of this file is usually good enough for the tables and code
> blocks; read README.md (English section) first for context.


> 本文档写给接手开发/改进此项目的 AI agent 或工程师。
> 包含：项目现状、代码结构、硬编码技术事实、已知坑、构建方法、改进方向。
> **改动前务必通读 §3 硬编码事实 和 §5 已知坑——每一条都是实测换来的。**

---

## 1. 项目是什么

把小米蓝牙遥控器 2 Pro（RC003）变成 Windows 上的 vibe coding 遥控器。

**当前完成度（全部真机验证）：**

| 功能 | 状态 |
|---|---|
| 按键捕获（Raw Input 按 VID/PID 过滤） | ✅ |
| 哑键救回（返回/音量±，Frida Gadget 注入 WUDFHost） | ✅ |
| 本地语音（ATVV→ADPCM→whisper→粘贴） | ✅ |
| 微信语音模式（松手播放桥接 WeType+VB-CABLE，Ctrl+Alt+V 按住式） | ✅ |
| Qt GUI（映射编辑/语音切换/日志/遥控器热区图） | ✅ |
| 单文件 exe（241MB PyInstaller） | ✅ |
| 开机静默自启 + 打开即启动守护 | ✅ |
| 托盘隐藏/唤回 | ✅ |

**交付物**：`小米遥控器.exe`（本目录）；源码在 `源码/` 子目录。

## 2. 代码结构

```
launcher.py            # PyInstaller 入口（顶层脚本 import 完整包，规避相对导入问题）
miremote.spec          # 打包配置（注意 datas：gadget xz + remote.jpg + VAD onnx 必须打包）
config.json            # 默认配置模板（运行时配置在 %APPDATA%\MiRemoteVibe\）
miremote/
├── __main__.py        # CLI 入口（devices/learn/run/selftest/gui/app/voice/
│                      #   diagnose/backkey/--inject/--silent 参数分发）
├── service.py         # ★ MiRemoteService：GUI/CLI 共用的守护服务封装
│                      #   （配置加载+迁移、按键引擎+语音+哑键的启停编排）
├── rawinput.py        # Raw Input 引擎（独立线程窗口+消息循环、启停、设备过滤）
├── keys.py            # VK 码表/解析
├── actions.py         # 动作系统（SendInput/剪贴板/聚焦/音量/粘贴）
├── voice.py           # ★ ATVV 客户端 + VoiceDaemon（含双模式：local/wechat）
│                      #   + whisper 子进程转写（干净环境隔离）
├── backkey.py         # 哑键消费端（监听 30685，解码 usage 边沿）
├── tapinject.py       # Frida Gadget 注入器（提权、护栏校验、布置运行时）
├── gui.py             # ★ PySide6 主窗口（4 标签页+托盘显隐+自启+录制键）
├── remote_widget.py   # QPainter 遥控器矢量控件（热区点击）
├── tray_qt.py         # Qt 系统托盘
├── learn_qt.py        # 学习新键对话框
└── diagnose.py        # 按键全通道诊断工具
```

## 3. 硬编码技术事实（实测，勿凭感觉改）

### 设备
- VID 2717 / PID 32B8；固件 2671；蓝牙 MAC C0:5D:39:C2:BE:B8
- 设备路径两种格式：BTHLE `VID&012717_PID&32b8` / USB `VID_2717`，过滤正则要兼容
- 按键→VK：方向=UP/DOWN/LEFT/RIGHT、OK=RETURN、语音=F5(scan 0x3F)、
  TV=0xC0、电源=0xFF、主页=HOME、菜单=APPS

### ATVV 语音协议
- 服务 `AB5E0001-5A21-4F05-BC7D-AF01F617B664`（TX=AB5E0002、音频=AB5E0003、控制=AB5E0004）
- 握手 `0A 01 00 00 03 03` → `0B 01 00 02 03 00 78 00 00`（16kHz ADPCM、120B 帧）
- 实测 opcode：`00 02`=松手停止、`00 00`=已关闭、`04 00 02 00`=mic 激活
- 音频：IMA/DVI ADPCM 16kHz，v1.0 帧 120B 无头（v0.4 是 134B 带头）

### 哑键（被 Windows 丢弃的键）
- usage：Back=0xF1、音量=0x80/0x81/0x7F（报文在 WUDFHost 的 IOCTL 0x80018483 里）
- 注入：frida-gadget 17.15.3（archive SHA b566d701...、DLL SHA 6fca4007...）
- WUDFHost PID 从注册表 `BTHLEDevice\{00001812-...}\...\WUDFDiagnosticInfo\HostPid` 读
- frida-python attach 会被 WUDFHost 拒（ProcessNotRespondingError），必须 Gadget DLL

### 微信模式（切换式架构）
- WeType 浮窗进程=微信输入法（非微信聊天客户端）；其设置可切语音麦克风
- 2026-08-29 起：热键=WeType「按住说话」**Ctrl+Alt+V**，程序按住式 down/up；
  旧切换式 Ctrl+Win+Shift 已弃用。防误触约束不变：硬件键按住期间 WeType 拒绝热键，
  实时（按下即出字）路线两日攻坚未果，见仓库 README「实时输入」节与桌面失败档案
- CABLE 设备 44.1/48kHz，16k 音频需线性重采样（voice.py _wechat_playback）

### 运行环境
- Python 3.14（ctranslate2 4.8.1+ 有 wheel）；RTX 4060（需 nvidia-cublas/cudnn-cu12 pip 包）

## 4. 构建 / 测试 / 打包

```bat
:: 源码运行 GUI
python -m miremote app

:: 无桌面验证 GUI 构建（offscreen）
set QT_QPA_PLATFORM=offscreen
python -c "from PySide6 import QtWidgets; from miremote.gui import MiRemoteWindow; a=QtWidgets.QApplication([]); MiRemoteWindow().show(); print('OK')"

:: 打包单文件 exe（产物 dist\，复制到发布目录）
pyinstaller miremote.spec --noconfirm
```

验证 exe：后台启动 + 查进程存活 + `exe devices` 子命令有输出。

## 5. 已知坑（改动前必读）

1. **ctypes：所有返回 HANDLE/指针的 API 必须 restype=c_void_p**（本项目踩了 4 次）
2. **SendInput 的 INPUT union 必须含全部三种输入结构**，否则 cbSize 不匹配静默返回 0
3. **Win32 消息循环必须在创建窗口的同一线程**
4. **ShellExecuteW "runas" 丢工作目录**：helper 必须 exe 自调用或绝对路径
5. **frozen 环境**：faulthandler.enable() 会 sys.stderr is None 崩溃（加 frozen 判断）；
   子进程要用 `[sys.executable, "子命令", ...]` 而不是脚本路径
6. **whisper 子进程要干净环境**（宿主 shell 配置污染会导致 CUDA 原生崩溃）+
   HF_HUB_OFFLINE=1 + 显式 UTF-8；空字符串环境变量不能传
7. **Qt 窗口不能被外部 ShowWindow 显示**（空白）；跨进程唤回用 %TEMP%\miremote_show.flag
   文件 + 500ms 轮询（QFileSystemWatcher 在 TEMP 上不触发）
8. **exe 参数必须在 __main__.main() 登记**（--silent/--inject），否则打印帮助秒退
9. **对话框必须 exec()**；QtTest 自动化测试在 PySide6.QtTest 模块
10. Mimosa 类安全钩子会拦 bash 内联 ctypes 和直接写源码的 cp——复用已过审代码

## 6. 改进方向（按性价比排序）

1. **开机自启的 UAC 优化**：哑键注入需要提权，开机自启场景每次弹 UAC 破坏静默。
   方案：改用**计划任务（以最高权限运行）**替代 HKCU Run，一次授权永久静默。
2. **转写准确率**：本地 medium 约 90%+。可试 FunASR/SenseVoice（中文 CER 约为
   whisper 一半、快 12 倍）或智谱云 API（用户环境已有 ZHIPU_API_KEY）。
3. **语音延迟**：微信模式松手后需 播放时长+识别时间；本地模式约 3s（子进程冷启动
   占大头，可做常驻 worker 池）。
4. **按键透传副作用**：语音键=F5、方向键会在焦点窗口真实输入（浏览器里刷新/滚动）。
   终极方案是低级键盘钩子按设备拦截（LL hook 无设备信息，需 RawInput 时间窗关联，
   项目早期调研过，复杂度高所以 v1 未做）。
5. **配置热更新**：改键位映射需重启守护；可在 service 里加重载。
6. **多遥控器/其他型号**：usage 表是 RC003 实测，其他型号需 learn 模式重新采集。

## 7. 调试工具箱（本项目中验证过的方法）

- **EnumWindows 快照 diff**：探测浮窗/窗口出现消失（配合 GetWindowThreadProcessId 查归属）
- **IMA ADPCM 编码器**（解码逆运算）：把任意 WAV 编回遥控器帧格式，
  可脱离蓝牙做语音管线全真模拟（tools/ 下有原型）
- **GetAsyncKeyState**：验证 SendInput 是否真的注入（沙箱内也有效）
- **faulthandler**：原生崩溃（CUDA DLL）也能打印崩溃点（仅源码模式）
- 沙箱限制参考：鼠标注入被拦（SetCursorPos）、后台 SetForegroundWindow 被前台锁定拦

---

*最后更新：2026-08-24。文档与代码同步，改代码请同步更新此文档。*
