"""网易云取歌客户端（联网部分）—— 从原 Node 版 ``netease-client.js`` 移植。

══════════════════════════════════════════════════════════════════════════
为什么需要登录态（实测数据，不是猜的）
══════════════════════════════════════════════════════════════════════════
  搜索：``/api/search/get/web`` 不需要登录，能拿到 id / 歌名 / 歌手 / 时长 → 可用
  取链接：
    · 不带登录态  → code=-110，url 为空；第三方免费镜像只能给 **30~45 秒试听片段**
    · 带登录态    → code=200，320kbps **完整歌曲**
  所以「下载完整歌曲」这一步必须带 cookie，没有别的免费正路。

══════════════════════════════════════════════════════════════════════════
登录态从哪来：优先读你本机已登录的网易云客户端
══════════════════════════════════════════════════════════════════════════
  客户端会把登录 cookie（MUSIC_U / MUSIC_A / __csrf …）明文写在它的数据目录里。
  本模块**只读不写**，而且是在需要下载的那一刻才读一次 ——
  这样你在客户端里重新登录/令牌轮换之后，插件下一首歌就会自动跟上。

⚠️ 安全边界：cookie 只发往 music.163.com 与 *.music.126.net（下载直链所在 CDN）。
   不写日志、不写任何状态文件、也不出现在给模型看的文本里。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:  # NeoBot 本体依赖 httpx；缺失时只有「自动下载」降级，不影响本地曲库
    import httpx
except Exception:  # pragma: no cover - 仅在极简环境触发
    httpx = None  # type: ignore[assignment]

try:
    from . import login_store
except ImportError:  # 单文件方式被 import 时（自测用文件路径加载）兜底
    import login_store  # type: ignore[no-redef]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
REFERER = "https://music.163.com/"

#: 音质档位 → 官方接口的 level 参数（实测 exhigh=320kbps mp3、lossless=flac）
QUALITY: Dict[str, Dict[str, str]] = {
    "standard": {"level": "standard", "encode_type": "mp3", "label": "标准 128kbps"},
    "exhigh": {"level": "exhigh", "encode_type": "mp3", "label": "较高 320kbps"},
    "lossless": {"level": "lossless", "encode_type": "flac", "label": "无损 FLAC"},
}

#: 认作音频的扩展名（下载缓存目录用，少了 .part 之类的中间文件）
AUDIO_EXT_CACHE = {"mp3", "flac", "m4a", "aac", "wav", "ogg", "oga", "opus", "wma", "ape", "mp4"}

_COOKIE_KEYS = ("MUSIC_U", "MUSIC_A", "__csrf", "NMTID", "NMCID", "JSESSIONID-WYYY")

#: 搜索接口候选（按可靠性排序）。实测 get/web 会被限流，其余几个当时仍可用。
_SEARCH_ENDPOINTS = (
    (
        "search/get/web",
        "https://music.163.com/api/search/get/web?s={kw}&type=1&limit={limit}&offset=0",
    ),
    ("search/get", "https://music.163.com/api/search/get?s={kw}&type=1&limit={limit}&offset=0"),
    (
        "cloudsearch/pc",
        "https://music.163.com/api/cloudsearch/pc?s={kw}&type=1&limit={limit}&offset=0",
    ),
    (
        "search/suggest/web",
        "https://music.163.com/api/search/suggest/web?s={kw}&limit={limit}",
    ),
)


class NeteaseUnavailable(RuntimeError):
    """httpx 不可用时的显式错误（而不是 ImportError 打到调用方脸上）。"""


def _require_httpx() -> Any:
    if httpx is None:
        raise NeteaseUnavailable("缺少 httpx，无法访问网易云（本插件只做本地曲库播放）")
    return httpx


# ───────────────────────────────────────────── 请求节流与风控
#
# 网易云的风控（``code: -460``）与限流（``code: 405``、HTTP 200 但「操作频繁」）
# 不是随机出现的，几乎都能归到三类原因：
#   ① 短时间打太多次（尤其是搜索和取直链）；
#   ② 没有登录态 / 登录态残缺（缺 __csrf 之类），请求看起来不像正常客户端；
#   ③ 换个写法反复重试同一首歌（越试越久，最后连正常请求也被牵连）。
# 所以这里把「降风控」做成四条具体机制，而不是一句口号：
#   · 全局请求节流：任意两次 API 调用之间至少隔 ``_min_interval_sec``（带抖动）；
#   · 结果缓存：同一个关键词的搜索结果、同一首歌的直链，短时间复用，不重复请求；
#   · 接口冷却：某个搜索接口被限流后，一段时间内直接跳过它（不硬撞）；
#   · 风控冷却：一旦撞上 -460，全局暂停 5 分钟，期间直接快速失败而不是继续试。

#: 两次 API 之间的最小间隔（秒）。0.5 以下很容易被判「操作频繁」。
_min_interval_sec = 1.5
#: 抖动上限（秒）：固定节奏比随机节奏更像脚本
_JITTER_SEC = 0.8
#: 搜索 / 直链结果缓存时长（秒）
_CACHE_TTL_SEC = 300.0
#: 某接口被限流后多久不再碰它（秒）
_ENDPOINT_COOLDOWN_SEC = 600.0
#: 撞上 -460 后全局冷却多久（秒）
_RISK_COOLDOWN_SEC = 300.0

_pace_lock: Optional[asyncio.Lock] = None
#: 全局下载闸：同一时刻只下一个文件（多群同时点歌时不一起冲 CDN）
_download_gate: Optional[asyncio.Semaphore] = None
_last_request_at = 0.0
_risk_until = 0.0
_endpoint_cooldown: Dict[str, float] = {}
_search_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_url_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def set_min_interval(seconds: Any) -> None:
    """按配置调整节流间隔（0.5~10 秒，越界夹到边界）。"""
    global _min_interval_sec
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return
    _min_interval_sec = max(0.5, min(10.0, value))


def _pace() -> asyncio.Lock:
    """惰性创建节流锁（不在 import 期绑定事件循环）。"""
    global _pace_lock
    if _pace_lock is None:
        _pace_lock = asyncio.Lock()
    return _pace_lock


def _download_slot() -> asyncio.Semaphore:
    global _download_gate
    if _download_gate is None:
        _download_gate = asyncio.Semaphore(1)
    return _download_gate


async def _wait_turn() -> None:
    global _last_request_at
    lock = _pace()
    async with lock:
        now = time.monotonic()
        wait = _last_request_at + _min_interval_sec - now
        if wait > 0:
            await asyncio.sleep(wait + random.uniform(0, _JITTER_SEC))
        _last_request_at = time.monotonic()


def risk_status() -> Dict[str, Any]:
    """给状态命令用的风控快照。"""
    now = time.monotonic()
    return {
        "min_interval_sec": _min_interval_sec,
        "risk_cooldown_left": max(0.0, _risk_until - now),
        "endpoints_in_cooldown": [
            name for name, until in _endpoint_cooldown.items() if until > now
        ],
    }


def _cache_get(cache: Dict[str, Tuple[float, Any]], key: str) -> Any:
    hit = cache.get(key)
    if hit is None:
        return None
    if time.monotonic() - hit[0] > _CACHE_TTL_SEC:
        cache.pop(key, None)
        return None
    return hit[1]


def _cache_put(cache: Dict[str, Tuple[float, Any]], key: str, value: Any) -> Any:
    if len(cache) > 500:  # 简单容量兜底
        oldest = min(cache.items(), key=lambda item: item[1][0], default=None)
        if oldest is not None:
            cache.pop(oldest[0], None)
    cache[key] = (time.monotonic(), value)
    return value


async def request_json(
    method: str,
    url: str,
    *,
    data: Optional[Dict[str, str]] = None,
    cookie: str = "",
    timeout_sec: float = 12.0,
) -> Dict[str, Any]:
    """统一的 JSON 请求入口：节流 + 风控识别。

    返回 ``{"json": dict, "status": int, "set_cookie": [str, ...]}`` 或 ``{"error": str, ...}``。
    ``set_cookie`` 原样给出响应里的 Set-Cookie（扫码登录要靠它拿 MUSIC_U）。
    """
    global _risk_until
    httpx_module = _require_httpx()
    now = time.monotonic()
    if now < _risk_until:
        left = int(_risk_until - now) + 1
        return {
            "error": "网易云正在风控冷却（{} 秒后恢复）：这是「打太快」的保护，不是坏歌".format(left),
            "risk_limited": True,
        }
    await _wait_turn()
    try:
        async with httpx_module.AsyncClient(
            headers=headers(cookie), timeout=timeout_sec, follow_redirects=True
        ) as client:
            if method.upper() == "POST":
                response = await client.post(url, data=data or {})
            else:
                response = await client.get(url)
            text = response.text
            set_cookie = response.headers.get_list("set-cookie")
            status = response.status_code
    except Exception as exc:  # 网络层错误：超时/连不上/DNS
        name = type(exc).__name__
        detail = "（超时）" if "Timeout" in name else "（{}）".format(name)
        return {"error": "{}{}".format(exc, detail)}
    try:
        import json

        payload = json.loads(text)
    except Exception:
        return {"error": "返回的不是 JSON（HTTP {}）".format(status)}
    if not isinstance(payload, dict):
        return {"error": "返回的不是 JSON 对象（HTTP {}）".format(status)}
    code = payload.get("code")
    if code == -460 or code == "-460":
        _risk_until = time.monotonic() + _RISK_COOLDOWN_SEC
        return {
            "json": payload,
            "status": status,
            "set_cookie": set_cookie,
            "risk_limited": True,
            "error": "网易云触发了风控（-460），已暂停 {} 分钟；这期间不要再刷".format(
                int(_RISK_COOLDOWN_SEC // 60)
            ),
        }
    return {"json": payload, "status": status, "set_cookie": set_cookie}


# ───────────────────────────────────────────── 登录态


def cookie_file_candidates() -> List[Path]:
    """客户端可能存放登录 cookie 的文件（按可能性排序）。"""
    local = os.environ.get("LOCALAPPDATA") or ""
    candidates: List[Path] = []
    if not local:
        return candidates
    root = Path(local) / "Netease" / "CloudMusic"
    ad_dir = root / "ad"
    # 实测：客户端把 cookie 明文写在这个 APM 存储库里（文件名带随机串，所以按目录扫）
    try:
        if ad_dir.is_dir():
            for entry in sorted(ad_dir.iterdir()):
                name = entry.name
                if name.startswith("APMStorageDatabase") and name.endswith(".db"):
                    candidates.append(entry)
    except OSError:
        pass
    candidates.append(root / "webapp91x64" / "Cookies")
    candidates.append(root / "webdata" / "file" / "essential")
    return candidates


def parse_cookie_text(text: str) -> Dict[str, str]:
    """从一段文本里抠出 cookie 键值（兼容 ``"K":"V"`` 与 ``K=V`` 两种形态）。"""
    found: Dict[str, str] = {}
    source = str(text or "")
    for key in _COOKIE_KEYS:
        escaped = re.escape(key).replace(r"\-", "[-_]").replace(r"\_", "[-_]")
        match = re.search(r'"' + escaped + r'"\s*:\s*"([^"]{8,})"', source) or re.search(
            escaped + r"=([A-Za-z0-9%._-]{8,})", source
        )
        if match is not None:
            found[key] = match.group(1)
    return found


def parse_manual_cookie(raw: str) -> Dict[str, str]:
    """解析用户手填的 cookie 字符串（``MUSIC_U=xxx; __csrf=yyy`` 形态）。"""
    found: Dict[str, str] = {}
    for part in re.split(r"[;\n]+", str(raw or "")):
        index = part.find("=")
        if index <= 0:
            continue
        key = part[:index].strip()
        value = part[index + 1 :].strip()
        if key and value:
            found[key] = value
    return found


def _join_cookie(values: Dict[str, str]) -> str:
    return "; ".join("{}={}".format(key, value) for key, value in values.items())


def resolve_cookie(manual: str = "", allow_client: bool = False, saved: Optional[str] = None) -> Dict[str, str]:
    """取登录态。优先级：设置里手填 → ``/vs-login`` 扫码保存的 → 旧的本机客户端文件。

    ``allow_client`` 默认 **False**：本机客户端那条路是 1.7.0 的做法，现在改用
    手机扫码登录（``/vs-login``）。老用户想继续用客户端 cookie 可以把它打开。

    每次下载都重新调一次（重新登录后能立刻跟上）。
    返回 ``{"cookie": str, "source": str}`` 或 ``{"error": str}``。
    """
    manual_values = parse_manual_cookie(manual)
    if manual_values.get("MUSIC_U") or manual_values.get("MUSIC_A"):
        return {"cookie": _join_cookie(manual_values), "source": "设置里手填"}

    saved_cookie = saved if saved is not None else login_store.get_current()
    if saved_cookie:
        return {"cookie": saved_cookie, "source": "扫码登录（已保存）"}

    if not allow_client:
        return {
            "error": "还没有登录：发 /vs-login 用手机扫码（或在设置里手填 cookie）"
        }
    for path in cookie_file_candidates():
        try:
            if not path.is_file():
                continue
            if path.stat().st_size > 64 * 1024 * 1024:
                continue  # 超大文件跳过（Cookies 是 sqlite，另说）
            text = path.read_bytes().decode("latin-1", "ignore")
            values = parse_cookie_text(text)
            if values.get("MUSIC_U") or values.get("MUSIC_A"):
                return {
                    "cookie": _join_cookie(values),
                    "source": "本机客户端（{}）".format(path.name),
                }
        except OSError:
            continue
    return {"error": "读不到本机网易云客户端的登录态（没装客户端、没登录，或文件位置变了）"}


def headers(cookie: str = "") -> Dict[str, str]:
    """尽量像浏览器：UA + Referer + Accept-Language + 常见客户端头。

    缺 Referer / UA 的请求在网易云那边很显眼，是「看起来像脚本」的主要特征之一。
    """
    result = {
        "User-Agent": USER_AGENT,
        "Referer": REFERER,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Origin": "https://music.163.com",
    }
    if cookie:
        result["Cookie"] = cookie
    return result


#: 兼容旧调用点
_headers = headers


async def _get_json(url: str, *, cookie: str = "", timeout_sec: float = 12.0) -> Dict[str, Any]:
    """GET + JSON，走统一的节流/风控入口。"""
    return await request_json("GET", url, cookie=cookie, timeout_sec=timeout_sec)


def _artist_of(song: Dict[str, Any]) -> str:
    artists = song.get("artists") or song.get("ar") or []
    if isinstance(artists, list) and artists:
        names = [str(item.get("name") or "") for item in artists if isinstance(item, dict)]
        return "/".join([name for name in names if name])
    raw = song.get("artist")
    return str(raw) if isinstance(raw, str) else ""


def _pick_songs(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    result = payload.get("result")
    if isinstance(result, dict) and isinstance(result.get("songs"), list):
        return result["songs"]
    if isinstance(payload.get("songs"), list):
        return payload["songs"]
    return []


async def search_songs(
    keyword: str, *, limit: int = 5, timeout_sec: float = 12.0
) -> Dict[str, Any]:
    """按歌名搜歌（官方接口，不需要登录）。**多接口自动降级**。

    ⚠️ 为什么必须多接口（真实故障）：
      网易云对 ``/api/search/get/web`` 会限流，HTTP 仍是 200，但 body 是
      ``{"code":405,"msg":"操作频繁，请稍候再试"}`` —— 歌曲数组为空。
      早先的代码把它当成「没搜到」，于是工具回「网易云搜不着」，
      模型转述给用户，还对着同一个词换 8 种写法硬试，把限流打得更死。
      实测同一时刻 search/get（老）与 search/suggest/web 仍然正常，
      所以这里逐个降级，并把「被限流」和「真没结果」分开报告。

    返回里带 fee：0=免费 1=VIP 4=需购买专辑 8=低音质免费。
    """
    wanted = str(keyword or "").strip()
    if not wanted:
        return {"ok": False, "empty": True, "error": "歌名是空的"}
    limit = max(1, min(30, int(limit or 5)))

    # 同一个关键词 5 分钟内不重复请求：群友反复点同一首歌时，这一步能省掉绝大多数请求
    cache_key = "{}|{}".format(wanted.lower(), limit)
    cached = _cache_get(_search_cache, cache_key)
    if cached is not None:
        return cached

    from urllib.parse import quote

    encoded = quote(wanted, safe="")
    tried: List[str] = []
    saw_rate_limit = False
    saw_request_failure = False
    saw_risk = False
    risk_error = ""
    skipped_cooling: List[str] = []
    now = time.monotonic()

    for name, template in _SEARCH_ENDPOINTS:
        # 这个接口刚被限流过：直接跳过，别硬撞（撞一次就把冷却续长）
        if _endpoint_cooldown.get(name, 0.0) > now:
            # ⚠️ 只是「这次不碰它」，**不能**算成「被限流」：否则其它接口正常、
            #    这首歌确实不存在时，用户看到的是「接口正在限流，过一会儿再试」
            #    （最长误报 10 分钟），与「真没结果」正好相反。
            skipped_cooling.append(name)
            tried.append("{}:冷却中".format(name))
            continue
        url = template.format(kw=encoded, limit=limit)
        result = await _get_json(url, timeout_sec=timeout_sec)
        if result.get("risk_limited"):
            # -460 风控冷却：不是网络问题，别把原因吞成「连不上网易云」
            saw_risk = True
            risk_error = str(result.get("error") or "网易云风控冷却中")
            tried.append("{}:风控冷却".format(name))
            continue
        if result.get("error"):
            saw_request_failure = True
            tried.append("{}:请求失败".format(name))
            continue
        payload = result["json"]
        code = int(payload.get("code") or 0)
        # 405 = 操作频繁（限流）。这时**不能**当成「没搜到」，必须换接口重试。
        if code == 405:
            saw_rate_limit = True
            _endpoint_cooldown[name] = time.monotonic() + _ENDPOINT_COOLDOWN_SEC
            tried.append("{}:被限流".format(name))
            continue
        raw_songs = _pick_songs(payload)
        if not raw_songs:
            tried.append("{}:无结果".format(name))
            continue
        songs: List[Dict[str, Any]] = []
        for item in raw_songs:
            if not isinstance(item, dict):
                continue
            song_id = str(item.get("id") or "")
            title = str(item.get("name") or "").strip()
            if not song_id or not title:
                continue
            album = item.get("album") or item.get("al") or {}
            fee: Optional[int] = None
            try:
                fee = int(item["fee"]) if item.get("fee") is not None else None
            except (TypeError, ValueError):
                fee = None
            songs.append(
                {
                    "id": song_id,
                    "name": title,
                    "artist": _artist_of(item),
                    "album": str((album or {}).get("name") or "").strip(),
                    "duration_ms": int(item.get("duration") or item.get("dt") or 0),
                    "fee": fee,
                }
            )
        if not songs:
            tried.append("{}:结果字段不全".format(name))
            continue
        return _cache_put(_search_cache, cache_key, {"ok": True, "songs": songs, "endpoint": name})

    if saw_risk:
        return {
            "ok": False,
            "rate_limited": True,
            "risk_limited": True,
            "error": risk_error,
            "tried": tried,
        }
    if saw_rate_limit and not saw_request_failure:
        return {
            "ok": False,
            "rate_limited": True,
            "error": "网易云的搜索接口正在限流（操作频繁），不是「这首歌不存在」——过一会儿再试",
            "tried": tried,
        }
    if saw_request_failure and not saw_rate_limit:
        return {
            "ok": False,
            "request_failed": True,
            "error": "连网易云都没连上：{}".format("、".join(tried)),
            "tried": tried,
        }
    if saw_rate_limit or saw_request_failure:
        return {
            "ok": False,
            "rate_limited": saw_rate_limit,
            "request_failed": saw_request_failure,
            "error": "搜索没成功：{}".format("、".join(tried)),
            "tried": tried,
        }
    cooling_note = (
        "（{} 个搜索接口正在冷却，过几分钟再试更稳）".format(len(skipped_cooling))
        if skipped_cooling
        else ""
    )
    return {
        "ok": False,
        "empty": True,
        "error": "没搜到「{}」（网易云真的没有匹配结果）{}".format(wanted, cooling_note),
        "tried": tried,
    }


async def song_url(
    song_id: Any,
    *,
    cookie: str = "",
    quality: str = "exhigh",
    timeout_sec: float = 12.0,
) -> Dict[str, Any]:
    """取某个音质的下载直链。

    同一首歌同一个音质的直链缓存 5 分钟：直链本身有效期远长于此，
    而「同一首歌被反复点」是最常见的重复请求来源。
    """
    from urllib.parse import quote

    profile = QUALITY.get(str(quality)) or QUALITY["exhigh"]
    cache_key = "{}|{}".format(song_id, profile["level"])
    cached = _cache_get(_url_cache, cache_key)
    if cached is not None:
        return cached

    url = (
        "https://music.163.com/api/song/enhance/player/url/v1?ids=[{}]"
        "&level={}&encodeType={}"
    ).format(quote(str(song_id), safe=""), profile["level"], profile["encode_type"])
    result = await _get_json(url, cookie=cookie, timeout_sec=timeout_sec)
    if result.get("error"):
        return {"ok": False, "error": "取直链失败：{}".format(result["error"])}
    data = (result["json"].get("data") or [None])[0]
    if not isinstance(data, dict):
        return {"ok": False, "error": "取直链失败：返回里没有歌曲数据"}
    try:
        code = int(data.get("code"))
    except (TypeError, ValueError):
        code = None
    if not data.get("url") or (code is not None and code != 200):
        if code == -110:
            hint = "登录态无效或已过期（code=-110）—— 发 /vs-login 用手机重新扫码即可"
        else:
            hint = "官方返回 code={}".format(code if code is not None else "未知")
        return {"ok": False, "code": code, "error": hint}
    # 非会员账号点 VIP 歌曲时，官方会返回 code=200 + 一条**试听**直链
    # （freeTrialInfo 非空、时长只有 30~45 秒）。不判它就会把片段当完整歌曲缓存 5 分钟，
    # 群里收到的是半首歌 —— 比「下载失败」更难解释。
    if data.get("freeTrialInfo"):
        return {
            "ok": False,
            "code": code,
            "free_trial": True,
            "error": "这首只返回了试听片段（需要会员账号才能取到完整音频）",
        }
    return _cache_put(
        _url_cache,
        cache_key,
        {
            "ok": True,
            "url": str(data["url"]),
            "br": int(data.get("br") or 0),
            "size": int(data.get("size") or 0),
        },
    )


def _looks_like_audio(content_type: str, head: bytes) -> bool:
    if re.search(r"audio|octet-stream|mpeg", content_type or "", re.IGNORECASE):
        return True
    if head[:3] == b"ID3":
        return True
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return True
    if head[:4] == b"fLaC":
        return True
    if head[4:8] == b"ftyp":
        return True
    return False


async def download_to(
    url: str,
    dest: Path,
    *,
    cookie: str = "",
    max_bytes: int = 30 * 1024 * 1024,
    timeout_sec: float = 90.0,
    retries: int = 2,
) -> Dict[str, Any]:
    """把音频下到本地文件。

    ⚠️ 带重试：实测网易云 CDN 会偶发连接超时 —— 同一首同一时刻第一次失败、
    紧接着重试就成功。没有重试的话，用户在群里看到的是「下载失败」，
    而其实再试一次就好。只重试「传输层失败」，HTTP 4xx/5xx 与
    「下到的不是音频」不重试（重试也没用）。

    ⚠️ 全局只允许一个下载同时在跑：多群同时点歌时一起冲 CDN 既慢又显眼。
    """
    httpx_module = _require_httpx()
    attempts = max(1, min(5, int(retries) + 1))
    last_error = ""
    dest = Path(dest)
    async with _download_slot():
        return await _download_to_locked(
            httpx_module,
            url,
            dest,
            cookie=cookie,
            max_bytes=max_bytes,
            timeout_sec=timeout_sec,
            attempts=attempts,
        )


async def _download_to_locked(
    httpx_module: Any,
    url: str,
    dest: Path,
    *,
    cookie: str,
    max_bytes: int,
    timeout_sec: float,
    attempts: int,
) -> Dict[str, Any]:
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            async with httpx_module.AsyncClient(
                headers=_headers(cookie), timeout=timeout_sec, follow_redirects=True
            ) as client:
                async with client.stream("GET", url) as response:
                    if response.status_code >= 400:
                        # HTTP 层错误：重试无用（直链多半已过期），直接把状态码报出去
                        return {
                            "ok": False,
                            "error": "下载失败：HTTP {}".format(response.status_code),
                            "tries": attempt,
                        }
                    declared = int(response.headers.get("content-length") or 0)
                    if declared and declared > max_bytes:
                        return {
                            "ok": False,
                            "error": "文件太大（{:.1f}MB，超过上限 {:.0f}MB）".format(
                                declared / 1048576.0, max_bytes / 1048576.0
                            ),
                            "tries": attempt,
                        }
                    chunks: List[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > max_bytes:
                            return {
                                "ok": False,
                                "error": "文件太大（超过上限 {:.0f}MB）".format(
                                    max_bytes / 1048576.0
                                ),
                                "tries": attempt,
                            }
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    content_type = response.headers.get("content-type") or ""
        except Exception as exc:
            name = type(exc).__name__
            last_error = "下载超时" if "Timeout" in name else "{}".format(exc)
            if attempt < attempts:
                await asyncio.sleep(0.4 * attempt)
            continue

        if not body:
            return {"ok": False, "error": "下载到 0 字节", "tries": attempt}
        if not _looks_like_audio(content_type, body[:12]):
            return {
                "ok": False,
                "error": "下到的不是音频（网易云可能返回了错误页）",
                "tries": attempt,
            }
        try:
            # 25~30MB 一次性写盘放在事件循环上，机械盘/杀毒扫描时能卡住所有会话
            # 几百毫秒到数秒 —— 和「扫描曲库」「读 ID3」一样丢线程。
            part = dest.with_name(dest.name + ".part")
            await asyncio.to_thread(_write_file_atomic, part, dest, body)
        except OSError as exc:
            return {"ok": False, "error": "写入失败：{}".format(exc), "tries": attempt}
        return {"ok": True, "bytes": len(body), "content_type": content_type, "tries": attempt}

    return {"ok": False, "error": last_error or "下载失败", "tries": attempts}


def _write_file_atomic(part: Path, dest: Path, body: bytes) -> None:
    """先写 ``.part`` 再原子替换：进程被杀也不会留下半个文件被当成缓存。"""
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(body)
    os.replace(str(part), str(dest))


# ───────────────────────────────────────────── 下载缓存


def safe_file_name(text: Any, max_len: int = 80) -> str:
    """文件名安全化：去掉 Windows 不允许的字符，限制长度。"""
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", str(text or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:max_len] or "song"


def default_download_dir(data_dir: Path) -> Path:
    """默认下载缓存目录：``我的音乐/CloudMusic/点歌缓存``；没有家目录时落到插件数据目录。"""
    try:
        home = Path.home()
    except (RuntimeError, OSError):
        home = None
    if home is not None and str(home):
        return home / "Music" / "CloudMusic" / "点歌缓存"
    return Path(data_dir) / "songs"


def list_cached(directory: Path, *, max_items: int = 500) -> List[Dict[str, Any]]:
    """列出下载缓存目录里已有的音频（不递归 —— 缓存目录是自己的，结构是平的）。

    用于「同一首歌点两次不要下两次」。
    """
    out: List[Dict[str, Any]] = []
    try:
        if not Path(directory).is_dir():
            return out
        for entry in sorted(Path(directory).iterdir()):
            name = entry.name
            index = name.rfind(".")
            if index <= 0:
                continue
            ext = name[index + 1 :].lower()
            if ext not in AUDIO_EXT_CACHE:
                continue  # .part 之类的中间文件天然被排除
            try:
                stat = entry.stat()
                if not entry.is_file() or stat.st_size < 32 * 1024:
                    continue
                out.append(
                    {
                        "path": str(entry),
                        "name": name,
                        "ext": ext,
                        "size": stat.st_size,
                        "mtime_ms": stat.st_mtime * 1000.0,
                        "title": "",
                        "artist": "",
                    }
                )
            except OSError:
                continue
            if len(out) >= max_items:
                break
    except OSError:
        pass
    return out


def base_name_of(file_name: str) -> str:
    """去掉扩展名与 `` [网易云id]`` 尾巴，用来判断「是不是同一首歌」。"""
    return re.sub(r"\s*\[\d+\]\s*$", "", re.sub(r"\.[A-Za-z0-9]{1,5}$", "", str(file_name or ""))).strip()


#: 我们下载时自己加在文件名尾巴上的「 [网易云id]」—— 也是「这个文件是本插件写的」的凭据
_DOWNLOADED_TAIL_RE = re.compile(r"\s*\[\d+\]\s*$")


def _norm_key(text: Any) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w]+", " ", str(text or "").lower())).strip()


def _quick_fingerprint(path: Path) -> str:
    """快速指纹：大小 + 头尾各 64KB 的内容哈希 —— 判断「是不是同一份文件」够用。"""
    try:
        size = path.stat().st_size
        chunk = 64 * 1024
        with open(path, "rb") as handle:
            head = handle.read(min(chunk, size))
            tail_length = min(chunk, max(0, size - len(head)))
            if tail_length:
                handle.seek(size - tail_length)
                tail = handle.read(tail_length)
            else:
                tail = b""
        digest = hashlib.sha256()
        digest.update(head)
        digest.update(tail)
        digest.update(str(size).encode("ascii"))
        return "{}:{}".format(size, digest.hexdigest()[:32])
    except OSError:
        return ""


def dedupe_cache(directory: Path, *, keep_newest: bool = True, max_remove: int = 30) -> Dict[str, Any]:
    """清理下载缓存里的重复内容。

    规则（**刻意保守** —— 这里会真删文件，误删用户的东西不可接受）：
      ① 只有本插件自己下下来的文件参与（文件名带 ``[网易云id]`` 尾巴）；
      ② 只有音频扩展名参与；
      ③ 只在「同一首歌」（去掉 [id] 后同名）之间比较；
      ④ 只有两种情况下才删：
         · 内容指纹完全相同 → 留最新的，删旧的（重复下载了同一版）；
         · 时长几乎一致（±2 秒）→ 认为是同一版的不同下载，留体积大的（音质好）；
      ⑤ 每组至少保留一个；任何一步拿不准就保留。

    ⚠️ 只会清理**下载缓存目录里插件自己写的文件**，永远不会碰用户的音乐库 ——
       即使有人把 download_dir 错指到自己的音乐文件夹。
    """
    from .library import estimate_audio_seconds

    out: Dict[str, Any] = {"removed": [], "freed_bytes": 0, "groups": 0, "kept": 0}
    directory = Path(directory)
    try:
        if not directory.is_dir():
            return out
        names = sorted(entry.name for entry in directory.iterdir())
    except OSError:
        return out

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for name in names:
        index = name.rfind(".")
        if index <= 0:
            continue
        ext = name[index + 1 :].lower()
        if ext not in AUDIO_EXT_CACHE:
            continue
        # ⚠️ 只碰**本插件自己下下来**的文件：``_download_chosen`` 写的名字恒为
        #    「歌名 [网易云id].mp3」。不认这个尾巴的话，用户把 download_dir 指到
        #    自己的音乐库时，去重就会删他的歌 —— 这种事一次都不该发生。
        if not _DOWNLOADED_TAIL_RE.search(name[:index]):
            continue
        full = directory / name
        try:
            stat = full.stat()
            if not full.is_file() or stat.st_size < 32 * 1024:
                continue
        except OSError:
            continue
        key = _norm_key(base_name_of(name))
        if not key:
            continue
        grouped.setdefault(key, []).append(
            {"name": name, "path": full, "size": stat.st_size, "mtime_ms": stat.st_mtime * 1000.0}
        )

    for group in grouped.values():
        if len(group) < 2:
            out["kept"] += len(group)
            continue
        out["groups"] += 1
        group.sort(key=lambda item: item["mtime_ms"], reverse=bool(keep_newest))
        dropped: set = set()

        # ① 指纹完全相同的：留第一个（最新）
        by_fingerprint: Dict[str, List[Dict[str, Any]]] = {}
        for item in group:
            fingerprint = _quick_fingerprint(Path(item["path"]))
            if not fingerprint:
                continue
            by_fingerprint.setdefault(fingerprint, []).append(item)
        for same in by_fingerprint.values():
            if len(same) < 2:
                continue
            for item in same[1:]:
                if len(dropped) >= max_remove:
                    break
                dropped.add(str(item["path"]))
                out["removed"].append(
                    {"name": item["name"], "reason": "内容完全相同（重复下载）", "bytes": item["size"]}
                )

        # ② 剩下的按「时长几乎一致」聚类，留体积最大的
        rest = [item for item in group if str(item["path"]) not in dropped]
        if len(rest) >= 2:
            with_duration = [
                dict(item, seconds=estimate_audio_seconds(str(item["path"]))) for item in rest
            ]
            used: set = set()
            for index, item in enumerate(with_duration):
                if index in used or not item["seconds"]:
                    continue
                cluster = [index]
                for other in range(index + 1, len(with_duration)):
                    candidate = with_duration[other]
                    if other in used or not candidate["seconds"]:
                        continue
                    if abs(candidate["seconds"] - item["seconds"]) <= 2:
                        cluster.append(other)
                        used.add(other)
                if len(cluster) < 2:
                    continue
                used.add(index)
                items = sorted(
                    [with_duration[position] for position in cluster],
                    key=lambda entry: entry["size"],
                    reverse=True,
                )
                for extra in items[1:]:
                    if len(dropped) >= max_remove:
                        break
                    dropped.add(str(extra["path"]))
                    out["removed"].append(
                        {
                            "name": extra["name"],
                            "reason": "同一版重复（保留了 {}）".format(items[0]["name"]),
                            "bytes": extra["size"],
                        }
                    )
        out["kept"] += len(group) - len(dropped)

    for record in list(out["removed"]):
        try:
            (directory / record["name"]).unlink()
            out["freed_bytes"] += record["bytes"]
        except OSError:
            # 删不掉（被占用等）就撤销这条记录，别谎报清理成功
            out["removed"] = [item for item in out["removed"] if item is not record]
    return out
