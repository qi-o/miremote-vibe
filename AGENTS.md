# 仓库协作边界(v2.0,裁剪版——参照 SayAll AGENTS.md 文化,按 Python 单仓现实定制)

> 本文件是给 AI Agent 与人类贡献者的成文约定。专项细节看 `docs/`,
> 真机验收状态看 `Testing/`。

## 架构边界

- **纯逻辑层零 IO**:`gestures.py`/`reconnect.py`/`telemetry.py` 及 voice.py 里的
  ADPCM/协议解析,不得 import Windows API、不得做网络/文件 IO(单测必须可离线跑)。
- **平台层隔离**:`rawinput.py`/`llhook.py`/`backkey.py`/`leaksup.py`/`actions.py`
  承载全部 Win32/ctypes 交互;纯逻辑模块不得反向依赖它们。
- **提权独立**:Frida 注入(tap4)必须保持可独立运行(`python -m miremote backkey`),
  主守护与 GUI 保持普通用户权限。
- **结构化日志**:新代码路径用 `telemetry.note(feature, action, **kv)`;只记状态
  与结果,不记绝对路径/蓝牙地址/设备标识。

## 成对与生命周期

- 任何 DOWN/UP、订阅/退订、开流/关流、注入/释放必须严格成对;异常与提前退出
  路径同样要释放(参考 voice.py 的 live 会话代际与回滚、actions.hold_chord_spaced
  的失败回滚)。
- 重试前先释放上一轮资源;断连/睡眠/配置变化必须幂等处理。
- 语音键不得引入双击等待或长按阈值(按住说话语义独占)。

## 第三方边界

- 不读取/修改第三方应用(微信输入法/微信 PC 等)的私有配置、内存或私有协议;
  只允许窗口标题检测、公开注册表(ConsentStore 只读)、热键注入与虚拟声卡。
- 注入必须免疫自家(LLKHF_INJECTED 放行)且失败可回滚。

## 测试与验收

- 纯逻辑改动:pytest + `tests/smoke_gestures.py` 全过是合并底线。
- 涉及真机(蓝牙/注入/输入法):自动化过了不算过,按 `Testing/手测清单.md`
  标注 deferred,真机验收后改 passed;结论写进 `Bugs/`(新 bug 建档)。
- 修 bug 先写复现与证据链,再写修复;已有错误结论不删,追加勘误。
