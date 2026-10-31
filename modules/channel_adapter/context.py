"""事件上下文

把「当前正在处理的事件来自哪个机器人」放到 ContextVar，插件调用
ChannelAPI 时不必显式传 appid，多机器人场景下会自动使用收到消息的机器人。

asyncio 任务在创建时会复制当前上下文，因此模块在 before_event 里设置的
appid 会被后续 `spawn` 出来的指令处理任务继承。
"""

from __future__ import annotations

import contextlib
from contextvars import ContextVar

_current_appid: ContextVar[str] = ContextVar('channel_adapter_appid', default='')


def current_appid() -> str:
    """返回当前事件对应的机器人 appid；不在事件上下文中时为空字符串。"""
    return _current_appid.get()


def set_current_appid(appid) -> object:
    return _current_appid.set(str(appid or '').strip())


def reset_current_appid(token) -> None:
    with contextlib.suppress(ValueError):
        _current_appid.reset(token)


@contextlib.contextmanager
def appid_context(appid):
    """在 with 块内绑定当前机器人 appid。"""
    token = set_current_appid(appid)
    try:
        yield
    finally:
        reset_current_appid(token)
