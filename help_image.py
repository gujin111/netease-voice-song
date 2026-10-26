"""把 ``/vs-help`` 的内容渲染成一张 PNG 图片。

为什么要单独一个文件：渲染是纯绘图（Pillow），和插件逻辑无关；缺 Pillow 时
上层会退回文字版，所以这里所有入口都必须「失败返回 None」而不是抛异常。

设计上有一点很关键：**图片和文字共用同一份结构化内容**（``_help_blocks`` 产出的
blocks）。如果各写一份，改了一边忘了另一边，迟早出现「图里写 /vs-login、文字里
写 /点歌登录」这种事。

支持三种 block：
  {"kind": "title"|"sub"|"h2"|"para"|"note"|"footer", "text": str}
  {"kind": "cmd", "cmd": str, "desc": str}
  {"kind": "spacer", "size": float}
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:  # 没有 Pillow 就只有文字版（不 raise，交给上层降级）
    from PIL import Image, ImageDraw, ImageFont
except Exception:  # pragma: no cover - 只在不装 Pillow 的环境触发
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]
    ImageFont = None  # type: ignore[assignment]

# ── 版式（逻辑像素；实际按 SCALE 放大后输出，手机上更清晰） ──
SCALE = 2
WIDTH = 760
PAD = 40
RADIUS = 22

_BG = "#F4F6FB"
_CARD = "#FFFFFF"
_BORDER = "#E4E9F2"
_TITLE = "#101828"
_TEXT = "#344054"
_MUTED = "#667085"
_ACCENT = "#D33A3A"  # 网易云红
_PILL_BG = "#FDF1F1"
_PILL_TEXT = "#B42323"

#: 按优先级找中文字体（Windows 优先微软雅黑，其次黑体/宋体；Linux/macOS 兜底）
_FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)
_BOLD_CANDIDATES = (
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)

_font_cache: Dict[Tuple[str, int, bool], Any] = {}


def _first_existing(candidates: Sequence[str]) -> Optional[str]:
    for item in candidates:
        try:
            if Path(item).is_file():
                return item
        except OSError:
            continue
    return None


_font_path: Optional[str] = None
_bold_path: Optional[str] = None
_resolved = False


def _resolve() -> None:
    global _font_path, _bold_path, _resolved
    if _resolved:
        return
    _resolved = True
    _font_path = _first_existing(_FONT_CANDIDATES)
    _bold_path = _first_existing(_BOLD_CANDIDATES) or _font_path


def available() -> bool:
    """能不能出图（Pillow + 中文字体都在）。"""
    if ImageFont is None:
        return False
    _resolve()
    return bool(_font_path)


def _font(size: float, bold: bool = False) -> Any:
    _resolve()
    path = (_bold_path if bold else _font_path) or _font_path
    key = (str(path), int(round(size * SCALE)), bool(bold))
    cached = _font_cache.get(key)
    if cached is None:
        cached = ImageFont.truetype(str(path), int(round(size * SCALE)))
        _font_cache[key] = cached
    return cached


def _wrap(draw: Any, text: str, font: Any, max_width: float) -> List[str]:
    """按宽度折行：中文按字符、英文尽量按词，超长词再按字符切。

    ⚠️ 单位：``max_width`` 是**逻辑像素**，而 ``draw.textlength`` 返回的是
       已经乘过 SCALE 的像素 —— 不换算的话每行只能写到一半就折。
    """
    text = str(text or "")
    if not text:
        return [""]
    limit = float(max_width) * SCALE
    tokens = re.findall(r"[A-Za-z0-9/_.:%+\-\[\]()<>@#&=|]+|\s+|.", text)
    lines: List[str] = []
    current = ""

    def width(value: str) -> float:
        return draw.textlength(value, font=font)

    for token in tokens:
        if width(current + token) <= limit:
            current += token
            continue
        if current.strip():
            lines.append(current.rstrip())
            current = ""
        if token.isspace():
            continue
        # 单个 token 就超宽：按字符切
        buffer = ""
        for char in token:
            if width(buffer + char) > limit and buffer:
                lines.append(buffer)
                buffer = ""
            buffer += char
        current = buffer
    if current.strip() or not lines:
        lines.append(current.rstrip())
    return lines


def _line_height(size: float) -> int:
    """行高（逻辑像素）——绘制时统一乘 SCALE，这里不要再乘一次。"""
    return int(round(size * 1.55))


def _ops_for_blocks(blocks: Sequence[Dict[str, Any]], draw: Any) -> Tuple[List[Dict[str, Any]], float]:
    """把 blocks 变成绘制指令，并算出总高度。"""
    ops: List[Dict[str, Any]] = []
    x = PAD
    content_w = WIDTH - PAD * 2
    y = float(PAD)

    for block in blocks:
        kind = str(block.get("kind") or "para")
        if kind == "spacer":
            y += float(block.get("size") or 16)
            continue
        if kind == "title":
            font = _font(34, True)
            for line in _wrap(draw, block.get("text") or "", font, content_w):
                ops.append({"kind": "text", "x": x, "y": y, "text": line, "font": font, "fill": _TITLE})
                y += _line_height(34)
            y += 6
        elif kind == "sub":
            font = _font(18)
            for line in _wrap(draw, block.get("text") or "", font, content_w):
                ops.append({"kind": "text", "x": x, "y": y, "text": line, "font": font, "fill": _MUTED})
                y += _line_height(18)
        elif kind == "h2":
            font = _font(23, True)
            y += 14
            ops.append({"kind": "rect", "x": x, "y": y + 6, "w": 5, "h": 26, "radius": 3, "fill": _ACCENT})
            for line in _wrap(draw, block.get("text") or "", font, content_w - 20):
                ops.append({"kind": "text", "x": x + 18, "y": y, "text": line, "font": font, "fill": _TITLE})
                y += _line_height(23)
            y += 8
        elif kind == "cmd":
            cmd = str(block.get("cmd") or "")
            desc = str(block.get("desc") or "")
            cmd_font = _font(19)
            desc_font = _font(19)
            pill_w = draw.textlength(cmd, font=cmd_font) / SCALE + 26
            pill_h = 34
            ops.append(
                {
                    "kind": "pill",
                    "x": x,
                    "y": y + 1,
                    "w": pill_w,
                    "h": pill_h,
                    "radius": 10,
                    "fill": _PILL_BG,
                }
            )
            ops.append(
                {
                    "kind": "text",
                    "x": x + 13,
                    "y": y + 6,
                    "text": cmd,
                    "font": cmd_font,
                    "fill": _PILL_TEXT,
                }
            )
            desc_x = x + pill_w + 16
            desc_w = content_w - (desc_x - x)
            if desc_w < 170:  # 太窄就把说明挪到下一行
                desc_x = x + 13
                desc_w = content_w - 13
                y += pill_h + 4
            lines = _wrap(draw, desc, desc_font, desc_w)
            for index, line in enumerate(lines):
                ops.append(
                    {
                        "kind": "text",
                        "x": desc_x,
                        "y": y + 6 + index * _line_height(19),
                        "text": line,
                        "font": desc_font,
                        "fill": _TEXT,
                    }
                )
            y += max(pill_h, _line_height(19) * len(lines)) + 12
        elif kind == "note":
            font = _font(17)
            y += 2
            ops.append({"kind": "text", "x": x, "y": y, "text": "·", "font": font, "fill": _ACCENT})
            for line in _wrap(draw, block.get("text") or "", font, content_w - 22):
                ops.append({"kind": "text", "x": x + 22, "y": y, "text": line, "font": font, "fill": _MUTED})
                y += _line_height(17)
        elif kind == "footer":
            font = _font(17)
            y += 10
            ops.append({"kind": "line", "x": x, "y": y, "w": content_w, "fill": _BORDER})
            y += 16
            for line in _wrap(draw, block.get("text") or "", font, content_w):
                ops.append({"kind": "text", "x": x, "y": y, "text": line, "font": font, "fill": _MUTED})
                y += _line_height(17)
        else:  # para
            font = _font(20)
            for line in _wrap(draw, block.get("text") or "", font, content_w):
                ops.append({"kind": "text", "x": x, "y": y, "text": line, "font": font, "fill": _TEXT})
                y += _line_height(20)
            y += 4
    return ops, y + PAD


def render(blocks: Sequence[Dict[str, Any]]) -> Optional[bytes]:
    """blocks → PNG bytes；不可用/出错时返回 None（上层退回文字）。"""
    if not available():
        return None
    try:
        probe = Image.new("RGB", (WIDTH * SCALE, 100), _BG)
        probe_draw = ImageDraw.Draw(probe)
        ops, total_h = _ops_for_blocks(blocks, probe_draw)

        width = WIDTH * SCALE
        height = int(round(total_h * SCALE))
        height = max(height, 360)
        canvas = Image.new("RGB", (width, height), _BG)
        draw = ImageDraw.Draw(canvas)
        draw.rounded_rectangle(
            (int(PAD * SCALE * 0.6), int(PAD * SCALE * 0.6), width - int(PAD * SCALE * 0.6), height - int(PAD * SCALE * 0.6)),
            radius=int(RADIUS * SCALE),
            fill=_CARD,
            outline=_BORDER,
            width=max(1, SCALE),
        )

        for op in ops:
            kind = op["kind"]
            x = int(round(op["x"] * SCALE))
            y = int(round(op["y"] * SCALE))
            if kind == "text":
                draw.text((x, y), op["text"], font=op["font"], fill=op["fill"])
            elif kind == "rect":
                draw.rounded_rectangle(
                    (x, y, x + int(round(op["w"] * SCALE)), y + int(round(op["h"] * SCALE))),
                    radius=int(round(op["radius"] * SCALE)),
                    fill=op["fill"],
                )
            elif kind == "pill":
                draw.rounded_rectangle(
                    (x, y, x + int(round(op["w"] * SCALE)), y + int(round(op["h"] * SCALE))),
                    radius=int(round(op["radius"] * SCALE)),
                    fill=op["fill"],
                )
            elif kind == "line":
                draw.line((x, y, x + int(round(op["w"] * SCALE)), y), fill=op["fill"], width=max(1, SCALE))

        buffer = io.BytesIO()
        canvas.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()
    except Exception:  # 渲染失败不能影响「发帮助」本身
        return None
