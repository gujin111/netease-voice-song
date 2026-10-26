"""联网搜歌 / 下载 / 候选合并 —— 从原 Node 版 ``logic.js`` 的下载流程移植。

一条设计约束（别改）：这个文件**不发消息、不认识框架**，所有依赖都从参数进来。
这样它可以拿假客户端完整离线测试，也方便以后换音源。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import library, logic, netease

Logger = Callable[[str], None]


def _noop(_message: str) -> None:
    return None


async def search_with_fallback(cfg: Any, wanted: str, log: Logger = _noop) -> Dict[str, Any]:
    """按歌名搜歌，**没够格的就自动换更宽的写法再搜一次**。

    为什么需要它（真实故障）：用户写「One Last Kiss 宇多田光」（中文名），
    网易云返回的歌手是「宇多田ヒカル」（日文名）—— 同一个人的两种写法。
    以前直接回「没有一首够像」，模型把它转述成「网易云搜不着」，
    然后开始自己乱换关键词硬试（日志里对同一首歌换了 8 种写法）。
    降级策略：原样 → 只取歌名主体（切掉艺人部分）→ 再切短一点。
    """
    min_score = int(getattr(cfg, "match_min_score", logic.NET_MIN_SCORE) or logic.NET_MIN_SCORE)
    limit = max(1, int(getattr(cfg, "search_limit", 5) or 5))
    fuzzy = bool(getattr(cfg, "fuzzy", True))

    # 候选查询词：原样，以及逐步切掉后半段（艺人名/后缀）
    variants: List[str] = [wanted]
    for separator in ("-", "—", "–", "(", "（", "["):
        head = str(wanted).split(separator)[0].strip()
        if head and head != wanted and len(head) >= 2 and head not in variants:
            variants.append(head)
    words = str(wanted).split()
    if len(words) >= 3:
        shorter = " ".join(words[: (len(words) + 1) // 2])
        if shorter and shorter not in variants:
            variants.append(shorter)

    attempts: List[str] = []
    last_found: Optional[Dict[str, Any]] = None
    saw_request_failure = False
    saw_rate_limit = False
    saw_risk = False
    risk_error = ""

    for keyword in variants:
        found = await netease.search_songs(keyword, limit=limit)
        if not found.get("ok"):
            # 四类失败要分开：风控冷却 / 接口限流 / 连不上 / 真的没结果。
            # 早先混成一句「没搜到」，结果「接口限流」看起来像「这首歌不存在」。
            if found.get("risk_limited"):
                # -460 的全局冷却：这时候再换写法重试只会把冷却续长，必须原样报给用户
                saw_risk = True
                risk_error = str(found.get("error") or "")
                attempts.append("{}（风控冷却）".format(keyword))
            elif found.get("rate_limited"):
                saw_rate_limit = True
                attempts.append("{}（接口限流）".format(keyword))
            elif found.get("request_failed"):
                saw_request_failure = True
                attempts.append("{}（请求失败：{}）".format(keyword, found.get("error", "")))
            else:
                attempts.append("{}（无结果）".format(keyword))
            continue

        last_found = found
        tokens = logic.tokens_of(keyword)
        scored = []
        for song in found.get("songs") or []:
            name = "{}{}".format(
                song.get("name") or "",
                " - {}".format(song["artist"]) if song.get("artist") else "",
            )
            score = logic.score_candidate(
                {"name": name, "title": song.get("name") or "", "artist": song.get("artist") or "", "ext": "mp3"},
                keyword,
                fuzzy=fuzzy,
                tokens=tokens,
            )
            if score > 0 and score >= min_score:
                scored.append({"song": song, "score": score})
        scored.sort(key=lambda item: item["score"], reverse=True)
        attempts.append(
            "{}（搜到 {} 条，够格 {} 条）".format(keyword, len(found.get("songs") or []), len(scored))
        )
        if scored:
            if keyword != wanted:
                log("「{}」不够格，改用「{}」重搜后命中 {} 条".format(wanted, keyword, len(scored)))
            return {
                "ok": True,
                "found": found,
                "scored": scored,
                "used_keyword": keyword,
                "attempts": attempts,
            }

    if last_found is None:
        if saw_risk:
            return {
                "ok": False,
                "rate_limited": True,
                "risk_limited": True,
                "error": risk_error
                or "网易云正在风控冷却（-460），过几分钟再点；这期间反复重试只会把冷却续长",
                "attempts": attempts,
            }
        if saw_rate_limit and not saw_request_failure:
            return {
                "ok": False,
                "rate_limited": True,
                "error": (
                    "网易云的搜索接口正在限流（提示「操作频繁」），**不是这首歌不存在**"
                    "——过几分钟再点，不要反复重试（越试越久）"
                ),
                "attempts": attempts,
            }
        return {
            "ok": False,
            "request_failed": saw_request_failure,
            "error": (
                "连网易云都没连上（{}）—— 网络/接口问题，不是「这首歌不存在」".format("；".join(attempts))
                if saw_request_failure
                else "网易云真的没有任何匹配结果（{}）".format("；".join(attempts))
            ),
            "attempts": attempts,
        }

    # 搜到了、但一条都不够像 —— 要把「搜到过什么」带上，让模型/用户一眼看出区别
    top = "、".join(
        "「{}」{}".format(song.get("name") or "", " — {}".format(song["artist"]) if song.get("artist") else "")
        for song in (last_found.get("songs") or [])[:3]
    )
    return {
        "ok": False,
        "error": (
            "搜到了歌，但没有一首够像「{}」（最低分 {}）。搜到的前几首是：{}。"
            "如果其中某一首就是你要的，请把歌名和歌手**一起**传进来；否则换个更准确的写法，不要反复试。"
        ).format(wanted, min_score, top),
        "attempts": attempts,
        "searched": True,
    }


async def song_choice_candidates(
    cfg: Any,
    song: str,
    *,
    local_files: Optional[List[Dict[str, Any]]] = None,
    log: Logger = _noop,
) -> Dict[str, Any]:
    """汇总**所有来源**的候选，供「必须先选」用：本地曲库 + 下载缓存 + 网易云搜索。

    为什么要把本地也列出来：如果只列网易云的，用户会以为「本地那首没了」；
    而如果本地命中就直接放，就违背了「每次都必须选」的要求。
    列表里两种来源混排，本地项排在最前并标上来源，用户一目了然。
    """
    wanted = str(song or "").strip()
    if not wanted:
        return {"ok": False, "error": "歌名是空的"}

    min_score = int(getattr(cfg, "match_min_score", logic.NET_MIN_SCORE) or logic.NET_MIN_SCORE)
    fuzzy = bool(getattr(cfg, "fuzzy", True))
    tokens = logic.tokens_of(wanted)
    max_choices = max(1, min(20, int(getattr(cfg, "max_choices", 8) or 8)))

    # ① 本地曲库 / 缓存（不联网，最快）
    local_scored = []
    for item in local_files or []:
        candidate = dict(item)
        candidate["title"] = candidate.get("title") or ""
        candidate["artist"] = candidate.get("artist") or ""
        score = logic.score_candidate(candidate, wanted, fuzzy=fuzzy, tokens=tokens)
        if score > 0:
            local_scored.append({"file": item, "score": score})
    local_scored.sort(key=lambda item: item["score"], reverse=True)

    # ② 网易云搜索
    remote: List[Dict[str, Any]] = []
    remote_error = ""
    rate_limited = False
    risk_limited = False
    try:
        searched = await search_with_fallback(cfg, wanted, log)
        if searched.get("ok"):
            for scored in searched["scored"]:
                remote.append({"song": scored["song"], "score": scored["score"]})
        else:
            remote_error = searched.get("error", "")
            rate_limited = bool(searched.get("rate_limited"))
            risk_limited = bool(searched.get("risk_limited"))
    except Exception as exc:
        remote_error = "搜索异常：{}".format(exc)

    # ③ 合并且去重（本地同名的只留分数最高的那条）
    choices: List[Dict[str, Any]] = []
    seen_local_title: set = set()
    for scored in local_scored:
        file_item = scored["file"]
        title = logic.strip_id_suffix(logic.strip_ext(file_item.get("name") or ""))
        key = logic.normalize_name(title)
        if key in seen_local_title:
            continue
        seen_local_title.add(key)
        choices.append(
            {
                "id": "file:{}".format(file_item.get("path")),
                "file_path": file_item.get("path"),
                "name": title,
                "artist": file_item.get("artist") or "",
                "album": "",
                "duration_ms": 0,
                "fee": None,
                "source": "local",
                "score": scored["score"],
            }
        )
    asked_local = bool(choices)

    seen_remote_id: set = set()
    remote_choices: List[Dict[str, Any]] = []
    for scored in remote:
        remote_song = scored["song"]
        song_id = str(remote_song.get("id") or "")
        if not song_id or song_id in seen_remote_id:
            continue
        seen_remote_id.add(song_id)
        remote_choices.append(
            {
                "id": song_id,
                "name": remote_song.get("name") or "",
                "artist": remote_song.get("artist") or "",
                "album": remote_song.get("album") or "",
                "duration_ms": remote_song.get("duration_ms") or 0,
                "fee": remote_song.get("fee"),
                "source": "netease",
                "score": scored["score"],
            }
        )

    # 本地项排最前（「用本地这份吗」才是问题的主体），紧接着是「下载一份新的」，
    # 最后才是网易云的具体版本。唯一实现见 logic.with_local_question。
    merged = logic.with_local_question(choices + remote_choices)

    if not merged["list"]:
        return {
            "ok": False,
            "rate_limited": rate_limited,
            "risk_limited": risk_limited,
            "searched": bool(remote_error),
            "error": remote_error or "没找到「{}」（本地和网易云都没有）".format(wanted),
        }
    trimmed = logic.trim_choices(merged["list"], max_choices)
    return {
        "ok": True,
        "choices": trimmed,
        "truncated": len(trimmed) < len(merged["list"]),
        "total": len(merged["list"]),
        "asked_local": merged["asked_local"],
    }


async def _download_chosen(
    cfg: Any,
    chosen: Dict[str, Any],
    download_dir: Path,
    *,
    cookie: str = "",
    source: str = "",
    log: Logger = _noop,
) -> Dict[str, Any]:
    """真正把某一首下下来。

    注意：chosen 可能来自本地缓存（有 file_path），这时直接返回该文件，不发任何请求 ——
    「每次都先选」之后，用户选中的很可能是本地已有的那首，再去网易云下一遍纯属浪费。
    """
    max_bytes = max(1, int(float(getattr(cfg, "download_max_mb", 25) or 25) * 1024 * 1024))
    local_path = chosen.get("file_path")
    if local_path:
        try:
            size = Path(str(local_path)).stat().st_size
        except OSError:
            size = 0
        if not size:
            return {"ok": False, "error": "选中的本地文件读不到了：{}".format(local_path)}
        return {
            "ok": True,
            "path": str(local_path),
            "title": chosen.get("name") or logic.strip_ext(Path(str(local_path)).name),
            "artist": chosen.get("artist") or "",
            "bytes": size,
            "from_cache": True,
            "quality": "",
            "note": "本地曲库/缓存",
        }

    url_result = await netease.song_url(
        chosen.get("id"),
        cookie=cookie,
        quality=getattr(cfg, "quality_key", "exhigh"),
        timeout_sec=float(getattr(cfg, "download_timeout_sec", 60) or 60),
    )
    if not url_result.get("ok"):
        hint = ""
        if logic.is_vip_only(chosen.get("fee")):
            hint = "（这首是{}歌曲，需要会员账号才能取到完整音频）".format(
                logic.fee_label(chosen.get("fee"))
            )
        return {
            "ok": False,
            "error": "{}{}".format(url_result.get("error", "取直链失败"), hint),
            "tried": ["{}（id {}）".format(chosen.get("name") or chosen.get("id"), chosen.get("id"))],
        }
    if url_result.get("size") and int(url_result["size"]) > max_bytes:
        return {
            "ok": False,
            "error": "这首 {:.1f}MB，超过上限 {}MB".format(
                int(url_result["size"]) / 1048576.0, getattr(cfg, "download_max_mb", 25)
            ),
        }

    ext = "flac" if getattr(cfg, "quality_key", "exhigh") == "lossless" else "mp3"
    # 文件名带上歌手：缓存去重是按「去掉 [id] 之后的名字」分组的，不带歌手时，
    # 两首同名、不同歌手、时长只差 1~2 秒的歌会被判成重复、删掉其中一首。
    # 先截「歌名 - 歌手」再拼 [id]，保证 id 尾巴（去重的凭据）不会被截断。
    title_part = " - ".join(
        part
        for part in (
            str(chosen.get("name") or "").strip(),
            str(chosen.get("artist") or "").strip(),
        )
        if part
    ) or "song"
    base = "{} [{}]".format(netease.safe_file_name(title_part, 80), chosen.get("id"))
    destination = Path(download_dir) / "{}.{}".format(base, ext)
    downloaded = await netease.download_to(
        url_result["url"],
        destination,
        cookie=cookie,
        max_bytes=max_bytes,
        timeout_sec=float(getattr(cfg, "download_timeout_sec", 60) or 60),
    )
    if not downloaded.get("ok"):
        return {
            "ok": False,
            "error": downloaded.get("error", "下载失败"),
            "tried": ["{}（id {}）".format(chosen.get("name") or chosen.get("id"), chosen.get("id"))],
        }

    log(
        "已下载《{}》{:.2f}MB → {}".format(
            chosen.get("name") or chosen.get("id"), downloaded.get("bytes", 0) / 1048576.0, destination
        )
    )
    return {
        "ok": True,
        "path": str(destination),
        "title": chosen.get("name") or str(chosen.get("id")),
        "artist": chosen.get("artist") or "",
        "bytes": downloaded.get("bytes", 0),
        "from_cache": False,
        "fee": chosen.get("fee"),
        "song_id": str(chosen.get("id")),
        "quality": (netease.QUALITY.get(getattr(cfg, "quality_key", "exhigh")) or {}).get("label", ""),
        "note": "取自网易云，登录态来自{}".format(source) if source else "取自网易云",
    }


async def download_song(
    cfg: Any,
    song: str,
    download_dir: Path,
    *,
    pick: str = "",
    choices: Optional[List[Dict[str, Any]]] = None,
    log: Logger = _noop,
) -> Dict[str, Any]:
    """本地没有这首歌时，去网易云下载。

    流程（顺序是有讲究的）：
      ① 先看缓存目录里是不是已经有了（同一首歌点两次不该下两次）
      ② 搜歌拿候选 → 用**同一套打分逻辑**挑最匹配的那首
         （不然会出现「点《海阔天空》却下了个同名翻唱」这种一眼假的结果）
      ③ 取直链 → 下载 → 写进缓存目录，文件名带歌名/id，下次直接命中

    同名多首时**不硬猜**：返回 ``{"ok": True, "need_choice": True, "choices": [...]}``，
    由调用方把列表发给点歌者，等他回数字后再带 pick 回来。

    ``pick`` 是用户已选定的候选 id —— 传了就跳过「歧义判定」，直接用这首。
    """
    wanted = str(song or "").strip()
    if not wanted:
        return {"ok": False, "error": "歌名是空的"}
    # 「自动下载」关掉时，这一层也要拦住：面板上的开关不能只对某一条入口生效，
    # 否则用户在设置里关掉它，插件照样联网搜歌、照样下载（配置项形同虚设）。
    if not bool(getattr(cfg, "enable_download", True)) and not str(pick or "").startswith("file:"):
        return {"ok": False, "error": "自动下载已关闭（只发本地已有的歌）"}
    if not str(download_dir or "").strip():
        return {"ok": False, "error": "没有配置下载目录"}
    directory = Path(download_dir)

    min_score = int(getattr(cfg, "match_min_score", logic.NET_MIN_SCORE) or logic.NET_MIN_SCORE)
    always_ask = bool(getattr(cfg, "always_ask", True))

    # ⓪ 用户已经从列表里选定 → 交付这一首。
    #    两种选法都要支持：
    #      · 选本地文件（id 形如 file:<路径>）→ 直接返回该文件，不联网；
    #      · 选网易云的某一版 → **联网重新查一次**再下。不能图省事只按 id 拼 URL：
    #        用户可能刚在网易云那边换了版本，列表是几分钟前的，重查一次拿到的才是当前正确的音频。
    if pick:
        if str(pick).startswith("file:"):
            local_path = str(pick)[5:]
            try:
                size = Path(local_path).stat().st_size
            except OSError:
                size = 0
            if not size:
                return {"ok": False, "error": "选中的本地文件读不到了：{}".format(local_path)}
            return {
                "ok": True,
                "path": local_path,
                "title": logic.strip_ext(Path(local_path).name),
                "artist": "",
                "bytes": size,
                "from_cache": True,
                "quality": "",
                "note": "本地曲库",
            }

        cookie = ""
        cookie_source = ""
        # 读 cookie 会碰磁盘（客户端的数据文件可能有几 MB），丢线程别占事件循环
        cookie_result = await asyncio.to_thread(
            netease.resolve_cookie,
            str(getattr(cfg, "cookie_secret", "") or ""),
            bool(getattr(cfg, "use_client_cookie", True)),
        )
        if not cookie_result.get("error"):
            cookie = cookie_result["cookie"]
            cookie_source = cookie_result["source"]

        # 先看看调用方给的候选里有没有这一条（能省一次搜索，也保住歌手/fee 等字段）
        chosen: Optional[Dict[str, Any]] = None
        for item in choices or []:
            if str(item.get("id")) == str(pick):
                chosen = item
                break
        # 无论如何都重查一次，拿当前最新的那一版
        try:
            searched = await search_with_fallback(cfg, wanted, log)
            if searched.get("ok"):
                for candidate in searched["found"].get("songs") or []:
                    if str(candidate.get("id")) == str(pick):
                        chosen = candidate
                        break
        except Exception as exc:
            log("选定后重查失败（沿用列表里的信息）：{}".format(exc))
        if chosen is None:
            return {
                "ok": False,
                "error": "你选的那一首（id {}）现在查不到了，可能已下架。换一首试试。".format(pick),
            }
        if not cookie:
            return {"ok": False, "error": "没有可用的登录态，取不到完整音频"}
        return await _download_chosen(
            cfg, chosen, directory, cookie=cookie, source=cookie_source, log=log
        )

    # ① 候选：本地缓存 + 网易云
    #
    # ⚠️ 关键规矩：**本地有缓存也要联网搜其他版本**。
    #    所以「必须先选」时永远联网，哪怕入口已经把本地候选传进来了 ——
    #    早先看到 choices 就直接跳过联网，结果列表里只有本地那一条，
    #    用户根本看不到网易云上的其他版本。这是实际发生过的缺陷。
    #
    #    反过来说：always_ask 关掉时，缓存命中就该直接放、**不联网**
    #    （「同一首歌点两次只下一次」的优化不能丢）。
    pool: List[Dict[str, Any]] = [dict(item) for item in (choices or [])]

    # ①-1 本地缓存回填：**无条件做**（不联网，很快）。
    #      ⚠️ 这一步必须在判断「要不要联网」之前：早先把它写在 need_remote 里面，
    #      而 need_remote 又要靠「回填后列表空不空」来决定 —— 逻辑错位，
    #      结果是永远先联网（缓存优化形同虚设）。
    if bool(getattr(cfg, "reuse_downloaded", True)):
        tokens = logic.tokens_of(wanted)
        # 列目录也是同步 IO（最多 500 条 stat），别放在事件循环上
        cached_files = await asyncio.to_thread(netease.list_cached, directory)
        for cached in cached_files:
            score = logic.score_candidate(
                dict(cached, title="", artist=""), wanted, fuzzy=bool(cfg.fuzzy), tokens=tokens
            )
            if score <= 0:
                continue
            already = any(
                item.get("file_path")
                and Path(str(item["file_path"])).resolve() == Path(str(cached["path"])).resolve()
                for item in pool
            )
            if already:
                continue
            pool.append(
                {
                    "id": "file:{}".format(cached["path"]),
                    "file_path": cached["path"],
                    "name": logic.strip_ext(cached["name"]),
                    "artist": "",
                    "album": "",
                    "duration_ms": 0,
                    "fee": None,
                    "source": "local",
                    "score": score,
                }
            )

    # ①-2 要不要联网找其他版本？
    #      用户明确要求：**本地有缓存也要联网搜其他版本**。所以 always_ask 时永远联网；
    #      不 always_ask 时，缓存命中就不联网。
    need_remote = always_ask or not pool
    if need_remote:
        gathered = await song_choice_candidates(cfg, wanted, local_files=[], log=log)
        if gathered.get("ok"):
            for candidate in gathered["choices"]:
                if candidate.get("source") == "local":
                    continue  # 本地项上面已经收过
                duplicated = any(str(item.get("id")) == str(candidate.get("id")) for item in pool) or (
                    candidate.get("source") == "netease"
                    and candidate.get("artist")
                    and any(
                        item.get("source") == "netease"
                        and logic.normalize_name(item.get("name")) == logic.normalize_name(candidate.get("name"))
                        and logic.normalize_name(item.get("artist"))
                        == logic.normalize_name(candidate.get("artist"))
                        for item in pool
                    )
                )
                if not duplicated:
                    pool.append(candidate)
        elif not pool:
            return {
                "ok": False,
                "error": gathered.get("error"),
                "rate_limited": gathered.get("rate_limited"),
                "risk_limited": gathered.get("risk_limited"),
                "searched": gathered.get("searched"),
            }

    # 按分数排序；同分时把网易云的排在本地前面（有版本信息：歌手/时长/VIP，更好选）
    pool.sort(key=lambda item: (item.get("score") or 0, 0 if item.get("source") == "local" else 1), reverse=True)

    # ② 决定：是让他选，还是直接放
    #
    #    本地项（缓存命中）不计入「够不够像」的闸：它本来就是同一首歌的本地副本，
    #    而且要被列出来问「用这份还是下新的」。
    eligible = [
        item for item in pool if item.get("source") == "local" or (item.get("score") or 0) >= min_score
    ]
    if not eligible:
        top = ""
        if pool:
            top = "（最接近的是 「{}」{}）".format(
                pool[0].get("name") or "",
                " — {}".format(pool[0]["artist"]) if pool[0].get("artist") else "",
            )
        return {
            "ok": False,
            "searched": True,
            "error": (
                "搜到了歌，但没有一首够像「{}」{}。"
                "如果其中某一首就是你要的，请把歌名和歌手**一起**传进来；"
                "否则换个更准确的写法，不要反复试。"
            ).format(wanted, top),
        }

    ambiguous = bool(getattr(cfg, "ask_when_ambiguous", True)) and len(eligible) > 1 and logic.is_ambiguous(
        eligible, ratio=float(getattr(cfg, "ambiguity_ratio", 0.92) or 0.92)
    )
    if always_ask or ambiguous:
        max_choices = max(1, min(20, int(getattr(cfg, "max_choices", 8) or 8)))
        shaped = logic.with_local_question(eligible)  # 唯一实现：本地有 → 插一条「下载一份新的」
        # ⚠️ 裁剪要保证动作项后面还有远程项，否则用户选「下载一份新的」会拿到空列表
        #    （而那时待选已经被消费掉了）→ 见 logic.trim_choices。
        trimmed = logic.trim_choices(shaped["list"], max_choices)
        return {
            "ok": True,
            "need_choice": True,
            "song": wanted,
            "always_ask": always_ask,
            "asked_local": shaped["asked_local"],
            "choices": [
                {
                    "id": item.get("id"),
                    "file_path": item.get("file_path") or "",
                    "name": item.get("name"),
                    "artist": item.get("artist") or "",
                    "album": item.get("album") or "",
                    "duration_ms": item.get("duration_ms") or 0,
                    "fee": item.get("fee"),
                    "source": item.get("source") or "netease",
                    "score": item.get("score"),
                }
                for item in trimmed
            ],
        }

    # ③ 不需要问：直接下最高分那首
    cookie_result = await asyncio.to_thread(
        netease.resolve_cookie,
        str(getattr(cfg, "cookie_secret", "") or ""),
        bool(getattr(cfg, "use_client_cookie", True)),
    )
    if cookie_result.get("error"):
        return {"ok": False, "error": cookie_result["error"]}
    return await _download_chosen(
        cfg,
        eligible[0],
        directory,
        cookie=cookie_result["cookie"],
        source=cookie_result["source"],
        log=log,
    )


def collect_local_candidates(cfg: Any, song: str, tagged_files: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """收集「这首歌的本地候选」：曲库（调用方已经补好 ID3 标签）。

    ⚠️ 两条路径（确定性点歌 / 模型调工具）必须用**同一个**函数收集，
    否则迟早出现「一条路径问『要不要用本地那份』、另一条不问」的不一致。

    ⚠️ 标签由调用方用 ``asyncio.to_thread`` 补好再传进来：这个函数会被
    每一条点歌调用，不能在这里读文件。
    """
    tokens = logic.tokens_of(song)
    seen: set = set()
    out: List[Dict[str, Any]] = []
    for item in tagged_files or []:
        score = logic.score_candidate(
            {"name": item.get("name"), "title": item.get("title") or "", "artist": item.get("artist") or ""},
            song,
            fuzzy=bool(getattr(cfg, "fuzzy", True)),
            tokens=tokens,
        )
        if score <= 0:
            continue
        key = str(item.get("path") or "").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        # 剥掉扩展名与 ` [网易云id]` 尾巴：本地项显示成「铁花飞」而不是「铁花飞 [3342981041]」
        name = logic.strip_id_suffix(
            logic.strip_ext(str(item.get("name") or Path(str(item.get("path"))).name))
        )
        out.append(
            {
                "id": "file:{}".format(item.get("path")),
                "file_path": item.get("path"),
                "name": name,
                "artist": item.get("artist") or "",
                "album": "",
                "duration_ms": 0,
                "fee": None,
                "source": "local",
                "score": score,
            }
        )
    out.sort(key=lambda item: item["score"], reverse=True)
    return out
