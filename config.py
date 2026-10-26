"""插件配置模型。

为什么要有 pydantic 模型：
  · 面板「插件 → 配置」靠它生成表单并做保存前校验；
  · NeoBot 在注入 ``ctx.config`` 前会用这个模型校验一次，越界/写坏的已存值
    回落到默认值，插件不会因为旧配置在 ``on_load`` 里直接进入 ERROR；
  · ``Field(description=...)`` 的说明会优先于 ``plugin.toml`` 里的注释显示。

键名从原 Node 版的 camelCase 改成 snake_case —— NeoBot 的配置文件是 TOML，
社区与面板都按 Python 习惯用小写下划线，混用 camelCase 在面板里很别扭。
对应关系在 README 里列了表。
"""

from __future__ import annotations

import re
from typing import List

from pydantic import BaseModel, ConfigDict, Field


class VoiceSongConfig(BaseModel):
    """点歌发语音条的全部可调项（默认值 = 原插件 1.7.0 的 DEFAULTS）。"""

    model_config = ConfigDict(extra="ignore")

    # ── 总开关 ──
    enabled: bool = Field(
        default=True,
        description="关掉后一条都不发：消息钩子会直接放行，模型照常接话，零副作用。",
    )

    # ── 识别 ──
    triggers: str = Field(
        default="点歌|来一首|来首|放一首|放首|我想听|想听",
        description=(
            "触发词，用 | 或逗号分隔。这些词出现在消息**开头**才算点歌，"
            "例如「点歌」「来一首」「我想听」。改完保存即生效。"
        ),
    )
    self_names: str = Field(
        default="",
        description=(
            "机器人别名，用逗号分隔，可留空。填了之后「小鲸鱼，来一首 xx」这种叫法也能认"
            "（会自动剥掉开头的名字）。@机器人 不用填也能认。"
        ),
    )

    # ── 找歌 ──
    search_directories: str = Field(
        default="",
        description="音乐文件夹，一行一个，可留空。留空 = 只用默认位置与下载缓存。例：D:\\Music\\网易云",
    )
    default_scan_dirs: bool = Field(
        default=True,
        description=(
            "开 = 除了上面填的，还去「我的音乐 / 网易云下载目录 / 桌面 / D:\\CloudMusic」"
            "等常见位置找。"
        ),
    )
    scan_depth: int = Field(
        default=3, ge=0, le=6,
        description="扫描音乐文件夹时往下钻几层（0~6，默认 3）。目录特别深可以调小。",
    )
    max_files: int = Field(
        default=4000, ge=100, le=40000,
        description="一次扫描最多看多少个音频文件（100~40000，默认 4000），防目录太大卡住收消息。",
    )
    fuzzy: bool = Field(
        default=True,
        description=(
            "开 = 歌名不完全一致也能找到（「海阔天空 live」匹配「海阔天空 (Live版)」）。"
            "关 = 只认明确包含歌名的文件，更不容易放错歌。"
        ),
    )

    # ── 自动下载 ──
    enable_download: bool = Field(
        default=True,
        description="开（默认）= 本地找不到时去网易云下载**完整歌曲**再发语音条。关 = 只发本地已有的歌。",
    )
    download_dir: str = Field(
        default="",
        description=(
            "下载缓存目录，留空用默认（我的音乐\\CloudMusic\\点歌缓存）。"
            "下好的歌放这里，下次点同一首直接秒发；这个目录会自动纳入找歌范围。"
        ),
    )
    quality: str = Field(
        default="exhigh",
        description="standard=标准 128kbps；exhigh=较高 320kbps（默认，推荐）；lossless=无损 FLAC。",
    )
    download_max_mb: float = Field(
        default=25, ge=1, le=200,
        description="单个文件超过这个体积就不下（默认 25MB）。用无损音质时建议调到 60 以上。",
    )
    download_timeout_sec: int = Field(
        default=60, ge=5, le=300,
        description="单首歌下载的最长时间（秒），默认 60。网慢可以调大。",
    )
    search_limit: int = Field(
        default=5, ge=1, le=20,
        description="按歌名去网易云搜，取前 N 条再按匹配度挑最像的（默认 5）。",
    )
    reuse_downloaded: bool = Field(
        default=True, description="开（默认）= 缓存目录里已有的歌直接发，不重复下载。"
    )

    # ── 登录态 ──
    login_qr_timeout_sec: int = Field(
        default=180, ge=30, le=900,
        description=(
            "扫码登录二维码的有效期（秒，默认 180）。超时后二维码作废，"
            "需要重新发 /vs-login。"
        ),
    )
    netease_min_interval_sec: float = Field(
        default=1.5, ge=0.5, le=10,
        description=(
            "两次网易云 API 调用之间的最小间隔（秒，默认 1.5，带随机抖动）。"
            "调大更保守、更不容易被限流；调到 0.5 附近会明显增加「操作频繁」的概率。"
        ),
    )
    use_client_cookie: bool = Field(
        default=False,
        description=(
            "旧方式（默认关）：从本机已登录的网易云客户端读 cookie。"
            "现在推荐用 /vs-login 让手机扫码；只有在机器人就跑在你自己电脑上、"
            "且不想扫码时才打开它。"
        ),
    )
    cookie_secret: str = Field(
        default="",
        description=(
            "手动填 cookie（可留空）。留空 = 用上面的自动读取。"
            "要手填就贴「MUSIC_U=...; __csrf=...」这一串。留空不会覆盖已有值。"
        ),
    )

    # ── 同名多首 ──
    always_ask: bool = Field(
        default=True,
        description=(
            "开（默认）= 任何一次点歌都先发一张候选列表，点歌的人回数字之后才真正放歌，"
            "绝不替他决定放哪一版。关 = 只有在多首分不出高下时才问。"
        ),
    )
    ask_when_ambiguous: bool = Field(
        default=True,
        description=(
            "开（默认）= 有几首难分高下时，先发一条带【歌手 · 时长 · 是否 VIP】的列表，"
            "让点歌者回数字选。关 = 直接挑最匹配的那首发。"
        ),
    )
    ambiguity_ratio: float = Field(
        default=0.92, ge=0.5, le=1.0,
        description=(
            "前两名分数差在这个比例以内就认为分不出高下（默认 0.92，即相差 8% 以内就问）。"
            "调小 = 更少发问、更爱自己挑；调到 1 = 只有完全同分才问。"
        ),
    )
    max_choices: int = Field(default=8, ge=2, le=10, description="候选列表最多列几首（2~10，默认 8）。")
    choice_ttl_sec: int = Field(
        default=180, ge=5, le=1800,
        description="列表发出后多久内回数字有效（秒），默认 180（3 分钟）。",
    )

    # ── 管理员解除限制 ──
    unlock_enabled: bool = Field(
        default=True,
        description=(
            "开（默认）= 管理员可以发指令清掉冷却与配额记录，也可以临时开一段「不限制」。"
            "关 = 谁都不能解除（指令会被当成普通聊天）。"
        ),
    )
    admin_qqs: str = Field(
        default="",
        description=(
            "机主/管理员 QQ 号，逗号分隔。填在这里的人可以解除点歌限制。"
            "留空则只认 NeoBot 自己的管理员配置（config.chat.admin_accounts / sub_admin_accounts）。"
        ),
    )
    trust_admin_accounts: bool = Field(
        default=True,
        description=(
            "开（默认）= 另外去问 NeoBot 的权限体系，超级管理员与次级管理员也能解除限制。"
            "关 = 只认上面手填的 QQ 号。"
        ),
    )
    unlock_default_min: int = Field(
        default=10, ge=0, le=1440,
        description="指令不带时长时，默认放开几分钟（默认 10）。填 0 = 指令只清记录、不开豁免。",
    )
    unlock_command: str = Field(
        default="解除点歌限制|解除限制|清除点歌冷却|重置点歌配额|解除点歌限流",
        description=(
            "解除指令的触发词，用 | 分隔。除了内置写法（解除/清除/重置 + 限制/冷却/配额），"
            "这里列的短语也会被当成解除指令，后面同样可以跟范围与时长，"
            "例如「解除点歌限制 本群 30分钟」。⚠️ 别填成「点歌」这类常用词。"
        ),
    )

    # ── 发送 ──
    max_file_mb: float = Field(
        default=20, ge=1, le=200,
        description="超过就跳过不发（默认 20MB）。无损 FLAC 常超过这个值，用无损时记得一起调大。",
    )
    reply_title: bool = Field(
        default=True,
        description="开 = 语音条发完跟一行「🎵 《歌名》— 歌手」，让人知道放的是哪首。",
    )
    reply_on_failure: bool = Field(
        default=False,
        description=(
            "开 = 没找到、下载失败时回一句说明原因。关（默认）= 安静处理（避免有人拿点歌刷屏），"
            "失败原因仍然记在日志里。"
        ),
    )

    # ── 防刷屏 ──
    cooldown_sec: int = Field(
        default=20, ge=0, le=3600,
        description="同一个群/好友两次点歌的最小间隔（秒），默认 20。填 0 = 不冷却。",
    )
    max_per_chat_hour: int = Field(
        default=10, ge=0, le=200,
        description="同一个群/好友每小时最多发几首（默认 10）。填 0 = 不限。",
    )
    max_per_hour: int = Field(
        default=30, ge=0, le=500,
        description="所有会话加起来每小时最多发几首（默认 30）。填 0 = 不限。",
    )
    max_per_user_hour: int = Field(
        default=5, ge=0, le=100,
        description="同一个人每小时最多点几首（默认 5）。填 0 = 不限。",
    )
    download_cooldown_min: int = Field(
        default=10, ge=0, le=1440,
        description="同一首歌在这个时间内不重复下载（分钟，默认 10）。填 0 = 不冷却。",
    )
    download_per_hour: int = Field(
        default=20, ge=0, le=500,
        description="所有会话合计每小时最多下载几首（默认 20）。填 0 = 不限。",
    )

    # ── 内部：不暴露给面板 ──
    match_min_score: int = Field(
        default=430, ge=0, le=1000,
        description=(
            "联网搜索结果的最低分闸（默认 430）。低于它宁可「没找到」也不放错歌。"
            "一般不用改；调低会更容易把不相干的结果放出来。"
        ),
    )

    # ── 派生视图（不是配置项，不会出现在 TOML 里） ──

    @property
    def trigger_list(self) -> List[str]:
        return _split_words(self.triggers, r"[|,，、;；\n]+")

    @property
    def self_name_list(self) -> List[str]:
        return _split_words(self.self_names, r"[,，、|;；\n]+")

    @property
    def search_dir_list(self) -> List[str]:
        from .logic import parse_dirs

        return parse_dirs(self.search_directories)

    @property
    def unlock_keyword_list(self) -> List[str]:
        return _split_words(self.unlock_command, r"[|,，、;；\n]+")

    @property
    def admin_qq_list(self) -> List[str]:
        raw = re.split(r"[,，、;；\s\n]+", str(self.admin_qqs or ""))
        return [item for item in (part.strip() for part in raw) if re.fullmatch(r"\d{5,12}", item)]

    @property
    def quality_key(self) -> str:
        return self.quality if self.quality in ("standard", "exhigh", "lossless") else "exhigh"


def _split_words(value: str, separators: str) -> List[str]:
    return [item for item in (part.strip() for part in re.split(separators, str(value or ""))) if item]
