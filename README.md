# 点歌发语音条（netease-voice-song）· NeoBot 2.0 插件

群里喊一声「点歌 歌名」，机器人就找出那首歌，以 **QQ 语音条**的形式发出来，
后面再跟一行歌名歌手。

本目录是原来 **DSH / qq-agent 的 Node 版插件**（`index.js` + `logic.js` + `netease-client.js`）
移植到 **NeoBot 2.0 插件 API** 的结果。打分公式、限流闸、同名多首的待选规则
都按原实现搬过来（连注释里那些「为什么是这个数字」也一起搬了），
挂载方式换成了 NeoBot 的 `@plugin.message` + `block`。

- **0 token**：命中「点歌」的消息由插件直接接管，不进回复管线、不产生模型会话；
- **本地优先**：先扫本机音乐库，没有再去网易云下载**完整歌曲**（不是试听片段）；
- **不替你猜**：同名多首时先发候选列表（歌手 · 时长 · 是否 VIP），回数字才放；
- **不碰你的文件**：只读曲库；只有「下载缓存目录」里的重复副本会被去重清理。

---

## 安装

### 方式 A：面板从 GitHub 安装（推荐）

NeoBot 的插件管理支持直接从 GitHub 仓库装插件，这个仓库的**根目录就是插件目录**
（`plugin.toml` 在根上），所以两者可以直接配合：

1. 打开网页面板 →「插件」→「安装插件」；
2. 仓库填 `gujin111/netease-voice-song`（也接受完整地址
   `https://github.com/gujin111/netease-voice-song`，或 `gujin111/netease-voice-song@main` 指定分支）；
3. 分支留空即用默认分支 `main`；
4. 安装完成后到「插件 → netease-voice-song」点**启动**，需要时在「配置」里调参数。

装好之后面板里会多一个「检查更新 / 更新此插件」——它读的是 `plugin.toml` 里的
`repo` 与 `branch` 字段（本仓库已填好），所以升级只要点一下。

> 仓库必须是 **public**：NeoBot 的安装器用 `codeload.github.com` 匿名下载 zip，
> 不支持带 token 访问私有仓库。

### 方式 B：手动拷贝

把整个 `netease-voice-song/` 目录放进 NeoBot **数据目录**下的 `plugins/`
（`config.plugins.dir`，默认 `data/plugins/`）：

```text
data/
  plugins/
    netease-voice-song/
      plugin.toml
      __init__.py
      logic.py
      library.py
      netease.py
      download.py
      config.py
  plugins_data/            ← 插件自己的运行数据（配置、缓存、临时文件）
    netease-voice-song/
      config.toml
```

然后重启 NeoBot，或到面板「插件」页点**启用 / 重载**。

### 装完之后

到「插件 → netease-voice-song → 配置」按需调参数（保存后会**重载本插件**，
因为插件在 `on_load` 时把配置读进了内存）。

依赖：只需要 `httpx`（NeoBot 本体已经依赖它）。`plugin.toml` 里没有声明
`python_dependencies`，所以安装时不会去联网装包。

---

## 怎么用

### 点歌

这些说法都认（触发词必须出现在消息**开头**）：

```text
点歌 海阔天空
来一首 海阔天空
/点歌 海阔天空
小鲸鱼，来一首《海阔天空》      ← 需要先在配置里填机器人别名
@机器人 点歌 海阔天空
```

@机器人、叫它的名字、加书名号/标点都不影响识别。只发「点歌」不给歌名时，
插件**不接管**，交给模型正常接话（它通常会问你想听哪首）。

### 同名多首

同名歌难分高下时（比如《海阔天空》有 Beyond 原版、邓紫棋版、各种 Live），
机器人**不替你猜**，而是发一条列表：

```text
「海阔天空」找到 3 首，回数字选一个：
1. 海阔天空 — Beyond · 3:59 · VIP
2. 海阔天空 — G.E.M.邓紫棋 · 4:27 · VIP
3. 海阔天空(Live) — 黄贯中/叶世荣/黄家强 · 6:07 · VIP(高音质)
```

