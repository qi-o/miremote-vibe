# v1.3.1：语音连接恢复与构建说明

更新日期：2026-09-19。

## 解决的问题

某些 ATVV 连接故障不会以 Python 异常的形式出现。旧实现虽然有异常后重连，但订阅返回失败状态时仍可能标为成功；连接静默断开或录音中丢失结束信号时，服务也可能一直等待，直到用户手动重连蓝牙。

本版补齐应用侧恢复路径：

- 检查 GATT 发现、控制/音频通知订阅和写命令结果，成功后才置就绪状态。
- 监听连接状态，空闲和录音中的等待可被断线或停止唤醒；每秒读取连接状态作为事件缺失时的补充，不主动开麦保活。
- 回收旧设备、服务和通知回调，以新的会话代次重建；迟到通知不能污染新连接。WinRT 通知交由所属事件循环处理。
- 连接操作上限为 20 秒，握手、订阅和收尾操作上限为 15 秒；失败后采用 2、4、8、16、30 秒的退避重试。这些不是录音总时长限制。
- 停止可取消挂起连接与等待；微信回放使用 50ms 音频块，在持有输出流的线程内收尾，避免写入与关闭并发。

继续使用 `wechat_live=false` 的按住录音、松手回放方式；不启用 live/live2、不改变蓝牙配对或驱动，不自动重启适配器/WUDFHost。设备不再广播、断电、固件停滞或已取消配对时，不能保证自动恢复。

## 日志

日用打包版的结构化恢复日志位于：

```text
%APPDATA%\MiRemoteVibe\logs\voice-recovery.jsonl
```

每份约 1 MiB，保留当前文件和 3 个备份。记录连接状态、会话、错误、重试和计数；不记录音频或识别文本。清空界面日志不会删除文件日志。日志写入失败不应阻断语音恢复。

报告问题时可提供相关时间段的 `voice.connecting`、`voice.ready`、`voice.disconnected`、`voice.retry` 事件，并说明失效的是语音还是所有按键。发布日志前请自行检查错误内容是否含有本机路径或设备信息。

## 构建日用版与候选版

需要 Windows、README 列出的构建依赖和指定版本的 Frida Gadget 压缩包。Gadget 不提交到仓库；文件缺失时完整功能构建会失败，运行时检查会验证压缩包与 DLL 的摘要。

PowerShell：

```powershell
# 日用版：正常配置、自启和单实例身份
.\tools\build_release.ps1

# 隔离候选版：独立配置/日志，手动启动守护，不注册开机自启
.\tools\build_release.ps1 -Profile candidate
```

| | 日用版 | 候选版 |
|---|---|---|
| 输出目录 | `release/` | `candidate/` |
| 配置目录 | `%APPDATA%\MiRemoteVibe` | `%APPDATA%\MiRemoteVibe-VoiceRecovery` |
| 自启/自动守护 | 按配置和界面设置 | 关闭 |
| 实时语音实验 | 关闭 | 关闭 |
| 硬件占用 | 单实例正常运行 | 启动前检查日用版及哑键端口占用 |

专用 runtime hook 将构建身份写入包启动流程。候选版改名不会因此写入日用配置。构建脚本只生成文件并运行导入/资源检查，不安装、替换运行中的程序或修改注册表。

也可直接构建日用版：

```powershell
$env:MIREMOTE_BUILD_PROFILE = 'release'
$env:MIREMOTE_BUILD_NAME = '小米遥控器'
python -m PyInstaller --noconfirm --distpath release --workpath build-release miremote.spec
```

包级检查示例（不启动 GUI、BLE、声卡或注入）：

```powershell
& '.\release\小米遥控器.exe' --runtime-check '.\audit\runtime-check.json'
```

日用版的“开机自动启动”使用当前用户的 `Run` 项和 `--silent`；“打开时自动启动守护”是独立配置。要在登录后自动接管遥控器，两者都需开启。升级前退出旧程序并备份 EXE 和用户配置，避免残留旧版本启动项。

## 其他修复

- 保存配置使用同目录临时文件和原子替换；默认配置的可变对象不跨实例共享。
- 过期手势定时器不再作用于同一按键的新一轮操作。
- 剪贴板写入失败时取消粘贴，避免输入旧剪贴板内容。
- 恢复与现有 UI 裁切匹配的横版手持遥控器图片，修正仓库素材和本机发布素材不一致的问题。

## 验证与边界

在维护者的 Windows 环境执行：

```powershell
python -m pytest -q
python tests\smoke_gestures.py
```

本次验证为 73 项 pytest、37 条独立手势检查通过。涵盖订阅失败、连接卡住、停止、断线后新录音、旧回调隔离、配置/日志和发布身份。测试不需要实际遥控器，但依赖本项目的 Windows Python 环境。

日用 EXE 已完成导入、资源摘要和身份检查；维护者机器上已观察到真实 GATT 连接、通知订阅成功及首页原图恢复。**这些不等于已完成长期闲置、超距返回、睡眠唤醒或所有固件的端到端语音验收。**

## English summary

Version 1.3.1 checks GATT results, detects disconnects during idle and capture,
retires stale sessions, and reconnects with bounded backoff. Cancellation and
shutdown release subscriptions and shared audio resources. WeType remains in
release-then-play mode; live experiments are disabled in daily/candidate builds.

Recovery diagnostics rotate under the corresponding AppData profile and do not
contain audio or dictated text. `tools/build_release.ps1` builds and validates
daily or isolated candidate packages without deploying them. The original
horizontal remote image is restored. Automated tests and connection readiness
do not establish long-running hardware recovery reliability.
