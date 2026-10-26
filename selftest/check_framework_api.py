"""静态契约检查：本插件用到的 NeoBot API，在框架源码里是否真的存在。

不 import 框架（不需要装 NeoBot），只用 ast 解析源码，把「插件调用的名字」
和「框架定义的签名」对一遍 —— 换框架版本后想知道「还兼容吗」，跑这个最快。

用法（第二个参数是 NeoBot 的 modloader 源码目录）：

    python selftest/check_framework_api.py /path/to/NeoBot/packages/modloader/src/neobot_modloader

也可以把它整个放进 NeoBot 仓库根目录跑；找不到目录时只提示、不报错。
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent

_ARG = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("NEOBOT_MODLOADER_DIR", "")
FRAMEWORK = Path(_ARG) if _ARG else Path("packages/modloader/src/neobot_modloader")
if not (FRAMEWORK / "plugin.py").is_file():
    print("跳过：没找到 NeoBot modloader 源码目录。")
    print("用法： python selftest/check_framework_api.py <NeoBot>/packages/modloader/src/neobot_modloader")
    sys.exit(0)

FAIL = []


def ok(label):
    print("  ✓ " + label)


def bad(label, detail=""):
    FAIL.append(label + (" " + detail if detail else ""))
    print("  ✗ " + label + (" " + detail if detail else ""))


def class_node(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    return None


def method_names(node):
    return {n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def kwonly_args(func):
    return [a.arg for a in func.args.kwonlyargs]


def params(func):
    a = func.args
    names = [x.arg for x in a.posonlyargs] + [x.arg for x in a.args] + [x.arg for x in a.kwonlyargs]
    return names


def load(path):
    return ast.parse(path.read_text(encoding="utf-8"))


print("· 框架源码解析")
plugin_py = load(FRAMEWORK / "plugin.py")
context_py = load(FRAMEWORK / "context.py")
message_py = load(FRAMEWORK / "message.py")
hooks_py = load(FRAMEWORK / "hooks.py")
reply_py = load(FRAMEWORK / "reply.py")

Plugin = class_node(plugin_py, "Plugin")
Context = class_node(context_py, "RuntimePluginContext")
Message = class_node(message_py, "Message")
Bus = class_node(hooks_py, "PluginHookBus")
Reply = class_node(reply_py, "Reply")

for name, node in [("Plugin", Plugin), ("RuntimePluginContext", Context), ("Message", Message),
                   ("PluginHookBus", Bus), ("Reply", Reply)]:
    if node is None:
        bad("框架里找不到类 " + name)
    else:
        ok("找到类 " + name)

print("· Plugin(...) 关键字参数")
plugin_init = next(n for n in Plugin.body if isinstance(n, ast.FunctionDef) and n.name == "__init__")
available = set(kwonly_args(plugin_init))
used = {
    "version", "description", "usage", "author", "config",
    "min_neobot_version", "hot_reload", "config_hot_reload",
}
missing = used - available
if missing:
    bad("Plugin() 缺关键字参数", str(sorted(missing)))
else:
    ok("Plugin() 支持 " + ", ".join(sorted(used)))

print("· 注册 API")
methods = method_names(Plugin)
for name in ["message", "tool", "capability", "on_load", "on_startup", "on_shutdown",
             "agent", "tool_package", "sqlite_database"]:
    if name in methods:
        ok("Plugin." + name)
    else:
        bad("Plugin 没有方法 " + name)

print("· plugin.message(...) 关键字参数")
msg_fn = next(n for n in Plugin.body if isinstance(n, ast.FunctionDef) and n.name == "message")
msg_kw = set(kwonly_args(msg_fn))
for name in ["rule", "priority", "block", "block_ai_reply", "group", "private", "parse_error"]:
    if name in msg_kw:
        ok("message(..., " + name + "=)")
    else:
        bad("message() 不接受参数 " + name)

print("· plugin.tool(...) 关键字参数")
tool_fn = next(n for n in Plugin.body if isinstance(n, ast.FunctionDef) and n.name == "tool")
for name in ["description", "parameters", "package"]:
    if name in kwonly_args(tool_fn):
        ok("tool(..., " + name + "=)")
    else:
        bad("tool() 不接受参数 " + name)

print("· 生命周期 hook 参数")
plugin_hook = class_node(hooks_py, "HookRegistration")
hook_fields = {n.target.id for n in plugin_hook.body if isinstance(n, ast.AnnAssign)}
for name in ["rule", "priority", "block", "block_ai_reply", "post_type", "message_type"]:
    if name in hook_fields:
        ok("HookRegistration." + name)
    else:
        bad("HookRegistration 没有字段 " + name)

print("· RuntimePluginContext 接口")
ctx_members = method_names(Context) | {
    n.target.id for n in Context.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
}
# __init__ 里的 self.xxx = ... 也是可用属性（app_commands / users 就是这样来的）
for node in ast.walk(Context):
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                    and target.value.id == "self"):
                ctx_members.add(target.attr)
props = {n.name for n in Context.body if isinstance(n, ast.FunctionDef) and any(
    isinstance(d, ast.Name) and d.id == "property" for d in n.decorator_list)}
for name in ["data_dir", "logger", "plugin_host", "config", "adapter", "app_commands", "users",
             "conversation_from_event", "send_audio", "send", "send_group", "send_private",
             "require_plugin", "agent_reply"]:
    if name in ctx_members or name in props:
        ok("ctx." + name)
    else:
        bad("ctx 没有 " + name)

print("· Message.text")
msg_props = {n.name for n in Message.body if isinstance(n, ast.FunctionDef) and any(
    isinstance(d, ast.Name) and d.id == "property" for d in n.decorator_list)}
if "text" in msg_props:
    ok("Message.text")
else:
    bad("Message 没有 text 属性")

print("· 插件源码里对 ctx / plugin 的调用面")
source = (PLUGIN / "__init__.py").read_text(encoding="utf-8")
tree = ast.parse(source)
ctx_attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
             and isinstance(n.value, ast.Name) and n.value.id in {"ctx", "context"}}
unknown = ctx_attrs - ctx_members - props
if unknown:
    bad("代码里用了 ctx 上不存在的属性", str(sorted(unknown)))
else:
    ok("代码里用到的 ctx 属性全都在框架里（{} 个）".format(len(ctx_attrs)))

plugin_calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                and n.func.value.id == "plugin"}
unknown_calls = plugin_calls - methods
if unknown_calls:
    bad("代码里调了 Plugin 上不存在的方法", str(sorted(unknown_calls)))
else:
    ok("代码里调用的 plugin 方法全都存在（{} 个）".format(len(plugin_calls)))

print("")
if FAIL:
    print("FAILED {} 项".format(len(FAIL)))
    for item in FAIL:
        print("  - " + item)
    sys.exit(1)
print("契约检查全部通过")
