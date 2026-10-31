"""频道指令拦截层

在频道消息进入核心通用分发之前截获指令，按模块独立配置的频道主人列表
判断权限，并且只把指令分发给频道场景的处理器。

权限判定分两层：
1. channel_owners（模块配置里的频道主人列表）：可执行全部 owner_only 指令
2. 频道身份组（d.member.roles）：只有 handler 显式声明了 role_ids 且命中时，
   非频道主人才可以执行该 owner_only 指令

未声明 role_ids 的 owner_only 指令默认只允许 channel_owners 执行，
符合最小权限原则。
"""

from __future__ import annotations

import asyncio

from core.application import get_app
from core.base.config import cfg as framework_cfg
from core.base.logger import EXTENSION, get_logger, report_error
from core.base.tasks import spawn
from core.plugin.context import _make_reply_log_cb

log = get_logger(EXTENSION, '频道指令')

AT_MESSAGE_CREATE = 'AT_MESSAGE_CREATE'
MESSAGE_CREATE = 'MESSAGE_CREATE'
DIRECT_MESSAGE_CREATE = 'DIRECT_MESSAGE_CREATE'

COMMAND_EVENTS = frozenset({AT_MESSAGE_CREATE, MESSAGE_CREATE, DIRECT_MESSAGE_CREATE})
_PUBLIC_EVENT = MESSAGE_CREATE
_NO_PREFIX_EVENTS = frozenset({AT_MESSAGE_CREATE, DIRECT_MESSAGE_CREATE})

COMMAND_DEFAULTS = {
    'enabled': True,
    'prefixes': ['/'],
    'at_without_prefix': True,
    'public_messages': False,
    'only_channel_handlers': True,
    'denied_reply': '暂无频道指令权限',
}

_HANDLER_TIMEOUT = 300
_MAX_PREFIXES = 10


def _as_bool(value, default):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ('1', 'true', 'yes', 'on'):
            return True
        if text in ('0', 'false', 'no', 'off'):
            return False
    return default


def normalize_command_config(raw):
    """规范化指令配置，补齐缺失字段并清理前缀。"""
    source = raw if isinstance(raw, dict) else {}
    enabled = _as_bool(source.get('enabled'), COMMAND_DEFAULTS['enabled'])
    at_without_prefix = _as_bool(source.get('at_without_prefix'), COMMAND_DEFAULTS['at_without_prefix'])
    public_messages = _as_bool(source.get('public_messages'), COMMAND_DEFAULTS['public_messages'])
    only_channel_handlers = _as_bool(source.get('only_channel_handlers'), COMMAND_DEFAULTS['only_channel_handlers'])

    raw_prefixes = source.get('prefixes') if 'prefixes' in source else COMMAND_DEFAULTS['prefixes']
    if isinstance(raw_prefixes, str):
        raw_prefixes = [raw_prefixes]
    prefixes = []
    if isinstance(raw_prefixes, (list, tuple)):
        for item in raw_prefixes:
            text = str(item).strip()
            if text and text not in prefixes:
                prefixes.append(text)
    prefixes = prefixes[:_MAX_PREFIXES]

    denied_reply = source.get('denied_reply', COMMAND_DEFAULTS['denied_reply'])
    denied_reply = '' if denied_reply is None else str(denied_reply).strip()

    return {
        'enabled': enabled,
        'prefixes': prefixes,
        'at_without_prefix': at_without_prefix,
        'public_messages': public_messages,
        'only_channel_handlers': only_channel_handlers,
        'denied_reply': denied_reply,
    }


def normalize_owners(raw):
    """规范化频道主人配置，返回 {appid: [user_id]}；* 表示所有机器人共用。

    支持 dict 形式 {appid: [id]} 与列表形式 [{appid: ..., user_ids: [...]}]，
    也接受纯字符串列表表示所有机器人共用。
    """
    owners = {}
    if isinstance(raw, str):
        raw = [raw]
    if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        raw = [{'appid': '*', 'user_ids': raw}]
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            appid = str(item.get('appid') or '*').strip() or '*'
            value = item.get('user_ids')
            if value is None:
                value = item.get('owners')
            if isinstance(value, str):
                value = [value]
            ids = []
            if isinstance(value, (list, tuple, set)):
                for uid in value:
                    text = str(uid).strip()
                    if text and text not in ids:
                        ids.append(text)
            owners.setdefault(appid, [])
            for uid in ids:
                if uid not in owners[appid]:
                    owners[appid].append(uid)
    elif isinstance(raw, dict):
        for key, value in raw.items():
            appid = str(key).strip() or '*'
            if isinstance(value, str):
                value = [value]
            ids = []
            if isinstance(value, (list, tuple, set)):
                for uid in value:
                    text = str(uid).strip()
                    if text and text not in ids:
                        ids.append(text)
            owners.setdefault(appid, [])
            for uid in ids:
                if uid not in owners[appid]:
                    owners[appid].append(uid)
    owners.setdefault('*', [])
    return owners


