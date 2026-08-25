"""小米遥控器控制台（PySide6/Qt 版）。

- Qt 原生高 DPI 支持（无需手动 hack）
- 标签页：控制 / 按键映射 / 语音 / 日志
- 遥控器矢量控件（QPainter，缩放无损，点击热区）
- Qt 原生系统托盘
"""

from __future__ import annotations

import json
import ctypes
import queue
import sys
import threading
import time
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from . import actions
from .remote_widget import RemoteWidget, KEY_RECTS
from .service import MiRemoteService, action_summary

# ---- 深色主题调色板 ----
BG = "#0d1117"
CARD = "#151a22"
CARD_HI = "#202a38"
BORDER = "#2a3543"
FG = "#f4f7fb"
DIM = "#94a0b2"
ACCENT = "#e0b66b"
ACCENT_STRONG = "#c58f3f"
GREEN = "#49d391"
RED = "#ff6f7d"
YELLOW = "#e8bd6a"


PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SINGLE_INSTANCE_MUTEX = "Local\\MiRemoteVibe.Gui"
# 二次启动的唤回信号文件（第一实例监听其所在目录，第二实例创建它）
import tempfile as _tempfile
_SHOW_FLAG = str(Path(_tempfile.gettempdir()).resolve() / "miremote_show.flag")

# 开机自启注册表（当前用户，无需管理员）
_BOOT_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_BOOT_RUN_NAME = "MiRemoteVibe"


def _boot_launch_command() -> str:
    import sys as _s
    exe = Path(_s.executable).resolve()
    if getattr(_s, "frozen", False):
        return f'"{exe}" --silent'
    # 源码模式：python + launcher.py
    root = Path(__file__).resolve().parent.parent
    return f'"{exe}" "{root / "launcher.py"}" --silent'


def _boot_launch_enabled() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _BOOT_RUN_KEY) as k:
            winreg.QueryValueEx(k, _BOOT_RUN_NAME)
            return True
    except OSError:
        return False


def _set_boot_launch(enable: bool) -> bool:
    try:
        import winreg
        if enable:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _BOOT_RUN_KEY,
                                0, winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, _BOOT_RUN_NAME, 0, winreg.REG_SZ,
                                  _boot_launch_command())
        else:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _BOOT_RUN_KEY,
                                    0, winreg.KEY_SET_VALUE) as k:
                    winreg.DeleteValue(k, _BOOT_RUN_NAME)
            except FileNotFoundError:
                pass
        return True
    except OSError:
        return False


