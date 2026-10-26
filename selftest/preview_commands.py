"""看一眼 /点歌状态 与 /点歌乐库 的真实输出（开发时用的样例生成器）。

跑法（在插件目录）：
    python selftest/preview_commands.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN_DIR = HERE.parent if (HERE.parent / "plugin.toml").is_file() else HERE.parent / "netease-voice-song"
sys.path.insert(0, str(HERE / "stubs"))

_PACKAGE = "netvs_preview"
spec = importlib.util.spec_from_file_location(
    _PACKAGE, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)]
)
module = importlib.util.module_from_spec(spec)
sys.modules[_PACKAGE] = module
spec.loader.exec_module(module)
config_module = sys.modules[_PACKAGE + ".config"]


class FakeLogger:
    def bind(self, **kw):
        return self

    def debug(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass

    def exception(self, *a, **k):
        pass


class FakeServices:
    def get(self, name, default=None):
        return default


class FakeHost:
    services = FakeServices()


class FakeCtx:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.logger = FakeLogger()
        self.plugin_host = FakeHost()
        self.app_commands = None


def make_library(root: Path, count: int) -> None:
    root.mkdir(parents=True, exist_ok=True)
    songs = [
        ("海阔天空 - Beyond", "Beyond"),
        ("光辉岁月 - Beyond", "Beyond"),
        ("晴天 - 周杰伦", "周杰伦"),
        ("稻香 - 周杰伦", "周杰伦"),
        ("One Last Kiss - 宇多田ヒカル", "宇多田ヒカル"),
    ]
    for index in range(count):
        name, artist = songs[index % len(songs)]
        path = root / "{} ({}).mp3".format(name, index + 1)
        # 造一个带 ID3v2 标签的最小 mp3（只为了 preview 里能显示歌手）
        title = name.split(" - ")[0]
        frames = b""
        payload = b"\x03" + title.encode("utf-8")
        frames += b"TIT2" + len(payload).to_bytes(4, "big") + b"\x00\x00" + payload
        payload = b"\x03" + artist.encode("utf-8")
        frames += b"TPE1" + len(payload).to_bytes(4, "big") + b"\x00\x00" + payload
        size = len(frames)
        header = b"ID3\x03\x00\x00" + bytes(
            [(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F]
        )
        path.write_bytes(header + frames + b"\x00" * 200_000)


async def main() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        make_library(temp / "music", 42)
        ctx = FakeCtx(temp / "data")
        module.login_store.save(
            temp / "data", {"MUSIC_U": "demo", "__csrf": "demo"}, source="qr"
        )
        config = config_module.VoiceSongConfig(
            search_directories=str(temp / "music"),
            default_scan_dirs=False,
            enable_download=True,
            admin_qqs="123456",
            cookie_secret="",
            use_client_cookie=False,
        )
        await module.plugin.load_handlers[0](ctx=ctx, config=config, logger=ctx.logger)
        module._engine.stats.sent = 12
        module._engine.stats.last_song = "海阔天空"
        module._engine.stats.downloads = 5
        module._engine.stats.cache_hits = 3
        module._engine.stats.last_note = ""

        print("=" * 60)
        print("发 /vs-help")
        print("=" * 60)
        print(module._render_help(module._engine, is_admin=True))
        print()
        print("=" * 60)
        print("群里普通群友发 /vs-status")
        print("=" * 60)
        print(await module._render_status(module._engine, is_admin=False))
        print()
        print("=" * 60)
        print("管理员发 /vs-status")
        print("=" * 60)
        print(await module._render_status(module._engine, is_admin=True))
        print()
        print("=" * 60)
        print("发 /vs-library")
        print("=" * 60)
        print(await module._render_library(module._engine, raw_args="", is_admin=True))
        print()
        print("=" * 60)
        print("发 /vs-library 周杰伦")
        print("=" * 60)
        print(await module._render_library(module._engine, raw_args="周杰伦", is_admin=True))
        print()
        print("=" * 60)
        print("发 /vs-help（图片版，写成文件方便看）")
        print("=" * 60)
        png = module.help_image.render(module._help_blocks(module._engine, is_admin=True))
        if png:
            out = Path.cwd() / "vs-help-preview.png"
            out.write_bytes(png)
            print("已写到 {}（{} 字节）".format(out, len(png)))
        else:
            print("本机没有 Pillow / 中文字体，只有文字版")
        print()
        await module.plugin.shutdown_handlers[0]()


asyncio.run(main())