def owners_to_config(owners):
    """把 {appid: [id]} 转成便于 YAML 渲染的列表形式。"""
    result = []
    for appid, ids in normalize_owners(owners).items():
        result.append({'appid': appid, 'user_ids': list(ids)})
    return result


def owners_for(owners, appid):
    """合并全局主人与该机器人的主人列表。"""
    target = str(appid or '').strip()
    universal = owners.get('*') or []
    specific = owners.get(target) or []
    merged = []
    for item in [*universal, *specific]:
        text = str(item).strip()
        if text and text not in merged:
            merged.append(text)
    return merged


# ==================== 身份组放行 ====================
# 非频道主人执行 owner_only 指令时，先看 handler 上显式声明的 role_ids，
# 未声明时回退到这里的全局默认身份组：
#   4 = 频道主，2 = 管理员
# 即频道主 / 管理员即使不在 channel_owners 白名单里，也能执行 owner_only 指令；
# 想让某个 owner_only 指令只允许 channel_owners 时，请给该 handler 显式声明
# 空集合（() 或 []）。
_ELEVATED_ROLE_IDS = frozenset({'4', '2'})


def _event_roles(event):
    """从事件原始载荷里读取 d.member.roles，返回字符串集合。"""
    raw = getattr(event, 'raw', None)
    if not isinstance(raw, dict):
        return set()

    data = raw.get('d') if isinstance(raw.get('d'), dict) else raw
    member = data.get('member') if isinstance(data.get('member'), dict) else None
    if not member:
        return set()

    roles = member.get('roles')
    if not isinstance(roles, (list, tuple, set)):
        return set()

    result = set()
    for item in roles:
        text = str(item).strip() if item is not None else ''
        if text:
            result.add(text)
    return result


def _wanted_roles(handler):
    """返回 handler 声明的角色 ID 集合。

    优先级：handler['role_ids'] > handler['func']._role_ids > 全局 _ELEVATED_ROLE_IDS。
    显式声明空集合（() 或 []）表示该指令只允许 channel_owners。
    """
    roles = handler.get('role_ids')
    if roles is None:
        func = handler.get('func')
        roles = getattr(func, '_role_ids', None)
    if roles is None:
        return _ELEVATED_ROLE_IDS
    return {
        str(item).strip()
        for item in roles
        if item is not None and str(item).strip()
    }


def _allowed_by_roles(handler, event):
    """非频道主人是否可以通过 d.member.roles 执行该 owner_only 指令。"""
    wanted = _wanted_roles(handler)
    if not wanted:
        return False
    return bool(wanted & _event_roles(event))


# ==================== 主人判定 ====================

def _is_owner(event, owners):
    user_id = str(getattr(event, 'user_id', '') or '').strip()
    if not user_id:
        return False
    return user_id in owners_for(owners, getattr(event, 'appid', ''))


def _strip_prefix(text, prefixes):
    """去掉最长的命中前缀，返回 (候选匹配文本, 是否命中前缀)。"""
    best = ''
    for prefix in prefixes:
        if prefix and text.startswith(prefix) and len(prefix) > len(best):
            best = prefix
    if not best:
        return _slash_variants(text), False
    stripped = text[len(best):].lstrip()
    return [stripped, text], True


def _slash_variants(text):
    """与核心分发保持一致: 原文与去掉/补上斜杠的写法都参与匹配。"""
    alt = text[1:] if text[:1] == '/' else '/' + text
    return [text, alt] if alt and alt != text else [text]