def _asset_path(name: str) -> Path:
    """Return an asset path that works both from source and PyInstaller."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", PROJECT_ROOT)) / "assets" / name
    return PROJECT_ROOT / "assets" / name


def _acquire_single_instance():
    if sys.platform != "win32":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    create_mutex.restype = ctypes.c_void_p
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_bool
    handle = create_mutex(None, False, _SINGLE_INSTANCE_MUTEX)
    if not handle:
        return None
    if ctypes.get_last_error() == 183:
        close_handle(handle)
        return None
    return close_handle, handle


PRESETS: list[tuple[str, dict]] = [
    ("无操作", {"type": "none"}),
    ("语音（按住说话）", {"type": "voice"}),
    ("音量+", {"type": "volume", "delta": 1}),
    ("音量-", {"type": "volume", "delta": -1}),
    ("静音", {"type": "volume", "delta": 0}),
    ("Esc 打断", {"type": "tap", "key": "VK_ESCAPE"}),
    ("回车 Enter", {"type": "tap", "key": "VK_RETURN"}),
    ("退格 Backspace", {"type": "tap", "key": "VK_BACK"}),
    ("Delete 删除", {"type": "tap", "key": "VK_DELETE"}),
    ("Home", {"type": "tap", "key": "VK_HOME"}),
    ("End", {"type": "tap", "key": "VK_END"}),
    ("Ctrl+C 复制", {"type": "keys", "combo": ["VK_CONTROL", "VK_C"]}),
    ("Ctrl+V 粘贴", {"type": "keys", "combo": ["VK_CONTROL", "VK_V"]}),
    ("Ctrl+A 全选", {"type": "keys", "combo": ["VK_CONTROL", "VK_A"]}),
    ("Shift+Tab 切权限", {"type": "keys", "combo": ["VK_SHIFT", "VK_TAB"]}),
    ("Win+H 语音输入", {"type": "keys", "combo": ["VK_LWIN", "VK_H"]}),
    ("Win+D 显示桌面", {"type": "keys", "combo": ["VK_LWIN", "VK_D"]}),
    ("录制单个键…", None),
    ("录制组合键…", None),
    ("输入文本…", None),
    ("运行程序…", None),
]


def _css() -> str:
    return f"""
    QMainWindow, QWidget {{ background-color: {BG}; color: {FG};
                            font-family: "Microsoft YaHei UI", "Segoe UI"; }}
    QLabel {{ color: {FG}; background: transparent; }}
    QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 16px;
                        background: {CARD}; top: -1px; }}
    QTabBar::tab {{ background: transparent; color: {DIM}; padding: 11px 20px;
                    margin: 4px 3px; border-radius: 10px; min-width: 76px;
                    font-size: 13px; }}
    QTabBar::tab:hover {{ background: #18212d; color: {FG}; }}
    QTabBar::tab:selected {{ background: {CARD_HI}; color: {FG};
                             font-weight: 600; }}
    QFrame#headerCard, QFrame#card {{ background: {CARD};
                                      border: 1px solid {BORDER};
                                      border-radius: 16px; }}
    QFrame#softCard {{ background: #111821; border: 1px solid #223040;
                       border-radius: 12px; }}
    QLabel#eyebrow {{ color: {ACCENT}; font-size: 11px; font-weight: 700;
                      letter-spacing: 1px; }}
    QLabel#pageTitle {{ color: {FG}; font-size: 24px; font-weight: 700; }}
    QLabel#sectionTitle {{ color: {FG}; font-size: 17px; font-weight: 650; }}
    QLabel#muted {{ color: {DIM}; font-size: 12px; }}
    QLabel#metricValue {{ color: {FG}; font-size: 16px; font-weight: 700; }}
    QLabel#metricLabel {{ color: {DIM}; font-size: 11px; }}
    QPushButton {{ background: {CARD_HI}; color: {FG}; border: 1px solid transparent;
                   padding: 9px 16px; min-height: 18px; border-radius: 9px;
                   font-size: 13px; }}
    QPushButton:hover {{ background: #2b3a4d; border-color: #3b526d; }}
    QPushButton:pressed {{ background: #172331; }}
    QPushButton#primary {{ background: {ACCENT_STRONG}; color: white;
                           border-color: #6a9dff; font-weight: 700; }}
    QPushButton#primary:hover {{ background: #6da1ff; }}
    QPushButton#danger {{ background: #7f3041; color: white;
                          border-color: #b84b5d; font-weight: 700; }}
    QPushButton#danger:hover {{ background: #a63e52; }}
    QPushButton#ghost {{ background: transparent; color: {DIM};
                         border-color: {BORDER}; }}
    QPushButton#ghost:hover {{ color: {FG}; background: #18212d; }}
    QListWidget {{ background: #10161e; border: 1px solid {BORDER};
                   border-radius: 12px; padding: 8px; outline: none; }}
    QListWidget::item {{ padding: 12px 10px; margin: 2px 0; border-radius: 9px;
                         color: {FG}; }}
    QListWidget::item:hover {{ background: #1b2735; }}
    QListWidget::item:selected {{ background: #6d512a; color: white; }}
    QComboBox {{ background: #111821; color: {FG}; border: 1px solid {BORDER};
                 padding: 8px 10px; min-height: 18px; border-radius: 8px; }}
    QComboBox:hover {{ border-color: #4a6b8f; }}
    QComboBox QAbstractItemView {{ background: {CARD}; color: {FG};
                                   selection-background-color: {ACCENT_STRONG};
                                   border: 1px solid {BORDER}; }}
    QTextEdit, QPlainTextEdit {{ background: #0b1016; color: #cdd5e0;
                                 border: 1px solid {BORDER}; border-radius: 12px;
                                 padding: 10px; selection-background-color: #315b98; }}
    QGroupBox {{ background: {CARD}; border: 1px solid {BORDER};
                 border-radius: 14px; margin-top: 18px; padding: 22px 16px 16px; }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 14px; padding: 0 7px;
                        color: {DIM}; font-size: 12px; font-weight: 700; }}
    QStatusBar {{ background: {CARD}; color: {DIM}; border-top: 1px solid {BORDER}; }}
    QCheckBox {{ color: {FG}; spacing: 8px; }}
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 4px; }}
    QScrollBar::handle:vertical {{ background: #33465b; border-radius: 5px; min-height: 30px; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    """


class MiRemoteWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("小米遥控器 · 控制台")
        self.resize(1080, 720)
        self.setMinimumSize(900, 620)
        self.setStyleSheet(_css())

        self.service = MiRemoteService(
            on_log=lambda m: self.log_signal.message.emit(m),
            on_status=lambda s: self.status_signal.status.emit(s),
        )
        self.current_key: str | None = None
        self._key_names: list[str] = []

        # 信号桥（先建，service 回调才能用）
        self.log_signal = _SignalBridge(self)
        self.log_signal.message.connect(self._append_log)
        self.status_signal = _StatusBridge(self)
        self.status_signal.status.connect(self._render_status)

        self._build()
        self._refresh_keys()
        self._start_tray()
        self._start_show_watcher()

    def _start_show_watcher(self):
        """轮询二次启动的唤回信号文件（500ms，比 QFileSystemWatcher 在 TEMP 上可靠）。"""
        import os
        try:
            os.remove(_SHOW_FLAG)  # 清掉残留
        except OSError:
            pass
        self._show_timer = QtCore.QTimer(self)
        self._show_timer.setInterval(500)
        self._show_timer.timeout.connect(self._on_show_flag)
        self._show_timer.start()

    def _on_show_flag(self):
        import os
        if not os.path.exists(_SHOW_FLAG):
            return
        try:
            os.remove(_SHOW_FLAG)
        except OSError:
            pass
        self.show()
        self.raise_()
        self.activateWindow()

    def _build(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(14)

        header_card = QtWidgets.QFrame()
        header_card.setObjectName("headerCard")
        header_card.setMinimumHeight(82)
        header = QtWidgets.QHBoxLayout(header_card)
        header.setContentsMargins(20, 14, 16, 14)
        header.setSpacing(16)

        brand = QtWidgets.QVBoxLayout()
        brand.setSpacing(2)
        eyebrow = QtWidgets.QLabel("MI REMOTE  /  RC003")
        eyebrow.setObjectName("eyebrow")
        brand.addWidget(eyebrow)
        title = QtWidgets.QLabel("小米蓝牙遥控器")
        title.setObjectName("pageTitle")
        brand.addWidget(title)
        sub = QtWidgets.QLabel("把客厅里的一个按键，变成你的编程控制台")
        sub.setObjectName("muted")
        brand.addWidget(sub)
        header.addLayout(brand, 1)

        status_box = QtWidgets.QVBoxLayout()
        status_box.setSpacing(2)
        status_caption = QtWidgets.QLabel("设备守护")
        status_caption.setObjectName("metricLabel")
        status_box.addWidget(status_caption, 0, QtCore.Qt.AlignRight)
        self.status_badge = QtWidgets.QLabel("● 已停止")
        self.status_badge.setObjectName("metricValue")
        self.status_badge.setAlignment(QtCore.Qt.AlignRight)
        status_box.addWidget(self.status_badge)
        self.status_hint = QtWidgets.QLabel("服务未启动")
        self.status_hint.setObjectName("metricLabel")
        self.status_hint.setAlignment(QtCore.Qt.AlignRight)
        status_box.addWidget(self.status_hint)
        header.addLayout(status_box)

        self.btn_toggle = QtWidgets.QPushButton("启动守护")
        self.btn_toggle.setObjectName("primary")
        self.btn_toggle.setMinimumWidth(132)
        self.btn_toggle.setMinimumHeight(42)
        self.btn_toggle.clicked.connect(self._toggle_service)
        header.addWidget(self.btn_toggle)
        root.addWidget(header_card)

        # 标签页
        self.tabs = QtWidgets.QTabWidget()
        root.addWidget(self.tabs, 1)

        self.tabs.addTab(self._build_control_tab(), "控制")
        self.tabs.addTab(self._build_keys_tab(), "按键映射")
        self.tabs.addTab(self._build_voice_tab(), "语音")
        self.tabs.addTab(self._build_log_tab(), "日志")

    # ---- 控制页 ----
    def _build_control_tab(self):
        w = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(w)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(16)

        hero = QtWidgets.QFrame()
        hero.setObjectName("card")
        hero_lay = QtWidgets.QVBoxLayout(hero)
        hero_lay.setContentsMargins(18, 18, 18, 16)
        hero_lay.setSpacing(10)
        hero_head = QtWidgets.QHBoxLayout()
        hero_title = QtWidgets.QVBoxLayout()
        hero_eyebrow = QtWidgets.QLabel("你的设备")
        hero_eyebrow.setObjectName("eyebrow")
        hero_title.addWidget(hero_eyebrow)
        hero_name = QtWidgets.QLabel("小米蓝牙遥控器 2 Pro")
        hero_name.setObjectName("sectionTitle")
        hero_title.addWidget(hero_name)
        hero_head.addLayout(hero_title)
        hero_head.addStretch(1)
        model_tag = QtWidgets.QLabel("RC003")
        model_tag.setObjectName("muted")
        hero_head.addWidget(model_tag, 0, QtCore.Qt.AlignTop)
        hero_lay.addLayout(hero_head)

        photo = QtWidgets.QLabel()
        photo.setObjectName("remotePhoto")
        photo.setAlignment(QtCore.Qt.AlignCenter)
        photo.setMinimumHeight(255)
        photo.setStyleSheet("background: #eef0f4; border-radius: 12px;")
        pixmap = QtGui.QPixmap(str(_asset_path("remote.jpg")))
        if not pixmap.isNull():
            crop_top = int(pixmap.height() * 0.28)
            cropped = pixmap.copy(0, crop_top, pixmap.width(), pixmap.height() - crop_top)
            photo.setPixmap(cropped.scaled(760, 330, QtCore.Qt.KeepAspectRatio,
                                           QtCore.Qt.SmoothTransformation))
        else:
            photo.setText("遥控器照片未找到")
        hero_lay.addWidget(photo, 1)

        photo_hint = QtWidgets.QLabel("按键已映射到右侧动作；需要调整时进入「按键映射」")
        photo_hint.setObjectName("muted")
        photo_hint.setAlignment(QtCore.Qt.AlignCenter)
        hero_lay.addWidget(photo_hint)

        metrics = QtWidgets.QHBoxLayout()
        metrics.setSpacing(8)
        for value, label in (("按住", "语音键说话"), ("OK", "批准 / 确认"), ("返回", "立即打断")):
            metrics.addWidget(self._metric_card(value, label))
        hero_lay.addLayout(metrics)
        lay.addWidget(hero, 3)

        guide = QtWidgets.QFrame()
        guide.setObjectName("card")
        right = QtWidgets.QVBoxLayout(guide)
        right.setContentsMargins(22, 22, 22, 18)
        right.setSpacing(12)
        guide_eyebrow = QtWidgets.QLabel("从这里开始")
        guide_eyebrow.setObjectName("eyebrow")
        right.addWidget(guide_eyebrow)
        guide_title = QtWidgets.QLabel("让遥控器接管你的编程")
        guide_title.setObjectName("sectionTitle")
        right.addWidget(guide_title)
        guide_desc = QtWidgets.QLabel(
            "不用记命令，也不用离开沙发。启动守护后，方向键、OK、返回和语音键都会在当前窗口里工作。"
        )
        guide_desc.setWordWrap(True)
        guide_desc.setObjectName("muted")
        right.addWidget(guide_desc)
        right.addSpacing(4)
        for number, title_text, body in (
            ("01", "启动守护", "打开后台监听，状态变成运行中即可开始。"),
            ("02", "按住语音键", "说完松手，转写结果会自动输入光标位置。"),
            ("03", "用 OK 批准", "返回键负责打断，方向键负责翻阅结果。"),
        ):
            right.addWidget(self._step_row(number, title_text, body))
        right.addStretch(1)

        action_row = QtWidgets.QHBoxLayout()
        self.btn_open_mapping = QtWidgets.QPushButton("编辑按键映射")
        self.btn_open_mapping.setObjectName("ghost")
        self.btn_open_mapping.clicked.connect(lambda: self.tabs.setCurrentIndex(1))
        action_row.addWidget(self.btn_open_mapping)
        self.btn_open_voice = QtWidgets.QPushButton("语音设置")
        self.btn_open_voice.setObjectName("ghost")
        self.btn_open_voice.clicked.connect(lambda: self.tabs.setCurrentIndex(2))
        action_row.addWidget(self.btn_open_voice)
        right.addLayout(action_row)

        # 托盘图标显示开关
        self.chk_tray = QtWidgets.QCheckBox("在系统托盘显示图标")
        chk_default = not self.service.config.get("hide_tray", False)
        self.chk_tray.setChecked(chk_default)
        self.chk_tray.stateChanged.connect(self._on_tray_checkbox)
        right.addWidget(self.chk_tray)

        # 打开时自动启动守护
        self.chk_autostart_service = QtWidgets.QCheckBox("打开时自动启动守护")
        self.chk_autostart_service.setChecked(
            self.service.config.get("auto_start_service", True))
        self.chk_autostart_service.toggled.connect(self._on_autostart_service)
        right.addWidget(self.chk_autostart_service)

        # 开机自启（静默）
        self.chk_boot_launch = QtWidgets.QCheckBox("开机自动启动（后台静默运行）")
        self.chk_boot_launch.setChecked(_boot_launch_enabled())
        self.chk_boot_launch.toggled.connect(self._on_boot_launch)
        right.addWidget(self.chk_boot_launch)
        lay.addWidget(guide, 2)
        return w

    # ---- 按键映射页 ----
    def _build_keys_tab(self):
        w = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(w)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(14)

        remote_card = QtWidgets.QFrame()
        remote_card.setObjectName("card")
        remote_card.setMinimumWidth(300)
        remote_card.setMaximumWidth(350)
        remote_lay = QtWidgets.QVBoxLayout(remote_card)
        remote_lay.setContentsMargins(16, 16, 16, 16)
        remote_label = QtWidgets.QLabel("点击遥控器上的按键")
        remote_label.setObjectName("sectionTitle")
        remote_lay.addWidget(remote_label)
        remote_hint = QtWidgets.QLabel("绿色表示已经绑定动作")
        remote_hint.setObjectName("muted")
        remote_lay.addWidget(remote_hint)
        self.remote = RemoteWidget()
        self.remote.setMinimumSize(260, 460)
        self.remote.clicked.connect(self._on_remote_click)
        remote_lay.addWidget(self.remote, 1)
        lay.addWidget(remote_card, 0)

        list_card = QtWidgets.QFrame()
        list_card.setObjectName("card")
        list_lay = QtWidgets.QVBoxLayout(list_card)
        list_lay.setContentsMargins(14, 16, 14, 14)
        lbl = QtWidgets.QLabel("按键映射")
        lbl.setObjectName("sectionTitle")
        list_lay.addWidget(lbl)
        list_hint = QtWidgets.QLabel("选择一个按键开始编辑")
        list_hint.setObjectName("muted")
        list_lay.addWidget(list_hint)
        self.key_list = QtWidgets.QListWidget()
        self.key_list.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.key_list.currentItemChanged.connect(self._on_select_key)
        list_lay.addWidget(self.key_list, 1)
        btn_learn = QtWidgets.QPushButton("学习新键")
        btn_learn.setObjectName("ghost")
        btn_learn.clicked.connect(self._learn_keys)
        list_lay.addWidget(btn_learn)
        lay.addWidget(list_card, 1)

        editor_card = QtWidgets.QFrame()
        editor_card.setObjectName("card")
        right = QtWidgets.QVBoxLayout(editor_card)
        right.setContentsMargins(20, 18, 20, 18)
        right.setSpacing(12)
        editor_eyebrow = QtWidgets.QLabel("动作编辑器")
        editor_eyebrow.setObjectName("eyebrow")
        right.addWidget(editor_eyebrow)
        self.edit_title = QtWidgets.QLabel("← 选择左侧按键或点遥控器图")
        self.edit_title.setObjectName("sectionTitle")
        right.addWidget(self.edit_title)
        edit_hint = QtWidgets.QLabel("动作会保存到本地配置，下一次启动仍会保留。")
        edit_hint.setObjectName("muted")
        right.addWidget(edit_hint)

        selection_card = QtWidgets.QFrame()
        selection_card.setObjectName("softCard")
        selection_lay = QtWidgets.QHBoxLayout(selection_card)
        selection_lay.setContentsMargins(14, 12, 14, 12)
        self.selected_key_chip = QtWidgets.QLabel("未选择")
        self.selected_key_chip.setObjectName("eyebrow")
        self.selected_key_chip.setMinimumWidth(58)
        selection_lay.addWidget(self.selected_key_chip, 0, QtCore.Qt.AlignTop)
        selection_copy = QtWidgets.QVBoxLayout()
        selection_copy.setSpacing(2)
        self.selected_key_title = QtWidgets.QLabel("等待选择一个按键")
        self.selected_key_title.setStyleSheet("font-weight: 650;")
        selection_copy.addWidget(self.selected_key_title)
        self.selected_key_meta = QtWidgets.QLabel("从左侧遥控器或列表中选择")
        self.selected_key_meta.setObjectName("muted")
        selection_copy.addWidget(self.selected_key_meta)
        selection_lay.addLayout(selection_copy, 1)
        right.addWidget(selection_card)

        form = QtWidgets.QGridLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(10)
        action_label = QtWidgets.QLabel("执行动作")
        action_label.setObjectName("muted")
        form.addWidget(action_label, 0, 0)
        self.action_box = QtWidgets.QComboBox()
        self.action_box.addItems([p[0] for p in PRESETS])
        self.action_box.currentIndexChanged.connect(self._on_preset)
        form.addWidget(self.action_box, 0, 1)
        right.addLayout(form)

        self.action_detail = QtWidgets.QLabel("")
        self.action_detail.setStyleSheet(
            f"color: {DIM}; font-family: Consolas, 'Cascadia Mono'; font-size: 11px; "
            "background: #10161e; border: 1px solid #223040; border-radius: 8px; padding: 10px;"
        )
        self.action_detail.setWordWrap(True)
        self.action_detail.setMinimumHeight(52)
        self.action_detail.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft)
        right.addWidget(self.action_detail)

        quick_label = QtWidgets.QLabel("常用动作")
        quick_label.setObjectName("muted")
        right.addWidget(quick_label)
        quick_grid = QtWidgets.QGridLayout()
        quick_grid.setHorizontalSpacing(8)
        quick_grid.setVerticalSpacing(8)
        for index, label in enumerate(("无操作", "Esc 打断", "回车 Enter", "语音（按住说话）")):
            quick_button = QtWidgets.QPushButton(label)
            quick_button.setObjectName("ghost")
            quick_button.clicked.connect(
                lambda _checked=False, preset_label=label: self.action_box.setCurrentText(preset_label)
            )
            quick_grid.addWidget(quick_button, index // 2, index % 2)
        right.addLayout(quick_grid)

        action_tip = QtWidgets.QLabel("小提示：先选择一个预设动作；需要更细的键值时再使用“录制”选项。")
        action_tip.setObjectName("muted")
        action_tip.setWordWrap(True)
        right.addWidget(action_tip)

        save_row = QtWidgets.QHBoxLayout()
        self.btn_save = QtWidgets.QPushButton("保存此键")
        self.btn_save.setObjectName("primary")
        self.btn_save.clicked.connect(self._save_key)
        save_row.addWidget(self.btn_save)
        save_row.addStretch(1)
        right.addLayout(save_row)
        right.addStretch(1)
        lay.addWidget(editor_card, 2)
        return w

    @staticmethod
    def _metric_card(value: str, label: str) -> QtWidgets.QFrame:
        card = QtWidgets.QFrame()
        card.setObjectName("softCard")
        box = QtWidgets.QVBoxLayout(card)
        box.setContentsMargins(10, 8, 10, 8)
        box.setSpacing(1)
        value_label = QtWidgets.QLabel(value)
        value_label.setObjectName("metricValue")
        box.addWidget(value_label)
        label_widget = QtWidgets.QLabel(label)
        label_widget.setObjectName("metricLabel")
        box.addWidget(label_widget)
        return card

    @staticmethod
    def _step_row(number: str, title: str, body: str) -> QtWidgets.QFrame:
        row = QtWidgets.QFrame()
        row.setObjectName("softCard")
        box = QtWidgets.QHBoxLayout(row)
        box.setContentsMargins(12, 10, 12, 10)
        box.setSpacing(12)
        marker = QtWidgets.QLabel(number)
        marker.setObjectName("eyebrow")
        marker.setMinimumWidth(24)
        box.addWidget(marker, 0, QtCore.Qt.AlignTop)
        copy = QtWidgets.QVBoxLayout()
        copy.setSpacing(2)
        title_label = QtWidgets.QLabel(title)
        title_label.setStyleSheet("font-weight: 650;")
        copy.addWidget(title_label)
        body_label = QtWidgets.QLabel(body)
        body_label.setObjectName("muted")
        body_label.setWordWrap(True)
        copy.addWidget(body_label)
        box.addLayout(copy, 1)
        return row

    # ---- 语音页 ----
    def _build_voice_tab(self):
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(14)

        heading = QtWidgets.QFrame()
        heading.setObjectName("card")
        heading_lay = QtWidgets.QVBoxLayout(heading)
        heading_lay.setContentsMargins(20, 16, 20, 16)
        eyebrow = QtWidgets.QLabel("VOICE PIPELINE")
        eyebrow.setObjectName("eyebrow")
        heading_lay.addWidget(eyebrow)
        title = QtWidgets.QLabel("语音输入")
        title.setObjectName("sectionTitle")
        heading_lay.addWidget(title)
        desc = QtWidgets.QLabel("按住遥控器的语音键说话，松手后把文字送到当前光标处。")
        desc.setObjectName("muted")
        heading_lay.addWidget(desc)
        lay.addWidget(heading)

        g = QtWidgets.QGroupBox("语音识别")
        gl = QtWidgets.QFormLayout(g)
        gl.setHorizontalSpacing(24)
        gl.setVerticalSpacing(14)
        self.mode_box = QtWidgets.QComboBox()
        self.mode_box.addItem("本地 whisper（离线）", "local")
        self.mode_box.addItem("微信输入法（去语气词）", "wechat")
        idx = self.mode_box.findData(self.service.config.get("voice_mode", "local"))
        self.mode_box.setCurrentIndex(max(0, idx))
        self.mode_box.currentIndexChanged.connect(self._on_mode_change)
        gl.addRow("模式:", self.mode_box)

        self.model_box = QtWidgets.QComboBox()
        self.model_box.addItems(["medium", "small", "base", "tiny"])
        m = self.service.config.get("voice_model", "medium")
        self.model_box.setCurrentText(m if m in ["medium", "small", "base", "tiny"] else "medium")
        self.model_box.currentIndexChanged.connect(self._on_model_change)
        gl.addRow("模型:", self.model_box)
        lay.addWidget(g)

        self.voice_desc = QtWidgets.QLabel(self._voice_desc())
        self.voice_desc.setWordWrap(True)
        self.voice_desc.setObjectName("muted")
        lay.addWidget(self.voice_desc)
        lay.addStretch(1)
        return w

    # ---- 日志页 ----
    def _build_log_tab(self):
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(10)
        toolbar = QtWidgets.QHBoxLayout()
        label = QtWidgets.QLabel("运行日志")
        label.setObjectName("sectionTitle")
        toolbar.addWidget(label)
        toolbar.addStretch(1)
        self.btn_clear_log = QtWidgets.QPushButton("清空")
        self.btn_clear_log.setObjectName("ghost")
        self.btn_clear_log.clicked.connect(lambda: self.log_text.clear())
        toolbar.addWidget(self.btn_clear_log)
        lay.addLayout(toolbar)
        self.log_text = QtWidgets.QPlainTextEdit()
        self.log_text.setReadOnly(True)
        lay.addWidget(self.log_text)
        return w

    def _start_tray(self):
        if self.service.config.get("hide_tray", False):
            self.tray = None
            return
        try:
            from .tray_qt import create_tray
            self.tray = create_tray(self, self.service)
        except Exception:
            self.tray = None

    # ---- 托盘显隐 ----
    def _on_tray_checkbox(self, state):
        want_show = state == QtCore.Qt.Checked
        self.service.config["hide_tray"] = not want_show
        self.service.save_config()
        if want_show and (self.tray is None or not self.tray.isVisible()):
            self._start_tray()
        elif not want_show and self.tray:
            self.tray.hide()

    # ---- 自动启动 ----
    def _on_autostart_service(self, checked: bool):
        self.service.config["auto_start_service"] = checked
        self.service.save_config()

    def _on_boot_launch(self, checked: bool):
        ok = _set_boot_launch(checked)
        if not ok:
            QtWidgets.QMessageBox.warning(self, "开机自启", "写注册表失败，未能设置开机自启。")
            self.chk_boot_launch.blockSignals(True)
            self.chk_boot_launch.setChecked(not checked)
            self.chk_boot_launch.blockSignals(False)

    def _auto_start_service(self):
        """打开后自动启动守护（配置 auto_start_service，默认开）。"""
        if self.service.running:
            return
        self._append_log("自动启动守护…")
        threading.Thread(target=self._do_start, daemon=True).start()

    def hide_tray_persist(self):
        """托盘菜单「隐藏托盘图标」：记住设置并隐藏。程序继续后台运行。"""
        self.service.config["hide_tray"] = True
        self.service.save_config()
        if self.tray:
            self.tray.hide()
        if hasattr(self, "chk_tray"):
            self.chk_tray.blockSignals(True)
            self.chk_tray.setChecked(False)
            self.chk_tray.blockSignals(False)
        QtWidgets.QMessageBox.information(
            self, "托盘图标已隐藏",
            "图标已隐藏，程序继续在后台运行。\n\n"
            "· 再次双击程序图标可打开这个窗口\n"
            "· 窗口里勾选「在系统托盘显示图标」可恢复",
        )

    # ---- 逻辑 ----
    def _voice_desc(self) -> str:
        mode = self.service.config.get("voice_mode")
        if mode == "wechat":
            return "微信语音模式：按住语音键说完松手，声音交给微信识别（自动去语气词、整理语句）。比本地模式多等几秒。"
        return "本地 whisper 转写：按住语音键说话，松手出字。模型越大越准、越慢。"

    def _on_mode_change(self):
        self.service.config["voice_mode"] = self.mode_box.currentData()
        self.service.save_config()
        self.voice_desc.setText(self._voice_desc())
        self._append_log(f"语音模式已切换为 {self.mode_box.currentData()}（需重启守护）")

    def _on_model_change(self):
        self.service.config["voice_model"] = self.model_box.currentText()
        self.service.save_config()
        self._append_log(f"识别模型已改为 {self.model_box.currentText()}")

    def _refresh_keys(self):
        self.key_list.clear()
        keys = self.service.config.get("keys", {})
        self._key_names = sorted(k for k in keys if not k.startswith("_"))
        for name in self._key_names:
            conf = keys[name]
            summary = action_summary(conf.get("on_down", {"type": "none"}))
            item = QtWidgets.QListWidgetItem(f"{conf.get('label', name)}    {summary}")
            item.setData(QtCore.Qt.UserRole, name)
            self.key_list.addItem(item)
        if self.key_list.count():
            selected_index = 0
            if self.current_key:
                for i in range(self.key_list.count()):
                    if self.key_list.item(i).data(QtCore.Qt.UserRole) == self.current_key:
                        selected_index = i
                        break
            self.key_list.setCurrentRow(selected_index)
        self._update_remote_state()
        self._render_status(self.service.status())

    def _update_remote_state(self):
        bound = set()
        labels = {}
        keys = self.service.config.get("keys", {})
        label_to_name = {}
        for bid, name, *_ in KEY_RECTS:
            label_to_name[name] = bid
        for k, conf in keys.items():
            if not isinstance(conf, dict):
                continue
            lbl = conf.get("label", "")
            display_label = "返回" if lbl.startswith("返回") else lbl
            if display_label in label_to_name:
                if conf.get("on_down", {}).get("type") != "none":
                    bound.add(display_label)
                labels[display_label] = action_summary(conf.get("on_down", {"type": "none"}))
        self.remote.set_state(bound, labels)

    def _on_remote_click(self, name: str):
        for k, conf in self.service.config.get("keys", {}).items():
            label = conf.get("label", "") if isinstance(conf, dict) else ""
            if label == name or (name == "返回" and label.startswith("返回")):
                self._select_key(k)
                return
        self._append_log(f"点中了遥控器【{name}】，但配置里没有对应键，请用「学习新键」绑定")

    def _select_key(self, name: str):
        self.current_key = name
        for i in range(self.key_list.count()):
            it = self.key_list.item(i)
            if it.data(QtCore.Qt.UserRole) == name:
                self.key_list.setCurrentItem(it)
                break
        conf = self.service.config["keys"][name]
        self.edit_title.setText(f"编辑: {conf.get('label', name)}  ({name})")
        action = conf.get("on_down", {"type": "none"})
        self.selected_key_chip.setText(conf.get("label", name))
        self.selected_key_title.setText(action_summary(action))
        self.selected_key_meta.setText(f"{name} · 修改后点击“保存此键”")
        self._pending_action = dict(action)
        self.action_detail.setText(json.dumps(action, ensure_ascii=False))
        # 匹配预设
        matched = self._match_preset(action)
        for i in range(self.action_box.count()):
            if self.action_box.itemText(i) == matched:
                self.action_box.blockSignals(True)
                self.action_box.setCurrentIndex(i)
                self.action_box.blockSignals(False)
                break

    def _on_select_key(self, item, _prev):
        if not item:
            return
        self._select_key(item.data(QtCore.Qt.UserRole))

    def _match_preset(self, action: dict) -> str:
        for label, a in PRESETS:
            if a is None:
                continue
            if a == action:
                return label
        t = action.get("type", "none")
        for label, a in PRESETS:
            if a and a.get("type") == t:
                return label
        return "无操作"

    def _on_preset(self, idx):
        label = self.action_box.itemText(idx)
        for l, preset in PRESETS:
            if l == label:
                if preset is None:
                    self._handle_special(l)
                else:
                    self._pending_action = dict(preset)
                    self.action_detail.setText(json.dumps(self._pending_action, ensure_ascii=False))
                return

    def _handle_special(self, label: str):
        if label == "录制单个键…":
            dlg = _RecordKeyDialog(self)
            if dlg.exec() == QtWidgets.QDialog.Accepted and dlg.result_value:
                self._pending_action = {"type": "tap", "key": dlg.result_value}
        elif label == "录制组合键…":
            dlg = _RecordKeyDialog(self, combo=True)
            if dlg.exec() == QtWidgets.QDialog.Accepted and dlg.result_value:
                self._pending_action = {"type": "keys", "combo": dlg.result_value}
        elif label == "输入文本…":
            text, ok = QtWidgets.QInputDialog.getText(self, "输入文本", "要输入的文本:")
            if ok and text:
                self._pending_action = {"type": "type", "text": text}
        elif label == "运行程序…":
            cmd, ok = QtWidgets.QInputDialog.getText(self, "运行程序", "可执行文件路径（可带参数）:")
            if ok and cmd:
                import shlex
                self._pending_action = {"type": "run", "argv": shlex.split(cmd)}
        if hasattr(self, "_pending_action"):
            self.action_detail.setText(json.dumps(self._pending_action, ensure_ascii=False))

    def _save_key(self):
        if not self.current_key:
            QtWidgets.QMessageBox.information(self, "提示", "请先选择一个按键")
            return
        action = getattr(self, "_pending_action", {"type": "none"})
        self.service.config["keys"][self.current_key]["on_down"] = action
        self.service.save_config()
        self._append_log(f"已保存 {self.current_key} -> {action_summary(action)}")
        self._refresh_keys()

    def _learn_keys(self):
        from .learn_qt import LearnDialog
        dlg = LearnDialog(self.service, self)
        dlg.exec()
        self._refresh_keys()

    def _toggle_service(self):
        if self.service.running:
            self.service.stop()
            self._append_log("守护已停止")
        else:
            self._append_log("正在启动守护…")
            threading.Thread(target=self._do_start, daemon=True).start()

    def _do_start(self):
        if not self.service.start():
            self._append_log("启动失败，见日志")

    def _render_status(self, st: dict):
        if st.get("running"):
            self.status_badge.setText("● 运行中")
            self.status_badge.setStyleSheet(
                f"color: {GREEN}; font-size: 16px; font-weight: 700;"
            )
            self.btn_toggle.setText("停止守护")
            self.btn_toggle.setObjectName("danger")
            if st.get("voice_ready"):
                self.status_hint.setText("按键监听 · 语音已就绪")
            else:
                self.status_hint.setText("按键监听 · 语音连接中…")
        else:
            self.status_badge.setText("● 已停止")
            self.status_badge.setStyleSheet(
                f"color: {RED}; font-size: 16px; font-weight: 700;"
            )
            self.btn_toggle.setText("启动守护")
            self.btn_toggle.setObjectName("primary")
            self.status_hint.setText("服务未启动")
        self.btn_toggle.style().unpolish(self.btn_toggle)
        self.btn_toggle.style().polish(self.btn_toggle)

    def _append_log(self, msg: str):
        self.log_text.appendPlainText(f"{time.strftime('%H:%M:%S')}  {msg}")

    # ---- 关闭到托盘 ----
    def closeEvent(self, ev):
        if self.tray and self.tray.isVisible():
            ev.ignore()
            self.hide()
            return
        # 托盘隐藏/不存在时：问用户是继续后台还是真正退出
        btn = QtWidgets.QMessageBox.question(
            self, "关闭窗口",
            "关闭窗口后程序要继续在后台运行吗？\n\n"
            "· 是 —— 继续后台运行（再次双击程序图标可打开窗口）\n"
            "· 否 —— 完全退出（守护停止）",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        )
        if btn == QtWidgets.QMessageBox.Yes:
            ev.ignore()
            self.hide()
        else:
            if self.service.running:
                self.service.stop()
            ev.accept()


class _SignalBridge(QtCore.QObject):
    message = QtCore.Signal(str)


class _StatusBridge(QtCore.QObject):
    status = QtCore.Signal(dict)


class _RecordKeyDialog(QtWidgets.QDialog):
    def __init__(self, parent, combo: bool = False):
        super().__init__(parent)
        self.combo = combo
        self.result_value = None
        self._pressed = set()
        self.setWindowTitle("录制" + ("组合键" if combo else "按键"))
        self.setModal(True)
        lay = QtWidgets.QVBoxLayout(self)
        if combo:
            tip = "按住修饰键（Ctrl/Shift/Alt/Win）再按主键，松开完成"
        else:
            tip = "按下任意一个键即完成录制"
        lay.addWidget(QtWidgets.QLabel(tip))
        self.lbl = QtWidgets.QLabel("等待按键…")
        self.lbl.setStyleSheet(f"color: {ACCENT}; font-size: 16px; font-weight: bold;")
        lay.addWidget(self.lbl)
        btn = QtWidgets.QPushButton("取消")
        btn.clicked.connect(self.reject)
        lay.addWidget(btn, 0, QtCore.Qt.AlignRight)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)

    def keyPressEvent(self, ev):
        vk = _vk_name(ev.key())
        if vk:
            self._pressed.add(vk)
            if not self.combo:
                self.result_value = vk
                self.lbl.setText(vk)
                self.accept()
            else:
                self.lbl.setText("+".join(sorted(self._pressed)))

    def keyReleaseEvent(self, ev):
        vk = _vk_name(ev.key())
        if vk and vk in self._pressed and self.combo and len(self._pressed) > 1:
            mods = {"VK_CONTROL", "VK_SHIFT", "VK_MENU", "VK_LWIN", "VK_RWIN"}
            main = [v for v in self._pressed if v not in mods]
            if main:
                self.result_value = sorted(self._pressed)
                self.accept()


def _vk_name(qt_key: int) -> str | None:
    from PySide6 import QtCore
    m = {
        QtCore.Qt.Key_Control: "VK_CONTROL", QtCore.Qt.Key_Shift: "VK_SHIFT",
        QtCore.Qt.Key_Alt: "VK_MENU", QtCore.Qt.Key_Meta: "VK_LWIN",
        QtCore.Qt.Key_Return: "VK_RETURN", QtCore.Qt.Key_Enter: "VK_RETURN",
        QtCore.Qt.Key_Escape: "VK_ESCAPE", QtCore.Qt.Key_Backspace: "VK_BACK",
        QtCore.Qt.Key_Delete: "VK_DELETE", QtCore.Qt.Key_Tab: "VK_TAB",
        QtCore.Qt.Key_Space: "VK_SPACE", QtCore.Qt.Key_Home: "VK_HOME",
        QtCore.Qt.Key_End: "VK_END", QtCore.Qt.Key_Left: "VK_LEFT",
        QtCore.Qt.Key_Right: "VK_RIGHT", QtCore.Qt.Key_Up: "VK_UP",
        QtCore.Qt.Key_Down: "VK_DOWN", QtCore.Qt.Key_PageUp: "VK_PRIOR",
        QtCore.Qt.Key_PageDown: "VK_NEXT",
    }
    if qt_key in m:
        return m[qt_key]
    c = chr(qt_key) if 0x20 <= qt_key <= 0x7e else ""
    if c.isalpha():
        return "VK_" + c.upper()
    if QtCore.Qt.Key_F1 <= qt_key <= QtCore.Qt.Key_F24:
        return f"VK_F{qt_key - QtCore.Qt.Key_F1 + 1}"
    return None


def main():
    import sys
    silent = "--silent" in sys.argv  # 开机自启的静默模式：不弹窗口
    app = QtWidgets.QApplication(sys.argv)
    mutex = _acquire_single_instance()
    if mutex is None:
        # 已有实例在跑：写唤回信号文件，第一实例的轮询定时器
        # 会自己 show/raise（Qt 亲自显示才正确重绘；外部 ShowWindow 会导致空白窗口）
        import tempfile
        flag = Path(tempfile.gettempdir()).resolve() / "miremote_show.flag"
        try:
            if ".." not in str(flag):
                flag.write_text("show", encoding="utf-8")
        except OSError:
            pass
        return
    app.aboutToQuit.connect(lambda: mutex[0](mutex[1]))
    app.setQuitOnLastWindowClosed(False)  # 关窗不退出，靠托盘
    win = MiRemoteWindow()
    if not silent:
        win.show()
    # 打开即自动启动守护（默认开，可在控制页关闭）
    if win.service.config.get("auto_start_service", True):
        QtCore.QTimer.singleShot(400, win._auto_start_service)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
