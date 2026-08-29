"""守护服务封装：按键捕获 + 语音引擎 + 哑键拦截，整合成可启停的服务对象。

GUI 和命令行共用本类。服务运行在后台线程，通过回调把日志/状态报给 UI。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

from . import actions
from .keys import vk_name
from .rawinput import RawInputEngine


def realtime_dev_build() -> bool:
    if _env_bool("MIREMOTE_REALTIME_DEV"):
        return True
    return bool(
        getattr(sys, "frozen", False)
        and any(
            marker in Path(sys.executable).stem
            for marker in ("实时实验版", "实时输入开发版")
        )
    )


def app_data_dir() -> Path:
    """配置/日志/模型的用户目录（打包后 exe 目录可能只读）。"""
    if getattr(sys, "frozen", False):
        base = os.environ.get("APPDATA", str(Path.home()))
        name = "MiRemoteVibe-RealtimeDev" if realtime_dev_build() else "MiRemoteVibe"
        return Path(base) / name
    return Path(__file__).resolve().parent.parent


def config_path() -> Path:
    return app_data_dir() / "config.json"


def _env_bool(name: str):
    value = os.environ.get(name)
    if value is None:
        return None
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _apply_env_overrides(cfg: dict) -> dict:
    if realtime_dev_build():
        cfg["voice_mode"] = "wechat"
        cfg["wechat_live"] = True
        cfg["wechat_hotkey"] = ["VK_CONTROL", "VK_LWIN", "VK_SHIFT"]
        cfg["hide_tray"] = False
        cfg["voice_diagnostics"] = True
    mode = os.environ.get("MIREMOTE_VOICE_MODE")
    if mode in {"local", "wechat"}:
        cfg["voice_mode"] = mode
    live = _env_bool("MIREMOTE_WECHAT_LIVE")
    if live is not None:
        cfg["wechat_live"] = live
    hide_tray = _env_bool("MIREMOTE_HIDE_TRAY")
    if hide_tray is not None:
        cfg["hide_tray"] = hide_tray
    auto_start = _env_bool("MIREMOTE_AUTO_START_SERVICE")
    if auto_start is not None:
        cfg["auto_start_service"] = auto_start
    return cfg


DEFAULT_CONFIG = {
    "device": {"vid": "2717", "pid": "32B8", "hid_tap": True, "voice": True},
    "voice_mode": "local",      # "local"=本地 whisper 转写；"wechat"=桥接输入法
    "voice_model": "medium",
    # 微信输入法"按住说话"快捷键（2026-08-28 与用户 WeType 设置对齐：
    # Ctrl+Alt+V，避开系统/输入法冲突组合；WeType 侧需在 语音输入 设置里
    # 给"按住说话"配置同样组合）。注意 WeType 快捷键必须含修饰键。
    "wechat_hotkey": ["VK_CONTROL", "VK_MENU", "VK_V"],
    # 播放策略：False=松手后整段重放（v2，8-24 验证过、不依赖吞键钩子）；
    # True=ATVV 驱动的实时实验模式（开发版强制开启；稳定版默认关闭）
    "wechat_live": False,
    "wechat_ready_delay": 0.45,   # 面板热键后等输入法就绪的秒数（丢了开头可调大）
    "voice_diagnostics": False,   # 调试时覆盖保存最后一段 WAV/ADPCM/指标
    "keys": {
        "VK_UP": {"label": "↑", "on_down": {"type": "none"}},
        "VK_DOWN": {"label": "↓", "on_down": {"type": "none"}},
        "VK_LEFT": {"label": "←", "on_down": {"type": "none"}},
        "VK_RIGHT": {"label": "→", "on_down": {"type": "none"}},
        "VK_RETURN": {"label": "OK", "on_down": {"type": "none"}},
        "VK_F5": {"label": "语音", "on_down": {"type": "voice"}},
        "VK_HOME": {"label": "主页", "on_down": {"type": "none"}},
        "VK_APPS": {"label": "菜单", "on_down": {"type": "none"}},
        "VK_0xC0": {"label": "TV", "on_down": {"type": "keys", "combo": ["VK_MENU", "VK_TAB"]}},
        "VK_NONE": {"label": "电源", "on_down": {"type": "none"}},
        "TAP_BACK": {"label": "返回", "on_down": {"type": "tap", "key": "VK_ESCAPE"}},
        "TAP_VOLUME_UP": {"label": "音量+", "on_down": {"type": "volume", "delta": 1}},
        "TAP_VOLUME_DOWN": {"label": "音量-", "on_down": {"type": "volume", "delta": -1}},
    },
    "hide_tray": False,   # 隐藏系统托盘图标（程序仍在后台，双击 exe 唤回）
}

# 动作类型元数据（GUI 编辑用）
ACTION_TYPES = [
    ("none", "无操作", {}),
    ("tap", "按一个键", {"key": "VK_ESCAPE"}),
    ("keys", "组合键", {"combo": ["VK_CONTROL", "VK_SHIFT", "VK_M"]}),
    ("volume", "音量", {"delta": 1}),
    ("voice", "语音（按住说话）", {}),
    ("type", "输入文本", {"text": ""}),
    ("focus_then_keys", "聚焦窗口后按键", {"title_regex": "", "combo": []}),
    ("run", "运行程序", {"argv": []}),
]


def action_summary(action: dict) -> str:
    """动作 -> 一行中文简述（列表展示用）。"""
    t = action.get("type", "none")
    if t == "none":
        return "无操作"
    if t == "tap":
        return f"按键 {action.get('key', '?')}"
    if t == "keys":
        return "+".join(action.get("combo", [])) or "组合键"
    if t == "volume":
        d = action.get("delta", 0)
        return "音量+" if d > 0 else ("音量-" if d < 0 else "静音")
    if t == "voice":
        return "语音（按住说话）"
    if t == "type":
        return f"输入文本（{len(action.get('text', ''))}字）"
    if t == "focus_then_keys":
        return f"聚焦[{action.get('title_regex', '')}]+按键"
    if t == "run":
        return f"运行 {action.get('argv', [])}"
    return str(t)


class MiRemoteService:
    def __init__(self, on_log=print, on_status=None):
        self.on_log = on_log
        self.on_status = on_status
        self.config = self._load_config()
        self.running = False
        self._lock = threading.Lock()
        self._held: set = set()
        self._engine: RawInputEngine | None = None
        self._voice = None
        self._tap = None
        self._llhook = None

    # ---- 配置 ----
    def _load_config(self) -> dict:
        p = config_path()
        if not p.exists() and realtime_dev_build():
            stable = Path(os.environ.get("APPDATA", str(Path.home()))) / "MiRemoteVibe" / "config.json"
            if stable.exists():
                p = stable
        if p.exists():
            try:
                cfg = json.loads(p.read_text(encoding="utf-8"))
                for k, v in DEFAULT_CONFIG.items():
                    cfg.setdefault(k, v)
                # 迁移：热键归一到 Ctrl+Win 两键按住式（旧三键切换式会触发两个录音）
                if cfg.get("wechat_hotkey") != DEFAULT_CONFIG["wechat_hotkey"]:
                    cfg["wechat_hotkey"] = list(DEFAULT_CONFIG["wechat_hotkey"])
                # 迁移：补齐后来新增的按键映射（老配置里没有的键）
                for k, v in DEFAULT_CONFIG["keys"].items():
                    cfg.setdefault("keys", {}).setdefault(k, json.loads(json.dumps(v)))
                # 迁移：移除幽灵静音键（RC003 实体没有这个键，usage 0x7F 从未出现）
                cfg.setdefault("keys", {}).pop("TAP_VOLUME_MUTE", None)
                # 迁移：修正 learn 阶段标错的 VK_HOME 标签
                home = cfg.get("keys", {}).get("VK_HOME")
                if isinstance(home, dict) and home.get("label") == "返回":
                    home["label"] = "主页"
                return _apply_env_overrides(cfg)
            except (json.JSONDecodeError, OSError):
                pass
        return _apply_env_overrides(json.loads(json.dumps(DEFAULT_CONFIG)))

    def _start_f5_hook_if_needed(self, eng: RawInputEngine):
        dev = self.config.get("device", {})
        if not (
            dev.get("voice", True)
            and self.config.get("wechat_live", False)
            and self._voice is not None
            and self._voice.ready
        ):
            return
        try:
            from .llhook import F5SuppressHook
            self._llhook = F5SuppressHook(
                eng,
                voice_probe=self._voice.atvv_recent,
                on_voice=self._voice.note_remote_f5,
                log=lambda m: self.on_log(f"[f5] {m}"),
                eager_resolve=False,
            )
            self._llhook.start()
        except Exception as e:
            self.on_log(f"F5 吞键不可用: {e}")

    def save_config(self, cfg: dict | None = None):
        if cfg is not None:
            self.config = cfg
        p = config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.config, ensure_ascii=False, indent=2),
                     encoding="utf-8")

    # ---- 生命周期 ----
    def start(self) -> bool:
        with self._lock:
            if self.running:
                return True
            try:
                self._start_inner()
            except Exception as e:
                self.on_log(f"启动失败: {e}")
                import traceback
                self.on_log(traceback.format_exc())
                self._cleanup()
                return False
            self.running = True
            self._emit_status()
            return True

    def _start_inner(self):
        dev = self.config.get("device", {})
        keys = self.config.get("keys", {})

        # 1) 按键引擎
        eng = RawInputEngine(vid=dev.get("vid", "2717"), pid=dev.get("pid", "32B8"),
                             name_contains=dev.get("name_contains"))
        eng.on_key = self._on_raw_key
        eng.start_background()
        self._engine = eng

        # 2) 语音引擎
        if dev.get("voice", True):
            try:
                from .voice import VoiceDaemon

                def on_text(text: str):
                    actions.type_text(text)
                    self.on_log(f"语音已输入: {text}")

                mode = self.config.get("voice_mode", "local")
                vd = VoiceDaemon(
                    on_text=on_text, log=lambda m: self.on_log(f"[语音] {m}"),
                    model=self.config.get("voice_model", "medium"),
                    mode=mode,
                    wechat_hotkey=self.config.get("wechat_hotkey"),
                    live=self.config.get("wechat_live", True),
                    ready_delay=self.config.get("wechat_ready_delay", 0.45),
                    diagnostics=self.config.get("voice_diagnostics", False),
                    diagnostics_root=app_data_dir() / "diagnostics",
                )
                self._voice = vd
                if vd.start():
                    if mode == "wechat":
                        playback_mode = "实时模式" if self.config.get("wechat_live", False) else "松手播放模式"
                        self.on_log(f"语音引擎就绪（微信输入法{playback_mode}）：按住语音键说话，"
                                    "松手出字")
                    else:
                        self.on_log("语音引擎就绪（本地模式）：按住【语音键】说话，"
                                    "松手后文字打进焦点窗口")
                else:
                    self.on_log("语音设备暂未就绪，后台会自动重试；连接成功后即可按住语音键说话")
            except Exception as e:
                self.on_log(f"语音引擎不可用: {e}")

        # 2b) 实时模式需要吞掉 RC003 暴露出的 F5。必须等 VoiceDaemon
        # 已创建后再启动，否则第一段会因为 voice_probe 为空被误判成笔记本 F5。
        self._start_f5_hook_if_needed(eng)

        # 3) 哑键拦截
        if dev.get("hid_tap", True):
            try:
                from .backkey import BackKeyTap

                TAP_KEYS = {
                    "back": "TAP_BACK", "volume_up": "TAP_VOLUME_UP",
                    "volume_down": "TAP_VOLUME_DOWN",
                }

                def on_edge(name: str, is_down: bool):
                    if not is_down:
                        return
                    key = TAP_KEYS.get(name)
                    conf = keys.get(key) if key else None
                    if conf is None:
                        return
                    self._run_action(conf, conf.get("label", name), f"tap:{name}")

                self._tap = BackKeyTap(
                    on_edge=on_edge,
                    log=lambda m: self.on_log(f"[tap] {m}"),
                    suppress_voice=False,
                )
                self._tap.start()
                self.on_log("哑键拦截已启动（首次会弹 UAC 提权）")
            except Exception as e:
                self.on_log(f"哑键拦截不可用: {e}")

    def stop(self):
        with self._lock:
            if not self.running:
                return
            self._cleanup()
            self.running = False
            self._emit_status()

    def _cleanup(self):
        if self._llhook:
            try:
                self._llhook.stop()
            except Exception:
                pass
            self._llhook = None
        if self._tap:
            try:
                self._tap.stop()
            except Exception:
                pass
            self._tap = None
        if self._voice:
            try:
                self._voice.stop()
            except Exception:
                pass
            self._voice = None
        if self._engine:
            try:
                self._engine.stop()
            except Exception:
                pass
            self._engine = None

    # ---- 按键处理 ----
    def _run_action(self, conf: dict, label: str, source: str):
        action = conf.get("on_down", {"type": "none"})
        if action.get("type") == "voice":
            if self._voice:
                self._voice.begin()
                self.on_log(f"{label} -> 开始录音（按住说话）")
            else:
                self.on_log(f"{label} -> 语音引擎未就绪")
            return
        desc = actions.perform(action)
        self.on_log(f"{label} ({source}) -> {desc}")

    def _on_raw_key(self, ev):
        if not ev.is_remote or ev.hid_bytes is not None:
            return
        keys = self.config.get("keys", {})
        name = vk_name(ev.vkey)
        if ev.is_up:
            self._held.discard(name)
            conf = keys.get(name)
            if conf and conf.get("on_down", {}).get("type") == "voice" and self._voice:
                self._voice.finish()
            return
        if name in self._held:
            return
        self._held.add(name)
        conf = keys.get(name)
        if conf is None:
            return
        self._run_action(conf, conf.get("label", name), name)

    # ---- 状态 ----
    def status(self) -> dict:
        return {
            "running": self.running,
            "voice_ready": bool(self._voice and self._voice.ready),
            "tap_ready": self._tap is not None,
            "key_count": len(self.config.get("keys", {})),
        }

    def _emit_status(self):
        if self.on_status:
            try:
                self.on_status(self.status())
            except Exception:
                pass
