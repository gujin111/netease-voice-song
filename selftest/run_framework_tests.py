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


class FakeRegistrar:
    """按 PluginCommandRegistrar 的形状记录注册（register 既可直接调用也可当装饰器）。"""

    available = True

    def __init__(self):
        self.commands = {}

    def register(self, command=None, *, prefixed=False, **options):
        name = command if isinstance(command, str) else str(options.get("name") or "")

        def decorate(handler):
            self.commands[name] = dict(options, name=name, handler=handler, prefixed=prefixed)
            return handler

        return decorate


class FakeCtx:
    def __init__(self, data_dir, logger, admins=()):
        self.data_dir = Path(data_dir)
        self.logger = logger
        self.plugin_host = FakeHost(FakeServices({"command_service": FakeCommandService(admins)}))
        self.app_commands = FakeRegistrar()
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
    # 真实 OneBot 事件一定带 self_id（机器人自己的 QQ）；带不带它会影响
    # 「群里没 @ 机器人时该不该由插件兜住指令」这条判定，所以测试也照实给。
    event = {
        "post_type": "message",
        "message_type": "private" if private else "group",
        "user_id": user_id,
        "message_id": message_id,
        "self_id": 10000,
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
    eq(plugin.version, "1.9.3", "版本")
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

        print("· 指令：命名空间（vs-*，不与歌名混）")
        registrar = module._engine.ctx.app_commands
        names = set(registrar.commands)
        expected = {"vs-help", "vs-status", "vs-library", "vs-login", "vs-logout"}
        check(names >= expected, "五个 vs-* 指令都注册了", names)
        check(not any(name.startswith("点歌") for name in names), "没有任何指令以「点歌」开头", names)
        check("vs" in (registrar.commands["vs-help"].get("aliases") or ()), "/vs 是帮助的简写")
        check("vs-lib" in (registrar.commands["vs-library"].get("aliases") or ()), "曲库指令有别名 /vs-lib")
        check("vs-qr" in (registrar.commands["vs-login"].get("aliases") or ()), "登录指令有别名 /vs-qr")
        check("指令" in (registrar.commands["vs-help"].get("description") or ""), "帮助指令有说明")

        print("· 指令：一屏看完所有入口")
        help_text = module._render_help(module._engine, is_admin=True)
        for token in ("/vs-help", "/vs-status", "/vs-library", "/vs-login", "/vs-logout",
                      "/点歌 海阔天空", "解除点歌限制"):
            check(token in help_text, "帮助里有 " + token, help_text[:80])
        check("点歌 / 来一首" in help_text, "帮助里带上了可配置的触发词", help_text[:200])
        check(len(help_text) <= module._CMD_MAX_CHARS, "帮助长度在上限内", len(help_text))
        guest_help = module._render_help(module._engine, is_admin=False)
        check("admin_qqs" in guest_help, "普通群友会看到「找管理员」的提示", guest_help[-120:])

        print("· 指令：状态 与 登录态")
        class Cmd:
            def __init__(self, raw_args="", user_id=456):
                self.raw_args = raw_args
                self.user_id = user_id

        # 关掉自动下载时不该去读 cookie，输出稳定
        plain_cfg = with_config(config, enable_download=False)
        module._engine.config = plain_cfg
        status_text = await module._command_status(Cmd(user_id=456))
        check("点歌发语音条" in status_text, "状态标题", status_text[:60])
        check("曲库：1 首" in status_text, "状态里有曲库数量", status_text)
        check("未启用自动下载" in status_text, "关下载时说明清楚", status_text)
        check("限流：" in status_text, "状态里有防刷屏配置", status_text)

        # 管理员才看得到路径/下载目录
        module._engine.config = with_config(config, enable_download=True, admin_qqs="123456")
        admin_text = await module._command_status(Cmd(user_id=123456))
        check("下载目录：" in admin_text, "管理员看得到下载目录", admin_text)
        guest_text = await module._command_status(Cmd(user_id=456))
        check("下载目录：" not in guest_text, "普通群友看不到路径", guest_text)
        check("登录：" in guest_text, "普通群友仍能看到登录态是否可用", guest_text)
        module._engine.config = config

        print("· 指令：曲库 列表 / 筛选 / 翻页")
        # 普通群友：只看得到数量，看不到文件名（文件名可能带歌手 / 私人录音名）
        guest_lib = await module._command_library(Cmd(""))
        check("曲库 1 首" in guest_lib, "普通群友看得到总数", guest_lib)
        check("1. 海阔天空" not in guest_lib, "普通群友看不到文件名", guest_lib)
        check("只对管理员开放" in guest_lib, "说清楚为什么看不到", guest_lib)

        module._engine.config = with_config(config, admin_qqs="123456")
        try:
            library_text = await module._command_library(Cmd("", user_id=123456))
            check("曲库 1 首" in library_text, "列表显示总数", library_text)
            check("1. 海阔天空" in library_text, "列表里有那首歌", library_text)
            check("/vs-library" in library_text or "翻页" in library_text, "给了翻页/筛选提示", library_text)
            hit_text = await module._command_library(Cmd("海阔", user_id=123456))
            check("筛选「海阔」命中 1 首" in hit_text, "关键词筛选", hit_text)
            miss_text = await module._command_library(Cmd("不存在的歌", user_id=123456))
            check("没有文件名含" in miss_text, "没命中时说清楚", miss_text)
            page_text = await module._command_library(Cmd("第9页", user_id=123456))
            check("1. 海阔天空" in page_text, "越界页码回到最后一页（仍能列出内容）", page_text)
            check("第 1/1 页" not in page_text, "只有一页时不显示页码行", page_text)
            numeric = await module._command_library(Cmd("1987", user_id=123456))
            check(
                "没有文件名含「1987」" in numeric,
                "纯数字当关键词，不是第 1987 页",
                numeric,
            )
        finally:
            module._engine.config = config

        print("· 指令：点和「点歌」开头的消息不再冲突")
        # 旧指令名 → 提示改名（不能被当成「点一首叫《状态》的歌」）
        eq(module._legacy_command_target("/点歌状态"), "vs-status", "旧名 /点歌状态 → vs-status")
        eq(module._legacy_command_target("/点歌登录 取消"), "vs-login", "旧名带参数也认得")
        eq(module._legacy_command_target("/点歌 状态"), "", "带空格的「/点歌 状态」是真点歌，不算旧指令")
        eq(module._legacy_command_target("/点歌海阔天空"), "", "「/点歌海阔天空」不算旧指令")

        legacy = text_event("/点歌状态", user_id=456, message_id=608)
        eq(module._message_rule(legacy), True, "旧指令会被接管（给提示，而不是放歌）")
        audio_before = len(ctx.audio)
        sent_before = len(ctx.sent)
        await module._on_message(legacy, ctx, config)
        await drain(module)
        hint_lines = [p for _, p in ctx.sent[sent_before:] if isinstance(p, str)]
        check(any("/vs-status" in line for line in hint_lines), "提示里给了新指令名", hint_lines)
        eq(len(ctx.audio), audio_before, "旧指令没有被当成点歌放歌")

        # 带空格的「点歌 状态」仍然是点歌请求（歌名就叫「状态」）
        spaced = text_event("/点歌 状态", message_id=609)
        eq(module._message_rule(spaced), True, "「/点歌 状态」当点歌处理")
        check(module._decide(spaced) == "song", "判定为点歌而不是旧指令", module._decide(spaced))

        print("· 指令：vs-* 的接管语义")
        # 群里没 @机器人 → 插件兜住
        no_at = text_event("/vs-status", user_id=456, message_id=601)
        eq(module._message_rule(no_at), True, "群里没 @ 时由插件兜住指令")
        audio_before = len(ctx.audio)
        sent_before = len(ctx.sent)
        await module._on_message(no_at, ctx, config)
        await drain(module)
        new_text = [p for _, p in ctx.sent[sent_before:] if isinstance(p, str)]
        check(any("点歌发语音条" in line for line in new_text), "兜底路径真的回了状态", new_text)
        eq(len(ctx.audio), audio_before, "指令没被当成点歌放歌")

        no_at_lib = text_event("/vs-library", user_id=456, message_id=602)
        eq(module._message_rule(no_at_lib), True, "乐库指令同样兜住")
        sent_before = len(ctx.sent)
        await module._on_message(no_at_lib, ctx, config)
        await drain(module)
        new_text = [p for _, p in ctx.sent[sent_before:] if isinstance(p, str)]
        check(any("曲库 1 首" in line for line in new_text), "乐库兜底输出", new_text)

        no_at_help = text_event("/vs-help", user_id=456, message_id=607)
        eq(module._message_rule(no_at_help), True, "群里没 @ 时帮助指令也兜住")
        sent_before = len(ctx.sent)
        await module._on_message(no_at_help, ctx, config)
        await drain(module)
        new_text = [p for _, p in ctx.sent[sent_before:] if isinstance(p, str)]
        check(any("全部指令" in line for line in new_text), "兜底路径回了帮助正文", new_text)

        # 群里 @了机器人 → 让给命令管线（否则会出现两条回复）
        mentioned = {
            "post_type": "message",
            "message_type": "group",
            "group_id": 123,
            "user_id": 456,
            "message_id": 603,
            "self_id": 10000,
            "message": [
                {"type": "at", "data": {"qq": "10000"}},
                {"type": "text", "data": {"text": " /vs-status"}},
            ],
        }
        eq(module._message_rule(mentioned), False, "@了机器人时应让给命令管线")
        # 私聊不需要 @ → 也让给命令管线
        eq(module._message_rule(text_event("/vs-status", private=True, message_id=604)), False, "私聊让给命令管线")
        # 别的 / 命令：不接管
        eq(module._message_rule(text_event("/help", message_id=605)), False, "别人的指令不接管")
        # 斜杠点歌仍然照常
        slash_song = text_event("/点歌 海阔天空", message_id=606)
        eq(module._message_rule(slash_song), True, "/点歌 歌名 仍走点歌钩子")
        module._engine.gate.reset("group:123", "456", "all")
        before_audio = len(ctx.audio)
        await module._on_message(slash_song, ctx, config)
        await drain(module)
        eq(len(ctx.audio), before_audio + 1, "斜杠点歌真的发了语音条")
        print("· 扫码登录：登录态存储")
        store = module.login_store
        login_dir = temp / "login"
        eq(store.bootstrap(login_dir), None, "没保存过时 bootstrap 为 None")
        eq(store.describe()["logged_in"], False, "初始未登录")
        eq(store.save(login_dir, {"MUSIC_U": "abc123", "__csrf": "csrf9"}, source="qr"), True, "保存成功")
        eq((login_dir / store.LOGIN_FILENAME).is_file(), True, "登录文件落地")
        eq(store.load(login_dir)["cookies"]["MUSIC_U"], "abc123", "读回 cookie")
        eq(store.describe()["logged_in"], True, "describe 认已登录")
        check("abc123" not in str(store.describe()), "describe 不回显 cookie 值")
        eq(store.cookie_string({"A": "1", "B": "2"}), "A=1; B=2", "拼 cookie")
        check(store.age_text(0) == "", "没有时间时不给文案")
        # age_text 用的是墙上时间（time.time），别拿单调时钟去喂它\n        check("分钟前" in store.age_text(store.time.time() - 120), "相对时间")
        store.clear(login_dir)
        eq(store.describe()["logged_in"], False, "登出后清空")

        print("· 扫码登录：Set-Cookie 解析与状态机")
        login = module.netease_login
        parsed = login.cookies_from_set_cookie(
            [
                "MUSIC_U=xyz123; Path=/; Domain=.music.163.com; HttpOnly",
                "__csrf=abc; Path=/",
                "NMTID=zzz",
                "bogus",
            ]
        )
        eq(parsed.get("MUSIC_U"), "xyz123", "取到 MUSIC_U")
        eq(parsed.get("__csrf"), "abc", "取到 __csrf（完整 cookie 更不像脚本）")
        eq(parsed.get("NMTID"), "zzz", "取到 NMTID")
        eq(login.is_logged_in(parsed), True, "判定已登录")
        eq(login.is_logged_in({"__csrf": "x"}), False, "只有 csrf 不算登录")
        eq(login.make_qr_png("https://music.163.com/login?codekey=x") is None, True,
           "本机没装 qrcode 时优雅降级（不抛异常）")

        # 把网络层换成假的：只测状态机，不联网
        real_request = module.netease.request_json
        poll_response = {"json": {"code": 801}}
        unikey_response = {"json": {"code": 200, "unikey": "KEY-1"}}

        async def fake_request(method, url, *, data=None, cookie="", timeout_sec=12.0):
            if "unikey" in url:
                return dict(unikey_response)
            if "client/login" in url:
                return dict(poll_response)
            return {"error": "unexpected url {}".format(url)}

        module.netease.request_json = fake_request
        try:
            got = await login.request_unikey()
            eq(got["ok"], True, "拿到 unikey")
            eq(got["url"], "https://music.163.com/login?codekey=KEY-1", "二维码内容")
            eq((await login.poll_once("KEY-1"))["state"], "waiting", "801 = 等待扫码")
            poll_response = {"json": {"code": 802}}
            eq((await login.poll_once("KEY-1"))["state"], "scanned", "802 = 已扫码待确认")
            poll_response = {"json": {"code": 800}}
            eq((await login.poll_once("KEY-1"))["state"], "expired", "800 = 二维码失效")
            poll_response = {"json": {"code": 803}, "set_cookie": ["MUSIC_U=fromqr; Path=/"]}
            ok_poll = await login.poll_once("KEY-1")
            eq(ok_poll["state"], "success", "803 = 成功")
            eq(ok_poll["cookies"]["MUSIC_U"], "fromqr", "成功时带回 cookie")
            poll_response = {"json": {"code": 803}, "set_cookie": []}
            eq((await login.poll_once("KEY-1"))["state"], "failed",
               "803 但没 cookie 要判失败（不能谎报成功）")
        finally:
            module.netease.request_json = real_request

        print("· 扫码登录：发起 → 扫码 → 保存 → 状态可见")
        module._LOGIN_POLL_SEC = 0
        module._LOGIN_POLL_JITTER_SEC = 0
        sent_lines = []
        images = []

        async def capture_text(value):
            sent_lines.append(value)

        async def capture_image(data, filename):
            images.append((len(data), filename))
            return True

        sender = module._Reply(capture_text, capture_image)
        real_unikey = login.request_unikey
        real_poll = login.poll_once
        real_png = login.make_qr_png
        real_save = store.save
        real_clear = store.clear

        async def fake_unikey(*, timeout_sec=12.0):
            return {
                "ok": True,
                "key": "KEY-2",
                "url": "https://music.163.com/login?codekey=KEY-2",
                # 真实接口会给 Set-Cookie；这一条验证「unikey 下发的 cookie 也会被攒起来」
                "set_cookie": ["__csrf=csrf-from-unikey; Path=/"],
            }

        async def fake_poll_once(key, *, cookie="", timeout_sec=12.0):
            polled_cookies.append(cookie)
            return {
                "state": "success",
                "code": 803,
                "cookies": {"MUSIC_U": "qr-ok", "__csrf": "c1"},
                "message": "登录成功",
            }

        polled_cookies = []

        login.request_unikey = fake_unikey
        login.poll_once = fake_poll_once
        login.make_qr_png = lambda url, **kw: b"\x89PNG-fake"
        store.save = real_save  # 真存，验证落盘
        try:
            module._engine.config = with_config(config, admin_qqs="123456")
            await module._start_login(
                module._engine, sender, requester="123456", chat_key="group:123"
            )
            await drain(module)
            check(any("二维码" in line or "扫码" in line for line in sent_lines), "发了扫码提示", sent_lines)
            eq(len(images), 1, "发了一张二维码图片")
            check(any("登录成功" in line for line in sent_lines), "成功后回了结果", sent_lines)
            eq(store.describe()["logged_in"], True, "登录态已进内存")
            eq((module._engine.data_dir / store.LOGIN_FILENAME).is_file(), True, "登录态已落盘")

            # 登录后：cookie 解析优先级 手填 > 扫码保存 > 客户端文件
            resolved = module.netease.resolve_cookie("", allow_client=False)
            check("扫码" in resolved.get("source", ""), "默认用扫码保存的 cookie", resolved)
            manual = module.netease.resolve_cookie("MUSIC_U=manual1", allow_client=False)
            eq(manual["source"], "设置里手填", "手填优先")

            # 状态里能看到已登录（登录态那几行只在开启自动下载时显示）
            module._engine.config = with_config(config, enable_download=True, admin_qqs="123456")
            status_after = await module._render_status(module._engine, is_admin=True)
            check("已登录" in status_after and "扫码登录" in status_after, "状态显示已登录", status_after)
        finally:
            login.request_unikey = real_unikey
            login.poll_once = real_poll
            login.make_qr_png = real_png

        print("· 扫码登录：权限与登出")
        class CmdCtx:
            def __init__(self, user_id, raw_args="", conv_id="123", kind="group"):
                self.user_id = user_id
                self.raw_args = raw_args
                self.conv_id = conv_id
                self.kind = kind
                self.service = FakeCmdService()

        class FakeCmdService:
            def __init__(self):
                self.texts = []
                self.images = 0

            async def reply_to(self, kind, conv_id, text, *, at_user_id):
                self.texts.append(text)

            async def send_image_bytes(self, kind, conv_id, data, *, at_user_id=None, filename=None):
                self.images += 1
                return True

        guest = await module._command_login(CmdCtx(456))
        check("管理员" in str(guest), "非管理员不能发起扫码登录", guest)

        admin_ctx = CmdCtx(123456)
        real_unikey2 = login.request_unikey
        real_poll2 = login.poll_once
        real_png2 = login.make_qr_png
        login.request_unikey = fake_unikey
        login.poll_once = fake_poll_once
        login.make_qr_png = lambda url, **kw: b"\x89PNG-fake"
        try:
            out = await module._command_login(admin_ctx)
            eq(out, None, "命令路径自己发过消息，返回 None")
            await drain(module)
            check(any("扫码" in line or "二维码" in line for line in admin_ctx.service.texts),
                  "命令路径发了二维码说明", admin_ctx.service.texts)
            eq(admin_ctx.service.images, 1, "命令路径发了二维码图片")
            eq(store.describe()["logged_in"], True, "命令路径也把登录态存下了")
        finally:
            login.request_unikey = real_unikey2
            login.poll_once = real_poll2
            login.make_qr_png = real_png2

        # 取消
        out = await module._command_login(CmdCtx(123456, raw_args="取消"))
        check("取消" in str(out), "「取消」能中断", out)

        # 登出
        out = await module._command_logout(CmdCtx(456))
        check("管理员" in str(out), "非管理员不能登出", out)
        out = await module._command_logout(CmdCtx(123456))
        check("已退出" in str(out), "管理员可以登出", out)
        eq(store.describe()["logged_in"], False, "登出后内存清空")
        eq((module._engine.data_dir / store.LOGIN_FILENAME).is_file(), False, "登出后文件删除")

        print("· 风控节流参数")
        module.netease.set_min_interval(3.0)
        eq(module.netease.risk_status()["min_interval_sec"], 3.0, "节流间隔可配置")
        module.netease.set_min_interval(0.1)
        eq(module.netease.risk_status()["min_interval_sec"], 0.5, "过小的间隔被夹到 0.5")
        module.netease.set_min_interval(1.5)
        eq(module.netease.risk_status()["risk_cooldown_left"], 0, "初始没有风控冷却")

        print("· 关闭开关后完全放手")
        module._engine.config = with_config(config, enabled=False)
        eq(module._message_rule(text_event("点歌 海阔天空")), False, "关掉后不接管")
        module._engine.config = config

        print("· 回归：/点歌帮助 等旧指令名直接执行（不再被当成歌名）")
        import inspect as _inspect

        check(
            _inspect.iscoroutinefunction(module._command_help),
            "命令处理器是协程函数（宿主是 await 调用，同步函数会报 can't be used in 'await'）",
        )
        for legacy_name, expect in (
            ("点歌帮助", "全部指令"),
            ("点歌状态", "点歌发语音条"),
            ("点歌乐库", "曲库"),
        ):
            legacy_event = text_event("/" + legacy_name, user_id=456, message_id=700 + len(legacy_name))
            eq(module._message_rule(legacy_event), True, "旧名 /{} 被接管".format(legacy_name))
            sent_before = len(ctx.sent)
            audio_before = len(ctx.audio)
            await module._on_message(legacy_event, ctx, config)
            await drain(module)
            new_text = [p for _, p in ctx.sent[sent_before:] if isinstance(p, str)]
            check(any("改名为" in line for line in new_text), "旧名给了改名说明", new_text)
            check(any(expect in line for line in new_text), "旧名真的执行了对应指令", new_text)
            eq(len(ctx.audio), audio_before, "旧名没有被当成点歌放歌")

        # 旧名 /点歌登录 也不能绕过管理员校验（这是安全边界，不是文案问题）
        sent_before = len(ctx.sent)
        login_event = text_event("/点歌登录", user_id=456, message_id=760)
        await module._on_message(login_event, ctx, config)
        await drain(module)
        login_text = [p for _, p in ctx.sent[sent_before:] if isinstance(p, str)]
        check(any("管理员" in line for line in login_text), "非管理员用旧名也被拒", login_text)
        eq(module._engine.login, None, "没有因此起一个登录流程")

        print("· 回归：关掉自动下载就一条网都不联")
        calls = {"search": 0}
        real_search = netease.search_songs

        async def counting_search(keyword, *, limit=5, timeout_sec=12.0):
            calls["search"] += 1
            return {"ok": True, "songs": [], "endpoint": "fake"}

        netease.search_songs = counting_search
        try:
            off_cfg = with_config(
                config,
                always_ask=True,
                enable_download=False,
                reply_on_failure=True,
                cooldown_sec=0,
                max_per_chat_hour=0,
                max_per_hour=0,
                max_per_user_hour=0,
            )
            module._engine.config = off_cfg
            module._engine.pending.clear()
            module._engine.index_cache.clear()
            sent_before = len(ctx.sent)
            audio_before = len(ctx.audio)
            event = text_event("点歌 海阔天空", message_id=810)
            await module._on_message(event, ctx, off_cfg)
            await drain(module)
            eq(calls["search"], 0, "关掉自动下载后一次搜索都没发")
            eq(len(ctx.audio), audio_before, "也没有发歌")
            listed = [p for _, p in ctx.sent[sent_before:] if isinstance(p, str) and "回数字选" in p]
            check(bool(listed), "仍然把本地候选列出来了", listed)
            check("本机找到" in listed[-1], "文案说清楚只有本地，不承诺下载", listed[-1:])
        finally:
            netease.search_songs = real_search

        print("· 回归：默认配置下 max_file_mb 仍然管用（选择路径）")
        big = music / "大地.mp3"
        big.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * (2 * 1024 * 1024))
        module._engine.index_cache.clear()
        cap_cfg = with_config(
            config,
            always_ask=True,
            enable_download=False,
            reply_on_failure=True,
            max_file_mb=1,
            cooldown_sec=0,
            max_per_chat_hour=0,
            max_per_hour=0,
            max_per_user_hour=0,
        )
        module._engine.config = cap_cfg
        module._engine.pending.clear()
        event = text_event("点歌 大地", message_id=820)
        await module._on_message(event, ctx, cap_cfg)
        await drain(module)
        sent_before = len(ctx.sent)
        audio_before = len(ctx.audio)
        await module._on_message(text_event("1", message_id=821), ctx, cap_cfg)
        await drain(module)
        eq(len(ctx.audio), audio_before, "超过 max_file_mb 的文件没发出去")
        check(
            any("超过上限" in p for _, p in ctx.sent[sent_before:] if isinstance(p, str)),
            "回了一句「超过上限」",
            [p for _, p in ctx.sent[sent_before:]],
        )
        big.unlink()

        print("· 回归：解除限制「全部」覆盖别的会话 / 下载闸按会话记账")
        module._engine.exempt.clear()
        admin_cfg = with_config(config, admin_qqs="123456", unlock_default_min=10)
        module._engine.config = admin_cfg
        event = text_event("解除点歌限制 全部 10分钟", user_id=123456, message_id=830)
        await module._on_message(event, ctx, admin_cfg)
        await drain(module)
        check(module._engine.exempt.check("group:999", "777")["exempt"], "别的会话也被豁免")

        gate = module.DownloadGate()
        dcfg = config_module.VoiceSongConfig(download_per_hour=2, download_cooldown_min=10)
        gate.commit(dcfg, "海阔天空", "group:1")
        gate.commit(dcfg, "晴天", "group:1")
        gate.reset("chat", "group:1")
        eq(gate.check(dcfg, "海阔天空", "group:1")["ok"], True, "本群那首歌的「刚下过」被清掉")
        gate.reset("all")
        gate.commit(dcfg, "海阔天空", "group:1")
        gate.commit(dcfg, "晴天", "group:1")
        eq(gate.check(dcfg, "随便一首", "group:2")["ok"], False, "别的群仍受全局每小时额度约束")
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
