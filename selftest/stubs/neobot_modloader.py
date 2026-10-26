"""测试替身：只实现本插件用到的 neobot_modloader 接口。

⚠️ 不属于插件。因为本机装不了 NeoBot（Python 3.9 / pydantic v1），
这里按真实 API 的形状做一个最小替身，用来验证：
  · 装饰器用法（message/tool/capability/on_load…）能不能正常注册；
  · 处理器签名在我们的调用方式下能不能跑通；
  · 端到端流程（点歌 → 发语音条、问候选 → 回数字 → 发歌、解除限制）。
真实环境请以 NeoBot 的 modloader 为准。
"""

from __future__ import annotations

from typing import Any, Dict, List


class Message:
    """与 neobot_modloader.message.Message 对齐：text 只拼 text 段。"""

    def __init__(self, raw_event: Dict[str, Any] | None = None) -> None:
        self.raw_event = dict(raw_event or {})
        self.raw_message = str(self.raw_event.get("raw_message") or "")
        self.segments: List[Dict[str, Any]] = list(self.raw_event.get("message") or [])

    @property
    def text(self) -> str:
        parts = []
        for segment in self.segments:
            if str(segment.get("type")) == "text":
                parts.append(str((segment.get("data") or {}).get("text") or ""))
        return "".join(parts)

    def of_type(self, segment_type: str) -> List[Dict[str, Any]]:
        return [item for item in self.segments if item.get("type") == segment_type]


class Plugin:
    def __init__(
        self,
        name: str,
        *,
        version: str = "0.1.0",
        description: str = "",
        usage: str = "",
        author: str = "",
        config: Any = None,
        command_prefix: str = "/",
        dependencies: Any = (),
        priority: int = 0,
        min_neobot_version: Any = None,
        python_dependencies: Any = (),
        hot_reload: bool = True,
        config_hot_reload: bool = True,
    ) -> None:
        self.name = name
        self.version = version
        self.description = description
        self.usage = usage
        self.author = author
        self.config_model = config
        self.command_prefix = command_prefix
        self.dependencies = tuple(dependencies)
        self.priority = priority
        self.min_neobot_version = min_neobot_version
        self.python_dependencies = tuple(python_dependencies)
        self.hot_reload = hot_reload
        self.config_hot_reload = config_hot_reload
        self.messages: List[Dict[str, Any]] = []
        self.tools: List[Dict[str, Any]] = []
        self.capabilities: Dict[str, Any] = {}
        self.load_handlers: List[Any] = []
        self.startup_handlers: List[Any] = []
        self.shutdown_handlers: List[Any] = []

    def message(self, pattern: Any = None, **options: Any) -> Any:
        def decorate(handler: Any) -> Any:
            self.messages.append(dict(options, pattern=pattern, handler=handler))
            return handler

        return decorate

    def tool(self, name: str, **options: Any) -> Any:
        def decorate(handler: Any) -> Any:
            self.tools.append(dict(options, name=name, handler=handler))
            return handler

        return decorate

    def capability(self, name: str) -> Any:
        def decorate(handler: Any) -> Any:
            self.capabilities[name] = handler
            return handler

        return decorate

    def on_load(self, handler: Any) -> Any:
        self.load_handlers.append(handler)
        return handler

    def on_startup(self, handler: Any) -> Any:
        self.startup_handlers.append(handler)
        return handler

    def on_shutdown(self, handler: Any) -> Any:
        self.shutdown_handlers.append(handler)
        return handler
