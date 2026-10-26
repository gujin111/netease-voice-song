"""点歌发语音条（netease-voice-song）—— NeoBot 插件。

══════════════════════════════════════════════════════════════════════════
这是什么
══════════════════════════════════════════════════════════════════════════
群里喊一声「点歌 歌名」，机器人就找出那首歌，以 **QQ 语音条**的形式发出来
（后面再跟一行歌名歌手）。本地曲库优先；本地没有就去网易云下载**完整歌曲**。

这个文件是原来 DSH / qq-agent 的 Node 版（``index.js``）移植到 NeoBot 2.0
插件 API 的结果。核心判据（打分公式、限流闸、同名多首的待选规则）保持原样，
挂载方式换成了 NeoBot 的 ``@plugin.message``：

══════════════════════════════════════════════════════════════════════════
一、为什么用 message hook + block，而不是命令
══════════════════════════════════════════════════════════════════════════
原版是「确定性插件」：命中就**不唤醒模型**，全程 0 token，也不产生会话。
NeoBot 里对应的机制是 ``@plugin.message(rule=..., block=True)``：

  · ``rule`` 在**每条**入站消息上跑一次同步判定（纯文本解析，零 IO）；
  · 命中 → 处理器执行，然后 ``block=True`` 让 HookBus 调 ``consume()``；
  · gateway 看到 ``consumed`` 就**直接 return**，消息根本不进回复管线
    （见 ``neobot_app/runtime/gateway.py`` 的 ``handle()``）。
    所以既不发模型请求、也不会出现「插件放歌 + 模型接话」双发。

⚠️ 关键实现约束：**处理器必须立刻返回**。
  OneBot 的事件分发是 ``await dispatcher.publish(event)`` 串行循环
  （``neobot_adapter/onebot/adapter.py`` 的 ``_dispatch_loop``），
  在处理器里 await 一次 60 秒的下载会**卡住整条事件流**（所有群的消息都收不到）。
  所以这里只做「同步判定 + 起一个后台任务」，真正的工作在任务里跑；
  同一会话的任务用 asyncio.Lock 串起来，保证两首歌不会叠在一起发。

══════════════════════════════════════════════════════════════════════════
二、语音条是怎么发出去的
══════════════════════════════════════════════════════════════════════════
原版依赖协议端（SnowLuma）自带的 ffmpeg 把 mp3 转 SILK。NeoBot 这边：

  ``ctx.send_audio(conversation, path=...)``
    → ``neobot_app/utils/media_sender.send_audio()``
    → ``prepare_audio_segment()``：FileServer 开着给 HTTP URL，关着给 ``file:///``
    → adapter 发 ``{"type": "record", "data": {"file": ...}}``
    → OneBot 协议端（NapCat / LLOneBot / Lagrange）自己读文件并按需转码

也就是说插件只需要给一个**本地音频文件路径**，不用自己转 SILK（和原版一致）。
只走这一条发送路径，**不做**「失败后再用别的方式发一遍」的兜底：echo 超时在某些
适配器实现里会以异常形式冒出来，兜底会让群里出现两条一模一样的语音。

══════════════════════════════════════════════════════════════════════════
三、模块划分
══════════════════════════════════════════════════════════════════════════
  logic.py     纯逻辑（解析 / 打分 / 限流 / 待选表），只用标准库，可离线单测
  library.py   本机曲库扫描 + ID3 读取（只读用户文件）
  netease.py   网易云客户端（搜索 / 直链 / 下载 / 登录态 / 缓存去重）
  download.py  搜歌→挑歌→下载的流程（依赖注入，可拿假客户端测试）
  config.py    pydantic 配置模型（面板表单 + 校验）
"""

from __future__ import annotations

import asyncio
import math
import secrets
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from neobot_modloader import Message, Plugin

from . import download, library, logic, netease
from .config import VoiceSongConfig

PLUGIN_NAME = "netease-voice-song"
PLUGIN_VERSION = "1.7.0"

#: 临时音频文件保留多久（协议端可能还在读，不能发完就删）
TMP_KEEP_SEC = 15 * 60
#: 曲库索引缓存
INDEX_TTL_SEC = 5 * 60
#: ID3 标签缓存
TAGS_TTL_SEC = 30 * 60
#: 扫描时间预算：不能卡住收消息热路径
SCAN_TIME_BUDGET_MS = 2500
#: 工具路径：拿多久以内的「最近一次交互」当作发送目标
TOOL_TARGET_TTL_SEC = 300
#: 「这一轮已经放过歌」的判定窗口（工具路径用）
TURN_PLAY_TTL_SEC = 600

try:  # 权限常量在 neobot_app.commands.model；取不到就按字面值 1（次级管理员）
    from neobot_app.commands.model import PERM_SUB_ADMIN as _PERM_SUB_ADMIN
except Exception:  # pragma: no cover - 只在非 NeoBot 环境触发
    _PERM_SUB_ADMIN = 1

try:  # 会话引用：框架的 frozen dataclass
    from neobot_contracts.models import ConversationRef as _ConversationRef
except Exception:  # pragma: no cover

    @dataclass(frozen=True)
    class _ConversationRef:  # type: ignore[no-redef]
        kind: str
        id: str


def _now() -> float:
    """统一用单调时钟：限流/待选/豁免都只关心时长，不关心墙上时间。"""
    return time.monotonic()


# ───────────────────────────────────────────── 统计与下载闸


@dataclass
class Stats:
    scanned: int = 0
    hits: int = 0
    sent: int = 0
    misses: int = 0
    skipped: int = 0
    errors: int = 0
    downloads: int = 0
    download_fails: int = 0
    cache_hits: int = 0
    asked: int = 0
    unlocked: int = 0
    unlock_denied: int = 0
    deduped: int = 0
    last_song: str = ""
    last_at: float = 0.0
    last_note: str = ""
    last_download: str = ""
    last_unlock: str = ""
    last_dedupe: str = ""


class DownloadGate:
    """下载闸：同一首歌冷却 + 每小时下载总量。

    为什么单独一套（不复用发送闸）：发送闸管的是「发消息频率」，
    这里管的是「往外网要文件的频率」—— 一个群反复点同一首歌时，
    真正该拦的是重复下载（缓存会命中，但第一次之外的失败重试会打爆对方接口）。
    """

    def __init__(self) -> None:
        self._last_by_song: Dict[str, float] = {}
        self._sent_at: List[float] = []

    def check(self, cfg: VoiceSongConfig, song: str) -> Dict[str, Any]:
        current = _now()
        self._sent_at = [at for at in self._sent_at if at > current - 3600]
        limit = int(cfg.download_per_hour or 0)
        if limit > 0 and len(self._sent_at) >= limit:
            return {"ok": False, "reason": "本小时下载已达上限（{} 首）".format(limit)}
        cooldown_min = int(cfg.download_cooldown_min or 0)
        if cooldown_min > 0:
            key = logic.normalize_name(song)
            last = self._last_by_song.get(key)
            if last is not None and current - last < cooldown_min * 60:
                left = math.ceil((cooldown_min * 60 - (current - last)) / 60)
                return {"ok": False, "reason": "这首歌刚下过（{} 分钟后可重试）".format(left)}
        return {"ok": True}

    def commit(self, cfg: VoiceSongConfig, song: str) -> None:
        current = _now()
        self._sent_at.append(current)
        if len(self._last_by_song) > 500:
            cutoff = current - 86400
            for key in [key for key, at in self._last_by_song.items() if at < cutoff]:
                self._last_by_song.pop(key, None)
        if int(cfg.download_cooldown_min or 0) > 0:
            self._last_by_song[logic.normalize_name(song)] = current

    def reset(self, scope: str = "chat") -> None:
        if scope == "all":
            self._last_by_song.clear()
            self._sent_at = []
        elif scope == "chat":
            self._sent_at = []


# ───────────────────────────────────────────── 引擎（运行时状态）


