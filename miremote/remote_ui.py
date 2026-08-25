"""用 PIL 绘制小米蓝牙遥控器 2 Pro (RC003) 矢量示意图 + 热区定位。

图标全部用几何绘制（圆弧、多边形、线条），不依赖字体是否支持特殊 Unicode
符号，避免出现 "□" 方框。机身与按键用较高对比度的配色，保证在深色背景上清晰。
"""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

CANVAS_W, CANVAS_H = 300, 620

# 按键物理布局（机身内相对坐标）: (id, 中文名, x, y, w, h)
KEY_RECTS: list[tuple[str, str, int, int, int, int]] = [
    ("power", "电源", 118, 58, 64, 38),
    ("voice", "语音", 118, 96, 64, 42),
    ("home", "主页", 118, 168, 50, 36),
    ("back", "返回", 132, 216, 50, 36),
    ("menu", "菜单", 118, 264, 50, 36),
    ("tv", "TV", 132, 312, 50, 36),
    ("up", "↑", 118, 372, 48, 42),
    ("left", "←", 70, 428, 42, 42),
    ("ok", "OK", 118, 428, 48, 42),
    ("right", "→", 166, 428, 42, 42),
    ("down", "↓", 118, 484, 48, 42),
    ("volup", "音量+", 74, 542, 56, 34),
    ("voldown", "音量-", 156, 542, 56, 34),
]

