"""网易云手机扫码登录（QR login）。

为什么改成扫码：1.7.0 是「读本机已登录客户端的 cookie」，前提是这台机器上装了
网易云客户端并且登录过 —— 对「机器人跑在服务器/另一台电脑上」的场景直接失效。
扫码登录不依赖本机客户端：插件生成二维码，用户用手机网易云 App 扫一下就完成授权，
cookie 由网易云下发，插件只负责保存和复用。

流程（与官方网页/客户端一致，三步）：
  1. ``POST /api/login/qrcode/unikey``   → 拿 unikey
  2. 二维码内容 = ``https://music.163.com/login?codekey=<unikey>``（手机扫码）
  3. ``POST /api/login/qrcode/client/login?key=<unikey>&type=1`` 轮询：
       800 二维码失效 / 801 等待扫码 / 802 已扫码待确认 / 803 成功（Set-Cookie 里带 MUSIC_U）

⚠️ 风控相关：轮询本身就应该是「慢」的。官方客户端大约 2 秒一次，这里用 3 秒 + 抖动，
   并且只在用户主动发 ``/vs-login`` 时才发起——绝不后台自动重登。
"""

from __future__ import annotations

import io
import re
from typing import Any, Dict, List, Optional

try:  # 与 netease.py 同源：httpx 由本体提供
    from . import netease
except ImportError:  # 自测时按文件路径加载
    import netease  # type: ignore[no-redef]

UNIKEY_URL = "https://music.163.com/api/login/qrcode/unikey"
POLL_URL = "https://music.163.com/api/login/qrcode/client/login"
QR_URL_TEMPLATE = "https://music.163.com/login?codekey={key}"

#: 轮询状态（与网易云返回码一一对应）
STATE_WAITING = "waiting"      # 801 等待扫码
STATE_SCANNED = "scanned"      # 802 已扫码，等手机确认
STATE_SUCCESS = "success"      # 803 授权成功
STATE_EXPIRED = "expired"      # 800 二维码失效
STATE_FAILED = "failed"        # 其它（网络/服务异常）

#: 需要拿到这两个之一才算真的登录了
_LOGIN_KEYS = ("MUSIC_U", "MUSIC_A")

_POLL_CODE_STATE = {
    800: STATE_EXPIRED,
    801: STATE_WAITING,
    802: STATE_SCANNED,
    803: STATE_SUCCESS,
}

_COOKIE_PAIR_RE = re.compile(r"^\s*([^=;\s]+)=([^;]*)")


def make_qr_png(url: str, *, box_size: int = 8, border: int = 2) -> Optional[bytes]:
    """把登录 URL 渲染成 PNG 二维码；没装 qrcode 依赖时返回 None（上层降级发文本）。"""
    try:
        import qrcode
    except ImportError:
        return None
    try:
        qr = qrcode.QRCode(box_size=box_size, border=border)
        qr.add_data(url)
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception:
        return None


def cookies_from_set_cookie(values: Any) -> Dict[str, str]:
    """从 Set-Cookie 头列表里抠出 cookie 键值。

    整份 cookie 都要留着：只带 MUSIC_U 而缺 ``__csrf`` / ``NMTID`` 的请求
    在网易云那边「不像正常客户端」，是风控的常见诱因之一。
    """
    found: Dict[str, str] = {}
    for raw in values or []:
        match = _COOKIE_PAIR_RE.match(str(raw or ""))
        if match is None:
            continue
        key, value = match.group(1), match.group(2)
        if key and value:
            found[key] = value
    return found


def cookie_string(cookies: Dict[str, str]) -> str:
    return "; ".join("{}={}".format(key, value) for key, value in (cookies or {}).items() if value)


def is_logged_in(cookies: Dict[str, str]) -> bool:
    return any(cookies.get(key) for key in _LOGIN_KEYS)


async def request_unikey(*, timeout_sec: float = 12.0) -> Dict[str, Any]:
    """第 1 步：申请一个二维码 key。"""
    result = await netease.request_json(
        "POST", UNIKEY_URL, data={"type": "1"}, timeout_sec=timeout_sec
    )
    if result.get("error"):
        return {"ok": False, "error": result["error"], "rate_limited": bool(result.get("risk_limited"))}
    payload = result.get("json") or {}
    key = str(payload.get("unikey") or "").strip()
    code = payload.get("code")
    if not key:
        return {
            "ok": False,
            "error": "网易云没有返回二维码 key（code={}）".format(code if code is not None else "未知"),
        }
    return {
        "ok": True,
        "key": key,
        "url": QR_URL_TEMPLATE.format(key=key),
        # 这一次的 Set-Cookie 也要留着：__csrf/NMTID 可能只在这里下发，
        # 只保存 803 那一次的话，登录态会只剩 MUSIC_U——正是 -460 的诱因之一。
        "set_cookie": result.get("set_cookie") or [],
    }


async def poll_once(key: str, *, cookie: str = "", timeout_sec: float = 12.0) -> Dict[str, Any]:
    """第 3 步：查一次扫码状态。

    ``cookie`` 是流程里已经攒到的 cookie（unikey / 之前的轮询），带上它，
    后续轮询的响应才可能补齐 __csrf/NMTID。
    返回 ``{"state": ..., "code": int, "cookies": {...}, "message": str}``。
    """
    result = await netease.request_json(
        "POST",
        POLL_URL,
        data={"key": key, "type": "1"},
        cookie=cookie,
        timeout_sec=timeout_sec,
    )
    if result.get("error"):
        # 网络问题不该让整个登录流程立刻失败 —— 上层按 failed 记一次，但可以继续轮询
        return {
            "state": STATE_FAILED,
            "code": -1,
            "cookies": {},
            "message": result["error"],
            "transient": not result.get("risk_limited"),
        }
    payload = result.get("json") or {}
    try:
        code = int(payload.get("code"))
    except (TypeError, ValueError):
        code = -1
    state = _POLL_CODE_STATE.get(code, STATE_FAILED)
    cookies = cookies_from_set_cookie(result.get("set_cookie"))
    if state == STATE_SUCCESS and not is_logged_in(cookies):
        return {
            "state": STATE_FAILED,
            "code": code,
            "cookies": cookies,
            "message": "扫码已确认，但没拿到登录 cookie（可能被风控或 cookie 被拦了）",
        }
    messages = {
        STATE_WAITING: "等待扫码",
        STATE_SCANNED: "已扫码，请在手机上点「确认登录」",
        STATE_SUCCESS: "登录成功",
        STATE_EXPIRED: "二维码已失效，请重新发起登录",
        STATE_FAILED: "扫码状态异常（code={}）".format(code),
    }
    return {
        "state": state,
        "code": code,
        "cookies": cookies,
        "message": messages.get(state, ""),
    }