- 回一个数字（`2`、`第2首`、`选2`、`２` 都认），或者回歌手名（`Beyond`，唯一命中时才认）；
- 列表发出后 3 分钟内有效，这期间的回复由插件自己接管，同样不花 token；
- 本机已经有这首歌时，列表里会把**本地那份**排在最前，并多一条
  「下载一份新的（不用本地这份）」让你选。

`always_ask`（默认开）打开时，**每一次**点歌都会先列候选；关掉后只有
分不出高下时才问，平时直接挑最匹配的那首。

### 管理员解除限制

```text
解除点歌限制                → 清本会话的冷却与配额记录
解除点歌限制 本群           → 同上（说清楚一点而已）
解除点歌限制 我             → 清自己
解除点歌限制 12345678       → 清那个人的记录
解除点歌限制 10分钟         → 顺带开 10 分钟不限制
解除点歌限制 本群 30分钟    → 本会话 30 分钟不限制
```

谁算管理员：配置里 `admin_qqs` 手填的号，**或者** NeoBot 自己的
`config.chat.admin_accounts` / `sub_admin_accounts`（`trust_admin_accounts` 控制）。

非管理员说这句话时插件**不接管**——群里有人抱怨「限制好多，解除点歌限制吧」，
应该由模型正常接话，而不是被冷处理。

---

## 配置

配置文件：`data/plugins_data/netease-voice-song/config.toml`（面板可编辑）。
`plugin.toml` 的 `[config]` 只是**打包默认值**，插件更新会覆盖它，但不会覆盖你改过的运行配置。

### 识别

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 关掉后一条都不发（消息钩子直接放行，零副作用）。 |
| `triggers` | `点歌\|来一首\|来首\|放一首\|放首\|我想听\|想听` | 触发词，用 `\|` 或逗号分隔，必须出现在消息开头。 |
| `self_names` | 空 | 机器人别名，逗号分隔。填了「小鲸鱼，来一首 xx」也能认。 |

> **注意**：`enabled` 是插件自己的功能开关；面板上的「启用/停用」是另一套记录
> （`plugin_state.json`）。两个都开才会真的工作。

### 找歌

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `search_directories` | 空 | 音乐文件夹，一行一个。留空 = 只用默认位置与下载缓存。 |
| `default_scan_dirs` | `true` | 同时扫「我的音乐 / 网易云下载目录 / 桌面 / `D:\CloudMusic`」等常见位置。 |
| `scan_depth` | `3` | 往下钻几层（0~6）。 |
| `max_files` | `4000` | 一次扫描最多看多少个音频文件。 |
| `fuzzy` | `true` | 允许歌名不完全一致；关掉更不容易放错歌。 |
| `max_file_mb` | `20` | 超过就跳过不发。用无损音质时记得一起调大。 |