class ChannelCommandRouter:
    """频道指令拦截与频道主人权限控制。"""

    __slots__ = ('_adapter',)

    def __init__(self, adapter):
        self._adapter = adapter

    async def handle(self, event):
        """返回 True 表示事件已被接管，不再进入核心通用分发。"""
        if getattr(event, 'event_type', '') not in COMMAND_EVENTS:
            return False
        content = str(getattr(event, 'content', '') or '')
        if not content:
            return False

        appid = str(getattr(event, 'appid', '') or '')
        if getattr(event, 'is_bot', False) and framework_cfg.get_bot_setting(appid, 'non_at_message.ignore_bot_sender', False):
            return False

        config = self._adapter.get_config() or {}
        settings = normalize_command_config(config.get('channel_commands'))
        if not settings['enabled']:
            return False
        if event.event_type == _PUBLIC_EVENT and not settings['public_messages']:
            return False
        if framework_cfg.get_bot_setting(appid, 'maintenance.enabled', False):
            return False

        candidates, prefixed = _strip_prefix(content, settings['prefixes'])
        if not prefixed and (event.event_type not in _NO_PREFIX_EVENTS or not settings['at_without_prefix']):
            return False

        app = get_app()
        plugin_manager = getattr(app, 'plugin_manager', None) if app is not None else None
        bot = app.get_bot(appid) if app is not None else None
        if plugin_manager is None or bot is None:
            return False

        owners = normalize_owners(config.get('channel_owners'))
        matched, denied = self._match(plugin_manager, event, candidates, settings, _is_owner(event, owners))
        if not matched and not denied:
            return False

        event._sender = getattr(bot, 'sender', None)

        check_blacklist = getattr(plugin_manager, '_check_blacklist', None)
        if callable(check_blacklist):
            try:
                if check_blacklist(event):
                    return False
            except Exception as exc:
                report_error(EXTENSION, '频道指令', exc, context={'appid': appid, 'event_type': event.event_type})

        self._record_message(app, bot, event)

        if await self._run_interceptors(plugin_manager, event):
            event.finish_dispatch()
            return True

        log.debug(f'[{appid}] 频道指令拦截: {content[:80]}')
        spawn(self._run(matched, denied, event, settings))
        return True

    async def _run(self, matched, denied, event, settings):
        try:
            if matched:
                await self._execute(matched, event)
            if denied:
                await self._reply_denied(event, settings)
        finally:
            event._reply_plugin_name = ''
            event.raw = None
            event.finish_dispatch()

    @staticmethod
    def _record_message(app, bot, event):
        """模块接管事件后沿用核心的消息日志与用户追踪。"""
        appid = str(getattr(event, 'appid', '') or '')
        for name, args in (
            ('_record_message_event', (bot, event, appid)),
            ('_enqueue_track', (bot, event, appid)),
        ):
            func = getattr(app, name, None)
            if not callable(func):
                continue
            try:
                func(*args)
            except Exception as exc:
                report_error(EXTENSION, '频道指令', exc, context={'appid': appid, 'event_type': event.event_type})

    async def _run_interceptors(self, plugin_manager, event):
        loop = asyncio.get_running_loop()
        for interceptor in getattr(plugin_manager, '_all_interceptors', []) or []:
            func = interceptor.get('func')
            if not callable(func):
                continue
            try:
                result = await func(event) if interceptor.get('is_coro') else await loop.run_in_executor(None, func, event)
            except Exception as exc:
                report_error(
                    EXTENSION,
                    interceptor.get('_plugin') or '频道指令',
                    exc,
                    context={'appid': event.appid, 'event_type': event.event_type},
                )
                continue
            if result is True:
                return True
        return False

    def _match(self, plugin_manager, event, candidates, settings, is_owner):
        appid = str(getattr(event, 'appid', '') or '')
        event_type = event.event_type
        only_channel = settings['only_channel_handlers']
        matched = []
        denied = False

        for handler in getattr(plugin_manager, '_all_handlers', []) or []:
            if handler.get('fallback'):
                continue
            if only_channel:
                if not handler.get('channel_only'):
                    continue
            elif handler.get('group_only') or handler.get('direct_only'):
                continue

            event_types = handler.get('event_types')
            if event_types and event_type not in event_types:
                continue
            allowed_bots = handler.get('_allowed_bots')
            if allowed_bots is not None and appid not in allowed_bots:
                continue

            compiled = handler.get('compiled')
            if compiled is None:
                continue
            match = None
            for text in candidates:
                match = compiled.search(text)
                if match is not None:
                    break
            if match is None:
                continue

            if handler.get('owner_only') and not is_owner:
                # 非频道主人：只有显式声明了 role_ids 且角色命中才放行
                if not _allowed_by_roles(handler, event):
                    denied = True
                    break
            matched.append((handler, match))
            if handler.get('block', False):
                break
        return matched, denied

    async def _execute(self, matched, event):
        loop = asyncio.get_running_loop()
        for handler, match in matched:
            plugin_name = handler.get('_plugin') or handler.get('name') or ''
            event._reply_plugin_name = plugin_name
            event._reply_log_cb = self._reply_log_cb(plugin_name, event)
            func = handler.get('func')
            if not callable(func):
                continue
            try:
                coro = func(event, match) if handler.get('is_coro') else loop.run_in_executor(None, func, event, match)
                async with asyncio.timeout(_HANDLER_TIMEOUT):
                    await coro
            except TimeoutError:
                report_error(
                    EXTENSION,
                    '频道指令',
                    f'指令处理器 [{handler.get("name") or plugin_name}] 超时({_HANDLER_TIMEOUT}s)',
                    context={'appid': event.appid, 'event_type': event.event_type},
                )
            except Exception as exc:
                report_error(
                    EXTENSION,
                    '频道指令',
                    exc,
                    context={'appid': event.appid, 'event_type': event.event_type, 'handler': handler.get('name') or ''},
                )
            finally:
                event._reply_log_cb = None

    def _reply_log_cb(self, plugin_name, event):
        """复用核心的回复日志回调，保证模块接管的指令也能写入消息日志。"""
        app = get_app()
        if app is None:
            return None
        bot = app.get_bot(str(getattr(event, 'appid', '') or ''))
        log_service = getattr(bot, 'log_service', None) if bot is not None else None
        if log_service is None:
            return None
        return _make_reply_log_cb(plugin_name, log_service)

    async def _reply_denied(self, event, settings):
        text = settings.get('denied_reply') or ''
        if not text or event.event_type == _PUBLIC_EVENT:
            return
        try:
            await event.reply(text)
        except Exception as exc:
            report_error(EXTENSION, '频道指令', exc, context={'appid': event.appid, 'event_type': event.event_type})