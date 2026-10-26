"""框架层离线自测：把插件真正 import 起来，跑端到端流程。

用 ``stubs/neobot_modloader.py`` 当替身（本机装不了 NeoBot），
但它按真实 API 的形状注册，所以能验出：
  · 装饰器 / 生命周期回调有没有写错；
  · 消息规则（rule）与 block 的接管语义；
  · 点歌 → 本地曲库 → 发语音条 + 报歌名；
  · 每次先选 → 候选列表 → 回数字 → 下载 → 发语音条（含「下载一份新的」分支）；
  · 管理员解除限制；非管理员不接管；
  · 工具路径（靠最近一次交互投递）与状态能力。

跑法：python work/out/_test/run_framework_tests.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 既支持发布形态（selftest 在插件目录里），也支持开发形态（selftest 与插件目录并列）
PLUGIN_DIR = HERE.parent if (HERE.parent / "plugin.toml").is_file() else HERE.parent / "netease-voice-song"
sys.path.insert(0, str(HERE / "stubs"))

import pydantic  # noqa: E402 - 确认用的是替身
import neobot_modloader  # noqa: E402

assert "stubs" in getattr(pydantic, "__file__", ""), "必须使用 stubs/pydantic.py"
assert "stubs" in getattr(neobot_modloader, "__file__", ""), "必须使用 stubs/neobot_modloader.py"

# 以包的形式加载插件（相对 import 要能解析）
_PACKAGE = "netvs_framework_test"
spec = importlib.util.spec_from_file_location(
    _PACKAGE, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)]
)
module = importlib.util.module_from_spec(spec)
sys.modules[_PACKAGE] = module
spec.loader.exec_module(module)

logic = sys.modules[_PACKAGE + ".logic"]
netease = sys.modules[_PACKAGE + ".netease"]
config_module = sys.modules[_PACKAGE + ".config"]

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


# ───────────────────────────────────────────── 假宿主


class FakeLogger:
    def __init__(self):
        self.lines = []

    def _record(self, level, message):
        self.lines.append((level, str(message)))

    def bind(self, **kw):
        return self

    def debug(self, message, **kw):
        self._record("debug", message)

    def info(self, message, **kw):
        self._record("info", message)

    def warning(self, message, **kw):
        self._record("warning", message)

    def error(self, message, **kw):
        self._record("error", message)

    def exception(self, message, **kw):
        self._record("exception", message)

    def text(self):
        return "\n".join(line for _, line in self.lines)


class FakeConversation:
    def __init__(self, kind, conversation_id):
        self.kind = kind
        self.id = conversation_id

    def __repr__(self):
        return "Conversation({}:{})".format(self.kind, self.id)


class FakePermissions:
    def __init__(self, admins=()):
        self.admins = {str(item) for item in admins}

    def can(self, user_id, level):
        return str(user_id) in self.admins


class FakeCommandService:
    def __init__(self, admins=()):
        self.permissions = FakePermissions(admins)


class FakeServices:
    def __init__(self, mapping):
        self._mapping = mapping

    def get(self, name, default=None):
        return self._mapping.get(name, default)


class FakeHost:
    def __init__(self, services):
        self.services = services


class FakeCtx:
    def __init__(self, data_dir, logger, admins=()):
        self.data_dir = Path(data_dir)
        self.logger = logger
        self.plugin_host = FakeHost(FakeServices({"command_service": FakeCommandService(admins)}))
        self.sent = []       # [(conversation, payload)]
        self.audio = []      # [(conversation, path)]
        self.audio_fails = 0
        self.send_fails = 0

    def conversation_from_event(self, event):
        if event.get("message_type") == "private" or not event.get("group_id"):
            return FakeConversation("private", str(event.get("user_id")))
        return FakeConversation("group", str(event.get("group_id")))

    async def send(self, conversation, message):
        if self.send_fails:
            raise RuntimeError("模拟发送失败")

        self.sent.append((conversation, message))

    async def send_audio(self, conversation, *, path):
        if self.audio_fails:
            raise RuntimeError("模拟语音发送失败")
        self.audio.append((conversation, Path(path)))


def text_event(text, *, user_id=456, group_id=123, message_id=1, private=False):
    event = {
        "post_type": "message",
        "message_type": "private" if private else "group",
        "user_id": user_id,
        "message_id": message_id,
        "message": [{"type": "text", "data": {"text": text}}],
    }
    if not private:
        event["group_id"] = group_id
    return event


async def drain(host_module):
    while host_module._engine is not None and host_module._engine.tasks:
        await asyncio.gather(*list(host_module._engine.tasks), return_exceptions=True)


def texts(ctx):
    out = []
    for _, payload in ctx.sent:
        if isinstance(payload, str):
            out.append(payload)
        elif isinstance(payload, list):
            for segment in payload:
                if str(segment.get("type")) == "text":
                    out.append(str((segment.get("data") or {}).get("text") or ""))
        else:
            out.append(str(payload))
    return out


def with_config(config, **changes):
    return config_module.VoiceSongConfig(**{**config.__dict__, **changes})


# ───────────────────────────────────────────── 用例


async def main():
    print("· 插件注册")
    plugin = module.plugin
    eq(plugin.name, "netease-voice-song", "插件名")
    eq(plugin.version, "1.7.0", "版本")
    eq(plugin.min_neobot_version, "2.0.0", "最低本体版本")
    rules = [item for item in plugin.messages if item.get("rule") is not None]
    eq(len(rules), 1, "一条带 rule 的消息注册")
    eq(rules[0].get("block"), True, "接管即消费（0 token）")
    eq(rules[0].get("priority"), 100, "接管优先级最高")
    eq(len([item for item in plugin.messages if item.get("rule") is None]), 1, "一条观察者")
    eq([item["name"] for item in plugin.tools], ["play_local_song"], "工具注册")
    check("status.netease-voice-song" in plugin.capabilities, "状态能力注册")
    eq(len(plugin.load_handlers), 1, "on_load")

    print("· plugin.toml 与代码一致")
    manifest = (PLUGIN_DIR / "plugin.toml").read_text(encoding="utf-8")
    check(not manifest.startswith("\ufeff"), "plugin.toml 没有 BOM（有 BOM 会让 TOML 解析失败）")
    check('name = "{}"'.format(plugin.name) in manifest, "清单 name 与 Plugin 一致")
    check('version = "{}"'.format(plugin.version) in manifest, "清单 version 与 Plugin 一致")
    check('min_neobot_version = "{}"'.format(plugin.min_neobot_version) in manifest, "清单 min_neobot_version 一致")
    toml_config = manifest.split("[config]", 1)[1]
    toml_keys = set(re.findall(r"^([a-z_][a-z0-9_]*)\s*=", toml_config, re.MULTILINE))
    model = config_module.VoiceSongConfig()
    model_keys = set(model._field_defaults()) - {"model_config"}
    eq(toml_keys, model_keys, "plugin.toml [config] 的键与配置模型完全一致")

    # 真解析一遍 TOML，并核对「清单默认值 == 模型默认值」，防止两边漂移
    try:
        import tomllib as _toml  # Python 3.11+
    except ImportError:  # 3.9 上跑自测时用 tomli
        try:
            import tomli as _toml  # type: ignore[no-redef]
        except ImportError:
            _toml = None
    if _toml is None:
        print("  （跳过 TOML 解析：本机没有 tomllib/tomli）")
    else:
        data = _toml.loads(manifest)
        eq(data.get("name"), plugin.name, "TOML：name")
        eq(data.get("version"), plugin.version, "TOML：version")
        packed = data.get("config") or {}
        eq(set(packed), model_keys, "TOML：[config] 解析出来的键一致")
        drift = [key for key, value in packed.items() if getattr(model, key, None) != value]
        eq(drift, [], "TOML：清单默认值与模型默认值一致")

    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        music = temp / "music" / "Beyond"
        music.mkdir(parents=True)
        (music / "海阔天空.mp3").write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 200_000)

        logger = FakeLogger()
        ctx = FakeCtx(temp / "data", logger, admins=())
        config = config_module.VoiceSongConfig(
            search_directories=str(temp / "music"),
            default_scan_dirs=False,
            always_ask=False,
            enable_download=False,
            reply_title=True,
            cooldown_sec=0,
            max_per_chat_hour=0,
            max_per_hour=0,
            max_per_user_hour=0,
            download_dir=str(temp / "cache"),
        )

        # 生命周期：on_load / on_startup
        await plugin.load_handlers[0](ctx=ctx, config=config, logger=logger)
        check(module._engine is not None, "on_load 建起引擎")
        await plugin.startup_handlers[0]()

        print("· 接管判定")
        eq(module._message_rule(text_event("点歌 海阔天空")), True, "点歌消息要接管")
        eq(module._message_rule(text_event("我刚点歌了")), False, "陈述句不接管")
        eq(module._message_rule(text_event("今天天气不错")), False, "普通聊天不接管")
        eq(module._message_rule(text_event("解除点歌限制")), False, "非管理员不接管指令")
        eq(module._message_rule(text_event("点歌")), False, "只给触发词不接管（交回模型）")
        eq(module._message_rule(text_event("２", message_id=9)), False, "无待选时数字不接管")
        self_event = text_event("点歌 海阔天空", user_id=10000, message_id=10)
        self_event["self_id"] = 10000
        eq(module._message_rule(self_event), False, "机器人自己发的消息不接管")

        print("· 点歌 → 发语音条")
        event = text_event("点歌 海阔天空", message_id=101)
        check(module._message_rule(event), "命中")
        await module._on_message(event, ctx, config)
        await drain(module)
        eq(len(ctx.audio), 1, "发出一条语音条")
        if ctx.audio:
            eq(ctx.audio[0][1].suffix, ".mp3", "语音文件是拷贝出来的临时副本")
            check(
                str(ctx.audio[0][1]).startswith(str(temp / "data")),
                "临时文件落在插件数据目录",
                str(ctx.audio[0][1]),
            )
        check(any("🎵 《海阔天空》" in line for line in texts(ctx)), "语音条后报歌名", texts(ctx))
        eq(module._engine.stats.sent, 1, "统计 +1")

        print("· 冷却与配额")
        config2 = with_config(config, cooldown_sec=30)
        module._engine.config = config2
        event = text_event("点歌 海阔天空", message_id=102)
        await module._on_message(event, ctx, config2)
        await drain(module)
        eq(len(ctx.audio), 1, "冷却期内不再发")
        check("等" in module._engine.stats.last_note, "冷却原因写进状态", module._engine.stats.last_note)
        module._engine.gate.reset("group:123", "456", "all")
        module._engine.config = config

        print("· 只给触发词（直接调用处理器时不炸）")
        before_audio = len(ctx.audio)
        await module._on_message(text_event("点歌", message_id=103), ctx, config)
        await drain(module)
        eq(len(ctx.audio), before_audio, "没给歌名不发送")

        print("· 发送失败时回一句")
        # 语音发不出去、但文本还能发 —— 这样才看得见提示语
        ctx.audio_fails = 1
        fail_config = with_config(config, reply_on_failure=True)
        module._engine.config = fail_config
        module._engine.gate.reset("group:123", "456", "all")
        before_audio = len(ctx.audio)
        sent_before = len(ctx.sent)
        await module._on_message(text_event("点歌 海阔天空", message_id=104), ctx, fail_config)
        await drain(module)
        ctx.audio_fails = 0
        module._engine.config = config
        eq(len(ctx.audio), before_audio, "发送失败时没有假成功")
        new_lines = [payload for _, payload in ctx.sent[sent_before:] if isinstance(payload, str)]
        check(any("没发出去" in line for line in new_lines), "失败提示语发出去了", new_lines)

        # 失败时也不该占配额
        check(module._engine.gate.check("group:123", "456", config)["ok"], "失败不占发送配额")
        module._engine.gate.reset("group:123", "456", "all")

        print("· 每次先选 → 候选列表")
        cache_dir = temp / "cache"
        cache_dir.mkdir()
        fake_songs = [
            {"id": "1", "name": "海阔天空", "artist": "Beyond", "album": "", "duration_ms": 239000, "fee": 1},
            {"id": "2", "name": "海阔天空", "artist": "G.E.M.邓紫棋", "album": "", "duration_ms": 267000, "fee": 1},
        ]
        original = (netease.search_songs, netease.song_url, netease.download_to)

        async def fake_search(keyword, *, limit=5, timeout_sec=12.0):
            return {"ok": True, "songs": fake_songs, "endpoint": "fake"}

        async def fake_url(song_id, *, cookie="", quality="exhigh", timeout_sec=12.0):
            return {"ok": True, "url": "https://example.invalid/x.mp3", "size": 1000}

        async def fake_download(url, dest, *, cookie="", max_bytes=0, timeout_sec=0):
            Path(dest).write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 64_000)
            return {"ok": True, "bytes": 64_004}

        netease.search_songs = fake_search
        netease.song_url = fake_url
        netease.download_to = fake_download
        try:
            ask_config = with_config(
                config,
                always_ask=True,
                enable_download=True,
                cookie_secret="MUSIC_U=abcdefgh123",
                use_client_cookie=False,
            )
            module._engine.config = ask_config
            module._engine.gate.reset("group:123", "456", "all")
            module._engine.pending.clear()
            before_audio = len(ctx.audio)
            event = text_event("点歌 海阔天空", message_id=201)
            check(module._message_rule(event), "命中")
            await module._on_message(event, ctx, ask_config)
            await drain(module)
            listed = [line for line in texts(ctx) if "回数字选" in line]
            eq(len(listed), 1, "发出候选列表")
            check("1. 海阔天空 — 本地已有" in listed[0], "本地项排第一", listed[0])
            check("2. 下载一份新的（不用本地这份）" in listed[0], "插入「下载新的」动作项", listed[0])
            check("3. 海阔天空 — Beyond · 3:59 · VIP" in listed[0], "远端项带歌手/时长/VIP", listed[0])
            eq(len(ctx.audio), before_audio, "此时还没发歌")

            # 回数字 3 → 选 Beyond 那一条
            reply_event = text_event("3", message_id=202)
            check(module._message_rule(reply_event), "数字回复被接管")
            await module._on_message(reply_event, ctx, ask_config)
            await drain(module)
            eq(len(ctx.audio), before_audio + 1, "选完真的发了语音条")
            check(
                any("Beyond" in line for line in texts(ctx)),
                "报的是选中的那一版",
                texts(ctx),
            )

            # 取过就作废：再回一个数字不该再放
            again = text_event("3", message_id=203)
            eq(module._message_rule(again), False, "待选已作废，数字回归普通聊天")

            # 「下载一份新的」分支：先列网易云版本，再选
            # （先把下载闸清掉：同一首歌 10 分钟冷却本来就是防重复下载的）
            module._engine.gate.reset("group:123", "456", "all")
            module._engine.download_gate.reset("all")
            before_audio = len(ctx.audio)
            event = text_event("点歌 海阔天空", message_id=204)
            await module._on_message(event, ctx, ask_config)
            await drain(module)
            # 缓存里已经有一份了，所以本地项是两条（曲库 1 + 缓存 1），动作项排第 3
            second_list = [line for line in texts(ctx) if "回数字选" in line][-1]
            check("3. 下载一份新的（不用本地这份）" in second_list, "动作项跟着本地项后移", second_list)
            reply_event = text_event("3", message_id=205)  # 动作项
            check(module._message_rule(reply_event), "动作项被接管")
            await module._on_message(reply_event, ctx, ask_config)
            await drain(module)
            remote_only = [line for line in texts(ctx) if "下载一份新的" not in line and "回数字选" in line]
            check(any("1. 海阔天空 — Beyond" in line for line in remote_only), "改列网易云版本", remote_only[-1:])
            eq(len(ctx.audio), before_audio, "动作项本身不放歌")
            final = text_event("2", message_id=206)
            check(module._message_rule(final), "第二次选择被接管")
            await module._on_message(final, ctx, ask_config)
            await drain(module)
            eq(len(ctx.audio), before_audio + 1, "选完发歌")
            check(any("G.E.M.邓紫棋" in line for line in texts(ctx)), "报的是邓紫棋版", texts(ctx)[-2:])
            module._engine.config = config
        finally:
            netease.search_songs, netease.song_url, netease.download_to = original

        print("· 管理员解除限制（机主名单）")
        admin_config = with_config(config, admin_qqs="123456", unlock_default_min=10)
        module._engine.config = admin_config
        module._engine.gate.commit("group:123", "456")
        admin_ctx = FakeCtx(temp / "data", logger, admins=())
        event = text_event("解除点歌限制 本群 10分钟", user_id=123456, message_id=301)
        eq(module._message_rule(event), True, "管理员指令被接管")
        await module._on_message(event, admin_ctx, admin_config)
        await drain(module)
        check(
            any("已解除点歌限制" in line for line in texts(admin_ctx)),
            "回复确认文案",
            texts(admin_ctx),
        )
        check(module._engine.exempt.check("group:123", "999")["exempt"], "会话级豁免生效")
        eq(module._engine.stats.unlocked, 1, "统计 +1")

        # 非管理员：不接管、也不该安静吞掉
        other = text_event("解除点歌限制", user_id=789, message_id=302)
        eq(module._message_rule(other), False, "非管理员不接管")
        module._engine.config = config

        print("· NeoBot 权限体系认管理员")
        perm_ctx = FakeCtx(temp / "data", logger, admins=("999",))
        module._engine.ctx = perm_ctx
        module._engine.config = with_config(config, admin_qqs="")
        check(module._is_admin(module._engine, "999"), "权限体系里的管理员被认可")
        check(not module._is_admin(module._engine, "456"), "普通成员不是管理员")
        event = text_event("解除点歌限制", user_id=999, message_id=303)
        eq(module._message_rule(event), True, "权限体系里的管理员被接管")
        module._engine.ctx = ctx
        module._engine.config = config

        print("· 工具路径（靠最近一次交互投递）")
        module._engine.gate.reset("group:123", "456", "all")
        module._engine.turn_plays.clear()  # 模拟新的一轮
        observer = [item for item in plugin.messages if item.get("rule") is None][0]["handler"]
        await observer(text_event("来一首 海阔天空", message_id=401))
        tool = plugin.tools[0]["handler"]
        before_audio = len(ctx.audio)
        result = await tool("海阔天空", "", ctx=module._engine.ctx, config=config, logger=logger)
        check("已用语音条发出" in result, "工具返回成功文案", result)
        eq(len(ctx.audio), before_audio + 1, "工具确实发了语音条")

        # 一轮只放一首
        result = await tool("海阔天空", "", ctx=module._engine.ctx, config=config, logger=logger)
        check("这一轮已经发过" in result, "同轮第二次被挡住", result)

        # 没有最近交互时给出可执行提示
        module._engine.turn_plays.clear()
        module._engine.last_seen.clear()
        module._engine.recent_chats.clear()
        module._engine.latest_seen = None
        result = await tool("海阔天空", "", ctx=module._engine.ctx, config=config, logger=logger)
        check("点歌 <歌名>" in result, "没有会话可投递时说清怎么办", result)

        print("· 工具路径：多个会话都在聊时不猜")
        module._engine.turn_plays.clear()
        module._engine.gate.reset("group:123", "456", "all")
        module._engine.gate.reset("group:999", "888", "all")
        await observer(text_event("来一首 海阔天空", user_id=456, group_id=123, message_id=403))
        await observer(text_event("今天吃什么", user_id=888, group_id=999, message_id=404))
        before_audio = len(ctx.audio)
        result = await tool("海阔天空", "", ctx=module._engine.ctx, config=config, logger=logger)
        check("点歌 <歌名>" in result, "两个会话都活跃时拒绝猜测", result)
        eq(len(ctx.audio), before_audio, "有歧义时不发歌")

        # 给了 user_id 就用那个人的会话（明确的提示，不是猜）
        module._engine.turn_plays.clear()
        result = await tool("海阔天空", "456", ctx=module._engine.ctx, config=config, logger=logger)
        check("已用语音条发出" in result, "给了 user_id 就能投递", result)
        eq(len(ctx.audio), before_audio + 1, "按 user_id 发到对应会话")
        eq(ctx.audio[-1][0].id, "123", "发到那个人所在的群")

        print("· 会话级状态会剪枝")
        module._engine.turn_plays["group:old"] = module._now() - 601
        module._engine.locks["group:old.idle"] = asyncio.Lock()
        busy = asyncio.Lock()
        await busy.acquire()
        module._engine.locks["group:busy"] = busy
        module._prune_state(module._engine)
        check("group:old" not in module._engine.turn_plays, "过期的「本轮已放」被清掉")
        check("group:old.idle" not in module._engine.locks, "没人用的锁被清掉")
        check("group:busy" in module._engine.locks, "正在用的锁必须留着")
        busy.release()

        print("· 状态能力")
        await observer(text_event("随便说句话", message_id=402))
        status = await plugin.capabilities["status.netease-voice-song"]({})
        eq(status["ok"], True, "能力返回 ok")
        labels = [line["label"] for line in status["lines"]]
        check("触发" in labels and "曲库" in labels, "状态含关键项", labels)
        check(status["stats"]["sent"] >= 1, "统计里能看到已发送数", status["stats"])

        print("· @机器人 开头的消息（at 段不在 text 里，只剩前导空格）")
        at_event = {
            "post_type": "message",
            "message_type": "group",
            "group_id": 123,
            "user_id": 456,
            "message_id": 501,
            "message": [
                {"type": "at", "data": {"qq": "10000"}},
                {"type": "text", "data": {"text": " 点歌 海阔天空"}},
            ],
        }
        eq(module._message_rule(at_event), True, "at 段 + 前导空格的「点歌」也接管")
        module._engine.gate.reset("group:123", "456", "all")
        before_audio = len(ctx.audio)
        await module._on_message(at_event, ctx, config)
        await drain(module)
        eq(len(ctx.audio), before_audio + 1, "at 开头也能正常发歌")

        print("· 私聊")
        module._engine.gate.reset("private:789", "789", "all")
        private_event = text_event("点歌 海阔天空", user_id=789, message_id=502, private=True)
        eq(module._message_rule(private_event), True, "私聊也接管")
        before_audio = len(ctx.audio)
        await module._on_message(private_event, ctx, config)
        await drain(module)
        eq(len(ctx.audio), before_audio + 1, "私聊能发歌")
        if ctx.audio:
            eq(ctx.audio[-1][0].kind, "private", "发到私聊会话")
            eq(ctx.audio[-1][0].id, "789", "会话 id 是对方 QQ")

        print("· 关闭开关后完全放手")
        module._engine.config = with_config(config, enabled=False)
        eq(module._message_rule(text_event("点歌 海阔天空")), False, "关掉后不接管")
        module._engine.config = config

        print("· 卸载")
        await plugin.shutdown_handlers[0]()
        eq(module._engine, None, "on_shutdown 清空引擎")

    print("")
    if FAIL:
        print("FAILED {} 项：".format(len(FAIL)))
        for item in FAIL:
            print("  ✗ " + item)
        sys.exit(1)
    print("全部通过：{} 项断言".format(PASS))


asyncio.run(main())