# 配色（相对较高的对比度，深色背景可见）
BODY_FILL = (40, 42, 48, 255)
BODY_EDGE = (180, 184, 192, 255)
BTN_FILL = (78, 82, 92, 255)
BTN_EDGE = (200, 204, 212, 255)
BTN_DPAD = (64, 68, 78, 255)
BTN_TEXT = (235, 238, 244, 255)
BRAND = (200, 204, 212, 255)
SUBTEXT = (150, 154, 162, 255)
HILITE_BOUND = (48, 92, 60, 255)     # 已绑定 -> 绿
HILITE_NONE = (110, 114, 124, 255)   # 未绑定 -> 灰


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    # 只用能可靠渲染的字体文件；字符只用字母/数字/标点/常用中文字
    for path in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc",
                 "C:/Windows/Fonts/segoeui.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _draw_icon(d: ImageDraw.ImageDraw, key_id: str, cx: int, cy: int,
               color, size: int):
    """绘制按键图标（全部几何或安全文字，不用特殊 Unicode 符号）。"""
    s = size
    if key_id == "power":
        # 电源符号：圆弧 + 顶部竖线
        d.arc((cx - s, cy - s + 2, cx + s, cy + s - 2), start=130, end=410,
              fill=color, width=3)
        d.line((cx, cy - s - 2, cx, cy - 1), fill=color, width=3)
    elif key_id == "voice":
        # 麦克风：圆头 + 竖杆 + 底座弧
        d.rounded_rectangle((cx - 5, cy - s // 2, cx + 5, cy + 5), radius=5,
                            fill=color)
        d.pieslice((cx - s // 2, cy - s // 2, cx + s // 2, cy + s // 2),
                   start=180, end=360, outline=color, width=3)
        d.line((cx, cy + 3, cx, cy + 8), fill=color, width=3)
    elif key_id == "home":
        # 房子：三角屋顶 + 矩形
        d.polygon([(cx - s, cy), (cx, cy - s), (cx + s, cy)], outline=color, width=2)
        d.rounded_rectangle((cx - s // 2, cy, cx + s // 2, cy + s), radius=2,
                            fill=color)
    elif key_id == "back":
        # 返回：左折线箭头
        d.line((cx + s // 2, cy, cx - s // 2, cy), fill=color, width=3)
        d.line((cx - s // 2, cy, cx - s // 2 + 6, cy - 6), fill=color, width=3)
        d.line((cx - s // 2, cy, cx - s // 2 + 6, cy + 6), fill=color, width=3)
    elif key_id == "menu":
        # 菜单：三条横线
        for dy in (-s // 2, 0, s // 2):
            d.line((cx - s // 2, cy + dy, cx + s // 2, cy + dy), fill=color, width=2)
    elif key_id in ("up", "down", "left", "right"):
        t = 5
        if key_id == "up":
            pts = [(cx, cy - s // 2), (cx - t, cy - s // 2 + t * 2), (cx + t, cy - s // 2 + t * 2)]
        elif key_id == "down":
            pts = [(cx, cy + s // 2), (cx - t, cy + s // 2 - t * 2), (cx + t, cy + s // 2 - t * 2)]
        elif key_id == "left":
            pts = [(cx - s // 2, cy), (cx - s // 2 + t * 2, cy - t), (cx - s // 2 + t * 2, cy + t)]
        else:
            pts = [(cx + s // 2, cy), (cx + s // 2 - t * 2, cy - t), (cx + s // 2 - t * 2, cy + t)]
        d.polygon(pts, fill=color)
    elif key_id == "ok":
        d.text((cx, cy), "OK", font=_font(13), fill=color, anchor="mm")
    elif key_id == "tv":
        d.text((cx, cy), "TV", font=_font(15), fill=color, anchor="mm")
    elif key_id == "volup":
        d.text((cx, cy), "+", font=_font(20), fill=color, anchor="mm")
    elif key_id == "voldown":
        d.text((cx, cy), "−", font=_font(20), fill=color, anchor="mm")


def draw_remote(highlight: dict[str, str] | None = None,
                scale: float = 1.0) -> Image.Image:
    """绘制遥控器示意图。highlight: {按键id: ('bound'|'unbound')}。
    scale>1 时输出放大版（所有坐标与文字同步放大），保持清晰。"""
    highlight = highlight or {}

    # 用虚拟画笔按原始坐标绘制，再整体放大（对文字也清晰）
    base = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    _draw_on(base, highlight)
    if scale != 1.0 and scale > 1:
        base = base.resize((int(CANVAS_W * scale), int(CANVAS_H * scale)),
                           Image.LANCZOS)
    return base


def _draw_on(img: Image.Image, highlight: dict[str, str]):
    d = ImageDraw.Draw(img)

    # 机身（圆角胶囊，带清晰描边）
    body_l, body_t, body_r, body_b = 36, 8, 264, 612
    d.rounded_rectangle((body_l, body_t, body_r, body_b), radius=62,
                        fill=BODY_FILL, outline=BODY_EDGE, width=3)
    # 顶部充电口
    d.rounded_rectangle((128, 14, 172, 26), radius=6, fill=(90, 94, 104, 255),
                        outline=BODY_EDGE, width=1)
    # 品牌
    d.text((150, 36), "MI", font=_font(13), fill=BRAND, anchor="mm")
    d.text((150, 51), "语音遥控器", font=_font(8), fill=SUBTEXT, anchor="mm")

    # 按键
    for key_id, _name, x, y, w, h in KEY_RECTS:
        cx, cy = x + w // 2, y + h // 2
        is_dpad = key_id in ("up", "down", "left", "right", "ok")
        state = highlight.get(key_id)
        if state == "bound":
            fill = (42, 82, 56, 255)
            edge = (80, 150, 96, 255)
        elif state == "unbound":
            fill = (110, 114, 124, 255)
            edge = (170, 174, 182, 255)
        else:
            fill = BTN_DPAD if is_dpad else BTN_FILL
            edge = BTN_EDGE

        if key_id == "ok":
            d.ellipse((x, y, x + w, y + h), fill=fill, outline=edge, width=2)
        else:
            d.rounded_rectangle((x, y, x + w, y + h), radius=9, fill=fill,
                                outline=edge, width=1)

        icon_color = (240, 242, 246, 255)
        _draw_icon(d, key_id, cx, cy, icon_color, min(w, h) // 2 - 2)

    return img


def hit_test(x: int, y: int) -> str | None:
    """画布坐标 -> 命中的按键 id。"""
    for key_id, _n, kx, ky, kw, kh in KEY_RECTS:
        if kx <= x <= kx + kw and ky <= y <= ky + kh:
            return key_id
    return None


if __name__ == "__main__":
    img = draw_remote()
    img.save("remote_check.png")
    print(f"已生成 remote_check.png ({img.width}x{img.height})")
    print("hit_test(120,372):", hit_test(120, 372))
