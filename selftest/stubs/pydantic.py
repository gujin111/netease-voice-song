"""本机测试用的极简 pydantic 替身。

⚠️ 这个文件**不属于插件**，只在本地离线自测时用：
   本机 Python 3.9 上是 pydantic v1，而 NeoBot 2.0 用 v2
   （``ConfigDict`` / ``model_validate``）。为了能在没有 v2 的机器上验证
   配置模型的默认值与派生属性，这里提供一个只有本测试用到的接口的替身。
"""

from __future__ import annotations

from typing import Any, Dict


class _FieldInfo:
    def __init__(self, default: Any = None, description: str = "") -> None:
        self.default = default
        self.description = description


def Field(default: Any = None, **kwargs: Any) -> _FieldInfo:  # noqa: N802 - 对齐 pydantic 命名
    return _FieldInfo(default=default, description=str(kwargs.get("description") or ""))


def ConfigDict(**kwargs: Any) -> Dict[str, Any]:  # noqa: N802
    return dict(kwargs)


class BaseModel:
    """只实现「按类属性默认值填充实例」这一件事。"""

    def __init__(self, **values: Any) -> None:
        for name, value in self._field_defaults().items():
            setattr(self, name, values.get(name, value))
        for name, value in values.items():
            if not hasattr(self, name):
                setattr(self, name, value)

    @classmethod
    def _field_defaults(cls) -> Dict[str, Any]:
        defaults: Dict[str, Any] = {}
        for klass in reversed(cls.__mro__):
            for name, value in vars(klass).items():
                if isinstance(value, _FieldInfo):
                    defaults[name] = value.default
                elif name.islower() and not name.startswith("_") and not callable(value):
                    if not isinstance(value, (property, classmethod, staticmethod)):
                        defaults.setdefault(name, value)
        return defaults
