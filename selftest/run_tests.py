"""离线自测：不联网、不依赖 NeoBot，只验移植后的核心逻辑。

跑法（在插件目录的父目录）：
    python work/out/_test/run_tests.py

覆盖：
  · 点歌解析 / 选择回复 / 解锁指令（原版行为逐条对齐）
  · 候选打分与同名多首的候选合并
  · 限流闸、待选表、临时豁免（注入时钟）
  · ID3v2 / ID3v1(GBK) 读取、MP3 时长估算
  · cookie 解析、文件名安全化、缓存列举与去重
  · download 流程（用假客户端）：缓存命中 / 最低分闸 / 需要选择 / 选定后下载
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 既支持发布形态（selftest 在插件目录里），也支持开发形态（selftest 与插件目录并列）
PLUGIN_DIR = HERE.parent if (HERE.parent / "plugin.toml").is_file() else HERE.parent / "netease-voice-song"
sys.path.insert(0, str(HERE / "stubs"))  # 假的 pydantic（本机是 v1，目标环境是 v2）

# 直接按文件加载子模块，绕开包的 __init__.py（它要 import neobot_modloader）。
_PACKAGE = "netvs_under_test"
_pkg = types.ModuleType(_PACKAGE)
_pkg.__path__ = [str(PLUGIN_DIR)]
sys.modules[_PACKAGE] = _pkg


def _load(module_name):
    full = "{}.{}".format(_PACKAGE, module_name)
    spec = importlib.util.spec_from_file_location(full, PLUGIN_DIR / (module_name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


logic = _load("logic")
library = _load("library")
netease = _load("netease")
download = _load("download")
config_module = _load("config")

PASS = 0
FAIL = []


def check(condition, label, detail=""):
    global PASS
    if condition:
        PASS += 1
    else:
        FAIL.append("{} {}".format(label, detail))


def eq(actual, expected, label):
    check(actual == expected, label, "(得到 {!r}，期望 {!r})".format(actual, expected))


def truthy(value, label, detail=""):
    check(bool(value), label, detail if detail else "(得到 {!r})".format(value))


# ───────────────────────────────────────────── 点歌解析

print("· 点歌解析")
eq(logic.parse_song_request("点歌 海阔天空")["song"], "海阔天空", "基本形态")
eq(logic.parse_song_request("点歌：海阔天空")["song"], "海阔天空", "全角冒号")
eq(logic.parse_song_request("来一首《晴天》")["song"], "晴天", "书名号包裹")
eq(logic.parse_song_request("/点歌 One Last Kiss")["song"], "One Last Kiss", "斜杠前缀")
eq(logic.parse_song_request("  点歌   海阔天空  ")["song"], "海阔天空", "多余空白")
eq(logic.parse_song_request("点歌海阔天空")["song"], "海阔天空", "无空格")
eq(logic.parse_song_request("点 歌 海阔天空")["song"], "海阔天空", "触发词内部空格")
eq(logic.parse_song_request("点歌 海阔天空吧")["song"], "海阔天空", "语气尾巴")
eq(logic.parse_song_request("我刚点歌了")["hit"], False, "陈述句不触发")
eq(logic.parse_song_request("今天想听点什么")["hit"], False, "句中触发词不触发")
eq(logic.parse_song_request("")["hit"], False, "空消息")
eq(logic.parse_song_request("点歌")["song"], "", "只给触发词")

parsed = logic.parse_song_request("小鲸鱼，来一首 稻香", self_names="小鲸鱼,小鲸")
eq(parsed["song"], "稻香", "剥掉机器人别名")
parsed = logic.parse_song_request("@机器人(QQ:123456) 点歌 稻香")
eq(parsed["song"], "稻香", "剥掉开头 @ 片段")
parsed = logic.parse_song_request("点歌 @张三 最爱的那首")
eq(parsed["song"], "@张三 最爱的那首", "正文中间的 @ 保留")
parsed = logic.parse_song_request("放首 海阔天空", keywords=["来一首"]) 
eq(parsed["hit"], False, "自定义触发词生效")

# ───────────────────────────────────────────── 选择回复

print("· 选择回复")
choices = [
    {"name": "海阔天空", "artist": "Beyond"},
    {"name": "海阔天空", "artist": "G.E.M.邓紫棋"},
    {"name": "海阔天空(Live)", "artist": "黄贯中/叶世荣/黄家强"},
]
eq(logic.parse_choice_reply("2", choices), {"index": 1}, "纯数字")
eq(logic.parse_choice_reply("２", choices), {"index": 1}, "全角数字")
eq(logic.parse_choice_reply("第2首", choices), {"index": 1}, "第N首")
eq(logic.parse_choice_reply("选2", choices), {"index": 1}, "选N")
eq(logic.parse_choice_reply("2.", choices), {"index": 1}, "数字带点")
eq(logic.parse_choice_reply("Beyond", choices), {"index": 0}, "歌手名唯一命中")
eq(logic.parse_choice_reply("9", choices), None, "越界数字")
eq(logic.parse_choice_reply("随便", choices), None, "普通聊天")

# ───────────────────────────────────────────── 打分与候选

print("· 候选打分")
eq(logic.normalize_name("Beyond - 海阔天空.flac"), "beyond 海阔天空 flac", "归一化")
eq(logic.strip_ext("海阔天空.mp3"), "海阔天空", "去扩展名")
eq(logic.tokens_of("One Last Kiss 宇多田光"), ["one", "last", "kiss", "宇多田光"], "分词")

eq(logic.score_candidate({"name": "海阔天空.mp3"}, "海阔天空"), 1000, "文件名整串相等")
truthy(
    logic.score_candidate({"name": "Beyond - 海阔天空.flac"}, "海阔天空") >= 700,
    "文件名包含歌名",
)
truthy(
    logic.score_candidate({"name": "x.mp3", "title": "晴天 (原唱 周杰伦)", "artist": "RyaVocal"}, "晴天 周杰伦")
    < logic.score_candidate({"name": "y.mp3", "title": "晴天", "artist": "周杰伦"}, "晴天 周杰伦"),
    "翻唱被扣分、正版优先",
)
eq(logic.score_candidate({"name": "无关.mp3"}, "海阔天空", fuzzy=False), 0, "关掉模糊匹配")

best = logic.pick_best(
    [
        {"name": "海阔天空.mp3", "title": "", "artist": "", "ext": "mp3", "mtime_ms": 1},
        {"name": "海阔天空.flac", "title": "", "artist": "", "ext": "flac", "mtime_ms": 1},
    ],
    "海阔天空",
)
eq(best["candidate"]["name"], "海阔天空.flac", "同分时音质好的优先")

truthy(logic.is_ambiguous([{"score": 990}, {"score": 960}]), "分差小 → 需要问")
eq(logic.is_ambiguous([{"score": 990}, {"score": 400}]), False, "分差大 → 不用问")

merged = logic.with_local_question(
    [
        {"id": "file:/a.mp3", "name": "海阔天空", "source": "local", "score": 990},
        {"id": "123", "name": "海阔天空", "source": "netease", "score": 950},
    ]
)
eq([item["source"] for item in merged["list"]], ["local", "action", "netease"], "本地+新下载+远端")
eq(logic.with_local_question([{"id": "123", "source": "netease"}])["asked_local"], False, "只有远端时不插动作项")

text = logic.format_choice_list(
    "海阔天空",
    [
        {"name": "海阔天空", "artist": "Beyond", "duration_ms": 239000, "fee": 1},
        {"name": "下载一份新的（不用本地这份）", "source": "action"},
    ],
)
truthy("1. 海阔天空 — Beyond · 3:59 · VIP" in text, "列表格式：歌手/时长/VIP", text)
truthy("2. 下载一份新的" in text, "动作项行", text)
eq(logic.fee_label(None), "", "fee=None 不编造")
eq(logic.fee_label(1), "VIP", "fee=1")
eq(logic.format_duration(239000), "3:59", "时长格式化")

# ───────────────────────────────────────────── 解锁指令

print("· 解锁指令")
cmd = logic.parse_unlock_command("解除点歌限制")
eq((cmd["hit"], cmd["scope"]), (True, "chat"), "默认范围=会话")
eq(logic.parse_unlock_command("解除点歌限制 本群")["scope"], "chat", "本群")
eq(logic.parse_unlock_command("解除点歌限制 我")["scope"], "self", "我")
eq(logic.parse_unlock_command("解除点歌限制 我的")["scope"], "self", "我的（长写法优先）")
eq(logic.parse_unlock_command("解除限制 所有群")["scope"], "all", "所有群")
cmd = logic.parse_unlock_command("解除点歌限制 12345678")
eq((cmd["scope"], cmd["target_id"]), ("user", "12345678"), "指定 QQ")
eq(logic.parse_unlock_command("解除点歌限制 10分钟")["unlimited_ms"], 600000, "10 分钟")
eq(logic.parse_unlock_command("解除点歌限制 2小时")["unlimited_ms"], 7200000, "2 小时")
eq(logic.parse_unlock_command("解除点歌限制 本群 30分钟")["unlimited_ms"], 1800000, "范围+时长")
eq(logic.parse_unlock_command("限制好多啊")["hit"], False, "闲聊不触发")
eq(logic.parse_unlock_command("清除点歌冷却")["hit"], True, "同义指令")

# 配置里的自定义触发词（原版这个键是死的，移植时接上了）
cmd = logic.parse_unlock_command("解锁点歌 本群 5分钟", keywords=["解锁点歌"])
eq((cmd["hit"], cmd["scope"], cmd["unlimited_ms"]), (True, "chat", 300000), "自定义触发词 + 范围 + 时长")
eq(
    logic.parse_unlock_command("解锁点歌", keywords=["解锁", "解锁点歌"])["matched"],
    "解锁点歌",
    "长词优先（「解锁」不该抢走「解锁点歌」）",
)
eq(
    logic.parse_unlock_command("解锁点歌", keywords=["解锁点歌"])["hit"],
    True,
    "自定义触发词单独也认得",
)
eq(logic.parse_unlock_command("点歌 海阔天空", keywords=[])["hit"], False, "没配置时不误伤点歌")

# ───────────────────────────────────────────── 限流闸 / 待选 / 豁免

print("· 限流与状态表")


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value


clock = Clock()
gate = logic.RateGate(now=clock)


class Cfg:
    cooldown_sec = 20
    max_per_chat_hour = 2
    max_per_hour = 3
    max_per_user_hour = 1


cfg = Cfg()
eq(gate.check("group:1", "u1", cfg)["ok"], True, "第一次放行")
gate.commit("group:1", "u1")
result = gate.check("group:1", "u1", cfg)
eq(result["ok"], False, "冷却生效")
truthy("等" in result["reason"], "冷却原因带等待时间")
clock.value += 21
eq(gate.check("group:1", "u2", cfg)["ok"], True, "冷却过后放行")
gate.commit("group:1", "u2")
clock.value += 21
result = gate.check("group:1", "u2", cfg)
truthy("同一个人每小时" in result["reason"] or "这个会话每小时" in result["reason"], "配额生效", result["reason"])
wiped = gate.reset("group:1", "u1", "chat")
eq(wiped["chat"], 1, "清会话记录")
eq(gate.check("group:1", "u3", cfg)["ok"], True, "清完立刻放行")

clock = Clock()
pending = logic.PendingChoices(ttl_sec=180, grace_sec=60, now=clock)
pending.put("group:1", "u1", {"song": "a", "choices": choices})
truthy(pending.peek("group:1", "u1") is not None, "本人可探测")
truthy(pending.peek("group:1", "u2") is None, "宽限期内别人探测不到")
clock.value += 61
truthy(pending.peek("group:1", "u2") is not None, "宽限期后别人可接")
eq(pending.take("group:1", "u2")["song"], "a", "取一次")
eq(pending.take("group:1", "u2"), None, "取过就没了")
pending.put("group:1", "u1", {"song": "b"})
clock.value += 181
eq(pending.peek("group:1", "u1"), None, "超时失效")

clock = Clock()
exempt = logic.Exemptions(now=clock)
exempt.grant("group:1", "u1", "chat", 600)
truthy(exempt.check("group:1", "u2")["exempt"], "会话豁免对所有人都生效")
clock.value += 601
eq(exempt.check("group:1", "u2")["exempt"], False, "豁免到期")
exempt.grant("group:2", "u1", "user", 60)
truthy(exempt.check("group:9", "u1")["exempt"], "user 豁免跨会话")

cache = logic.TtlCache(ttl_sec=1, now=clock)
cache.set("k", "v")
eq(cache.get("k"), "v", "缓存命中")
clock.value += 1.5
eq(cache.get("k"), None, "缓存过期")

# ───────────────────────────────────────────── ID3 / 时长


def synchsafe(value: int) -> bytes:
    return bytes([(value >> 21) & 0x7F, (value >> 14) & 0x7F, (value >> 7) & 0x7F, value & 0x7F])


def id3v2_file(path: Path, title: str, artist: str) -> None:
    frames = b""
    payload = b"\x03" + title.encode("utf-8")
    frames += b"TIT2" + len(payload).to_bytes(4, "big") + b"\x00\x00" + payload
    payload = b"\x01" + artist.encode("utf-16")  # 带 BOM
    frames += b"TPE1" + len(payload).to_bytes(4, "big") + b"\x00\x00" + payload
    header = b"ID3\x03\x00\x00" + synchsafe(len(frames))
    path.write_bytes(header + frames + b"\x00" * 1024)


def id3v1_file(path: Path, title: str, artist: str) -> None:
    def field(value: str, size: int) -> bytes:
        raw = value.encode("gbk")
        return raw[:size].ljust(size, b"\x00")

    body = b"\x00" * 2048
    tag = b"TAG" + field(title, 30) + field(artist, 30) + field("album", 30) + b"2001" + b"\x00" * 30 + b"\x0c"
    path.write_bytes(body + tag)


print("· ID3 与时长")
with tempfile.TemporaryDirectory() as temp_dir:
    temp = Path(temp_dir)

    v2 = temp / "v2.mp3"
    id3v2_file(v2, "海阔天空", "Beyond")
    tags = library.read_audio_tags(str(v2))
    eq((tags["title"], tags["artist"]), ("海阔天空", "Beyond"), "ID3v2 中文")

    v1 = temp / "v1.mp3"
    id3v1_file(v1, "光辉岁月", "Beyond")
    tags = library.read_audio_tags(str(v1))
    eq((tags["title"], tags["artist"]), ("光辉岁月", "Beyond"), "ID3v1 GBK")

    # 60 秒 128kbps：帧头 0xFF 0xFB 0x90 0x00
    mp3 = temp / "long.mp3"
    size = 960_000
    mp3.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * (size - 4))
    seconds = library.estimate_audio_seconds(str(mp3))
    check(55 <= seconds <= 65, "MP3 时长估算", "(得到 {})".format(seconds))

    empty = temp / "empty.mp3"
    empty.write_bytes(b"\x00" * 4096)
    eq(library.read_audio_tags(str(empty)), {"title": "", "artist": ""}, "无标签不报错")

    # 曲库扫描
    sub = temp / "music" / "Beyond"
    sub.mkdir(parents=True)
    (sub / "海阔天空.mp3").write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 40_000)
    (sub / "cover.jpg").write_bytes(b"\x00" * 40_000)
    (sub / "tiny.mp3").write_bytes(b"\x00" * 100)
    files = library.load_library([temp / "music"], max_depth=3, max_files=100)
    eq([Path(item["name"]).name for item in files], ["海阔天空.mp3"], "扫描只收够大的音频")
    tagged = library.with_tags(files, logic.TtlCache(ttl_sec=60, max_size=10))
    eq(tagged[0]["title"], "", "空标签")
    # 时间预算用完时仍要返回全部候选（只是没有标签，文件名匹配照旧）
    starved = library.with_tags(files, None, budget_sec=0)
    eq(len(starved), len(files), "预算为 0 时仍然返回候选")
    eq(starved[0].get("title"), "", "预算用完不读标签")
    (temp / "music" / "x.ncm").write_bytes(b"\x00")
    truthy(library.has_encrypted_ncm([temp / "music"]), "识别 .ncm")

    # 下载缓存列举与去重
    cache_dir = temp / "cache"
    cache_dir.mkdir()
    audio = b"\xff\xfb\x90\x00" + b"\x01" * 200_000
    (cache_dir / "海阔天空 [1].mp3").write_bytes(audio)
    (cache_dir / "海阔天空 [2].mp3").write_bytes(audio)  # 内容完全相同
    (cache_dir / "海阔天空 [1].mp3.part").write_bytes(b"x")
    (cache_dir / "其它 [3].mp3").write_bytes(b"\xff\xfb\x90\x00" + b"\x02" * 200_000)
    # 用户自己放进来的文件（没有 [id] 尾巴）：去重必须完全不碰它
    (cache_dir / "我的收藏.mp3").write_bytes(audio)
    listed = netease.list_cached(cache_dir)
    eq(len(listed), 4, "缓存列举（排除 .part）")
    dedupe = netease.dedupe_cache(cache_dir)
    eq(len(dedupe["removed"]), 1, "内容相同的副本被清理")
    eq(len([p for p in cache_dir.iterdir() if p.suffix == ".mp3"]), 3, "每组至少留一个")
    truthy((cache_dir / "我的收藏.mp3").is_file(), "非本插件命名的文件绝不删")

# ───────────────────────────────────────────── cookie / 文件名

print("· cookie 与文件名")
values = netease.parse_manual_cookie("MUSIC_U=abcdefgh123; __csrf=zzzzzzzz9")
eq(values.get("MUSIC_U"), "abcdefgh123", "手填 cookie")
values = netease.parse_cookie_text('{"MUSIC_U":"abcdefgh123","__csrf":"zzzzzzzz9"}')
eq(values.get("MUSIC_U"), "abcdefgh123", "从客户端文本抠 cookie")
eq(netease.resolve_cookie(manual="MUSIC_U=abcdefgh123")["source"], "设置里手填", "手填优先")
truthy(netease.resolve_cookie(manual="", allow_client=False)["error"], "关闭自动读取时报错")
eq(netease.safe_file_name('a/b:c*?"<>|d'), "a b c d", "文件名安全化")
eq(netease.base_name_of("海阔天空 [3342981041].mp3"), "海阔天空", "去 id 尾巴")
eq(Path(netease.default_download_dir(Path("."))).name, "点歌缓存", "默认下载目录")

# ───────────────────────────────────────────── download 流程


class FakeCfg:
    match_min_score = logic.NET_MIN_SCORE
    search_limit = 5
    fuzzy = True
    max_choices = 8
    always_ask = True
    ask_when_ambiguous = True
    ambiguity_ratio = 0.92
    reuse_downloaded = True
    download_max_mb = 25
    quality_key = "exhigh"
    download_timeout_sec = 5
    cookie_secret = "MUSIC_U=abcdefgh123"
    use_client_cookie = False


class FakeClient:
    def __init__(self, songs, rate_limited=False):
        self.songs = songs
        self.rate_limited = rate_limited
        self.calls = 0
        self.downloaded = []

    async def search_songs(self, keyword, *, limit=5, timeout_sec=12.0):
        self.calls += 1
        if self.rate_limited:
            return {"ok": False, "rate_limited": True, "error": "限流"}
        return {"ok": True, "songs": self.songs, "endpoint": "fake"}

    async def song_url(self, song_id, *, cookie="", quality="exhigh", timeout_sec=12.0):
        return {"ok": True, "url": "https://example.invalid/a.mp3", "size": 1024}

    async def download_to(self, url, dest, *, cookie="", max_bytes=0, timeout_sec=0):
        # 体积要过「太小当作封面」那道闸（32KB），否则缓存列举会跳过它
        payload = b"\xff\xfb\x90\x00" + b"\x00" * 64_000
        Path(dest).write_bytes(payload)
        self.downloaded.append(str(dest))
        return {"ok": True, "bytes": len(payload)}


async def download_tests():
    print("· 下载流程")
    original = (netease.search_songs, netease.song_url, netease.download_to)
    fake = FakeClient(
        [
            {"id": "1", "name": "海阔天空", "artist": "Beyond", "album": "", "duration_ms": 239000, "fee": 1},
            {"id": "2", "name": "海阔天空", "artist": "G.E.M.邓紫棋", "album": "", "duration_ms": 267000, "fee": 1},
        ]
    )
    netease.search_songs = fake.search_songs
    netease.song_url = fake.song_url
    netease.download_to = fake.download_to
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            result = await download.download_song(FakeCfg(), "海阔天空", temp)
            truthy(result.get("need_choice"), "同名两首 → 需要选择", result)
            eq(len(result["choices"]), 2, "两个候选")

            result = await download.download_song(
                FakeCfg(), "海阔天空", temp, pick="1", choices=result["choices"]
            )
            truthy(result.get("ok"), "选定后下载成功", result)
            truthy(Path(result["path"]).exists(), "文件落地")

            # 缓存命中：always_ask=False 时不该联网
            quiet = FakeCfg()
            quiet.always_ask = False
            before = fake.calls
            result = await download.download_song(quiet, "海阔天空", temp)
            truthy(result.get("ok") and result.get("from_cache"), "缓存命中直接返回", result)
            eq(fake.calls, before, "缓存命中不联网")

            # 最低分闸：不相干结果要被挡掉（用一个干净的缓存目录，避免本地命中先接管）
            empty_cache = temp / "cold_cache"
            empty_cache.mkdir()
            fake.songs = [{"id": "9", "name": "完全不同的歌", "artist": "某人", "album": "", "duration_ms": 1, "fee": 0}]
            result = await download.download_song(FakeCfg(), "海阔天空", empty_cache)
            eq(result.get("ok"), False, "不相干结果不放行")
            truthy("够像" in (result.get("error") or ""), "错误说明清楚", result)

            # 限流要说清楚是限流，不是「不存在」
            fake.songs = []
            fake.rate_limited = True
            result = await download.download_song(FakeCfg(), "海阔天空", empty_cache)
            truthy("限流" in (result.get("error") or ""), "限流与没结果分开报告", result)
    finally:
        netease.search_songs, netease.song_url, netease.download_to = original


asyncio.run(download_tests())

# ───────────────────────────────────────────── 配置模型（假 pydantic）


async def config_tests():
    print("· 配置")
    cfg = config_module.VoiceSongConfig()
    eq(cfg.trigger_list, ["点歌", "来一首", "来首", "放一首", "放首", "我想听", "想听"], "默认触发词")
    eq(cfg.self_name_list, [], "默分别名空")
    eq(cfg.quality_key, "exhigh", "默认音质")
    eq(config_module.VoiceSongConfig(quality="nope").quality_key, "exhigh", "非法音质回落")
    eq(config_module.VoiceSongConfig(admin_qqs="123456, abc, 654321").admin_qq_list, ["123456", "654321"], "管理员名单过滤")
    eq(
        config_module.VoiceSongConfig(unlock_command="解除限制|解锁").unlock_keyword_list,
        ["解除限制", "解锁"],
        "解锁触发词拆分",
    )
    eq(
        config_module.VoiceSongConfig(search_directories="D:\\Music\\网易云\nE:\\Songs").search_dir_list,
        ["D:\\Music\\网易云", "E:\\Songs"],
        "目录按行拆分",
    )


asyncio.run(config_tests())

# ───────────────────────────────────────────── 结果

print("")
if FAIL:
    print("FAILED {} 项：".format(len(FAIL)))
    for item in FAIL:
        print("  ✗ " + item)
    sys.exit(1)
print("全部通过：{} 项断言".format(PASS))
