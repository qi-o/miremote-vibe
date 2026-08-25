"""PySide6 遥控器控件：QPainter 矢量绘制 + 鼠标热区点击。

相比 PIL 位图方案，Qt 矢量绘制在高 DPI 下缩放无损、文字渲染清晰，
热区坐标精确（与 KEY_RECTS 一致），且能随窗口尺寸自适应。
"""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

# 按键物理布局（逻辑坐标，绘制时按控件尺寸等比映射）: (id, 中文名, x, y, w, h)
KEY_RECTS: list[tuple[str, str, float, float, float, float]] = [
    ("power", "电源", 0.30, 0.07, 0.18, 0.075),
    ("voice", "语音", 0.52, 0.07, 0.18, 0.075),
    ("up", "↑", 0.40, 0.18, 0.20, 0.065),
    ("left", "←", 0.27, 0.25, 0.13, 0.105),
    ("ok", "OK", 0.40, 0.25, 0.20, 0.105),
    ("right", "→", 0.60, 0.25, 0.13, 0.105),
    ("down", "↓", 0.40, 0.355, 0.20, 0.065),
    ("back", "返回", 0.29, 0.46, 0.18, 0.085),
    ("volup", "音量+", 0.53, 0.46, 0.18, 0.085),
    ("home", "主页", 0.29, 0.565, 0.18, 0.085),
    ("voldown", "音量-", 0.53, 0.565, 0.18, 0.085),
    ("menu", "菜单", 0.29, 0.67, 0.18, 0.085),
    ("tv", "TV", 0.53, 0.67, 0.18, 0.085),
]

_D_PAD_IDS = {"up", "left", "ok", "right", "down"}


