"""点歌发语音条 —— 纯逻辑层（不碰网络、不碰框架 API，可离线单测）。

从原 Node 版 ``logic.js`` 逐段移植：解析、打分、候选合并、同名多首的待选表、
限流闸、临时豁免、ID3 之外的小工具。移植时保留原实现的判据与分数，
因为那些数字是被真实故障反向调出来的（注释里都写了原因）。

依赖：仅标准库。这样这个文件可以脱离 NeoBot 单独跑测试。
"""

from __future__ import annotations

import math
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Pattern, Sequence, Tuple

# ───────────────────────────────────────────── 通用小工具


def to_list(value: Any) -> List[str]:
    """数组/字符串归一：去空白、保留原顺序、丢掉空值。"""
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item or "").strip()]
    if value is None:
        return []
    text = str(value).strip()
    return [text] if text else []


def split_words(value: Any, separators: str = r"[|,，、;；\n]+") -> List[str]:
    """把「用 | 或逗号分隔的一串词」拆成列表；已经是列表就原样归一。

    设置面板里这些字段是字符串（``triggers = "点歌|来一首"``），
    而调用方又经常直接给列表。两种都吃，省得每个调用点各拆一次。
    """
    if isinstance(value, (list, tuple)):
        return to_list(value)
    if value is None:
        return []
    return [item for item in (part.strip() for part in re.split(separators, str(value))) if item]


def cut(text: Any, max_len: int = 60) -> str:
    """字符串截断（防日志/提示刷屏）。"""
    collapsed = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(collapsed) <= max_len:
        return collapsed
    return collapsed[:max_len] + "…"


_PUNCT_RE = re.compile(
    r"[()[\]{}<>【】（）《》「」『』\"'`~!@#$%^&*+=|\\/.,;:?!\-_—–·、，。；：？！…]+"
)
_NON_WORD_RE = re.compile(r"[^\w\s]+")

#: 默认触发词。``parse_song_request`` 不传 keywords 时用它 ——
#: 原版把这份默认值放在调用方（index.js 的 DEFAULTS），移植时收到逻辑层，
#: 这样这个模块单独拿去测也有确定行为。
DEFAULT_TRIGGERS = ("点歌", "来一首", "来首", "放一首", "放首", "我想听", "想听")