### 自动下载

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enable_download` | `true` | 本地找不到时去网易云下载完整歌曲。 |
| `download_dir` | 空 | 下载缓存目录，留空 = `我的音乐\CloudMusic\点歌缓存`。 |
| `quality` | `exhigh` | `standard`(128k) / `exhigh`(320k) / `lossless`(FLAC)。 |
| `download_max_mb` | `25` | 单个文件体积上限。无损建议 ≥ 60。 |
| `download_timeout_sec` | `60` | 单首歌下载超时。 |
| `search_limit` | `5` | 按歌名搜，取前 N 条再按匹配度挑。 |
| `reuse_downloaded` | `true` | 缓存里已有就直接发，不重复下载。 |
| `match_min_score` | `430` | 联网结果的最低分闸。低于它宁可「没找到」也不放错歌。 |

### 登录态

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `use_client_cookie` | `true` | 从本机已登录的网易云客户端读 cookie（只读、不写、不改客户端）。 |
| `cookie_secret` | 空 | 手动填 `MUSIC_U=...; __csrf=...`。留空 = 用自动读取；面板里只显示「已设置/未设置」。 |

### 同名多首

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `always_ask` | `true` | 每次都先列候选让人选。 |
| `ask_when_ambiguous` | `true` | 分不出高下时才问（`always_ask` 关掉后才有意义）。 |
| `ambiguity_ratio` | `0.92` | 前两名分数差在这个比例内就认为难分高下。 |
| `max_choices` | `8` | 列表最多列几首（2~10）。 |
| `choice_ttl_sec` | `180` | 列表发出后多久内回数字有效。 |

### 管理员解除限制

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `unlock_enabled` | `true` | 允许管理员解除点歌限制。 |
| `admin_qqs` | 空 | 机主/管理员 QQ 号，逗号分隔（5~12 位数字）。 |
| `trust_admin_accounts` | `true` | 同时认 NeoBot 权限体系里的超级/次级管理员。 |
| `unlock_default_min` | `10` | 指令不带时长时默认放开几分钟（0 = 只清记录）。 |
| `unlock_command` | `解除点歌限制\|解除限制\|清除点歌冷却\|重置点歌配额\|解除点歌限流` | 除内置写法外额外识别的触发短语（别填成「点歌」这类常用词）。 |

### 发送与防刷屏

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `reply_title` | `true` | 语音条后再报一句「🎵 《歌名》— 歌手」。 |
| `reply_on_failure` | `false` | 失败/找不到时回一句；默认安静（原因仍记日志）。 |
| `cooldown_sec` | `20` | 同会话冷却。 |
| `max_per_chat_hour` | `10` | 同会话每小时上限。 |
| `max_per_hour` | `30` | 全局每小时上限。 |
| `max_per_user_hour` | `5` | 同一个人每小时上限。 |
| `download_cooldown_min` | `10` | 同一首歌的下载冷却（分钟）。 |
| `download_per_hour` | `20` | 全局每小时下载上限。 |

以上「上限」填 `0` 都表示不限。

---

## 与 Node 版的差异（都是被框架逼出来的，不是随手改的）

| 项 | Node 版 | 本版（NeoBot） | 为什么 |
| --- | --- | --- | --- |
| 挂载点 | 包一层 `Orchestrator.prototype.onIncoming`，命中就不调原方法 | `@plugin.message(rule=…, block=True, priority=100)` | NeoBot 的 HookBus 在 `consume()` 后让 gateway 直接 return，等价于「不排唤醒」 |
| 长耗时工作 | 接管后异步 `handle()`，不阻塞宿主 | 处理器只做同步判定，然后 `asyncio.create_task` | OneBot 事件分发是 `await publish(event)` 的串行循环，在处理器里 await 下载会卡住**所有会话** |
| 配置键名 | camelCase（`maxFileMB`） | snake_case（`max_file_mb`） | NeoBot 的配置文件是 TOML，面板与社区都按 Python 习惯 |
| 配置读取 | 每次调用现读（改设置立刻生效） | `on_load` 时读进内存，保存后面板重载插件 | NeoBot 用 pydantic 校验后注入 `ctx.config`；`config_hot_reload = true` 让面板保存即重载 |
| 状态卡 | `providers['status.netease-voice-song']` | 能力 `status.netease-voice-song` | NeoBot 用能力（capability）把数据开放给别的插件 |
| 管理员名单 | 软查 `permission.check`（admin-roster 提供） | NeoBot 的 `command_service.permissions.can()` | 目标框架没有 admin-roster；`ctx.plugin_host.services` 里能拿到命令服务 |
| 工具认人 | `ctx.session.trigger` 里找最后一条非自己发的消息 | 记录「最近一次交互」（5 分钟内有效），**多个会话都在活跃时不猜、直接拒绝** | **NeoBot 的插件工具没有入站事件**（`event` 恒为 `{}`），这是框架限制；而「最近一次交互」是全局的，照着它发会把语音条发到别的群 —— 所以有歧义就退回「让用户直接发『点歌 歌名』」 |
| 发送失败之后 | `markEntryUnread()` 把消息翻回未读，模型还能补救 | 无法翻回（消息已被 `block` 消费） | NeoBot 的消费在处理器返回时就完成了；`ctx.agent_reply()` 只能在事件分发期间用，后台任务里已经晚了。所以 `reply_on_failure`（默认关）是唯一的提示通道 |
| 工具触发的播放记账 | 发送闸记在空 sender 名下（不占「同一人」配额） | 记在点歌人名下 | 更严一点：防止有人用工具绕过「同一人每小时」；副作用是工具放歌会占该用户的配额 |
| 数值配置 | `clampNumber` 逐项夹到区间 | pydantic 的 `ge/le` 边界，越界值回落默认 | 面板保存前就会校验；写坏的旧配置不会让插件带着 `max_file_mb=-1` 跑 |
| `unlock_command` | 配置项写了但没接线（死键） | 真的生效：除内置写法外额外识别这些词 | 原版两个配置键一直是空的；移植时接上，避免面板里出现「改了没反应」的字段 |
| 读 ID3 标签 | 在事件循环上同步读（几千个文件会卡） | `asyncio.to_thread` + 2 秒时间预算 | 首次点歌时标签缓存是空的；没有预算的话一次能把整个曲库读一遍，期间所有群的消息都停着 |
| 下载闸时机 | 列候选**之前**就查 | 只在真的要下载时查（列候选不查） | 原版在 `always_ask` 下会让「同一首歌 10 分钟内再点一次」被**静默丢掉**，哪怕只是想在缓存里再放一遍 |
| GBK 解码 | 手搓 23940 项码表（Node 的 Buffer 不认 gbk） | `bytes.decode("gbk")` | Python 原生有 codec |
| 临时文件清理 | `setInterval` 每 5 分钟 | 每次发送前 + 启动 + 卸载时清 | 不引入常驻定时器，热重载更干净 |
| 临时目录 | `data/voice-song/tmp` | `data/plugins_data/netease-voice-song/tmp/songs` | NeoBot 给插件的 `ctx.data_dir` |
| 日志 | `data/logs/qq-agent-*.log` | 宿主 logger（`ctx.logger`） | 统一进 NeoBot 日志 |

---

## 前置条件与已知限制

- **语音条要协议端支持 `record` 段**。插件只给一个本地文件路径（或 FileServer 的 HTTP URL），
  真正转 SILK / 上传的是协议端（NapCat、LLOneBot、Lagrange 等通常自带 ffmpeg）。
  如果发出去是「文件」而不是语音，先查协议端的语音/ffmpeg 支持。
- **文件服务器**：`config.file_server.enabled = true`（默认）时走 HTTP URL，机器人可以部署在
  另一台机器上；关掉时走 `file:///` 本地路径，**只有协议端和 NeoBot 在同一台机器上**才可用。