class RemoteWidget(QtWidgets.QWidget):
    """遥控器示意图控件，可点击。点击回调 on_key_clicked(中文名)。"""

    clicked = QtCore.Signal(str)  # 中文键名

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(220, 440)
        self.setSizePolicy(QtWidgets.QSizePolicy.Preferred,
                           QtWidgets.QSizePolicy.Preferred)
        self.setMouseTracking(True)
        self._bound: set[str] = set()   # 已绑定按键的 中文名
        self._labels: dict[str, str] = {}  # 中文名 -> 当前动作简述
        self._hovered: str | None = None

    def set_state(self, bound_names: set[str], labels: dict[str, str]):
        self._bound = bound_names
        self._labels = labels
        self.update()

    def _rect_for(self, rid: str) -> QtCore.QRectF:
        for bid, _n, x, y, w, h in KEY_RECTS:
            if bid == rid:
                return QtCore.QRectF(
                    x * self.width(), y * self.height(),
                    w * self.width(), h * self.height())
        return QtCore.QRectF()

    # ---- 绘制 ----
    def paintEvent(self, _ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        W, H = self.width(), self.height()

        body = QtCore.QRectF(W * 0.22, H * 0.015, W * 0.56, H * 0.95)
        shadow = body.translated(0, 5)
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(QtGui.QColor(0, 0, 0, 85))
        p.drawRoundedRect(shadow, W * 0.055, W * 0.055)
        p.setPen(QtGui.QPen(QtGui.QColor("#6d7580"), 1.5))
        gradient = QtGui.QLinearGradient(body.topLeft(), body.topRight())
        gradient.setColorAt(0.0, QtGui.QColor("#9da3aa"))
        gradient.setColorAt(0.16, QtGui.QColor("#e7e9ea"))
        gradient.setColorAt(0.5, QtGui.QColor("#c5c9cd"))
        gradient.setColorAt(0.84, QtGui.QColor("#eef0f1"))
        gradient.setColorAt(1.0, QtGui.QColor("#8f969e"))
        p.setBrush(gradient)
        p.drawRoundedRect(body, W * 0.055, W * 0.055)

        f = QtGui.QFont("Segoe UI", max(9, int(W * 0.04)))
        f.setBold(True)
        p.setFont(f)
        p.setPen(QtGui.QColor("#34383d"))
        p.drawText(QtCore.QRectF(0, H * 0.885, W, H * 0.025),
                   QtCore.Qt.AlignCenter, "N")
        f.setPointSize(max(8, int(W * 0.032)))
        p.setFont(f)
        p.drawText(QtCore.QRectF(0, H * 0.915, W, H * 0.025),
                   QtCore.Qt.AlignCenter, "xiaomi")

        dpad = QtCore.QRectF(W * 0.275, H * 0.17, W * 0.45, H * 0.27)
        dpad_hover = any(self._hovered == key for key in _D_PAD_IDS)
        dpad_edge = QtGui.QColor("#d7ad62") if dpad_hover else QtGui.QColor("#20252a")
        p.setPen(QtGui.QPen(dpad_edge, 1.5))
        dpad_gradient = QtGui.QRadialGradient(dpad.center(), dpad.width() * 0.7)
        dpad_gradient.setColorAt(0.0, QtGui.QColor("#32373d"))
        dpad_gradient.setColorAt(1.0, QtGui.QColor("#101316"))
        p.setBrush(dpad_gradient)
        p.drawEllipse(dpad)
        center = QtCore.QRectF(W * 0.39, H * 0.245, W * 0.22, H * 0.12)
        p.setPen(QtGui.QPen(QtGui.QColor("#616871"), 1))
        p.setBrush(QtGui.QColor("#252a2f"))
        p.drawEllipse(center)

        for rid, name, x, y, w, h in KEY_RECTS:
            if rid in _D_PAD_IDS:
                r = QtCore.QRectF(x * W, y * H, w * W, h * H)
                if rid == "ok":
                    p.setPen(QtGui.QColor("#f1f3f4"))
                    _draw_key_glyph(p, rid, r, W)
                elif self._hovered == name:
                    _draw_key_glyph(p, rid, r, W)
                else:
                    _draw_key_glyph(p, rid, r, W)
                continue
            r = QtCore.QRectF(x * W, y * H, w * W, h * H)
            if name == self._hovered:
                fill = QtGui.QColor("#8e6930")
                edge = QtGui.QColor("#f2c777")
            elif name in self._bound:
                fill = QtGui.QColor("#245d4b")
                edge = QtGui.QColor("#57d5a0")
            else:
                fill = QtGui.QColor("#15191d")
                edge = QtGui.QColor("#565e66")
            p.setPen(QtGui.QPen(edge, 1))
            if name in self._bound or name == self._hovered:
                p.setBrush(fill)
            else:
                button_gradient = QtGui.QRadialGradient(r.center(), r.width() * 0.7)
                button_gradient.setColorAt(0.0, QtGui.QColor("#353b41"))
                button_gradient.setColorAt(1.0, QtGui.QColor("#0e1114"))
                p.setBrush(button_gradient)
            p.drawEllipse(r)
            p.setPen(QtGui.QColor("#e8eaef"))
            _draw_key_glyph(p, rid, r, W)

        p.end()

    # ---- 鼠标 ----
    def mousePressEvent(self, ev):
        pos = ev.position()
        for bid, name, x, y, w, h in KEY_RECTS:
            r = QtCore.QRectF(x * self.width(), y * self.height(),
                              w * self.width(), h * self.height())
            if r.contains(pos):
                self.clicked.emit(name)
                return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        pos = ev.position()
        hovered = None
        for _bid, name, x, y, w, h in KEY_RECTS:
            if QtCore.QRectF(x * self.width(), y * self.height(),
                             w * self.width(), h * self.height()).contains(pos):
                hovered = name
                break
        if hovered != self._hovered:
            self._hovered = hovered
            self.setCursor(QtCore.Qt.PointingHandCursor if hovered else QtCore.Qt.ArrowCursor)
            self.update()
        super().mouseMoveEvent(ev)

    def leaveEvent(self, ev):
        if self._hovered is not None:
            self._hovered = None
            self.setCursor(QtCore.Qt.ArrowCursor)
            self.update()
        super().leaveEvent(ev)


def _draw_key_glyph(p: QtGui.QPainter, rid: str, r: QtCore.QRectF, W: float):
    cx, cy = r.center().x(), r.center().y()
    s = min(r.width(), r.height()) / 2
    pen = QtGui.QPen(QtGui.QColor("#e8eaef"), max(1.5, W * 0.008))
    pen.setCapStyle(QtCore.Qt.RoundCap)
    pen.setJoinStyle(QtCore.Qt.RoundJoin)
    p.setPen(pen)

    if rid == "power":
        p.drawArc(QtCore.QRectF(cx - s * 0.6, cy - s * 0.5, s * 1.2, s * 1.0),
                  int(130 * 16), int(280 * 16))
        p.drawLine(QtCore.QPointF(cx, cy - s * 0.6), QtCore.QPointF(cx, cy))
    elif rid == "voice":
        p.setBrush(QtGui.QColor("#e8eaef"))
        p.drawRoundedRect(QtCore.QRectF(cx - s * 0.3, cy - s * 0.6, s * 0.6, s * 0.8), 3, 3)
        p.setBrush(QtCore.Qt.NoBrush)
        p.drawArc(QtCore.QRectF(cx - s * 0.5, cy - s * 0.4, s, s), int(180 * 16), int(180 * 16))
        p.drawLine(QtCore.QPointF(cx, cy + 0.1 * s), QtCore.QPointF(cx, cy + 0.4 * s))
    elif rid == "home":
        p.drawPolygon([QtCore.QPointF(cx - s * 0.5, cy), QtCore.QPointF(cx, cy - s * 0.5),
                       QtCore.QPointF(cx + s * 0.5, cy)])
        p.setBrush(QtGui.QColor("#e8eaef"))
        p.drawRect(QtCore.QRectF(cx - s * 0.3, cy, s * 0.6, s * 0.45))
    elif rid == "back":
        p.drawLine(QtCore.QPointF(cx + s * 0.4, cy), QtCore.QPointF(cx - s * 0.4, cy))
        p.drawLine(QtCore.QPointF(cx - s * 0.4, cy), QtCore.QPointF(cx - s * 0.1, cy - s * 0.3))
        p.drawLine(QtCore.QPointF(cx - s * 0.4, cy), QtCore.QPointF(cx - s * 0.1, cy + s * 0.3))
    elif rid == "menu":
        for dy in (-s * 0.4, 0, s * 0.4):
            p.drawLine(QtCore.QPointF(cx - s * 0.4, cy + dy), QtCore.QPointF(cx + s * 0.4, cy + dy))
    elif rid in ("up", "down", "left", "right"):
        t = s * 0.45
        if rid == "up":
            pts = [QtCore.QPointF(cx, cy - t), QtCore.QPointF(cx - t, cy + t * 0.4), QtCore.QPointF(cx + t, cy + t * 0.4)]
        elif rid == "down":
            pts = [QtCore.QPointF(cx, cy + t), QtCore.QPointF(cx - t, cy - t * 0.4), QtCore.QPointF(cx + t, cy - t * 0.4)]
        elif rid == "left":
            pts = [QtCore.QPointF(cx - t, cy), QtCore.QPointF(cx + t * 0.4, cy - t), QtCore.QPointF(cx + t * 0.4, cy + t)]
        else:
            pts = [QtCore.QPointF(cx + t, cy), QtCore.QPointF(cx - t * 0.4, cy - t), QtCore.QPointF(cx - t * 0.4, cy + t)]
        p.setBrush(QtGui.QColor("#e8eaef"))
        p.drawPolygon(pts)
    elif rid == "ok":
        f = QtGui.QFont("Segoe UI", max(9, int(W * 0.042)))
        f.setBold(True)
        p.setFont(f)
        p.drawText(r, QtCore.Qt.AlignCenter, "OK")
    elif rid == "tv":
        f = QtGui.QFont("Segoe UI", max(9, int(W * 0.05)))
        f.setBold(True)
        p.setFont(f)
        p.drawText(r, QtCore.Qt.AlignCenter, "TV")
    elif rid == "volup":
        f = QtGui.QFont("Segoe UI", max(12, int(W * 0.06)))
        f.setBold(True)
        p.setFont(f)
        p.drawText(r, QtCore.Qt.AlignCenter, "+")
    elif rid == "voldown":
        f = QtGui.QFont("Segoe UI", max(12, int(W * 0.06)))
        f.setBold(True)
        p.setFont(f)
        p.drawText(r, QtCore.Qt.AlignCenter, "−")