@dataclass
class Engine:
    """一次插件加载期内的全部运行时状态。重载即重建（换新模块命名空间）。"""

    ctx: Any
    config: VoiceSongConfig
    logger: Any
    data_dir: Path
    gate: logic.RateGate = field(default_factory=logic.RateGate)
    pending: logic.PendingChoices = field(default_factory=logic.PendingChoices)
    exempt: logic.Exemptions = field(default_factory=logic.Exemptions)
    index_cache: logic.TtlCache = field(
        default_factory=lambda: logic.TtlCache(ttl_sec=INDEX_TTL_SEC, max_size=4)
    )
    tags_cache: logic.TtlCache = field(
        default_factory=lambda: logic.TtlCache(ttl_sec=TAGS_TTL_SEC, max_size=4000)
    )
    download_gate: DownloadGate = field(default_factory=DownloadGate)
    stats: Stats = field(default_factory=Stats)
    #: 会话级串行锁：同群两首点歌不会叠在一起发
    locks: Dict[str, asyncio.Lock] = field(default_factory=dict)
    #: 后台任务的强引用（丢弃返回值可能被 GC 回收）
    tasks: Set[Any] = field(default_factory=set)
    #: sender_id -> (at, kind, conv_id, sender_id)：工具路径靠它认出该发到哪
    last_seen: Dict[str, Tuple[float, str, str, str]] = field(default_factory=dict)
    latest_seen: Optional[Tuple[float, str, str, str]] = None
    #: chat_key -> 最后一次说话时间：工具要判断「最近是不是只有一个会话在聊」
    recent_chats: Dict[str, float] = field(default_factory=dict)
    #: chat_key -> 本轮是否已经放过歌（挡模型一轮连放）
    turn_plays: Dict[str, float] = field(default_factory=dict)


#: 当前加载代的引擎。插件重载会换一份新的模块命名空间，因此这里是「本代」的状态。
_engine: Optional[Engine] = None
#: rule 与处理器之间传递「这条消息该怎么接管」（一次性，处理器取走即删）
_decisions: Dict[str, str] = {}


def _ensure_engine(ctx: Any, config: Any, logger: Any) -> Engine:
    global _engine
    if _engine is None:
        if config is None:  # 理论上不会发生（插件声明了 config 模型），但不该因此崩掉
            config = VoiceSongConfig()
        _engine = Engine(
            ctx=ctx,
            config=config,
            logger=logger,
            data_dir=Path(getattr(ctx, "data_dir", Path("."))),
            pending=logic.PendingChoices(
                ttl_sec=lambda: max(5, int(_engine.config.choice_ttl_sec or 180)),
                grace_sec=lambda: min(
                    120, max(15, max(5, int(_engine.config.choice_ttl_sec or 180)) / 3)
                ),
            ),
        )
    return _engine


# ───────────────────────────────────────────── 小工具


def _sender_id(event: Dict[str, Any]) -> str:
    value = event.get("user_id")
    return "" if value is None else str(value)


def _conversation_key(event: Dict[str, Any]) -> str:
    """会话键：群 → ``group:<群号>``，私聊 → ``private:<QQ号>``。

    比原版的 ``group:`` / ``friend:`` 命名更贴近 NeoBot 的 message_type，
    但仍然是一个字符串键，待选表/限流闸都只把它当 key 用。
    """
    if str(event.get("message_type") or "") == "private" or not event.get("group_id"):
        return "private:{}".format(_sender_id(event))
    return "group:{}".format(event.get("group_id"))


def _event_key(event: Dict[str, Any]) -> str:
    """事件的一次性键：rule 里算出的决定，交给同一事件的处理器，避免算两遍。"""
    message_id = event.get("message_id")
    return "{}:{}".format(_conversation_key(event), message_id if message_id is not None else id(event))


def _is_self_event(event: Dict[str, Any]) -> bool:
    """这条消息是不是机器人自己发出来的？

    有些协议端会把上行消息也回显一遍（原版用 ``entry.self`` 挡的就是这个）。
    不挡的话，机器人可能对着自己发的歌名再点一次歌。
    """
    self_id = event.get("self_id")
    if self_id is None:
        return False
    return str(event.get("user_id") or "") == str(self_id)


def _download_dir(engine: Engine) -> Path:
    configured = str(engine.config.download_dir or "").strip()
    if configured:
        return Path(configured).expanduser()
    return netease.default_download_dir(engine.data_dir)


def _tmp_dir(engine: Engine) -> Path:
    return engine.data_dir / "tmp" / "songs"


def _scan_roots(engine: Engine) -> List[Path]:
    cfg = engine.config
    return library.scan_roots(
        search_directories=cfg.search_dir_list,
        download_dir=_download_dir(engine) if cfg.enable_download else None,
        default_scan_dirs=cfg.default_scan_dirs,
    )


def _sweep_tmp(engine: Engine, force: bool = False) -> int:
    """清掉过期临时文件。协议端可能还在读，所以保留 15 分钟。"""
    directory = _tmp_dir(engine)
    try:
        if not directory.is_dir():
            return 0
        cutoff = 0.0 if force else time.time() - TMP_KEEP_SEC
        removed = 0
        for entry in directory.iterdir():
            try:
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    entry.unlink()
                    removed += 1
            except OSError:
                continue  # 被占用就下次再清
        return removed
    except OSError:
        return 0


def _work_file(engine: Engine, source: str) -> Path:
    directory = _tmp_dir(engine)
    directory.mkdir(parents=True, exist_ok=True)
    suffix = Path(source).suffix or ".mp3"
    stem = "song-{}-{}".format(int(time.time() * 1000), secrets.token_hex(3))
    return directory / (stem + suffix)


def _copy_to_tmp(engine: Engine, source: str) -> Path:
    """把要发的歌拷一份到临时目录。

    为什么拷而不是直接发原文件：用户可能在发送过程中移动/删除文件；
    而且拷贝后我们能在自己的目录里按 15 分钟窗口清理，不碰用户的音乐库。
    """
    target = _work_file(engine, source)
    shutil.copyfile(source, target)
    return target


def _record_error(engine: Engine, message: str) -> None:
    engine.stats.errors += 1
    engine.stats.last_note = logic.cut(message, 200)


def _log(engine: Engine, message: str) -> None:
    try:
        engine.logger.info(message)
    except Exception:  # 日志失败不影响功能
        pass


def _warn(engine: Engine, message: str) -> None:
    try:
        engine.logger.warning(message)
    except Exception:
        pass


# ───────────────────────────────────────────── 权限 / 闸


def _is_admin(engine: Engine, sender_id: str) -> bool:
    """判断一个人是不是管理员。两条通道，任一条通就算：

      ① 本插件设置里手填的「机主/管理员 QQ 号」（不依赖任何其它配置，最可靠）
      ② NeoBot 自己的权限体系（超级管理员 / 次级管理员）

    ⚠️ 为什么必须做①：权限服务是**软依赖** —— 拿不到时不能静默返回 True，
       否则「管理员能解除限制」在别人机器上会悄悄变成「谁都能解除」。
    失败方向也是刻意的：两条都拿不到结论 → **判为不是管理员**（失败关闭）。
    """
    cfg = engine.config
    if not sender_id:
        return False
    if sender_id in cfg.admin_qq_list:
        return True
    if not cfg.trust_admin_accounts:
        return False
    try:
        host = getattr(engine.ctx, "plugin_host", None)
        services = getattr(host, "services", None)
        service = services.get("command_service") if services is not None else None
        permissions = getattr(service, "permissions", None)
        can = getattr(permissions, "can", None)
        if callable(can):
            return bool(can(sender_id, _PERM_SUB_ADMIN))
    except Exception as exc:
        _warn(engine, "查 NeoBot 管理员权限失败（按不是管理员处理）：{}".format(exc))
    return False


def _check_admin(engine: Engine, sender_id: str) -> Dict[str, Any]:
    if not sender_id:
        return {"ok": False, "who": "", "reason": "拿不到发送者 QQ 号"}
    if sender_id in engine.config.admin_qq_list:
        return {"ok": True, "who": "机主名单", "reason": ""}
    if _is_admin(engine, sender_id):
        return {"ok": True, "who": "NeoBot 管理员", "reason": ""}
    return {"ok": False, "who": "", "reason": "既不在本插件的机主名单里，也不是 NeoBot 管理员"}


def _allow_send(engine: Engine, chat_key: str, sender_id: str, song: str = "") -> Dict[str, Any]:
    """统一的「能不能放这一首」检查：**先看豁免，再看闸**。

    把两步收敛成一个函数，是为了避免以后新增闸时漏掉豁免判定。
    """
    exemption = engine.exempt.check(chat_key, sender_id)
    if exemption.get("exempt"):
        return {
            "ok": True,
            "exempt": True,
            "left_sec": exemption["until"] - _now(),
        }
    return engine.gate.check(chat_key, sender_id, engine.config)


def _allow_download(engine: Engine, chat_key: str, sender_id: str, song: str) -> Dict[str, Any]:
    exemption = engine.exempt.check(chat_key, sender_id)
    if exemption.get("exempt"):
        return {"ok": True, "exempt": True}
    return engine.download_gate.check(engine.config, song)