- **网易云登录态**：只读本机客户端的 cookie 文件，不发往除 `music.163.com` 与
  `*.music.126.net` 以外的任何地方，也不写日志。没有登录态时取不到完整歌曲（`code=-110`）。
- 只从网易云取**你自己账号本来就能听**的歌，不绕过任何付费限制。
- 曲库扫描**只读**：不移动、不改名、不写入任何音乐目录。唯一的删除动作是
  「下载缓存目录里的重复副本去重」（保守规则：内容指纹相同、或时长几乎一致才删，每组至少留一个）。
- 「同一首歌下载冷却」默认 10 分钟：10 分钟内再选**同一首歌的另一个版本**会被拦（这是原版就有的防刷
  设计）。觉得碍事就把 `download_cooldown_min` 调成 `0`。
- **发送失败默认是安静的**：`reply_on_failure = false` 时，语音发不出去只会记日志 + 计入状态卡，
  群里看不到提示（原版会回一句「我记日志了」）。要看提示就把它打开；失败原因在
  `status.netease-voice-song` 的 `stats.last_note` 里。
- 模型侧工具 `netease-voice-song__play_local_song` 只是**补充**入口：用户直接发「点歌 xxx」时
  走的是确定性路径，模型根本看不到那条消息。工具只在「最近只有一个会话在聊」或模型显式给了
  `user_id` 时才会投递 —— 其余情况它会回一句让用户直接发「点歌 歌名」。

