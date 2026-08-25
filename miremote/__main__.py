"""miremote 命令行入口。

用法:
  python -m miremote devices   # 查看系统 Raw Input 设备，确认遥控器可见
  python -m miremote learn     # 交互式学习：按键并输入键名，自动写进 config.json
  python -m miremote run       # 守护模式：按 config.json 执行映射动作
  python -m miremote selftest  # 捕获引擎自检（合成按键验证管道）
  python -m miremote gui         # 可视化按键学习界面（推荐）
  python -m miremote voice --caps      # ATVV 语音握手自检
  python -m miremote voice --record 12 # 录 12 秒语音并解码成 WAV
  python -m miremote backkey   # 返回/音量哑键拦截测试（首次弹 UAC）
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from . import actions
from .keys import key_label, vk_name
from .rawinput import RawInputEngine, list_raw_devices
from . import voice

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"

TYPE_NAME = {0: "mouse", 1: "keyboard", 2: "hid"}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {"device": {"vid": "2717", "pid": "32B8"}, "keys": {}}


def cmd_devices():
    print("== Raw Input 设备列表 ==")
    eng = RawInputEngine()
    found = False
    for d in list_raw_devices():
        remote = eng.is_remote_device(d["path"])
        mark = "  <-- 小米遥控器" if remote else ""
        if remote:
            found = True
        print(f"[{TYPE_NAME.get(d['type'], '?'):8}] {d['path']}{mark}")
    if not found:
        print("\n未发现遥控器设备（VID 2717/PID 32B8）。请检查蓝牙连接。")
    else:
        print("\n遥控器可见，可以用 `python -m miremote learn` 学习按键。")


# 这些 VK 一眼能认出来，learn 时自动标注，不问用户
AUTO_LABELS = {
    "VK_UP": "方向↑", "VK_DOWN": "方向↓", "VK_LEFT": "方向←", "VK_RIGHT": "方向→",
    "VK_RETURN": "OK确认", "VK_HOME": "主页", "VK_APPS": "菜单",
    "VK_VOLUME_UP": "音量+", "VK_VOLUME_DOWN": "音量-",
    "VK_ESCAPE": "返回", "VK_BACK": "返回(退格)",
}


def cmd_learn():
    """交互式学习：能自动识别的键直接记录，认不出的才提问，自动合并进 config.json。"""
    import queue
    import threading

    cfg = load_config()
    dev = cfg.get("device", {})
    keys = cfg.setdefault("keys", {})
    eng = RawInputEngine(vid=dev.get("vid", "2717"), pid=dev.get("pid", "32B8"),
                         name_contains=dev.get("name_contains"))
    q: "queue.Queue" = queue.Queue()

    def on_key(ev):
        if ev.is_remote and ev.hid_bytes is None:
            q.put(ev)

    eng.on_key = on_key
    # 窗口必须和消息循环在同一个线程（Windows 消息按创建线程投递），
    # 所以 start() 也放进工作线程；主线程留给 input() 问答。
    started = threading.Event()
    start_err: list = []

    def worker():
        try:
            eng.start()
        except Exception as e:  # 启动失败要告诉主线程
            start_err.append(e)
            started.set()
            return
        started.set()
        eng.run_forever()

    threading.Thread(target=worker, daemon=True).start()
    started.wait(3)
    if start_err:
        print("引擎启动失败:", start_err[0])
        return

    print("交互式学习")
    print("  方向↑↓←→、OK、主页、菜单、音量±、返回 —— 我能自动认出来，按了直接记录")
    print("  认不出的键（语音、电源、TV 等）会逐个问你名字")
    print("  想结束就输入 q 保存退出\n")
    held = set()
    labeled: dict[tuple, str] = {}
    try:
        while True:
            try:
                ev = q.get(timeout=0.5)
            except queue.Empty:
                continue  # 带超时轮询，保证 Ctrl+C 能退出
            name = vk_name(ev.vkey)
            if ev.is_up:
                held.discard((ev.vkey, ev.scan))
                continue
            if (ev.vkey, ev.scan) in held:
                continue  # 抑制自动重复
            held.add((ev.vkey, ev.scan))
            if name in AUTO_LABELS:
                labeled[(ev.vkey, ev.scan)] = AUTO_LABELS[name]
                print(f"  [自动] {name} = {AUTO_LABELS[name]}")
                continue
            default = key_label(ev.vkey, ev.scan)
            try:
                label = input(f"刚按下 {name} (scan=0x{ev.scan:02X}) —— 这个键叫什么? ").strip()
            except EOFError:
                break
            if label.lower() == "q":
                break
            if not label:
                continue  # 回车跳过，等下一次按
            labeled[(ev.vkey, ev.scan)] = label
            print(f"  已记录: {name} = {label}")
    except KeyboardInterrupt:
        pass

    if not labeled:
        print("没有记录到任何键。")
        return
    print("\n== 学习结果 ==")
    for (vk, scan), label in labeled.items():
        name = vk_name(vk)
        existed = name in keys
        entry = keys.get(name, {})
        entry["label"] = label
        entry["scan"] = f"0x{scan:02X}"
        if "on_down" not in entry:
            if "语音" in label:
                entry["on_down"] = {"type": "keys", "combo": ["VK_LWIN", "VK_H"]}
                print(f"  {name:14} = {label:10} -> 自动绑定 Win+H 语音输入")
            else:
                entry["on_down"] = {"type": "none"}
                print(f"  {name:14} = {label}")
        else:
            print(f"  {name:14} = {label:10} (保留原动作)")
        keys[name] = entry
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n已写入 {CONFIG_PATH}")


def cmd_run():
    cfg = load_config()
    dev = cfg.get("device", {})
    keys = cfg.get("keys", {})
    eng = RawInputEngine(vid=dev.get("vid", "2717"), pid=dev.get("pid", "32B8"),
                         name_contains=dev.get("name_contains"))
    held: set = set()

    # 语音引擎（按住说话 -> 松手 -> 转写 -> 打进焦点窗口）
    voice_daemon = None
    if dev.get("voice", True):
        try:
            from .voice import VoiceDaemon

            def on_text(text: str):
                actions.type_text(text)
                print(f"[{time.strftime('%H:%M:%S')}] [语音] 已输入: {text}", flush=True)

            voice_daemon = VoiceDaemon(
                on_text=on_text, log=lambda m: print(f"[语音] {m}", flush=True),
                model=cfg.get("voice_model", "medium"),
            )
            if voice_daemon.start():
                print("语音引擎已就绪：按住【语音键】说话，松手后文字打进焦点窗口")
            else:
                print("语音引擎启动失败（遥控器没连上？），语音键不可用")
                voice_daemon = None
        except Exception as e:
            print(f"语音引擎不可用: {e}")

    def run_action(conf: dict, label: str, source: str):
        action = conf.get("on_down", {"type": "none"})
        ts = time.strftime("%H:%M:%S")
        if action.get("type") == "voice":
            if voice_daemon:
                voice_daemon.begin()
                print(f"[{ts}] {label} ({source}) -> 开始录音（按住说话）")
            else:
                print(f"[{ts}] {label} -> 语音引擎未就绪")
            return
        desc = actions.perform(action)
        print(f"[{ts}] {label} ({source}) -> {desc}")

    def on_key(ev):
        if not ev.is_remote or ev.is_up or ev.hid_bytes is not None:
            return
        name = vk_name(ev.vkey)
        if name in held:  # 抑制自动重复
            return
        held.add(name)
        conf = keys.get(name)
        ts = time.strftime("%H:%M:%S")
        if conf is None:
            print(f"[{ts}] {name} 未配置动作")
            return
        run_action(conf, conf.get("label", name), name)

    def on_key_up(ev):
        if not ev.is_remote or ev.hid_bytes is not None:
            return
        name = vk_name(ev.vkey)
        held.discard(name)
        conf = keys.get(name)
        if conf and conf.get("on_down", {}).get("type") == "voice" and voice_daemon:
            voice_daemon.finish()  # 松手兜底（遥控器自报停止通常先到）

    # 合并成单个回调（引擎只支持一个 on_key）
    def cb(ev):
        if ev.is_up:
            on_key_up(ev)
        else:
            on_key(ev)

    eng.on_key = cb
    eng.start()

    # 哑键拦截（返回/音量±/静音）：钩住 WUDFHost 取回被 Windows 丢弃的报文
    tap = None
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
                ts = time.strftime("%H:%M:%S")
                if conf is None:
                    print(f"[{ts}] tap:{name} 未配置动作")
                    return
                run_action(conf, conf.get("label", name), f"tap:{name}")

            tap = BackKeyTap(on_edge=on_edge, log=lambda m: print(f"[tap] {m}"))
            tap.start()
            print("哑键拦截已启动（首次会弹 UAC 提权，附加 WUDFHost 需要管理员）")
        except ImportError:
            print("未安装 frida，哑键拦截停用（pip install frida 开启）")

    print(f"守护模式已启动（{len(keys)} 个键位映射），Ctrl+C 退出。")
    eng.run_forever()
    if tap:
        tap.stop()
    if voice_daemon:
        voice_daemon.stop()
    print("已退出。")


def cmd_selftest():
    print("== 捕获引擎自检 ==")
    events = []
    eng = RawInputEngine()
    eng.on_key = lambda ev: events.append(ev)
    eng.start()
    print("1) 引擎启动 OK，注入合成按键（来自本进程，hDevice 为空 → 应被识别为非遥控器）")
    actions.send_combo(["VK_CONTROL", "VK_SHIFT", "VK_F13"], hold_last_ms=20)
    deadline = time.time() + 2
    while time.time() < deadline and not events:
        eng.pump()
        time.sleep(0.02)
    eng.pump()
    downs = [e for e in events if not e.is_up]
    if downs:
        print(f"2) 收到 {len(events)} 个事件（含按下/松开），管道正常。示例:")
        for e in events[:6]:
            tag = "遥控器" if e.is_remote else "本机/合成"
            print(f"   {tag} {vk_name(e.vkey)} device={e.device[:60]}")
        if all(not e.is_remote for e in events):
            print("3) 设备过滤正常：合成输入未被误判为遥控器。")
    else:
        print("2) 未收到合成事件 — SendInput 需要交互桌面，请在本地终端运行本自检。")
    print("自检结束。")


def voice_main():
    """语音子命令：转发参数给 miremote.voice。"""
    import sys as _sys
    _sys.argv = ["miremote voice"] + _sys.argv[2:]
    voice.main()


def gui_main():
    """启动控制台 GUI。"""
    from . import gui
    gui.main()


def app_main():
    """完整应用入口：控制台 GUI + 托盘（打包后默认走这里）。"""
    gui_main()


def diagnose_main():
    """诊断"没反应"的按键：全通道监听 HID + GATT。"""
    from . import diagnose
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 45.0
    diagnose.main(secs)


def backkey_main():
    """哑键拦截测试：python -m miremote backkey。"""
    from . import backkey
    backkey.main(sys.argv[2:])


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "app"
    table = {
        "devices": cmd_devices,
        "voice": voice_main,
        "learn": cmd_learn,
        "run": cmd_run,
        "selftest": cmd_selftest,
        "gui": gui_main,
        "app": app_main,
        "diagnose": diagnose_main,
        "backkey": backkey_main,
    }
    if cmd == "--inject":
        # frozen exe 的提权注入器自调用入口（backkey._launch_helper 用）
        from . import tapinject
        sys.exit(tapinject.cli(sys.argv[1:]))
    if cmd == "--silent":
        # 开机自启的静默模式：走 GUI 但不弹窗口（gui.main 内部解析该参数）
        gui_main()
        return
    if cmd not in table:
        print(__doc__)
        sys.exit(2)
    table[cmd]()


if __name__ == "__main__":
    main()
