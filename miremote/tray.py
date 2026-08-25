"""系统托盘：后台常驻，菜单控制守护启停与窗口显隐。"""

from __future__ import annotations

import threading

from PIL import Image, ImageDraw


def _make_icon() -> Image.Image:
    """画一个简单的遥控器图标（无外部图片依赖）。"""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 遥控器机身（圆角竖条）
    d.rounded_rectangle((24, 6, 40, 58), radius=8, fill=(61, 126, 166, 255))
    # 方向键圆盘
    d.ellipse((16, 14, 48, 46), outline=(232, 232, 232, 255), width=3)
    d.ellipse((29, 27, 35, 33), fill=(232, 232, 232, 255))  # 中心 OK
    # 顶部麦克风点
    d.ellipse((28, 10, 36, 18), fill=(232, 232, 232, 255))
    return img


class TrayController:
    """封装 pystray 托盘，与主 GUI 解耦。"""

    def __init__(self, service, on_show=None, on_exit=None):
        self.service = service
        self.on_show = on_show
        self.on_exit = on_exit
        self.icon = None
        self._thread: threading.Thread | None = None

    def _menu(self):
        import pystray

        def toggle(_icon, _item):
            if self.service.running:
                self.service.stop()
            else:
                threading.Thread(target=self.service.start, daemon=True).start()

        items = [
            pystray.MenuItem("显示控制台", lambda i, it: self.on_show and self.on_show()),
            pystray.MenuItem(
                lambda item: "停止守护" if self.service.running else "启动守护",
                toggle,
            ),
            pystray.MenuItem("退出", self._quit),
        ]
        return pystray.Menu(*items)

    def _quit(self, _icon, _item):
        if self.service.running:
            self.service.stop()
        if self.on_exit:
            self.on_exit()
        if self.icon:
            self.icon.stop()

    def start(self):
        import pystray

        self.icon = pystray.Icon(
            "miremote", _make_icon(), "小米遥控器", menu=self._menu()
        )
        self._thread = threading.Thread(target=self.icon.run, daemon=True)
        self._thread.start()

    def stop(self):
        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                pass


def run_tray(service, on_show=None, on_exit=None) -> TrayController:
    ctl = TrayController(service, on_show, on_exit)
    ctl.start()
    return ctl
