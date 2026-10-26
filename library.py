"""本机曲库扫描与音频标签读取。

从原 Node 版 ``logic.js`` 的 ID3 部分与 ``index.js`` 的扫描部分移植。
这里**只读**用户的文件：不移动、不改名、不写入任何音乐目录。

为什么单独成文件：扫描是这条链路上唯一的重 IO（几千个 stat + 读标签），
放进独立模块后可以被 ``asyncio.to_thread`` 整体丢到线程里，
不会卡住 NeoBot 的事件循环。
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

#: 认作音频的扩展名
AUDIO_EXT = {
    "mp3",
    "flac",
    "m4a",
    "aac",
    "wav",
    "ogg",
    "oga",
    "opus",
    "wma",
    "ape",
    "mp4",
}

#: 这些扩展名即使符合 AUDIO_EXT 也要排除（封面/歌词/加密文件）
COVER_EXT = {"jpg", "jpeg", "png", "webp", "bmp", "gif", "lrc", "txt", "cue", "ini", "db", "ncm"}

#: 小于这个体积的多半是封面/占位文件
_MIN_AUDIO_BYTES = 32 * 1024


def _ext_of(name: str) -> str:
    _, dot, suffix = name.rpartition(".")
    return suffix.lower() if dot else ""


def default_scan_roots(*, home: Optional[Path] = None) -> List[Path]:
    """「同时扫描默认位置」打开时，除了用户填的目录还去这些地方找。"""
    home = home or Path.home()
    return [
        home / "Music" / "CloudMusic",
        home / "Music" / "VipSongsDownload",
        home / "Music",
        home / "Documents" / "CloudMusic",
        home / "Desktop",
        Path("D:/CloudMusic"),
        Path("E:/CloudMusic"),
    ]


def scan_roots(
    *,
    search_directories: Any,
    download_dir: Optional[Path],
    default_scan_dirs: bool,
    home: Optional[Path] = None,
) -> List[Path]:
    """要扫的目录：设置里填的优先，其次**下载缓存目录**，最后本机默认位置。

    去重（大小写不敏感）后只留真实存在的目录。
    """
    seen: List[str] = []
    ordered: List[Path] = []

    def push(candidate: Any) -> None:
        if candidate is None:
            return
        text = str(candidate).strip()
        if not text:
            return
        try:
            path = Path(text).expanduser().resolve()
        except (OSError, ValueError):
            return
        key = str(path).lower()
        if key in seen:
            return
        seen.append(key)
        ordered.append(path)

    for directory in search_directories if isinstance(search_directories, (list, tuple)) else []:
        push(directory)
    # 下载缓存目录放在「用户填的目录」之后、「默认位置」之前：
    # 这样本地曲库优先，其次是上一首刚下好的（命中即秒发，不用再联网）。
    push(download_dir)
    if default_scan_dirs:
        for directory in default_scan_roots(home=home):
            push(directory)
    return [path for path in ordered if path.is_dir()]


def collect_audio_files(
    root: Path,
    *,
    max_depth: int,
    max_files: int,
    deadline: float,
) -> List[Dict[str, Any]]:
    """递归收音频文件（有深度、条数、时间三重上限，绝不卡住收消息）。"""
    files: List[Dict[str, Any]] = []
    stack: List[Tuple[Path, int]] = [(root, 0)]
    while stack:
        if time.monotonic() > deadline or len(files) >= max_files:
            break
        directory, depth = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if len(files) >= max_files or time.monotonic() > deadline:
                        break
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if depth < max_depth and not entry.name.startswith("."):
                                stack.append((Path(entry.path), depth + 1))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        ext = _ext_of(entry.name)
                        if ext not in AUDIO_EXT or ext in COVER_EXT:
                            continue
                        stat = entry.stat()
                        if stat.st_size < _MIN_AUDIO_BYTES:
                            continue
                        files.append(
                            {
                                "path": str(entry.path),
                                "name": entry.name,
                                "ext": ext,
                                "size": stat.st_size,
                                "mtime_ms": stat.st_mtime * 1000.0,
                            }
                        )
                    except OSError:
                        continue
        except OSError:
            continue
    return files


def load_library(
    roots: List[Path],
    *,
    max_depth: int,
    max_files: int,
    time_budget_ms: float = 2500,
    cache: Any = None,
) -> List[Dict[str, Any]]:
    """读一个目录集合里的全部音频（结果进 TTL 缓存）。"""
    key = "|".join(str(root) for root in roots) + "#{}#{}".format(max_depth, max_files)
    if cache is not None:
        cached = cache.get(key)
        if cached is not None:
            return cached
    deadline = time.monotonic() + max(0.1, time_budget_ms / 1000.0)
    files: List[Dict[str, Any]] = []
    for root in roots:
        files.extend(collect_audio_files(root, max_depth=max_depth, max_files=max_files, deadline=deadline))
        if time.monotonic() > deadline or len(files) >= max_files:
            break
    # ⚠️ 只缓存**扫完的**结果：被时间/条数截断的那一份如果也进缓存，接下来 5 分钟里
    #    后半部分的歌都会被当成「本地没有」，于是明明有歌也要重新下载一遍。
    complete = len(files) < max_files and time.monotonic() <= deadline
    if cache is not None and complete:
        cache.set(key, files)
    return files


def has_encrypted_ncm(roots: List[Path]) -> bool:
    """目录里是不是有 .ncm（网易云加密文件）—— 用于给一句人能看懂的提示。"""
    for root in roots:
        try:
            with os.scandir(root) as entries:
                for entry in entries:
                    try:
                        if entry.is_file() and entry.name.lower().endswith(".ncm"):
                            return True
                        if entry.is_dir():
                            with os.scandir(entry.path) as children:
                                if any(child.name.lower().endswith(".ncm") for child in children):
                                    return True
                    except OSError:
                        continue
        except OSError:
            continue
    return False


# ───────────────────────────────────────────── ID3

_ID3_FRAME_ID_RE = re.compile(r"^[A-Z0-9]{4}$")


def _decode_id3_text(data: bytes) -> str:
    """解 ID3 文本帧：按编码字节切，返回第一个非空段。"""
    if not data:
        return ""
    encoding = data[0]
    body = data[1:]
    try:
        if encoding == 1:
            text = body.decode("utf-16", "ignore")
        elif encoding == 2:
            text = body.decode("utf-16-be", "ignore")
        elif encoding == 3:
            text = body.decode("utf-8", "ignore")
        else:
            text = body.decode("latin-1", "ignore")
    except (UnicodeError, LookupError):
        return ""
    for part in text.split("\x00"):
        if part.strip():
            return part.strip()
    return ""


def _decode_gbk(raw: bytes) -> str:
    """ID3v1 没有编码标记；中文 Windows 上的 mp3 基本都用 GBK 写标题。

    Node 版得手搓一张 GBK 表（Buffer 不认 gbk），Python 直接有 codec，省掉那一整块。
    """
    for encoding in ("gbk", "utf-8", "latin-1"):
        try:
            return raw.decode(encoding, "ignore").strip()
        except LookupError:
            continue
    return ""


def _pick_v1_slice(raw: bytes) -> str:
    end = raw.find(b"\x00")
    body = raw[:end] if end >= 0 else raw
    if not body:
        return ""
    ascii_text = body.decode("latin-1", "ignore").strip()
    if not any(byte >= 0x80 for byte in body):  # 纯 ASCII：不用猜编码
        return ascii_text
    return _decode_gbk(body) or ascii_text


def read_audio_tags(path: str, *, max_bytes: int = 1024 * 1024) -> Dict[str, str]:
    """极简 ID3 读取：只取 TIT2（标题）与 TPE1（艺术家）。

    为什么要它：群里搜「海阔天空」时，文件名可能是「Beyond - 海阔天空.flac」
    甚至「01 track.flac」，光看文件名匹配不上。
    只读文件头 + 尾部 128 字节（ID3v1），**不整文件读入内存**：
    一个 40MB 的 flac 全读进来只为看个标签，纯属浪费。
    """
    title = ""
    artist = ""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            head = handle.read(min(size, max_bytes))
            if len(head) > 10 and head[:3] == b"ID3":
                major = head[3]
                flags = head[5]
                tag_size = (
                    ((head[6] & 0x7F) << 21)
                    | ((head[7] & 0x7F) << 14)
                    | ((head[8] & 0x7F) << 7)
                    | (head[9] & 0x7F)
                )
                offset = 10
                if flags & 0x40:  # extended header
                    # 长度语义跟版本走：v2.3 的大小字段**不含**自身（还要再跳那 4 字节），
                    # v2.4 的大小字段**含**自身。以前固定 += 4，两个版本都会停在扩展头中间，
                    # 后面每个 frame_id 都对不上 → 整段标签读不出来。
                    if len(head) >= 14:
                        size_bytes = head[10:14]
                        if major >= 4:
                            ext_size = (
                                ((size_bytes[0] & 0x7F) << 21)
                                | ((size_bytes[1] & 0x7F) << 14)
                                | ((size_bytes[2] & 0x7F) << 7)
                                | (size_bytes[3] & 0x7F)
                            )
                            offset += ext_size if ext_size >= 6 else 6
                        else:
                            ext_size = int.from_bytes(size_bytes, "big")
                            offset += 4 + ext_size
                        offset = min(offset, len(head))
                end = min(len(head), 10 + tag_size)
                while offset + 10 <= end:
                    frame_id = head[offset : offset + 4].decode("latin-1", "ignore")
                    if not _ID3_FRAME_ID_RE.match(frame_id):
                        break  # 帧区结束（padding）
                    if major >= 4:
                        frame_size = (
                            ((head[offset + 4] & 0x7F) << 21)
                            | ((head[offset + 5] & 0x7F) << 14)
                            | ((head[offset + 6] & 0x7F) << 7)
                            | (head[offset + 7] & 0x7F)
                        )
                    else:
                        frame_size = int.from_bytes(head[offset + 4 : offset + 8], "big")
                    if frame_size <= 0 or frame_size > len(head):
                        break
                    data = head[offset + 10 : offset + 10 + frame_size]
                    if frame_id == "TIT2":
                        title = _decode_id3_text(data)
                    elif frame_id == "TPE1":
                        artist = _decode_id3_text(data)
                    offset += 10 + frame_size
                    if title and artist:
                        break

            # ID3v1 兜底（最后 128 字节 "TAG"）。
            if (not title or not artist) and size > 128:
                handle.seek(size - 128)
                tail = handle.read(128)
                if tail[:3] == b"TAG":
                    if not title:
                        title = _pick_v1_slice(tail[3:33])
                    if not artist:
                        artist = _pick_v1_slice(tail[33:63])
    except OSError:
        return {"title": "", "artist": ""}

    return {"title": _cut(title), "artist": _cut(artist)}


def _cut(text: str, max_len: int = 80) -> str:
    collapsed = re.sub(r"\s+", " ", str(text or "")).strip()
    return collapsed[:max_len]


def with_tags(
    files: List[Dict[str, Any]], cache: Any = None, *, budget_sec: float = 2.0
) -> List[Dict[str, Any]]:
    """给候选补上 ID3 标签（结果进 TTL 缓存）。

    ⚠️ 两个必须做的事，缺一个都会出事：
      ① 调用方必须把本函数丢进线程（``asyncio.to_thread``）再调 ——
         它会对每个没缓存的文件 open + 读最多 1MiB，几千个文件同步跑在事件循环上
         就是「几千个群的消息全部卡住」；
      ② 有**时间预算**：预算用完就不再读新标签，剩下的只按文件名匹配。
         第一次点歌时标签缓存是空的，没有预算的话一次能把整个曲库读一遍。
    """
    deadline = time.monotonic() + max(0.0, float(budget_sec))
    out: List[Dict[str, Any]] = []
    for item in files or []:
        key = "{}|{}|{}".format(item.get("path"), item.get("mtime_ms"), item.get("size"))
        cached = cache.get(key) if cache is not None else None
        if cached is None:
            if time.monotonic() > deadline:
                # 预算用完：这一条不读标签，但文件本身仍然参与「按文件名打分」
                out.append(dict(item, title="", artist=""))
                continue
            cached = read_audio_tags(str(item.get("path") or ""))
            if cache is not None:
                cache.set(key, cached)
        merged = dict(item)
        merged.update(cached)
        out.append(merged)
    return out


# ───────────────────────────────────────────── 时长估算（判重缓存用）


def estimate_audio_seconds(path: str) -> int:
    """估算 MP3 时长（秒）：找第一个有效帧头，按位率算。

    只用于「确认两个文件是不是同一版」；估不准返回 0，调用方会因此保守处理（保留文件）。
    """
    try:
        size = os.path.getsize(path)
        if size < 1024:
            return 0
        with open(path, "rb") as handle:
            head = handle.read(min(size, 256 * 1024))
    except OSError:
        return 0

    offset = 0
    if head[:3] == b"ID3":
        offset = 10 + (
            ((head[6] & 0x7F) << 21)
            | ((head[7] & 0x7F) << 14)
            | ((head[8] & 0x7F) << 7)
            | (head[9] & 0x7F)
        )
    # MPEG 帧头里 byte1 的 bit1-2 是层号：01 = Layer III（mp3 就是它）。
    # 两张表分开写，避免「拿 Layer I 的表去算 mp3」这种一眼看不出来的错。
    bitrates_layer3 = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
    bitrates_layer12 = (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384)
    for index in range(offset, max(offset, len(head) - 4)):
        if head[index] != 0xFF or (head[index + 1] & 0xE0) != 0xE0:
            continue
        layer = (head[index + 1] >> 1) & 0x03
        bitrate_index = (head[index + 2] >> 4) & 0x0F
        sample_index = (head[index + 2] >> 2) & 0x03
        if layer == 0 or bitrate_index == 0 or bitrate_index == 15 or sample_index == 3:
            continue
        table = bitrates_layer3 if layer == 1 else bitrates_layer12
        kbps = table[bitrate_index]
        if not kbps:
            return 0
        return round((size - offset) * 8 / (kbps * 1000))
    return 0
