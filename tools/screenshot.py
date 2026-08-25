"""给 GitHub README 截 UI 图：启动真窗口 -> 逐页截图 -> 退出。"""

import sys
import time

sys.path.insert(0, ".")

from PySide6 import QtCore, QtWidgets


def main():
    app = QtWidgets.QApplication(sys.argv)
    from miremote.gui import MiRemoteWindow
    win = MiRemoteWindow()
    win.resize(1280, 800)
    win.show()

    shots = {}

    def grab(name):
        app.processEvents()
        time.sleep(0.4)
        app.processEvents()
        pix = win.grab()
        shots[name] = pix
        print(f"已截: {name} {pix.width()}x{pix.height()}")

    def step():
        grab("ui-control.png")
        win.tabs.setCurrentIndex(1)
        QtCore.QTimer.singleShot(500, step2)

    def step2():
        grab("ui-mapping.png")
        win.tabs.setCurrentIndex(2)
        QtCore.QTimer.singleShot(500, step3)

    def step3():
        grab("ui-voice.png")
        for name, pix in shots.items():
            pix.save(name)
            print(f"已保存: {name}")
        app.quit()

    QtCore.QTimer.singleShot(1200, step)
    QtCore.QTimer.singleShot(15000, app.quit)  # 兜底退出
    app.exec()
    print("完成")


if __name__ == "__main__":
    main()