# ───────────────────────────────────────────── 发送


async def _send_voice(engine: Engine, ctx: Any, conversation: Any, path: Path) -> bool:
    """发一条语音。

    只用框架的 ``ctx.send_audio``：它内部走 ``prepare_audio_segment``，
    FileServer 开着就给 HTTP URL、关着就给 ``file:///`` 本地路径，
    然后由适配器发 ``{"type": "record", "data": {"file": ...}}``。

    ⚠️ 不做「失败后改用 record 段再发一遍」的兜底：adapter 的 echo 超时/断连
       在某些实现里会以异常形式冒出来，兜底会在语音其实已经发出去之后再发一次
       —— 群里看到两条一模一样的语音，比一条都没有更让人困惑。发不出去就记日志、
       算这次失败（``reply_on_failure`` 打开时会回一句）。
    """
    try:
        await ctx.send_audio(conversation, path=Path(path))
        return True
    except Exception as exc:
        _record_error(engine, "语音条发送失败：{}".format(exc))
        _warn(engine, "语音条发送失败：{}".format(exc))
        return False


async def _send_voice_and_title(
    engine: Engine,
    ctx: Any,
    conversation: Any,
    *,
    file_path: Path,
    title: str,
    artist: str,
) -> bool:
    if not await _send_voice(engine, ctx, conversation, file_path):
        return False
    if not engine.config.reply_title:
        return True
    await asyncio.sleep(0.9)  # 和语音条错开，别挤成一个气泡
    line = "🎵 《{}》 — {}".format(title, artist) if artist else "🎵 《{}》".format(title)
    try:
        await ctx.send(conversation, line)
    except Exception as exc:
        # 报歌名失败不该算整单失败：语音条已经出去了
        _warn(engine, "歌名没发出去（语音条已发送）：{}".format(exc))
    return True


async def _reply_quiet(engine: Engine, ctx: Any, conversation: Any, text: str) -> None:
    try:
        await ctx.send(conversation, text)
    except Exception as exc:
        _warn(engine, "提示语没发出去：{}".format(exc))


# ───────────────────────────────────────────── 曲库与缓存


async def _load_library(engine: Engine, roots: List[Path]) -> List[Dict[str, Any]]:
    """扫描曲库。放到线程里跑：几千个 stat + 读标签不能占住事件循环。"""
    if not roots:
        return []
    cfg = engine.config
    try:
        return await asyncio.to_thread(
            library.load_library,
            roots,
            max_depth=int(cfg.scan_depth or 3),
            max_files=int(cfg.max_files or 4000),
            time_budget_ms=SCAN_TIME_BUDGET_MS,
            cache=engine.index_cache,
        )
    except Exception as exc:
        _warn(engine, "扫描曲库失败（按空曲库处理）：{}".format(exc))
        return []


