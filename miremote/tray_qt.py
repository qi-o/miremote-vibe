"""Qt 系统托盘。"""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets


def create_tray(window, service):
    """创建托盘图标，返回 QSystemTrayIcon。"""
    tray = QtWidgets.QSystemTrayIcon(window)

    # 画一个简单的遥控器图标
    pm = QtGui.QPixmap(64, 64)
    pm.fill(QtCore.Qt.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    p.setBrush(QtGui.QColor("#4f8cff"))
    p.setPen(QtCore.Qt.NoPen)
    p.drawRoundedRect(24, 6, 16, 52, 6, 6)
    p.setPen(QtGui.QPen(QtGui.QColor("#e9eaec"), 3))
    p.setBrush(QtCore.Qt.NoBrush)
    p.drawEllipse(16, 14, 32, 32)
    p.setBrush(QtGui.QColor("#e9eaec"))
    p.drawEllipse(28, 26, 8, 8)
    p.end()

    tray.setIcon(QtGui.QIcon(pm))
    tray.setToolTip("小米遥控器")

    menu = QtWidgets.QMenu()
    act_show = menu.addAction("显示控制台")
    act_show.triggered.connect(lambda: (window.show(), window.raise_()))
    act_toggle = menu.addAction("停止守护" if service.running else "启动守护")
    act_toggle.triggered.connect(lambda: (
        service.stop() if service.running else service.start()))
    menu.addSeparator()
    act_hide_tray = menu.addAction("隐藏托盘图标")
    act_hide_tray.triggered.connect(lambda: window.hide_tray_persist())
    menu.addSeparator()
    act_quit = menu.addAction("退出")
    act_quit.triggered.connect(lambda: _quit(window, service, tray))
    tray.setContextMenu(menu)
    tray.activated.connect(lambda reason: (
        window.show(), window.raise_()) if reason == QtWidgets.QSystemTrayIcon.DoubleClick else None)
    tray.show()
    return tray


def _quit(window, service, tray):
    if service.running:
        service.stop()
    tray.hide()
    window.close()
    QtWidgets.QApplication.quit()
