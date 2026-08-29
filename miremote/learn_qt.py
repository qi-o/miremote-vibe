"""学习新键对话框（PySide6）：选名字 -> 按遥控器物理键 -> 绑定。"""

from __future__ import annotations

import queue
import threading

from PySide6 import QtCore, QtWidgets

from .keys import vk_name
from .rawinput import RawInputEngine


class LearnDialog(QtWidgets.QDialog):
    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self.setWindowTitle("学习新键")
        self.setModal(True)
        self.resize(430, 270)
        self._queue: "queue.Queue" = queue.Queue()
        self._engine = None

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 20)
        lay.setSpacing(12)
        title = QtWidgets.QLabel("绑定一个新的遥控器按键")
        title.setObjectName("sectionTitle")
        lay.addWidget(title)
        intro = QtWidgets.QLabel("先选择它的名字，再按下遥控器上对应的实体按键。")
        intro.setObjectName("muted")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        self.name_box = QtWidgets.QComboBox()
        self.name_box.addItems(["返回", "主页", "菜单", "TV", "电源", "语音",
                                "音量+", "音量-"])
        lay.addWidget(self.name_box)

        self.lbl = QtWidgets.QLabel("等待按遥控器…")
        self.lbl.setObjectName("metricValue")
        self.lbl.setAlignment(QtCore.Qt.AlignCenter)
        self.lbl.setMinimumHeight(42)
        lay.addWidget(self.lbl)

        btn = QtWidgets.QPushButton("取消")
        btn.setObjectName("ghost")
        btn.clicked.connect(self.reject)
        lay.addWidget(btn)

        self._start_capture()
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(100)

    def _start_capture(self):
        eng = RawInputEngine()

        def on_key(ev):
            if ev.is_remote and not ev.is_up and ev.hid_bytes is None:
                self._queue.put((vk_name(ev.vkey), ev.scan))

        eng.on_key = on_key
        self._engine = eng
        threading.Thread(target=self._run_loop, args=(eng,), daemon=True).start()

    def _run_loop(self, eng):
        eng.start()
        eng.run_forever()

    def _poll(self):
        try:
            vk, scan = self._queue.get_nowait()
        except queue.Empty:
            return
        name = self.name_box.currentText()
        self.service.config["keys"][vk] = {
            "label": name, "scan": f"0x{scan:02X}",
            "on_down": {"type": "none"},
        }
        self.service.save_config()
        self.lbl.setText(f"已绑定 {name} = {vk}")
        self.timer.stop()
        QtCore.QTimer.singleShot(700, self.accept)