async def _tag_files(engine: Engine, files: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """补 ID3 标签（线程里跑，带时间预算）。

    曲库扫描已经在 ``_load_library`` 里进了线程，但「读标签」是另一件重活：
    每个没缓存的文件都要 open + 读最多 1MiB。几十个群同时在聊的时候，
    把这件事留在事件循环上 = 整条事件流卡住几秒。
    """
    if not files:
        return []
    try:
        return await asyncio.to_thread(library.with_tags, files, engine.tags_cache)
    except Exception as exc:
        _warn(engine, "读取音频标签失败（只用文件名匹配）：{}".format(exc))
        return [dict(item, title="", artist="") for item in files]


async def _safe_download(
    cfg: VoiceSongConfig,
    song: str,
    directory: Path,
    **kwargs: Any,
) -> Dict[str, Any]:
    """``download.download_song`` 的包装：任何异常都变成一次「下载失败」。

    原版三处调用都是 ``try { downloadSong() } catch { {ok:false, error:'下载异常…'} }``。
    不包的话异常会逃出后台任务，变成一条「点歌任务异常」——
    统计里看不到 download_fails，用户也可能什么都收不到。
    """
    try:
        return await download.download_song(cfg, song, directory, **kwargs)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return {"ok": False, "error": "下载异常：{}".format(exc)}


async def _cleanup_cache(engine: Engine, reason: str = "") -> None:
    """清理下载缓存里的重复内容，并让曲库索引立刻反映结果。

    什么时候调：启动时一次、每次下载成功后一次。
    为什么：同一首歌会被反复下载成多个版本（换音质、换写法、模型乱试），
    时间一长缓存目录里就会堆内容相同的副本 —— 既占地方，
    又让「选歌列表」里出现一堆看不出区别的条目。

    ⚠️ 只清下载缓存目录（插件自己写的地方），永远不碰用户的音乐库。
    """
    try:
        cfg = engine.config
        if not cfg.enable_download:
            return
        directory = _download_dir(engine)
        result = await asyncio.to_thread(netease.dedupe_cache, directory)
        removed = result.get("removed") or []
        if removed:
            engine.stats.deduped += len(removed)
            freed = result.get("freed_bytes", 0) / 1048576.0
            names = "、".join(item["name"] for item in removed)[:120]
            engine.stats.last_dedupe = "清理 {} 个重复（释放 {:.1f}MB）{}：{}".format(
                len(removed), freed, " · " + reason if reason else "", names
            )
            _log(
                engine,
                "缓存去重：清掉 {} 个重复文件，释放 {:.1f}MB（{}）{}".format(
                    len(removed), freed, names, " [{}]".format(reason) if reason else ""
                ),
            )
            # 文件变了，曲库索引必须重建，否则列表里还在推荐已经被删掉的文件
            engine.index_cache.clear()
    except Exception as exc:
        _warn(engine, "缓存去重失败（不影响点歌）：{}".format(exc))


# ───────────────────────────────────────────── 同名多首：问、答、然后发


async def _ask_for_choice(
    engine: Engine,
    ctx: Any,
    conversation: Any,
    *,
    chat_key: str,
    sender_id: str,
    song: str,
    choices: List[Dict[str, Any]],
) -> None:
    """把候选列表发出去，并记住「这个人在等选择」。"""
    cfg = engine.config
    text = logic.format_choice_list(song, choices, max_items=int(cfg.max_choices or 8))
    try:
        engine.pending.put(
            chat_key,
            sender_id,
            {"song": song, "choices": choices, "at": _now(), "ttl_sec": int(cfg.choice_ttl_sec or 180)},
        )
        await _reply_quiet(engine, ctx, conversation, text)
        engine.stats.asked += 1
        engine.stats.last_note = "等选择：{}（{} 首）".format(song, len(choices))
        _log(engine, "{} 「{}」有 {} 个候选，已发出选择列表等回复".format(chat_key, song, len(choices)))
    except Exception as exc:
        _record_error(engine, "发送选择列表失败：{}".format(exc))
        _warn(engine, "{} 发送选择列表失败：{}".format(chat_key, exc))


# ───────────────────────────────────────────── 主流程：点歌


async def _handle_song(
    engine: Engine, ctx: Any, event: Dict[str, Any], conversation: Any, chat_key: str
) -> None:
    """确定性点歌路径：解析 → 找歌 / 下载 → 发语音条。"""
    cfg = engine.config
    sender_id = _sender_id(event)
    parsed = logic.parse_song_request(
        Message(event).text,
        keywords=cfg.trigger_list,
        self_names=cfg.self_name_list,
    )
    engine.stats.scanned += 1
    if not parsed["hit"]:
        return
    song = parsed["song"]
    if not song:
        engine.stats.skipped += 1
        engine.stats.last_note = "只说了触发词、没给歌名"
        _log(engine, "{} 只说了「{}」没给歌名，忽略".format(chat_key, parsed["matched"]))
        return

    gate = _allow_send(engine, chat_key, sender_id, song)
    if not gate["ok"]:
        engine.stats.skipped += 1
        engine.stats.last_note = gate["reason"]
        _log(engine, "{} 跳过点歌「{}」：{}".format(chat_key, song, gate["reason"]))
        return

    roots = _scan_roots(engine)
    files = await _load_library(engine, roots)
    # 补 ID3 标签必须进线程：每个没缓存的文件都要 open + 读最多 1MiB，
    # 几千个文件同步跑在事件循环上会把所有会话的消息一起卡住。
    tagged = await _tag_files(engine, files)
    # ⚠️ 开启「每次都先选」(always_ask) 时，**本地命中也必须走选择流程**：
    #    列表里会同时列出本地那首和网易云的各个版本，由点歌的人自己决定放哪个。
    #    所以这里先不挑本地命中。
    best = None
    if not (cfg.always_ask or not tagged):
        best = logic.pick_best(tagged, song, fuzzy=cfg.fuzzy)

    # ── 本地没有（或开启「每次都先选」）→ 去网易云找 ──
    if best is None:
        engine.stats.misses += 1
        engine.stats.last_note = (
            "曲库里没找到「{}」".format(song) if files else "音乐目录里没有音频文件"
        )
        if cfg.always_ask:
            _log(engine, "{} 按设置先列候选（本地 {} 首 + 网易云）".format(chat_key, len(files)))
        elif files:
            _log(engine, "{} 曲库 {} 首里没找到「{}」，转去网易云找".format(chat_key, len(files), song))
        else:
            _log(engine, "{} 曲库为空，转去网易云找「{}」".format(chat_key, song))

        if not cfg.enable_download and not cfg.always_ask:
            # 关掉下载时保持原来的行为：安静 + 可选提示
            if cfg.reply_on_failure:
                has_ncm = False
                if roots:
                    try:
                        has_ncm = await asyncio.to_thread(library.has_encrypted_ncm, roots)
                    except Exception:
                        has_ncm = False
                await _reply_quiet(
                    engine,
                    ctx,
                    conversation,
                    (
                        "找到的都是 .ncm 加密文件，发不了语音条；自动下载又是关着的，"
                        "去设置里打开「本地没有就自动下载」吧。"
                        if has_ncm
                        else "曲库里没找到「{}」（自动下载是关着的）。".format(song)
                    ),
                )
            return

        # ⚠️ 下载闸只在一件事上生效：「这次是不是真的要去下文件」。
        #    always_ask 打开时这里只是列候选（不下载），真正的下载发生在用户
        #    回数字之后 —— 那道闸在 _handle_choice 里还会再查一次。
        #    原版在列候选之前就查了下载闸，结果是「同一首歌 10 分钟内第二次点歌
        #    被静默丢掉」：用户明明只是想在缓存里再放一次，却什么都没发生。
        if not cfg.always_ask:
            download_gate = _allow_download(engine, chat_key, sender_id, song)
            if not download_gate["ok"]:
                engine.stats.skipped += 1
                engine.stats.last_note = download_gate["reason"]
                _log(engine, "{} 不下载「{}」：{}".format(chat_key, song, download_gate["reason"]))
                return

        # 本地候选：曲库 + 下载缓存。只在「必须先选」时需要 ——
        # 否则本地命中早就直接放出去了。
        local_candidates = (
            download.collect_local_candidates(cfg, song, tagged) if cfg.always_ask else []
        )

        result = await _safe_download(
            cfg,
            song,
            _download_dir(engine),
            choices=local_candidates or None,
            log=lambda message: _log(engine, message),
        )

        # ── 同名多首：把候选列表发出去，等他回数字（不替他猜）──
        if result.get("ok") and result.get("need_choice"):
            await _ask_for_choice(
                engine,
                ctx,
                conversation,
                chat_key=chat_key,
                sender_id=sender_id,
                song=song,
                choices=result.get("choices") or [],
            )
            return

        if not result.get("ok") or not result.get("path"):
            engine.stats.download_fails += 1
            engine.stats.last_note = "自动下载失败：{}".format(result.get("error"))
            _record_error(engine, "自动下载失败：{}".format(result.get("error")))
            _log(engine, "{} 自动下载「{}」失败：{}".format(chat_key, song, result.get("error")))
            if cfg.reply_on_failure:
                await _reply_quiet(
                    engine,
                    ctx,
                    conversation,
                    "这首歌本地没有，去网易云下也没成功：{}".format(logic.cut(result.get("error"), 80)),
                )
            return

        if result.get("from_cache"):
            engine.stats.cache_hits += 1
        else:
            engine.download_gate.commit(cfg, song)
            engine.stats.downloads += 1
        engine.stats.last_download = "{}{}".format(
            result.get("title") or "", " — " + result["artist"] if result.get("artist") else ""
        )
        # 下完就清一次重复：同一首歌可能被反复下成多个副本（换音质/换写法/模型重试）
        await _cleanup_cache(engine, "下载后")
        engine.index_cache.clear()

        best = {
            "candidate": {
                "path": result["path"],
                "name": Path(result["path"]).name,
                "title": result.get("title") or "",
                "artist": result.get("artist") or "",
                "mtime_ms": time.time() * 1000.0,
                "size": int(result.get("bytes") or 0),
                "ext": Path(result["path"]).suffix.lstrip(".").lower(),
            },
            "score": 1000,
        }
        _log(
            engine,
            "{} 已就绪：《{}》{}{}".format(
                chat_key,
                result.get("title") or "",
                " — " + result["artist"] if result.get("artist") else "",
                "（缓存命中）"
                if result.get("from_cache")
                else "（刚下载 {:.2f}MB）".format(int(result.get("bytes") or 0) / 1048576.0),
            ),
        )

    hit = best["candidate"]
    max_bytes = float(cfg.max_file_mb or 20) * 1024 * 1024
    if float(hit.get("size") or 0) > max_bytes:
        engine.stats.skipped += 1
        size_mb = round(float(hit.get("size") or 0) / 1048576.0)
        engine.stats.last_note = "文件太大（{}MB）".format(size_mb)
        _log(engine, "{} 找到「{}」但超过 {}MB，跳过".format(chat_key, hit.get("name"), cfg.max_file_mb))
        if cfg.reply_on_failure:
            await _reply_quiet(
                engine,
                ctx,
                conversation,
                "找到了这首，但文件有 {}MB 太大，发不出来。".format(size_mb),
            )
        return

    title = logic.strip_ext(hit.get("title") or hit.get("name") or "") or song
    artist = hit.get("artist") or ""
    # ⚠️ 顺序很重要：**先把本地准备做完（拷出临时副本），再发**。
    #    如果拷文件这一步就失败（磁盘满/权限/源文件被删），我们应该什么都没发出去，
    #    而不是让这条点歌静默消失。
    try:
        _sweep_tmp(engine)
        tmp_path = await asyncio.to_thread(_copy_to_tmp, engine, str(hit["path"]))
    except Exception as exc:
        _record_error(engine, "准备临时文件失败：{}".format(exc))
        _warn(engine, "{} 准备「{}」的临时文件失败：{}".format(chat_key, title, exc))
        if cfg.reply_on_failure:
            await _reply_quiet(engine, ctx, conversation, "这首没准备好（本地文件读不出来），我记日志了。")
        return

    if await _send_voice_and_title(
        engine, ctx, conversation, file_path=tmp_path, title=title, artist=artist
    ):
        engine.gate.commit(chat_key, sender_id)
        engine.stats.sent += 1
        engine.stats.hits += 1
        engine.stats.last_song = title
        engine.stats.last_at = _now()
        engine.stats.last_note = ""
        _log(
            engine,
            "{} 已发送语音条：《{}》{}（来自 {}）".format(
                chat_key, title, " — " + artist if artist else "", hit.get("name")
            ),
        )
        # 与工具路径共用「本轮已放」标记：模型若在同一轮里又调工具，会被挡住
        engine.turn_plays[chat_key] = _now()
    elif cfg.reply_on_failure:
        await _reply_quiet(engine, ctx, conversation, "这首没发出去（语音发送失败），我记日志了。")


# ───────────────────────────────────────────── 主流程：选择回复


async def _handle_choice(
    engine: Engine, ctx: Any, event: Dict[str, Any], conversation: Any, chat_key: str
) -> None:
    sender_id = _sender_id(event)
    pending = engine.pending.peek(chat_key, sender_id)
    if not pending:
        return
    reply = logic.parse_choice_reply(Message(event).text, pending.get("choices"))
    if reply is None:
        return

    # 取一次就作废：避免同一个人回第二次数字被反复消费
    engine.pending.take(chat_key, sender_id)
    choices = pending.get("choices") or []
    index = int(reply["index"])
    if index < 0 or index >= len(choices):
        return
    chosen = choices[index]
    cfg = engine.config

    # 「下载一份新的（不用本地这份）」→ 把网易云那些版本重新列一次给他挑。
    # 这一步不下载任何东西：先让他选具体版本，避免「以为在下同一首，结果下成别的」。
    if str(chosen.get("id")) == logic.DOWNLOAD_NEW_ID or chosen.get("source") == "action":
        remote = [item for item in choices if item.get("source") == "netease"]
        if not remote:
            await _reply_quiet(
                engine, ctx, conversation, "网易云那边这次没搜到别的版本，就用本地那份吧 —— 回数字重新选。"
            )
            return
        await _ask_for_choice(
            engine,
            ctx,
            conversation,
            chat_key=chat_key,
            sender_id=sender_id,
            song=pending.get("song") or "",
            choices=remote,
        )
        _log(engine, "{} 用户选了「下载一份新的」，改列网易云的 {} 个版本".format(chat_key, len(remote)))
        return

    _log(
        engine,
        "{} 用户选择了第 {} 首：《{}》{}".format(
            chat_key,
            index + 1,
            chosen.get("name") or "",
            " — " + chosen["artist"] if chosen.get("artist") else "",
        ),
    )

    # 发送闸（放歌这一步照旧受限流约束；管理员解除限制后豁免）
    gate = _allow_send(engine, chat_key, sender_id, chosen.get("name") or "")
    if not gate["ok"]:
        engine.stats.skipped += 1
        engine.stats.last_note = gate["reason"]
        _log(engine, "{} 选择被发送闸拦下：{}".format(chat_key, gate["reason"]))
        if cfg.reply_on_failure:
            await _reply_quiet(engine, ctx, conversation, "这次没发：{}。".format(gate["reason"]))
        return

    # 选中的是**本地/缓存文件**（候选来自缓存目录，没有网易云 id）→ 直接发这个文件，不用下载
    local_file = chosen.get("file_path") or ""
    if not local_file and str(chosen.get("id") or "").startswith("file:"):
        local_file = str(chosen["id"])[5:]
    if local_file:
        try:
            _sweep_tmp(engine)
            tmp_path = await asyncio.to_thread(_copy_to_tmp, engine, local_file)
        except Exception as exc:
            _record_error(engine, "发送所选文件失败：{}".format(exc))
            _warn(engine, "{} 发送所选文件失败：{}".format(chat_key, exc))
            if cfg.reply_on_failure:
                await _reply_quiet(engine, ctx, conversation, "这首没发出去（语音发送失败），我记日志了。")
            return
        title = logic.strip_ext(chosen.get("name") or "") or pending.get("song") or ""
        if await _send_voice_and_title(
            engine,
            ctx,
            conversation,
            file_path=tmp_path,
            title=title,
            artist=chosen.get("artist") or "",
        ):
            engine.gate.commit(chat_key, sender_id)
            engine.stats.sent += 1
            engine.stats.hits += 1
            engine.stats.cache_hits += 1
            engine.stats.last_song = title
            engine.stats.last_at = _now()
            engine.stats.last_note = ""
            _log(engine, "{} 已按选择发送本地文件：《{}》".format(chat_key, title))
        elif cfg.reply_on_failure:
            await _reply_quiet(engine, ctx, conversation, "这首没发出去（语音发送失败），我记日志了。")
        return

    download_gate = _allow_download(engine, chat_key, sender_id, chosen.get("name") or "")
    if not download_gate["ok"]:
        engine.stats.skipped += 1
        engine.stats.last_note = download_gate["reason"]
        _log(engine, "{} 选择被下载闸拦下：{}".format(chat_key, download_gate["reason"]))
        if cfg.reply_on_failure:
            await _reply_quiet(engine, ctx, conversation, "这次没下：{}。".format(download_gate["reason"]))
        return

    result = await _safe_download(
        cfg,
        pending.get("song") or "",
        _download_dir(engine),
        pick=str(chosen.get("id") or ""),
        choices=choices,
        log=lambda message: _log(engine, message),
    )
    if not result.get("ok") or not result.get("path"):
        engine.stats.download_fails += 1
        _record_error(engine, "选的歌下载失败：{}".format(result.get("error")))
        _warn(engine, "{} 下载所选的《{}》失败：{}".format(chat_key, chosen.get("name"), result.get("error")))
        if cfg.reply_on_failure:
            await _reply_quiet(
                engine, ctx, conversation, "这首没下成：{}".format(logic.cut(result.get("error"), 100))
            )
        return

    engine.download_gate.commit(cfg, chosen.get("name") or "")
    engine.stats.downloads += 1
    engine.stats.last_download = "{}{}".format(
        chosen.get("name") or "", " — " + chosen["artist"] if chosen.get("artist") else ""
    )
    # 下完就清一次重复：同一首歌可能被反复下成多个副本（换音质/换写法/模型重试）
    await _cleanup_cache(engine, "下载后")
    engine.index_cache.clear()

    try:
        _sweep_tmp(engine)
        tmp_path = await asyncio.to_thread(_copy_to_tmp, engine, str(result["path"]))
    except Exception as exc:
        _record_error(engine, "发送所选的歌失败：{}".format(exc))
        _warn(engine, "{} 发送《{}》失败：{}".format(chat_key, chosen.get("name"), exc))
        if cfg.reply_on_failure:
            await _reply_quiet(engine, ctx, conversation, "这首没发出去（语音发送失败），我记日志了。")
        return

    title = logic.strip_ext(result.get("title") or chosen.get("name") or "") or (pending.get("song") or "")
    if await _send_voice_and_title(
        engine, ctx, conversation, file_path=tmp_path, title=title, artist=chosen.get("artist") or ""
    ):
        engine.gate.commit(chat_key, sender_id)
        engine.stats.sent += 1
        engine.stats.hits += 1
        engine.stats.last_song = title
        engine.stats.last_at = _now()
        engine.stats.last_note = ""
        _log(
            engine,
            "{} 已按选择发送语音条：《{}》{}（{}）".format(
                chat_key,
                title,
                " — " + chosen["artist"] if chosen.get("artist") else "",
                "缓存命中"
                if result.get("from_cache")
                else "刚下载 {:.2f}MB".format(int(result.get("bytes") or 0) / 1048576.0),
            ),
        )
    elif cfg.reply_on_failure:
        await _reply_quiet(engine, ctx, conversation, "这首没发出去（语音发送失败），我记日志了。")


# ───────────────────────────────────────────── 主流程：管理员解除限制


async def _handle_unlock(
    engine: Engine, ctx: Any, event: Dict[str, Any], conversation: Any, chat_key: str
) -> None:
    """处理「解除点歌限制」。三种范围 × 可选时长：

      解除点歌限制            → 清本会话记录
      解除点歌限制 本群        → 同上（说清楚一点而已）
      解除点歌限制 我          → 清自己
      解除点歌限制 <QQ号>      → 清那个人
      解除点歌限制 10分钟      → 清记录 + 10 分钟内不再限制
    """
    cfg = engine.config
    command = logic.parse_unlock_command(
        Message(event).text, keywords=cfg.unlock_keyword_list
    )
    if not command["hit"]:
        return
    sender_id = _sender_id(event)
    who = _check_admin(engine, sender_id)
    if not who["ok"]:
        engine.stats.unlock_denied += 1
        _log(
            engine,
            "{} 拒绝解除限制：{}（发起人 {}）".format(chat_key, who.get("reason") or "无权限", sender_id),
        )
        # ⚠️ 刻意**不**安静吞掉：非管理员说这句话多半是在抱怨，
        #    该由模型正常接话，所以我们根本不该接管（见 _decide）。
        if cfg.reply_on_failure:
            await _reply_quiet(engine, ctx, conversation, "这条指令要管理员才能用。")
        return

    scope = command["scope"]
    target_id = command["target_id"] if scope == "user" and command["target_id"] else sender_id

    # ① 清记录
    reset_scope = "all" if scope == "all" else ("user" if scope in ("self", "user") else "chat")
    wiped = engine.gate.reset(chat_key, target_id, reset_scope)
    # 下载闸也一起放行：否则「解除了限制」却仍被「这首歌刚下过」拦住，用户会觉得没生效
    if scope in ("all", "chat"):
        engine.download_gate.reset(scope)

    # ② 可选：开一段时间的「不限制」
    #    scope=chat 时豁免整个会话；scope=self/user 时只豁免当事人。
    #    指令里没写时长就用设置里的「默认放开几分钟」（0 = 不开豁免，只清记录）。
    grant_sec = int(command["unlimited_ms"] or 0) / 1000.0
    if grant_sec <= 0:
        grant_sec = max(0, int(cfg.unlock_default_min or 0)) * 60.0
    if grant_sec > 0:
        engine.exempt.grant(chat_key, target_id, scope, grant_sec)

    grant_min = round(grant_sec / 60)
    engine.stats.unlocked += 1
    engine.stats.last_unlock = "{} 于 {}：{}{}".format(
        who["who"],
        time.strftime("%H:%M:%S"),
        scope,
        " + {} 分钟不限制".format(grant_min) if grant_min > 0 else "",
    )
    _log(
        engine,
        "{} 管理员（{}）解除点歌限制：范围={}{}；清掉 会话记录 {} 条 / 用户记录 {} 条 / 配额 {} 条".format(
            chat_key,
            who["who"],
            scope,
            "，{} 分钟内不再限制".format(grant_min) if grant_min > 0 else "",
            wiped["chat"],
            wiped["user"],
            wiped["all"],
        ),
    )

    scope_text = {
        "all": "所有会话",
        "self": "你自己",
        "user": str(target_id),
    }.get(scope, "本会话")
    lines = ["已解除点歌限制（{}）：冷却与配额记录已清空。".format(scope_text)]
    if grant_min > 0:
        lines.append("{} 分钟内不再受防刷屏限制。".format(grant_min))
    else:
        lines.append("（想连限制也一起去掉，可以在指令后加时长，例如「解除点歌限制 10分钟」。）")
    await _reply_quiet(engine, ctx, conversation, "\n".join(lines))


# ───────────────────────────────────────────── 接管判定（同步、零 IO）


def _decide(event: Dict[str, Any]) -> str:
    """这条消息我们要不要接管？

    返回 ``"song"`` / ``"choice"`` / ``"unlock"`` / ``""``（不接管）。

    必须是**纯同步、零 IO** 的：它在每条消息的同步路径上被调用，
    一旦这里 await，就等于把决定权让回给宿主的回复管线。
    """
    engine = _engine
    if engine is None:
        return ""
    try:
        cfg = engine.config
        if not cfg.enabled:
            return ""
        if _is_self_event(event):
            return ""
        text = Message(event).text
        if not text.strip():
            return ""
        chat_key = _conversation_key(event)
        sender_id = _sender_id(event)

        # ① 管理员解除限制指令
        #    ⚠️ 必须在这里就确认**管理员身份**，不能只判断「像不像指令」：
        #       群友随口一句「限制好多，解除点歌限制吧」本来该由模型正常接话，
        #       接管了就再也没机会交回去。_is_admin 是同步的（不联网、不 await）。
        if cfg.unlock_enabled and logic.parse_unlock_command(
            text, keywords=cfg.unlock_keyword_list
        )["hit"]:
            return "unlock" if _is_admin(engine, sender_id) else ""

        # ② 在回复上一条选择列表（数字 / 歌手名）
        pending = engine.pending.peek(chat_key, sender_id)
        if pending is not None and logic.parse_choice_reply(text, pending.get("choices")) is not None:
            return "choice"

        # ③ 普通点歌
        parsed = logic.parse_song_request(
            text, keywords=cfg.trigger_list, self_names=cfg.self_name_list
        )
        if parsed["hit"] and parsed["song"]:
            return "song"
    except Exception:
        try:
            if engine is not None:
                engine.logger.exception("点歌接管判定失败（本次交回给模型）")
        except Exception:
            pass
    return ""


def _message_rule(event: Dict[str, Any]) -> bool:
    """``@plugin.message`` 的 rule：只在我们要接管时放行到处理器。

    返回 False 时这条消息**完全不受影响**（不 block、不标记已读），
    模型照常接话 —— 这正是原版「接管与否」语义在 NeoBot 里的落点。
    """
    kind = _decide(event)
    if not kind:
        return False
    if len(_decisions) > 1000:
        _decisions.clear()
    _decisions[_event_key(event)] = kind
    return True


# ───────────────────────────────────────────── 任务调度


def _on_task_done(engine: Engine, task: Any) -> None:
    engine.tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _record_error(engine, "点歌任务异常：{}".format(exc))
        try:
            engine.logger.exception("点歌任务异常: {}".format(exc))
        except Exception:
            pass


def _prune_state(engine: Engine) -> None:
    """剪掉只增不减的会话级状态（locks / turn_plays）。

    两者都是按 chat_key 记的，不剪的话机器人跑久了就是个缓慢泄漏。
    只丢「没被持有、也没人排队」的锁：正在发歌的会话绝对不能丢锁，
    否则同群两首歌会并发发出去。
    """
    current = _now()
    for key in [key for key, at in engine.turn_plays.items() if current - at > TURN_PLAY_TTL_SEC]:
        engine.turn_plays.pop(key, None)
    for key, lock in list(engine.locks.items()):
        if not lock.locked():
            engine.locks.pop(key, None)


def _dispatch(engine: Engine, kind: str, ctx: Any, event: Dict[str, Any]) -> None:
    """把真正的工作丢到后台任务里，**立刻把事件循环还给事件分发**。

    ⚠️ 为什么必须这样：OneBot 事件分发是 ``await dispatcher.publish(event)`` 的
       串行循环，在处理器里 await 一次下载会卡住所有会话的消息接收。
       原版（Node）也是这个思路：接管后异步执行，不同步阻塞宿主。
    """
    chat_key = _conversation_key(event)
    if len(engine.locks) > 200 or len(engine.turn_plays) > 200:
        _prune_state(engine)

    async def runner() -> None:
        # 会话引用在任务里算：万一事件形状异常，异常只会让这次任务失败，
        # 而不会在 handler 里抛出去 —— 那样 HookBus 会认为「没处理」，
        # block 静默失效、消息反而回到模型那边。
        try:
            conversation = ctx.conversation_from_event(event)
        except Exception as exc:
            _warn(engine, "无法从事件推断会话，本次不处理：{}".format(exc))
            return
        lock = engine.locks.get(chat_key)
        if lock is None:
            lock = asyncio.Lock()
            engine.locks[chat_key] = lock
        async with lock:
            if kind == "unlock":
                await _handle_unlock(engine, ctx, event, conversation, chat_key)
            elif kind == "choice":
                await _handle_choice(engine, ctx, event, conversation, chat_key)
            else:
                await _handle_song(engine, ctx, event, conversation, chat_key)

    task = asyncio.create_task(runner())
    engine.tasks.add(task)
    task.add_done_callback(lambda finished: _on_task_done(engine, finished))


# ───────────────────────────────────────────── Plugin 定义


plugin = Plugin(
    PLUGIN_NAME,
    version=PLUGIN_VERSION,
    description=(
        "点歌发语音条：群里说「点歌 歌名」，机器人先从本机音乐库找，"
        "本地没有就去网易云下载完整歌曲，再以 QQ 语音条发出来（0 token，不经过模型）。"
    ),
    usage="直接发「点歌 歌名」/「来一首 歌名」；管理员可发「解除点歌限制 [本群|我|QQ号] [10分钟]」",
    author="DSH（NeoBot 移植）",
    config=VoiceSongConfig,
    min_neobot_version="2.0.0",
)


# ───────────────────────────────────────────── 消息入口


@plugin.message(rule=_message_rule, block=True, priority=100)
async def _on_message(
    event: Dict[str, Any],
    ctx: Any,
    config: VoiceSongConfig,
) -> None:
    """命中点歌 / 选择 / 解除指令的入口。

    ``block=True``：HookBus 在本处理器返回后调 ``ctx.consume()``，
    gateway 直接 return —— 消息不进回复管线，不产生模型会话（0 token）。
    """
    engine = _ensure_engine(ctx, config, ctx.logger)
    kind = _decisions.pop(_event_key(event), "") or _decide(event)
    if not kind:
        return
    _dispatch(engine, kind, ctx, event)


@plugin.message(priority=-100)
async def _observe_message(event: Dict[str, Any]) -> None:
    """记下「谁刚刚在哪个会话说过话」。

    为什么需要它：NeoBot 的插件工具（function-calling）**没有入站事件**
    （``tools.py`` 里 event 恒为 ``{}``），所以工具无法知道该把语音条发到哪个会话。
    官方 minigame 的做法也是「用最近一次交互认人」，这里沿用：
    留下最近一次交互的会话与发送者，工具在 TTL 内按它投递。

    这个处理器**不 block**，绝不改变消息的去向。
    """
    engine = _engine
    if engine is None:
        return
    try:
        if _is_self_event(event):
            return
        key = _conversation_key(event)
        sender_id = _sender_id(event)
        if not sender_id:
            return
        kind, _, conversation_id = key.partition(":")
        now = _now()
        record = (now, kind, conversation_id, sender_id)
        engine.last_seen[sender_id] = record
        engine.latest_seen = record
        engine.recent_chats[key] = now
        # 简单的容量兜底
        if len(engine.last_seen) > 200:
            oldest = min(engine.last_seen.items(), key=lambda item: item[1][0], default=None)
            if oldest is not None:
                engine.last_seen.pop(oldest[0], None)
        if len(engine.recent_chats) > 200:
            for chat_key in [
                chat_key
                for chat_key, at in engine.recent_chats.items()
                if now - at > TOOL_TARGET_TTL_SEC
            ]:
                engine.recent_chats.pop(chat_key, None)
    except Exception:
        return


# ───────────────────────────────────────────── 工具（给模型用，可选）


@plugin.tool(
    "play_local_song",
    description=(
        "按歌名找一首歌，并以 **QQ 语音条**的形式发到当前会话。"
        "先在机器人本机的音乐库里找；本地没有时会自动去网易云下载（完整歌曲，"
        "需要本机网易云客户端的登录态）再发。\n"
        "\n"
        "【什么时候用】只在这两种情况："
        "① 对方在这条消息里**明确说出了想听的那首歌的歌名**"
        "（例如「来一首海阔天空」「放一下 One Last Kiss」「换 hop」）；"
        "② 对方紧接着纠正/更换歌曲（「不对，我要的是 XX 版」）——这时把新版歌名传进来。\n"
        "\n"
        "【什么时候绝对不要用】"
        "· 对方只是在**聊音乐、提到歌名、引用消息、吐槽点歌机**——一句都不要放，这是最容易犯的错；"
        "· 对方只是泛泛地说「唱个歌听听」「来点音乐」而**没给具体歌名**——不要自己挑一首放，"
        "可以问他想听哪首；"
        "· 这一轮**已经放过歌了**——一轮最多放一首，不要连着放；"
        "· 拿不准对方到底想不想听——就不放。\n"
        "\n"
        "【失败怎么办】搜不到/下不到就**如实说一次**，不要把同一个歌名反复换写法重试"
        "（换个歌手名再试一次可以，两次都不行就停）。同名多首时系统会列出候选让点歌的人自己选，"
        "不用你替他决定。\n"
        "\n"
        "【注意】用户直接发「点歌 歌名」时**不需要**你调用这个工具 —— 那条消息由插件自己接管，"
        "不会轮到你。"
    ),
    parameters={
        "properties": {
            "song": {
                "type": "string",
                "description": "歌名，也可以写「歌名 歌手」提高准确度。",
            },
            "user_id": {
                "type": "string",
                "description": "可选：点歌人的 QQ 号。省略时按最近一次交互的会话投递。",
            },
        },
        "required": ["song"],
    },
)
async def _tool_play_local_song(
    song: str,
    user_id: str = "",
    ctx: Any = None,
    config: VoiceSongConfig = None,  # type: ignore[assignment]
    logger: Any = None,
) -> str:
    engine = _ensure_engine(ctx, config, logger or ctx.logger)
    cfg = engine.config
    if not cfg.enabled:
        return "点歌发语音条已经被关掉了。"

    song_text = logic.cut(str(song or ""), 80)
    if not song_text:
        return "错误：没给歌名。"

    conversation, target_key, asker_id = _resolve_tool_target(engine, user_id)
    if conversation is None:
        return (
            "找不到要发到哪个会话。请让用户直接发「点歌 <歌名>」——"
            "那条消息由插件自己接管，不需要工具。"
        )

    # 一轮最多放一首：模型曾经在一轮里连放三首（它自己都说「点串了」）。
    # 提示词里已经写明，但提示词挡不住的时候得靠代码挡。
    last_play = engine.turn_plays.get(target_key)
    if last_play is not None and _now() - last_play < TURN_PLAY_TTL_SEC:
        engine.stats.skipped += 1
        engine.stats.last_note = "本轮已经放过歌了"
        _log(engine, "{} 本轮已经放过一首，拒绝再放（歌名「{}」）".format(target_key, song_text))
        return "这一轮已经发过一首语音了，不要再放第二首。要换歌的话，等对方明确说了再放。"

    gate = _allow_send(engine, target_key, asker_id, song_text)
    if not gate["ok"]:
        return "这次没发：{}。".format(gate["reason"])

    roots = _scan_roots(engine)
    files = await _load_library(engine, roots)
    tagged = await _tag_files(engine, files)
    best = None
    if not (cfg.always_ask or not tagged):
        best = logic.pick_best(tagged, song_text, fuzzy=cfg.fuzzy)
    source = "local"

    if best is None:
        if not cfg.enable_download:
            return "音乐库里没找到「{}」，而且自动下载是关着的。".format(song_text)
        # 与确定性路径同一条规矩：列候选不算下载，真正的下载闸在选定之后
        if not cfg.always_ask:
            download_gate = _allow_download(engine, target_key, asker_id, song_text)
            if not download_gate["ok"]:
                return "这次没下：{}。".format(download_gate["reason"])

        local_candidates = (
            download.collect_local_candidates(cfg, song_text, tagged) if cfg.always_ask else []
        )
        result = await _safe_download(
            cfg,
            song_text,
            _download_dir(engine),
            choices=local_candidates or None,
            log=lambda message: _log(engine, message),
        )

        # 需要先选 → 把列表发给点歌者，等他回数字（与确定性路径一致）
        if result.get("ok") and result.get("need_choice"):
            await _ask_for_choice(
                engine,
                ctx,
                conversation,
                chat_key=target_key,
                sender_id=asker_id,
                song=song_text,
                choices=result.get("choices") or [],
            )
            # ⚠️ 返回值必须**明确禁止模型自己下结论**。真实故障：
            #    用户回数字后模型又调了一次本工具，工具回了一句含糊的
            #    「已经把候选列表发出去了…」，模型把它读成「工具拒绝播放」，
            #    于是自己编了「这首 VIP 的，抓不下来」去搪塞用户 ——
            #    而实际上全部候选都能正常下载。
            return (
                "已把候选列表发到会话里，**正在等对方回数字**。"
                "这一轮到此结束：不要再说这首歌放不了、不要说它是 VIP/收费、不要推荐换别的歌、"
                "也不要自己挑一首放。对方回数字后系统会自动播放那一首，**不需要你再做任何事**。"
                "你现在只需要结束这一轮（可以一句都不说）。"
            )

        if not result.get("ok") or not result.get("path"):
            engine.stats.download_fails += 1
            _record_error(engine, "自动下载失败（工具触发）：{}".format(result.get("error")))
            return "本地没有「{}」，去网易云下也没成功：{}".format(
                song_text, logic.cut(result.get("error"), 120)
            )
        if result.get("from_cache"):
            engine.stats.cache_hits += 1
        else:
            engine.download_gate.commit(cfg, song_text)
            engine.stats.downloads += 1
        engine.stats.last_download = "{}{}".format(
            result.get("title") or "", " — " + result["artist"] if result.get("artist") else ""
        )
        await _cleanup_cache(engine, "下载后")
        best = {
            "candidate": {
                "path": result["path"],
                "name": Path(result["path"]).name,
                "title": result.get("title") or "",
                "artist": result.get("artist") or "",
                "size": int(result.get("bytes") or 0),
                "mtime_ms": time.time() * 1000.0,
                "ext": Path(result["path"]).suffix.lstrip(".").lower(),
            },
            "score": 1000,
        }
        source = "cache" if result.get("from_cache") else "download"

    hit = best["candidate"]
    if float(hit.get("size") or 0) > float(cfg.max_file_mb or 20) * 1024 * 1024:
        return "找到了「{}」但超过 {}MB，太大了发不出来。".format(hit.get("name"), cfg.max_file_mb)

    try:
        _sweep_tmp(engine)
        tmp_path = await asyncio.to_thread(_copy_to_tmp, engine, str(hit["path"]))
    except Exception as exc:
        _record_error(engine, "工具发送失败（准备临时文件）：{}".format(exc))
        return "发送失败：{}".format(exc)

    title = logic.strip_ext(hit.get("title") or hit.get("name") or "") or song_text
    artist = hit.get("artist") or ""
    if not await _send_voice_and_title(
        engine, ctx, conversation, file_path=tmp_path, title=title, artist=artist
    ):
        return "发送失败：语音条没发出去（原因已记日志）。"

    engine.gate.commit(target_key, asker_id)
    engine.stats.sent += 1
    engine.stats.hits += 1
    engine.stats.last_song = title
    engine.stats.last_at = _now()
    _log(
        engine,
        "{} 模型触发点歌，已发送语音条：《{}》（{}）".format(
            target_key,
            title,
            "本地 {}".format(hit.get("name"))
            if source == "local"
            else ("下载缓存" if source == "cache" else "刚下载"),
        ),
    )
    # 标记「本轮已放」：同一轮里模型再调一次会被挡住
    engine.turn_plays[target_key] = _now()
    return "已用语音条发出《{}》{}。不要再复述或再用别的工具发一次。".format(
        title, " — " + artist if artist else ""
    )


def _resolve_tool_target(engine: Engine, user_id: str) -> Tuple[Any, str, str]:
    """给工具路径找「该发到哪个会话 + 点歌的人是谁」。

    NeoBot 的插件工具**没有入站事件**（``tools.py`` 里 event 恒为 ``{}``），
    所以只能靠 ``_observe_message`` 留下的最近交互。但这是一个**全局**视角：
    A 群正在跟模型对话时，B 群只要有人说一句话就会把「最近交互」顶掉。
    照着它发，语音条就发到 B 群去了 —— 而且待选会记在 B 群那个人名下，
    真正点歌的人回数字时又被宽限期挡住 → 静默无响应（正是原版注释里那个故障）。

    所以这里的规则是「**有歧义就不猜**」：
      · 调用方给了 user_id，且这个人在窗口内说过话 → 用他那个会话（明确的提示）；
      · 否则只有「窗口内只有一个会话在活跃」时才敢投递；
      · 其余情况返回空，让工具明确告诉模型「让用户直接发『点歌 歌名』」。
    """
    current = _now()
    wanted = str(user_id or "").strip()
    if wanted:
        record = engine.last_seen.get(wanted)
        if record is not None and current - record[0] <= TOOL_TARGET_TTL_SEC:
            kind, conversation_id = record[1], record[2]
            return (
                _ConversationRef(kind=kind, id=conversation_id),
                "{}:{}".format(kind, conversation_id),
                record[3],
            )

    active = [
        chat_key
        for chat_key, at in engine.recent_chats.items()
        if current - at <= TOOL_TARGET_TTL_SEC
    ]
    latest = engine.latest_seen
    if len(active) == 1 and latest is not None and current - latest[0] <= TOOL_TARGET_TTL_SEC:
        kind, conversation_id = latest[1], latest[2]
        return (
            _ConversationRef(kind=kind, id=conversation_id),
            "{}:{}".format(kind, conversation_id),
            latest[3],
        )
    return None, "", ""


# ───────────────────────────────────────────── 对外能力（排障用）


@plugin.capability("status.netease-voice-song")
async def _capability_status(payload: Any = None) -> Dict[str, Any]:
    """状态卡：控制台/排障能直接看到它在干什么。

    返回结构化数据（不是纯文本），调用方可以自己排版。
    """
    engine = _engine
    if engine is None:
        return {"ok": False, "error": "插件尚未加载"}
    cfg = engine.config
    if not cfg.enabled:
        return {"ok": False, "error": "插件已在设置里关闭"}
    roots = _scan_roots(engine)
    files = await _load_library(engine, roots)
    lines: List[Dict[str, str]] = [
        {"label": "触发", "value": " / ".join(cfg.trigger_list)},
        {
            "label": "曲库",
            "value": "{} 首（{} 个目录）".format(len(files), len(roots))
            if files
            else "空（本地没有就自动下载）",
        },
        {
            "label": "已发送",
            "value": "{} 首{}".format(
                engine.stats.sent,
                "，最近：{}".format(engine.stats.last_song) if engine.stats.last_song else "",
            ),
        },
    ]
    if cfg.unlock_enabled:
        active = engine.exempt.list()
        lines.append(
            {
                "label": "解除限制",
                "value": "已用 {} 次，被拒 {} 次；当前豁免 {}".format(
                    engine.stats.unlocked,
                    engine.stats.unlock_denied,
                    "、".join(
                        "{}:{}分".format(item["scope"], max(1, math.ceil(item["left_sec"] / 60)))
                        for item in active
                    )
                    if active
                    else "无",
                ),
            }
        )
        if engine.stats.last_unlock:
            lines.append({"label": "最近解除", "value": engine.stats.last_unlock})
    else:
        lines.append({"label": "解除限制", "value": "已关闭"})

    if cfg.enable_download:
        lines.append(
            {
                "label": "自动下载",
                "value": "{}｜已下载 {} 首，缓存命中 {} 次，失败 {} 次".format(
                    cfg.quality_key, engine.stats.downloads, engine.stats.cache_hits, engine.stats.download_fails
                ),
            }
        )
        # 读 cookie 会碰磁盘，放线程里（状态卡是面板按需调用的，但不能卡事件循环）
        cookie = await asyncio.to_thread(
            netease.resolve_cookie, cfg.cookie_secret, cfg.use_client_cookie
        )
        lines.append(
            {
                "label": "登录态",
                "value": (
                    "不可用：{}".format(cookie.get("error"))
                    if cookie.get("error")
                    else "可用（{}）".format(cookie.get("source"))
                ),
            }
        )
        lines.append({"label": "下载目录", "value": str(_download_dir(engine))})
        lines.append(
            {
                "label": "缓存去重",
                "value": "已清掉 {} 个重复文件".format(engine.stats.deduped)
                if engine.stats.deduped
                else "暂无重复",
            }
        )
        lines.append(
            {
                "label": "同名多首",
                "value": (
                    "每次都先列候选让人选（已问 {} 次，最多 {} 个，{} 分钟内有效）".format(
                        engine.stats.asked,
                        cfg.max_choices,
                        round(int(cfg.choice_ttl_sec or 180) / 60) or 1,
                    )
                    if cfg.always_ask
                    else ("只在难分高下时列出候选" if cfg.ask_when_ambiguous else "自动挑最匹配的一首")
                ),
            }
        )
    else:
        lines.append({"label": "自动下载", "value": "已关闭"})

    return {
        "ok": True,
        "icon": "🎤",
        "title": "点歌发语音条",
        "priority": 50,
        "lines": lines,
        "stats": {
            "scanned": engine.stats.scanned,
            "hits": engine.stats.hits,
            "sent": engine.stats.sent,
            "misses": engine.stats.misses,
            "skipped": engine.stats.skipped,
            "errors": engine.stats.errors,
            "downloads": engine.stats.downloads,
            "download_fails": engine.stats.download_fails,
            "cache_hits": engine.stats.cache_hits,
            "asked": engine.stats.asked,
            "last_note": engine.stats.last_note,
        },
    }


# ───────────────────────────────────────────── 生命周期


@plugin.on_load
async def _on_load(ctx: Any, config: VoiceSongConfig, logger: Any) -> None:
    global _engine
    _engine = _ensure_engine(ctx, config, logger)
    engine = _engine

    cfg = engine.config
    roots = _scan_roots(engine)
    logger.info(
        "点歌发语音条已加载：触发词「{}」；音乐目录 {} 个{}；最多 {}MB；同会话冷却 {} 秒".format(
            " / ".join(cfg.trigger_list),
            len(roots),
            "（{}）".format("、".join(item.name for item in roots)) if roots else "（还没有可用的目录，去设置里填）",
            cfg.max_file_mb,
            cfg.cooldown_sec,
        )
    )
    if cfg.enable_download:
        cookie = netease.resolve_cookie(
            manual=cfg.cookie_secret, allow_client=cfg.use_client_cookie
        )
        logger.info(
            "自动下载已开启：音质 {}，下载目录 {}；登录态 {}".format(
                cfg.quality_key,
                _download_dir(engine),
                "不可用（{}）→ 只能靠本地曲库".format(cookie["error"])
                if cookie.get("error")
                else "来自{}".format(cookie.get("source")),
            )
        )
    else:
        logger.info("自动下载未开启（本地没有的歌不会去下）")


@plugin.on_startup
async def _on_startup() -> None:
    engine = _engine
    if engine is None:
        return
    try:
        removed = _sweep_tmp(engine, force=True)
        if removed:
            _log(engine, "启动清理：删掉 {} 个上次残留的临时音频".format(removed))
    except Exception:
        pass
    # 启动时清一次缓存重复：上次运行可能因为重试/换写法反复下过同一首
    try:
        await _cleanup_cache(engine, "启动")
    except Exception:
        pass


@plugin.on_shutdown
async def _on_shutdown() -> None:
    global _engine
    engine = _engine
    _engine = None
    if engine is None:
        return
    for task in list(engine.tasks):
        task.cancel()
    if engine.tasks:
        try:
            await asyncio.gather(*engine.tasks, return_exceptions=True)
        except Exception:
            pass
    engine.tasks.clear()
    try:
        _sweep_tmp(engine, force=True)
    except Exception:
        pass
    engine.index_cache.clear()
    engine.tags_cache.clear()
    engine.pending.clear()
    engine.exempt.clear()
    engine.locks.clear()
    engine.turn_plays.clear()
    engine.last_seen.clear()
    engine.recent_chats.clear()
    engine.latest_seen = None
