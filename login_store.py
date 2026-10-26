"""网易云登录态存储：``plugins_data/<插件名>/netease_login.json``。

为什么单独一个模块：登录态有三个读者（点歌下载、``/vs-status``、``/vs-login``），
而它又必须**只落一个地方**、并且永远不进日志。集中在这里，谁要用谁 import。

⚠️ 安全约定（改代码时别破坏）：
  · cookie 只写进 ``ctx.data_dir``，不写仓库、不写日志、不进给模型看的文本；
  · 文件尽量 ``0600``（Windows 上 chmod 只在部分文件系统生效，尽力而为）；
  · 状态输出只显示「已登录／未登录 + 来源 + 时间」，绝不回显 cookie 的值。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

LOGIN_FILENAME = "netease_login.json"
#: 只认这几个（其余 Set-Cookie 一并保存，但判断「有没有登录态」看它们）
REQUIRED_KEYS = ("MUSIC_U", "MUSIC_A")

#: 当前进程内的登录 cookie（``resolve_cookie`` 直接读它，省掉每次下载读盘）
_current_cookie: str = ""
_current_source: str = ""
_current_saved_at: float = 0.0


def login_path(data_dir: Any) -> Path:
    return Path(data_dir) / LOGIN_FILENAME


def cookie_string(cookies: Dict[str, str]) -> str:
    """把 cookie 字典拼成 ``K=V; K=V``。"""
    return "; ".join("{}={}".format(key, value) for key, value in (cookies or {}).items() if value)


def _cookie_from_pairs(raw: Dict[str, str]) -> str:
    return cookie_string(raw)


def save(data_dir: Any, cookies: Dict[str, str], *, source: str = "qr") -> bool:
    """保存登录 cookie。返回是否写盘成功（内存态无论如何都会更新）。"""
    values = {str(key): str(value) for key, value in (cookies or {}).items() if value}
    if not values:
        return False
    record = {
        "version": 1,
        "source": str(source or "qr"),
        "saved_at": time.time(),
        "cookies": values,
    }
    path = login_path(data_dir)
    ok = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(path))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        ok = True
    except OSError:
        ok = False
    set_current(_cookie_from_pairs(values), source=record["source"], saved_at=record["saved_at"])
    return ok


def load(data_dir: Any) -> Dict[str, Any]:
    """读已保存的登录记录；没有/坏了就返回空字典（不抛异常）。"""
    path = login_path(data_dir)
    try:
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    cookies = data.get("cookies")
    if not isinstance(cookies, dict):
        return {}
    return {
        "cookies": {str(k): str(v) for k, v in cookies.items() if v},
        "source": str(data.get("source") or "qr"),
        "saved_at": float(data.get("saved_at") or 0.0),
    }


def clear(data_dir: Any) -> bool:
    """删除已保存的登录态（内存态一并清空）。"""
    set_current("", source="", saved_at=0.0)
    path = login_path(data_dir)
    try:
        if path.is_file():
            path.unlink()
        return True
    except OSError:
        return False


def set_current(cookie: str, *, source: str = "", saved_at: float = 0.0) -> None:
    global _current_cookie, _current_source, _current_saved_at
    _current_cookie = str(cookie or "")
    _current_source = str(source or "")
    _current_saved_at = float(saved_at or 0.0)


def get_current() -> str:
    return _current_cookie


def describe() -> Dict[str, Any]:
    """给状态输出用的摘要（**不含 cookie 值**）。"""
    if not _current_cookie:
        return {"logged_in": False, "source": "", "saved_at": 0.0, "pending": False}
    keys = [
        part.split("=", 1)[0]
        for part in _current_cookie.split(";")
        if "=" in part
    ]
    return {
        "logged_in": bool(any(key in REQUIRED_KEYS for key in keys)),
        "source": _current_source,
        "saved_at": _current_saved_at,
        "pending": False,
    }


def bootstrap(data_dir: Any) -> Optional[Dict[str, Any]]:
    """插件加载时调用：把磁盘上的登录态读进内存。返回记录（没有则 None）。"""
    record = load(data_dir)
    cookies = record.get("cookies") or {}
    if not cookies:
        set_current("")
        return None
    set_current(
        _cookie_from_pairs(cookies),
        source=record.get("source", "qr"),
        saved_at=float(record.get("saved_at") or 0.0),
    )
    return record


def age_text(saved_at: float) -> str:
    """「3 分钟前」这种人类可读的时间。"""
    if not saved_at:
        return ""
    delta = max(0.0, time.time() - saved_at)
    if delta < 90:
        return "刚刚"
    if delta < 3600:
        return "{} 分钟前".format(int(delta // 60))
    if delta < 86400:
        return "{} 小时前".format(int(delta // 3600))
    return "{} 天前".format(int(delta // 86400))