---

## 排障

| 现象 | 先看什么 |
| --- | --- |
| 完全没反应 | `enabled` 是否为 true；面板里插件是否「运行中」；触发词是否在消息**开头** |
| 一直不接管 | 日志里搜 `点歌`：可能有冷却/配额提示；或歌名是空的（只发了触发词） |
| 「找了歌但没发」 | `stats.last_note`（状态能力里能看到）与日志里的 `跳过 / 没找到 / 下载失败` |
| 说「这首歌刚下过」 | `download_cooldown_min`；或管理员发「解除点歌限制 10分钟」 |
| 下载都失败且提示 `code=-110` | 网易云客户端登录态过期：打开客户端重新登录，插件下一首会自动跟上 |
| 回数字没反应 | 列表超过 `choice_ttl_sec`（默认 3 分钟）就失效了，重新点一次 |
| 收到的是文件不是语音条 | 协议端的 `record` 支持 / ffmpeg |
| 想重置所有状态 | 面板重载插件即可（冷却、待选、豁免都在内存里，重载即清） |

状态卡（给排障用）：

```python
handle = ctx.require_plugin("netease-voice-song")
status = await handle.call("status.netease-voice-song", {})
# status["lines"] 是给人看的行，status["stats"] 是计数
```

---

## 目录结构

仓库根目录**就是插件目录**（`plugin.toml` 在根上）—— NeoBot 的「从 GitHub 安装」
要求压缩包里只有一个含 `plugin.toml` 的目录，这样布局能直接装上。

```text
netease-voice-song/           ← 仓库根 = 插件目录
  plugin.toml                 清单 + 打包默认配置（键上方的注释会被面板渲染成字段说明）
  __init__.py                 Plugin 定义：消息钩子、工具、状态能力、生命周期
  logic.py                    纯逻辑：解析 / 打分 / 限流 / 待选表（只用标准库）
  library.py                  本机曲库扫描 + ID3 读取（ID3v2 / ID3v1 含 GBK）
  netease.py                  网易云客户端：搜索 / 直链 / 下载 / 登录态 / 缓存去重
  download.py                 搜歌 → 挑歌 → 下载的流程（依赖注入，可拿假客户端测）
  config.py                   pydantic 配置模型（面板表单 + 校验）
  README.md                   本文
  CHANGELOG.md                版本记录
  LICENSE                     MIT
  docs/开发记录.md            移植过程、踩过的坑、验证方法（想知道「为什么这么写」看这个）
  selftest/                   开发自测，NeoBot 不会加载它
    run_tests.py
    run_framework_tests.py
    check_framework_api.py
    stubs/
```

`logic.py` / `library.py` / `netease.py` / `download.py` 都不 import 框架，
可以脱离 NeoBot 单独跑（换音源、写单测都方便）。

---

## 自测

```bash
# 纯逻辑：解析、打分、限流、ID3、缓存去重、下载流程（假客户端）
python selftest/run_tests.py

# 框架层：用替身 modloader 把插件真的 import 起来，跑端到端流程
python selftest/run_framework_tests.py

# 契约检查：插件用到的 NeoBot API 在你的框架源码里是否都存在（换版本后先跑这个）
python selftest/check_framework_api.py /path/to/NeoBot/packages/modloader/src/neobot_modloader
```

前两个脚本不联网；第二个用 `selftest/stubs/` 里按 NeoBot API 形状做的替身
（这样在没装 NeoBot 的机器上也能验证接线是否正确）。
第三个只读框架源码、不 import 它 —— 升级 NeoBot 之后跑一遍，就知道插件还兼容不兼容。
真机行为请以 NeoBot 日志为准。
