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


def app_data_dir() -> Path:
    """配置/日志/模型的用户目录（打包后 exe 目录可能只读）。"""
    if getattr(sys, "frozen", False):
        base = os.environ.get("APPDATA", str(Path.home()))
        return Path(base) / "MiRemoteVibe"
    return Path(__file__).resolve().parent.parent


def config_path() -> Path:
    return app_data_dir() / "config.json"


DEFAULT_CONFIG = {
    "device": {"vid": "2717", "pid": "32B8", "hid_tap": True, "voice": True},
    "voice_mode": "local",      # "local"=本地 whisper 转写；"wechat"=桥接输入法
    "voice_model": "medium",
    # 微信切换式语音热键（短击开启/结束持续语音）。语音键 F5 透传会干扰
    # 按住式 Ctrl+Win 检测，故用 Ctrl+Win+Shift 切换式 + 松手后播放架构
    "wechat_hotkey": ["VK_CONTROL", "VK_LWIN", "VK_SHIFT"],
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
        "TAP_VOLUME_MUTE": {"label": "静音", "on_down": {"type": "volume", "delta": 0}},
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

    # ---- 配置 ----
    def _load_config(self) -> dict:
        p = config_path()
        if p.exists():
            try:
                cfg = json.loads(p.read_text(encoding="utf-8"))
                for k, v in DEFAULT_CONFIG.items():
                    cfg.setdefault(k, v)
                # 迁移：旧的按住式两键热键升级为切换式三键（F5 透传会干扰两键检测）
                if cfg.get("wechat_hotkey") == ["VK_CONTROL", "VK_LWIN"]:
                    cfg["wechat_hotkey"] = list(DEFAULT_CONFIG["wechat_hotkey"])
                # 迁移：补齐后来新增的按键映射（老配置里没有的键）
                for k, v in DEFAULT_CONFIG["keys"].items():
                    cfg.setdefault("keys", {}).setdefault(k, json.loads(json.dumps(v)))
                # 迁移：修正 learn 阶段标错的 VK_HOME 标签
                home = cfg.get("keys", {}).get("VK_HOME")
                if isinstance(home, dict) and home.get("label") == "返回":
                    home["label"] = "主页"
                return cfg
            except (json.JSONDecodeError, OSError):
                pass
        return json.loads(json.dumps(DEFAULT_CONFIG))

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
                )
                self._voice = vd
                if vd.start():
                    if mode == "wechat":
                        self.on_log("语音引擎就绪（微信输入法模式）：按住语音键说话，"
                                    "声音桥接给输入法，松手出字")
                    else:
                        self.on_log("语音引擎就绪（本地模式）：按住【语音键】说话，"
                                    "松手后文字打进焦点窗口")
                else:
                    self.on_log("语音设备暂未就绪，后台会自动重试；连接成功后即可按住语音键说话")
            except Exception as e:
                self.on_log(f"语音引擎不可用: {e}")

        # 3) 哑键拦截
        if dev.get("hid_tap", True):
            try:
                from .backkey import BackKeyTap

                TAP_KEYS = {
                    "back": "TAP_BACK", "volume_up": "TAP_VOLUME_UP",
                    "volume_down": "TAP_VOLUME_DOWN", "volume_mute": "TAP_VOLUME_MUTE",
                }

                def on_edge(name: str, is_down: bool):
                    if not is_down:
                        return
                    key = TAP_KEYS.get(name)
                    conf = keys.get(key) if key else None
                    if conf is None:
                        return
                    self._run_action(conf, conf.get("label", name), f"tap:{name}")

                self._tap = BackKeyTap(on_edge=on_edge,
                                       log=lambda m: self.on_log(f"[tap] {m}"))
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