def normalize_name(text: Any) -> str:
    """归一化歌名/文件名，用于匹配。

    要处理的真实形态（常见下载命名）：
      「海阔天空.mp3」「Beyond - 海阔天空.flac」「01. 海阔天空.flac」
      「海阔天空 (Live)」「海阔天空【HQ】」「海阔天空_320K」
    做法：小写 → 各类括号/下划线/连字符/点 变空格 → 去掉非字母数字与中文的字符 →
    压缩连续空格。中英文混排都能兜住（Python 的 ``\\w`` 在 str 模式下含 CJK 与字母）。
    """
    lowered = str(text or "").lower()
    lowered = re.sub(r"[\u3000\s]+", " ", lowered)
    lowered = _PUNCT_RE.sub(" ", lowered)
    lowered = _NON_WORD_RE.sub(" ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def strip_ext(name: Any) -> str:
    """去掉扩展名。"""
    return re.sub(r"\.[A-Za-z0-9]{1,5}$", "", str(name or ""))


_ID_SUFFIX_RE = re.compile(r"\s*\[\d+\]\s*$")


def strip_id_suffix(name: Any) -> str:
    """去掉下载缓存文件名尾巴上的 `` [网易云id]``。

    缓存文件按「歌名 [id].mp3」命名（同一首歌的不同 id 不会互相覆盖），
    但列表里给人看的时候要显示成「海阔天空」而不是「海阔天空 [3342981041]」。
    """
    return _ID_SUFFIX_RE.sub("", str(name or "")).strip()


# ───────────────────────────────────────────── 点歌解析

# 开头连续的 @片段（OneBot 文本渲染形如「@名字(QQ:123)」）。
# 只剥**开头**的：正文中间的 @ 属于歌名的一部分（「点歌 @张三 最爱的那首」）。
_AT_PREFIX_RE = re.compile(r"^\s*(?:@[^\s@]{0,40}?\(QQ:\d+\)\s*|@[^\s@]{0,40}?\s+)+")
_LEAD_PUNCT_RE = re.compile(r"^\s*[，,。.、:：!！~～/／]*\s*")


def build_trigger_regex(keywords: Any = None) -> Optional[Pattern[str]]:
    """把关键词数组编译成一个「可出现在消息开头」的正则。"""
    words = [word for word in split_words(DEFAULT_TRIGGERS if keywords is None else keywords) if word]
    if not words:
        return None
    words.sort(key=len, reverse=True)
    # 允许关键词内部夹空白（「点 歌」也算），但不允许跨字符——否则误触太宽
    parts = ["\\s*".join(re.escape(char) for char in word) for word in words]
    return re.compile("(?:" + "|".join(parts) + ")", re.IGNORECASE)


def parse_song_request(
    text: Any,
    *,
    keywords: Any = None,
    self_names: Any = None,
    regex: Optional[Pattern[str]] = None,
) -> Dict[str, Any]:
    """从一条消息里解析出「点歌意图 + 歌名」。

    支持的写法（自然说话，不需要记命令）：
      点歌 海阔天空 / 点歌：海阔天空 / 来一首 海阔天空 / 放首 海阔天空
      /点歌 海阔天空 / 小鲸鱼，来一首《海阔天空》 / 点歌海阔天空

    NeoBot 的 ``Message.text`` 只拼 text 段，@ 不会留在正文里；这里仍然剥一遍
    开头的 @ 片段，是为了兼容把 @ 渲染进文本的适配器实现。
    """
    raw = "" if text is None else str(text)
    if not raw.strip():
        return {"hit": False, "song": "", "matched": ""}
    pattern = regex if isinstance(regex, re.Pattern) else build_trigger_regex(keywords)
    if pattern is None:
        return {"hit": False, "song": "", "matched": ""}

    # ① 剥掉正文开头的 @片段（含 @全体成员）与前导标点
    body = _AT_PREFIX_RE.sub("", raw)
    body = _LEAD_PUNCT_RE.sub("", body)

    # ② 剥掉开头的机器人自称 + 标点（「小鲸鱼，来一首…」）
    names = sorted(split_words(self_names, r"[,，、|;；\n]+"), key=len, reverse=True)
    for name in names:
        body = re.sub(
            r"^\s*" + re.escape(name) + r"\s*[，,。.、:：!！~～]*\s*", "", body
        )

    # ③ 触发词必须出现在**开头**（避免「我刚点歌了」这种陈述句被当成点歌）
    match = pattern.match(body)
    if match is None:
        return {"hit": False, "song": "", "matched": ""}

    # ④ 歌名 = 触发词之后的全部内容，去掉包裹符号与常见语气尾巴
    song = body[match.end() :]
    song = re.sub(r"^\s*[，,。.、:：!！~～\-—]+", "", song)
    song = re.sub(r"^[的《》「」『』\"'“”‘’\s]+", "", song)
    song = re.sub(r"[《》「」『』\"'“”‘’\s]+$", "", song)
    # 语气词尾巴可能叠着说（「海阔天空 吧 谢谢」）：剥一次之后再看看还有没有，
    # 只剥一层的话会把「吧」带进搜索词，分词/包含判定都会变差。
    previous = None
    while previous != song:
        previous = song
        song = re.sub(r"(?:这首歌|那首歌|一下|吧|呗|谢谢|蟹蟹|好不好|行不行)\s*$", "", song)
    return {"hit": True, "song": cut(song.strip(), 80), "matched": match.group(0)}


def parse_dirs(value: Any) -> List[str]:
    """把多行设置里的目录拆成数组。"""
    parts = re.split(r"[\r\n;]+", str(value or ""))
    cleaned = []
    for part in parts:
        item = part.strip().strip("\"'")
        if item:
            cleaned.append(item)
    return cleaned


# ───────────────────────────────────────────── 候选打分与挑选

EXT_WEIGHT = {"flac": 3, "wav": 2, "mp3": 2, "m4a": 1, "aac": 1, "ogg": 1, "wma": 0, "ape": 0}

#: 分词时丢掉的停用词与单字
STOP_TOKENS = {"的", "了", "吧", "版", "the", "a", "an", "of", "feat", "ft", "live"}


def tokens_of(song: Any) -> List[str]:
    """把歌名拆成用于匹配的 token（过滤停用词与单字）。"""
    tokens = []
    for token in normalize_name(song).split(" "):
        token = token.strip()
        if len(token) >= 2 and token not in STOP_TOKENS:
            tokens.append(token)
    return tokens[:8]


_COVER_WORDS = (
    "翻唱",
    "cover",
    "原唱",
    "伴奏",
    "instrumental",
    "karaoke",
    "remix",
    "抖音",
    "铃声",
    "片段",
    "demo",
)


def _has_cover_word(text: str) -> bool:
    return any(word in text for word in _COVER_WORDS)


def _artist_bias(title: str, artist: str, file: str, song: str, tokens: Any) -> int:
    """歌手偏置：用户写了歌手名时，「歌手对得上」加分，「歌手对不上」轻微扣分。

    这里只管「歌手」这一件事 —— 「翻唱/伴奏/原唱」的扣分在 ``_cover_penalty`` 里，
    因为它必须在每一条打分分支上都生效（见那个函数的说明）。

    真实故障：用户点「晴天 周杰伦」，下到的是「晴天 (原唱 周杰伦)」— RyaVocal ——
    那是翻唱，标题里恰好带着「原唱 周杰伦」，把所有分词都命中了，于是排到正版前面。
    这类结果比「找不到」更糟：用户会以为点歌机坏了。
    """
    query = normalize_name(song)
    if not query:
        return 0
    hay_title = normalize_name(title or file)
    hay_artist = normalize_name(artist)

    token_list = tokens if isinstance(tokens, list) else tokens_of(song)
    name_tokens = tokens_of(title or file)
    # 用户写在查询里的那部分（歌名之外）当作「想要的歌手」
    want_artists = [
        token
        for token in token_list
        if len(token) >= 2 and token not in name_tokens and token not in hay_title
    ]
    bias = 0
    if want_artists and hay_artist:
        if any(token in hay_artist for token in want_artists):
            bias += 120
        else:
            # 歌手对不上：轻微扣分，但不至于一票否决（可能是别名/多歌手）
            bias -= 60
    if want_artists and not hay_artist and _has_cover_word(hay_title):
        bias -= 100
    return bias


def _cover_penalty(query: str, title: str, artist: str, file_name: str) -> int:
    """「原唱 / 翻唱 / 伴奏 / remix …」扣分，**每一条打分分支都要加**。

    为什么单独拎出来：早先这段只在「query 出现在文件名 / 标题里」两个分支生效，
    而「晴天 周杰伦」对「晴天 (原唱 周杰伦)」这类候选走的是**分词分支**
    （带歌手的 query 不是标题的连续子串），扣分被整段跳过 —— 翻唱以 526 分
    过了 ``match_min_score``（430）那道闸。后果不是「排第二」，而是正版不在
    这一轮结果里时，把翻唱当正版下下来发进群。

    用户自己就写了翻唱 / remix 这类词时不扣分，否则「点歌 海阔天空 remix」
    会被自己扣死。
    """
    haystack = " ".join(part for part in (title, artist, file_name) if part)
    if not haystack or not _has_cover_word(haystack):
        return 0
    if query and _has_cover_word(query):
        return 0
    return -200


def score_candidate(candidate: Dict[str, Any], song: Any, *, fuzzy: bool = True, tokens: Any = None) -> int:
    """给单个候选打分，越高越像用户要的那首。"""
    query = normalize_name(song)
    if not query:
        return 0
    file_name = normalize_name(strip_ext(candidate.get("name")))
    title = normalize_name(candidate.get("title"))
    artist = normalize_name(candidate.get("artist"))
    haystack = (title + " " + artist + " " + file_name).strip()

    def adjusted(score: int, *, artist_bias: bool = True) -> int:
        total = score + _cover_penalty(query, title, artist, file_name)
        if artist_bias:
            total += _artist_bias(title, artist, file_name, query, tokens)
        return total

    if file_name == query:
        return 1000  # 文件名整串就是歌名
    if title and title == query:
        return 990  # 标签标题整串就是歌名
    if query in file_name:  # 文件名包含（覆盖率越高越像）
        return adjusted(700 + round(len(query) / max(len(file_name), 1) * 190))
    if title and query in title:
        return adjusted(660 + round(len(query) / max(len(title), 1) * 190))
    if query in haystack:
        # 只靠「歌名+歌手拼起来包含」判断不了歌手对不对（跨语言写法很常见：
        # 「宇多田光」对应「宇多田ヒカル」），所以这里只加翻唱扣分、不加歌手偏置。
        return adjusted(560, artist_bias=False)

    # 分词匹配（歌名带歌手、或文件名带歌手时）。
    token_list = tokens if isinstance(tokens, list) else tokens_of(song)
    if not token_list or fuzzy is False:
        return 0
    hits = [token for token in token_list if token in haystack]
    if not hits:
        return 0
    ratio = len(hits) / len(token_list)
    longest = max(len(token) for token in hits)

    # 全部/大部分 token 命中：稳。
    # 分数要随「命中比例」走，不能一刀切给 260 —— 用户写「One Last Kiss 宇多田光」，
    # 网易云返回歌手「宇多田ヒカル」，4 个 token 命中 3 个本该很确定。
    if ratio >= 0.6:
        return adjusted(round(260 + ratio * 260 + min(12, longest) * 2), artist_bias=False)

    # 只命中部分 token 时**不能要求全中**：最长的那个命中词够长（>= 4）就认可。
    coverage = round(longest / max(len(haystack), 1) * 40)
    if longest >= 4:
        return adjusted(480 + coverage, artist_bias=False)
    if longest == 3:
        return adjusted(310 + coverage, artist_bias=False)
    return 0


def _better_than(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    if left["score"] != right["score"]:
        return left["score"] > right["score"]
    left_mtime = float(left["candidate"].get("mtime_ms") or 0)
    right_mtime = float(right["candidate"].get("mtime_ms") or 0)
    if left_mtime != right_mtime:
        return left_mtime > right_mtime  # 新下载的优先
    left_ext = EXT_WEIGHT.get(str(left["candidate"].get("ext") or ""), -1)
    right_ext = EXT_WEIGHT.get(str(right["candidate"].get("ext") or ""), -1)
    if left_ext != right_ext:
        return left_ext > right_ext  # 音质好的优先
    return str(left["candidate"].get("name") or "") < str(right["candidate"].get("name") or "")


def pick_best(
    candidates: Any,
    song: Any,
    *,
    fuzzy: bool = True,
    min_score: int = 0,
) -> Optional[Dict[str, Any]]:
    """从候选里挑最优；返回 None = 没找到。

    同分排序：新下载的优先（mtime 降序）→ 音质好的扩展名 → 文件名。

    min_score：最低分闸。**联网结果必须过这道闸** —— 网易云搜索对不存在的歌名
    也会返回一堆不相干结果，只按「谁分高」挑就会把不相干的歌下下来发进群，
    比「没找到」糟得多。本地曲库给 0（本地文件都是用户自己的，模糊一点无所谓）。
    """
    tokens = tokens_of(song)
    best: Optional[Dict[str, Any]] = None
    for candidate in candidates if isinstance(candidates, (list, tuple)) else []:
        score = score_candidate(candidate, song, fuzzy=fuzzy, tokens=tokens)
        if score <= 0 or score < min_score:
            continue
        item = {"candidate": candidate, "score": score}
        if best is None or _better_than(item, best):
            best = item
    return best


#: 联网搜索结果的默认最低分：低于它宁可「没找到」，也不要放错歌。
NET_MIN_SCORE = 430

#: 「下载一份新的（不用本地这份）」这条动作项的 id
DOWNLOAD_NEW_ID = "__vs_download_new__"


def with_local_question(items: Any) -> Dict[str, Any]:
    """给候选列表补上「本地有 → 问用户用哪份」的那一条。

    规则：
      · 本地项永远排最前（「用本地这份吗」才是问题的主体）；
      · 本地有命中**且**有网易云版本时，插一条「下载一份新的」；
      · 本地没有时什么都不做（不要画蛇添足）。
    """
    source = [dict(item) for item in (items if isinstance(items, (list, tuple)) else [])]
    locals_ = [item for item in source if item.get("source") == "local"]
    remotes = [
        item
        for item in source
        if item.get("source") != "local" and item.get("id") != DOWNLOAD_NEW_ID
    ]
    if not locals_ or not remotes:
        return {"list": source, "asked_local": bool(locals_)}
    locals_.sort(key=lambda item: item.get("score") or 0, reverse=True)
    out = list(locals_)
    out.append(
        {
            "id": DOWNLOAD_NEW_ID,
            "name": "下载一份新的（不用本地这份）",
            "artist": "",
            "album": "",
            "duration_ms": 0,
            "fee": None,
            "source": "action",
            "score": -1,
        }
    )
    out.extend(remotes)
    return {"list": out, "asked_local": True}


def trim_choices(items: Any, max_items: Any) -> List[Dict[str, Any]]:
    """按上限裁剪候选，并保证「下载一份新的」后面还有远程项可挑。

    为什么要单独一个函数：``with_local_question`` 插动作项的前提是「本地和远程都有」，
    但裁剪发生在它之后 —— 本地候选一多，动作项就会被挤到最后一个位置、远程项全被切掉。
    用户选了「下载一份新的」之后拿不到任何远程项（`_handle_choice` 只能回一句
    「回数字重新选」，而此时待选已经被消费掉）→ 死胡同。
    所以这里反过来：动作项若被留下、远程项却一个不剩，就把动作项丢掉。
    """
    source = [dict(item) for item in (items if isinstance(items, (list, tuple)) else [])]
    try:
        limit = max(1, int(max_items or 1))
    except (TypeError, ValueError):
        limit = 1
    trimmed = source[:limit]
    has_action = any(str(item.get("id")) == DOWNLOAD_NEW_ID for item in trimmed)
    has_remote = any(item.get("source") not in ("local", "action") for item in trimmed)
    if has_action and not has_remote:
        trimmed = [item for item in source if str(item.get("id")) != DOWNLOAD_NEW_ID][:limit]
    return trimmed


# ───────────────────────────────────────────── 展示与选择回复


def fee_label(fee: Any) -> str:
    """fee 字段 → 人话。网易云的约定（实测多组结果核对）：
    0 = 免费；1 = VIP 歌曲；4 = 需购买专辑；8 = 低音质免费（高音质要会员）
    """
    # 必须先把 None/"" 挡掉：``float(None)`` 会抛异常，而 ``int(None)`` 在别处是 0，
    # 本地曲库和缓存里的歌根本没有 fee 字段，标成「免费」是编造信息。
    if fee is None or fee == "":
        return ""
    try:
        number = int(float(fee))
    except (TypeError, ValueError):
        return ""
    if number == 0:
        return "免费"
    if number == 1:
        return "VIP"
    if number == 4:
        return "需购买专辑"
    if number == 8:
        return "VIP(高音质)"
    return "fee=" + str(number)


def is_vip_only(fee: Any) -> bool:
    try:
        return int(float(fee)) in (1, 4)
    except (TypeError, ValueError):
        return False


def format_duration(duration_ms: Any) -> str:
    """毫秒 → 3:56。列表里带时长，是分辨「原版 / Live / 加长版」最直观的一条线索。"""
    try:
        total = round((float(duration_ms) or 0) / 1000)
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    return "{}:{:02d}".format(total // 60, total % 60)


def format_choice_list(song: str, choices: Any, *, max_items: int = 8) -> str:
    """把候选列表渲染成一条能直接发进群的文本。编号从 1 开始。"""
    items = [item for item in (choices if isinstance(choices, (list, tuple)) else [])][
        : max(1, int(max_items or 8))
    ]
    has_local = any(item.get("source") == "local" for item in items)
    has_action = any(str(item.get("id")) == DOWNLOAD_NEW_ID for item in items)
    if has_local and has_action:
        head = "「{}」本机已经有，要放这一份还是下新的？（回数字选）".format(song)
    elif has_local:
        # 只列本地（例如「自动下载」关掉时）：不能说成「还是下新的」，那会让人以为有得下
        head = "「{}」本机找到 {} 份，回数字选一个：".format(song, len(items))
    else:
        head = "「{}」找到 {} 首，回数字选一个：".format(song, len(items))
    lines = [head]
    for index, item in enumerate(items):
        if item.get("source") == "action":
            lines.append("{}. {}".format(index + 1, item.get("name") or ""))
            continue
        bits = []
        if item.get("source") == "local":
            bits.append("本地已有")
        if item.get("artist"):
            bits.append(str(item["artist"]))
        duration = format_duration(item.get("duration_ms"))
        if duration:
            bits.append(duration)
        fee = fee_label(item.get("fee"))
        if fee:
            bits.append(fee)
        suffix = " — " + " · ".join(bits) if bits else ""
        lines.append("{}. {}{}".format(index + 1, item.get("name") or "", suffix))
    return "\n".join(lines)


_FULLWIDTH_DIGITS = {ord("０") + offset: ord("0") + offset for offset in range(10)}
_CHOICE_NUMBER_RE = re.compile(r"^\s*(?:选|第|择)?\s*(\d{1,2})\s*(?:首|个|号)?\s*[.。!！]?\s*$")


def parse_choice_reply(text: Any, choices: Any) -> Optional[Dict[str, int]]:
    """解析「选择回复」。只认两种形态，避免误吃普通聊天：
      ① 纯数字          2 / ２ / 第2首 / 选2 / 2.
      ② 与某个候选歌手同名（「Beyond」）—— 必须完全一致且唯一命中
    返回 {"index": 0 基下标} 或 None。
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    items = choices if isinstance(choices, (list, tuple)) else []

    normalized = raw.translate(_FULLWIDTH_DIGITS)
    match = _CHOICE_NUMBER_RE.match(normalized)
    if match is not None:
        number = int(match.group(1))
        if 1 <= number <= len(items):
            return {"index": number - 1}
        return None

    query = normalize_name(raw)
    if not query or len(query) < 2:
        return None
    hits = [
        index
        for index, item in enumerate(items)
        if normalize_name(item.get("artist")) == query
    ]
    if len(hits) == 1:
        return {"index": hits[0]}
    return None


def is_ambiguous(scored: Any, *, ratio: float = 0.92) -> bool:
    """候选是否「分不出高下」。

    判据：最优与次优分数太接近（默认 0.92 以上），或前几名分属不同歌手。
    为什么不能只看「分数一样」：同一首歌在不同专辑里分差常常只有 1~2 分，
    而用户在意的是「Beyond 还是邓紫棋」，不是小数点。
    """
    items = [
        item
        for item in (scored if isinstance(scored, (list, tuple)) else [])
        if item and float(item.get("score") or 0) > 0
    ]
    if len(items) < 2:
        return False
    ordered = sorted(items, key=lambda item: item["score"], reverse=True)
    top, second = ordered[0], ordered[1]
    if top["score"] <= 0:
        return False
    if second["score"] >= top["score"] * ratio:
        return True
    artists = set()
    for item in ordered[: min(3, len(ordered))]:
        artist = normalize_name(item.get("artist") or (item.get("song") or {}).get("artist"))
        if artist:
            artists.add(artist)
    return len(artists) >= 2 and second["score"] >= top["score"] * 0.75


# ───────────────────────────────────────────── 待选缓存


class PendingChoices:
    """待选缓存：记住「谁在哪个会话里被问过要选哪一首」。

    三次真实故障都出在这里，规则是逐步逼出来的：
      ① 只按会话存 → 群里两个人同时点歌会串味（A 回 2 却放了 B 想听的）→ 加了 sender_id；
      ② 工具路径拿不到点歌人 → 待选存在空 key 下，用户回数字用自己的 QQ 号查不到
         —— 表现是「回数字后一直不放歌」→ 加了按会话的兜底；
      ③ 兜底太宽 → **别人回个数字就能替点歌的人放歌**。
         现在用「宽限期」折中：知道是谁点的，就先只认他（grace）；
         超过宽限期还没回，才允许别人接 —— 否则点歌的人一去不回，这首歌就永远放不出来了。
    """

    def __init__(
        self,
        *,
        ttl_sec: Any = 180,
        grace_sec: Any = 60,
        max_size: int = 300,
        now: Optional[Callable[[], float]] = None,
    ) -> None:
        self._ttl_source = ttl_sec
        self._grace_source = grace_sec
        self._now = now or time.monotonic
        self._max = max(1, int(max_size))
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._last_ask: Dict[str, Dict[str, Any]] = {}

    def _resolve(self, source: Any, fallback: float) -> float:
        value = source() if callable(source) else source
        try:
            number = float(value)
        except (TypeError, ValueError):
            return fallback
        return number if number > 0 else fallback

    @property
    def ttl_sec(self) -> float:
        return self._resolve(self._ttl_source, 180.0)

    @property
    def grace_sec(self) -> float:
        return self._resolve(self._grace_source, 60.0)

    def _key(self, chat_key: str, sender_id: str) -> str:
        return "{}|{}".format(chat_key, sender_id or "")

    def put(self, chat_key: str, sender_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        key = self._key(chat_key, sender_id)
        at = self._now()
        if len(self._pending) >= self._max:
            oldest = min(self._pending.items(), key=lambda item: item[1]["at"], default=None)
            if oldest is not None:
                self._pending.pop(oldest[0], None)
        self._pending[key] = {"at": at, "payload": payload}
        self._last_ask[str(chat_key)] = {
            "at": at,
            "sender_id": str(sender_id or ""),
            "payload": payload,
        }
        return payload

    def take(self, chat_key: str, sender_id: str) -> Optional[Dict[str, Any]]:
        """取一次就作废（避免回个数字被反复消费）。"""
        key = self._key(chat_key, sender_id)
        hit = self._pending.pop(key, None)
        if hit is None:
            record = self._last_ask_of(chat_key)
            # 宽限期内只认点歌人本人：否则「别人回个数字就能替点歌的人放歌」
            hit = record if record and self._fallback_allowed(record, sender_id) else None
        if hit is None:
            return None
        self._last_ask.pop(str(chat_key), None)
        if self._now() - hit["at"] > self.ttl_sec:
            return None
        return hit["payload"]

    def peek(self, chat_key: str, sender_id: str) -> Optional[Dict[str, Any]]:
        """只读探测（不改状态）：用来判断「我要不要接管这条消息」。"""
        key = self._key(chat_key, sender_id)
        hit = self._pending.get(key)
        if hit is not None and self._now() - hit["at"] > self.ttl_sec:
            self._pending.pop(key, None)
            hit = None
        if hit is not None:
            return hit["payload"]
        record = self._last_ask_of(chat_key)
        if record is None:
            return None
        return record["payload"] if self._fallback_allowed(record, sender_id) else None

    def _last_ask_of(self, chat_key: str) -> Optional[Dict[str, Any]]:
        record = self._last_ask.get(str(chat_key))
        if record is None:
            return None
        if self._now() - record["at"] > self.ttl_sec:
            self._last_ask.pop(str(chat_key), None)
            return None
        return record

    def _fallback_allowed(self, record: Dict[str, Any], sender_id: str) -> bool:
        asker = str(record.get("sender_id") or "")
        if not asker:
            return True  # 不知道是谁点的，谁接都行
        if asker == str(sender_id or ""):
            return True
        return self._now() - record["at"] > self.grace_sec

    def asked_who(self, chat_key: str) -> str:
        record = self._last_ask.get(str(chat_key))
        return str(record.get("sender_id") or "") if record else ""

    def drop(self, chat_key: str, sender_id: str) -> bool:
        first = self._pending.pop(self._key(chat_key, sender_id), None) is not None
        second = self._last_ask.pop(str(chat_key), None) is not None
        return first or second

    @property
    def size(self) -> int:
        return len(self._pending)

    def clear(self) -> None:
        self._pending.clear()
        self._last_ask.clear()


# ───────────────────────────────────────────── 管理员解除限制


_UNLOCK_HEAD_RE = re.compile(
    r"^(?:解除|清除|清空|重置|重设)\s*(?:点歌)?\s*(?:限制|限流|冷却|配额|额度)\s*"
)
_UNLOCK_SCOPES = (
    ("self", re.compile(r"^(?:我的|我自己|自己|我)")),
    ("all", re.compile(r"^(?:全部会话|所有会话|所有群|全部|所有|全群)")),
    ("chat", re.compile(r"^(?:这个会话|这个群|当前群|本会话|本群|这里)")),
)
_UNLOCK_DURATION_RE = re.compile(r"^(\d{1,4})\s*(分钟|分|小时|时|min|h|m)", re.IGNORECASE)


def parse_unlock_command(text: Any, keywords: Any = None) -> Dict[str, Any]:
    """解析「解除限制」指令。必须**只认明确写法**，否则群里说一句「限制好多」就被当成管理员指令了。

    认这些（动作词 + 限制词，中间可夹「点歌」）：
      解除点歌限制 / 解除限制 / 清除点歌冷却 / 重置点歌配额 / 解除点歌限流
    后面可以再跟范围与时长：
      解除点歌限制 本群          → 只清这个会话
      解除点歌限制 我             → 只清自己
      解除点歌限制 12345678      → 清那个人（QQ 号）
      解除点歌限制 10分钟        → 顺带开 10 分钟不限制（不填 = 只清记录）

    ``keywords`` 是配置项 ``unlock_command`` 拆出来的**额外**触发写法：
    原版把这两个配置键写了却没用（一直是死键），这里真的接上 ——
    命中配置里的词之后，后面的范围与时长照同一套语法解析。
    """
    empty = {"hit": False, "scope": "chat", "target_id": "", "unlimited_ms": 0, "matched": ""}
    raw = str(text or "").strip()
    if not raw:
        return empty

    body = _AT_PREFIX_RE.sub("", raw)
    body = re.sub(r"^\s*[，,。.、:：!！~～]+\s*", "", body).strip()

    # 先看配置里的自定义触发词（长的优先：「解除点歌限制」不该被「解除」抢走）
    rest = ""
    matched = ""
    for word in sorted(split_words(keywords), key=len, reverse=True):
        if body.startswith(word):
            rest = body[len(word) :].strip()
            matched = word
            break

    if not matched:
        head = _UNLOCK_HEAD_RE.match(body)
        if head is None:
            return empty
        rest = body[head.end() :].strip()
        matched = head.group(0).strip()

    scope = "chat"
    target_id = ""
    consumed = ""
    # 表里按「长的写法在前」排（「我的」要排在「我」前面，否则「我的」只会吃掉「我」）
    for name, pattern in _UNLOCK_SCOPES:
        found = pattern.match(rest)
        if found is not None:
            scope = name
            consumed = found.group(0)
            break
    if not consumed:
        id_match = re.match(r"^(\d{5,12})", rest)
        if id_match is not None:
            scope = "user"
            target_id = id_match.group(1)
            consumed = id_match.group(0)
    rest = rest[len(consumed) :].strip()

    unlimited_ms = 0
    duration = _UNLOCK_DURATION_RE.match(rest)
    if duration is not None:
        amount = int(duration.group(1))
        unit = duration.group(2).lower()
        multiplier = 3600000 if unit in ("小时", "时", "h") else 60000
        unlimited_ms = max(0, min(24 * 3600000, amount * multiplier))

    return {
        "hit": True,
        "scope": scope,
        "target_id": target_id,
        "unlimited_ms": unlimited_ms,
        "matched": matched,
    }


class Exemptions:
    """临时豁免表：管理员「解除限制」后，在有效期内完全跳过防刷屏闸。

    三种作用域各自一张表，键是：
      chat → chat_key        self → chat_key|sender_id      user → 任意会话里的这个 QQ
    user 作用域不绑会话：管理员的意图是「这个人别再被拦了」。
    **all 作用域是一张全局表**：指令说的是「所有会话」，那就必须真的对所有会话生效
    —— 早先 all 只写当前会话的 `_chat`，回执却印着「所有会话 N 分钟内不再限制」。
    """

    def __init__(
        self, *, now: Optional[Callable[[], float]] = None, max_size: int = 500
    ) -> None:
        self._now = now or time.monotonic
        self._max = max(1, int(max_size))
        self._chat: Dict[str, float] = {}
        self._self: Dict[str, float] = {}
        self._user: Dict[str, float] = {}
        #: 「全部会话」的全局豁免（单调时钟的到期时刻，0 = 没有）
        self._global_until: float = 0.0

    def _prune(self) -> None:
        current = self._now()
        for table in (self._chat, self._self, self._user):
            for key in [key for key, until in table.items() if until <= current]:
                table.pop(key, None)

    def grant(self, chat_key: str, sender_id: str = "", scope: str = "chat", duration_sec: float = 600) -> float:
        """授予豁免。返回绝对时间戳（单调时钟）。"""
        self._prune()
        until = self._now() + max(0.0, float(duration_sec or 0))
        if scope == "all":
            # 「所有会话」= 全局：任何会话、任何人都放行（回执上写的就是这个意思）
            self._global_until = max(self._global_until, until)
            self._chat[str(chat_key)] = until
            if sender_id:
                self._self["{}|{}".format(chat_key, sender_id)] = until
        elif scope == "self":
            if sender_id:
                self._self["{}|{}".format(chat_key, sender_id)] = until
        elif scope == "user":
            if sender_id:
                self._user[str(sender_id)] = until
        else:
            self._chat[str(chat_key)] = until
        if len(self._chat) + len(self._self) + len(self._user) > self._max:
            self._prune()
        return until

    def check(self, chat_key: str, sender_id: str = "") -> Dict[str, Any]:
        """这个人在这个会话里是否被豁免。"""
        current = self._now()
        key = str(chat_key)
        if self._global_until > current:
            return {"exempt": True, "until": self._global_until, "scope": "all"}
        if self._global_until:
            self._global_until = 0.0
        until = self._chat.get(key)
        if until is not None:
            if until > current:
                return {"exempt": True, "until": until, "scope": "chat"}
            self._chat.pop(key, None)
        if sender_id:
            until = self._self.get("{}|{}".format(key, sender_id))
            if until is not None:
                if until > current:
                    return {"exempt": True, "until": until, "scope": "self"}
                self._self.pop("{}|{}".format(key, sender_id), None)
            until = self._user.get(str(sender_id))
            if until is not None:
                if until > current:
                    return {"exempt": True, "until": until, "scope": "user"}
                self._user.pop(str(sender_id), None)
        return {"exempt": False, "until": 0.0, "scope": ""}

    def revoke(self, chat_key: str, sender_id: str = "", scope: str = "chat") -> int:
        key = str(chat_key)
        removed = 0
        if scope == "all" and self._global_until > 0:
            self._global_until = 0.0
            removed += 1
        if scope in ("all", "chat") and self._chat.pop(key, None) is not None:
            removed += 1
        if scope in ("all", "self") and self._self.pop("{}|{}".format(key, sender_id), None) is not None:
            removed += 1
        if scope in ("all", "user") and sender_id and self._user.pop(str(sender_id), None) is not None:
            removed += 1
        return removed

    def list(self) -> List[Dict[str, Any]]:
        self._prune()
        current = self._now()
        out = []
        if self._global_until > current:
            out.append({"scope": "all", "key": "*", "left_sec": self._global_until - current})
        for scope, table in (("chat", self._chat), ("self", self._self), ("user", self._user)):
            for key, until in table.items():
                out.append({"scope": scope, "key": key, "left_sec": until - current})
        return out

    def clear(self) -> None:
        self._chat.clear()
        self._self.clear()
        self._user.clear()
        self._global_until = 0.0


# ───────────────────────────────────────────── 限流闸


class RateGate:
    """确定性插件必须自己防刷屏（核心的发送限频只兜总量，不认「谁点的歌」）。

    四道闸：同会话冷却 / 同会话配额 / 全局配额 / 同一人配额。
    只有**真的发出去**才记账（判定失败、找不到歌都不占配额）。
    """

    def __init__(self, *, now: Optional[Callable[[], float]] = None) -> None:
        self._now = now or time.monotonic
        self._last_by_chat: Dict[str, float] = {}
        self._sent_by_chat: List[Dict[str, Any]] = []
        self._sent_all: List[float] = []
        self._sent_by_user: List[Dict[str, Any]] = []

    def check(self, chat_key: str, sender_id: str, cfg: Any) -> Dict[str, Any]:
        current = self._now()
        hour_ago = current - 3600
        self._sent_all = [at for at in self._sent_all if at > hour_ago]
        self._sent_by_chat = [item for item in self._sent_by_chat if item["at"] > hour_ago]
        self._sent_by_user = [item for item in self._sent_by_user if item["at"] > hour_ago]

        # 从没发过的会话必须放行：这里不能用 ``get(chat, 0)`` 的真值兜底 —
        # 注入式时钟下 current 可能是小数值，``current - 0 < cooldown`` 会把
        # 「第一次点歌」直接误判成冷却中。用 -inf 表示「没有记录」，语义明确。
        last = self._last_by_chat.get(chat_key, float("-inf"))
        cooldown_sec = float(getattr(cfg, "cooldown_sec", 0) or 0)
        if cooldown_sec > 0 and current - last < cooldown_sec:
            left = int(math.ceil(cooldown_sec - (current - last)))
            return {"ok": False, "reason": "刚发过一首，还要等 {} 秒".format(left)}

        max_per_chat = int(getattr(cfg, "max_per_chat_hour", 0) or 0)
        if max_per_chat > 0:
            used = len([item for item in self._sent_by_chat if item["chat"] == chat_key])
            if used >= max_per_chat:
                return {"ok": False, "reason": "这个会话每小时最多 {} 首，已经到量".format(max_per_chat)}

        max_per_hour = int(getattr(cfg, "max_per_hour", 0) or 0)
        if max_per_hour > 0 and len(self._sent_all) >= max_per_hour:
            return {"ok": False, "reason": "所有会话合计每小时最多 {} 首，已经到量".format(max_per_hour)}

        max_per_user = int(getattr(cfg, "max_per_user_hour", 0) or 0)
        if max_per_user > 0 and sender_id:
            used_by_user = len(
                [item for item in self._sent_by_user if str(item["user"]) == str(sender_id)]
            )
            if used_by_user >= max_per_user:
                return {"ok": False, "reason": "同一个人每小时最多点 {} 首，已经到量".format(max_per_user)}
        return {"ok": True}

    def commit(self, chat_key: str, sender_id: str = "") -> None:
        current = self._now()
        self._last_by_chat[chat_key] = current
        self._sent_by_chat.append({"chat": chat_key, "at": current, "sender": sender_id or ""})
        self._sent_all.append(current)
        if sender_id:
            self._sent_by_user.append({"user": sender_id, "at": current})
        # 内存兜底：Map 不随会话数无限增长（时钟是**秒**，别照抄原版的毫秒常量）
        if len(self._last_by_chat) > 500:
            cutoff = current - 3600
            for key in [key for key, at in self._last_by_chat.items() if at < cutoff]:
                self._last_by_chat.pop(key, None)

    def reset(self, chat_key: str = "", sender_id: str = "", scope: str = "all") -> Dict[str, int]:
        """清记录。三种粒度（管理员「解除限制」用）：
          chat → 只清这个会话；user → 只清这个人；all → 全清
        """
        out = {"chat": 0, "user": 0, "all": 0}
        if scope == "all":
            out["chat"] = len(self._last_by_chat)
            out["user"] = len(self._sent_by_user)
            out["all"] = len(self._sent_all)
            self._last_by_chat.clear()
            self._sent_by_chat = []
            self._sent_all = []
            self._sent_by_user = []
            return out
        if scope == "chat":
            if self._last_by_chat.pop(chat_key, None) is not None:
                out["chat"] = 1
            before = len(self._sent_by_chat)
            self._sent_by_chat = [item for item in self._sent_by_chat if item["chat"] != chat_key]
            out["all"] = before - len(self._sent_by_chat)
            return out
        if scope == "user":
            before_user = len(self._sent_by_user)
            self._sent_by_user = [
                item for item in self._sent_by_user if str(item["user"]) != str(sender_id)
            ]
            out["user"] = before_user - len(self._sent_by_user)
            before_chat = len(self._sent_by_chat)
            self._sent_by_chat = [
                item for item in self._sent_by_chat if str(item.get("sender") or "") != str(sender_id)
            ]
            out["all"] = before_chat - len(self._sent_by_chat)
            return out
        return out


# ───────────────────────────────────────────── 带 TTL 的缓存


class TtlCache:
    """极简 TTL 缓存：曲库索引（5 分钟）与 ID3 标签（30 分钟）都用它。

    ⚠️ 必须带锁：两个调用点都在 ``asyncio.to_thread`` 里（``library.load_library``
    与 ``library.with_tags``），两个群同时点歌时是真并行。``set()`` 淘汰最旧一条要
    遍历整个字典，另一个线程这时插入/删除就会抛
    ``RuntimeError: dictionary changed size during iteration`` —— 而
    ``_load_library`` 的兜底是「返回空曲库」，于是本地明明有歌也会被判成没有、转去下载。
    """

    def __init__(
        self,
        *,
        ttl_sec: float = 60,
        max_size: int = 200,
        now: Optional[Callable[[], float]] = None,
    ) -> None:
        self._ttl_sec = float(ttl_sec)
        self._now = now or time.monotonic
        self._max = max(1, int(max_size))
        self._items: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: Any) -> Any:
        name = str(key)
        with self._lock:
            item = self._items.get(name)
            if item is None:
                return None
            if self._now() - item["at"] > self._ttl_sec:
                self._items.pop(name, None)
                return None
            return item["value"]

    def set(self, key: Any, value: Any) -> Any:
        name = str(key)
        with self._lock:
            if len(self._items) >= self._max:
                oldest = min(self._items.items(), key=lambda item: item[1]["at"], default=None)
                if oldest is not None:
                    self._items.pop(oldest[0], None)
            self._items[name] = {"at": self._now(), "value": value}
        return value

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
