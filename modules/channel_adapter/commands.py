"""通用指令拦截层

在事件进入核心通用分发之前截获指令，统一支持 QQ 频道、群聊（@ 与全量）
和个人好友聊天，并按模块独立配置的频道主人列表判断权限。

权限判定分两层：
1. 频道场景使用 channel_owners（模块配置里的频道主人列表）；
   群聊 / 好友聊天使用框架 bot.yaml 的 owner_ids。
2. 频道身份组（d.member.roles）：handler 未声明 role_ids 时默认放行
   频道主(4)与管理员(2)；显式声明 role_ids 时改为只放行声明的身份组，
   声明空集合表示只允许主人执行。

owner_only 指令只有在两层中任一层命中时才执行，否则回复 denied_reply。
"""

from __future__ import annotations

import asyncio

from core.application import get_app
from core.base.config import cfg as framework_cfg
from core.base.logger import EXTENSION, get_logger, report_error
from core.base.tasks import spawn
from core.plugin.context import _make_reply_log_cb

log = get_logger(EXTENSION, '通用指令')

AT_MESSAGE_CREATE = 'AT_MESSAGE_CREATE'
MESSAGE_CREATE = 'MESSAGE_CREATE'
DIRECT_MESSAGE_CREATE = 'DIRECT_MESSAGE_CREATE'
GROUP_AT_MESSAGE_CREATE = 'GROUP_AT_MESSAGE_CREATE'
GROUP_MESSAGE_CREATE = 'GROUP_MESSAGE_CREATE'
C2C_MESSAGE_CREATE = 'C2C_MESSAGE_CREATE'

CHANNEL_EVENTS = frozenset({AT_MESSAGE_CREATE, DIRECT_MESSAGE_CREATE, MESSAGE_CREATE})
GROUP_EVENTS = frozenset({GROUP_AT_MESSAGE_CREATE, GROUP_MESSAGE_CREATE})
C2C_EVENTS = frozenset({C2C_MESSAGE_CREATE})
COMMAND_EVENTS = CHANNEL_EVENTS | GROUP_EVENTS | C2C_EVENTS

# 场景位掩码，与 core/plugin/_dispatch.py 保持一致
_S_GROUP = 1
_S_DIRECT = 2
_S_CHANNEL = 4

_EVENT_SCENES = {
    AT_MESSAGE_CREATE: _S_CHANNEL,
    MESSAGE_CREATE: _S_CHANNEL,
    DIRECT_MESSAGE_CREATE: _S_CHANNEL | _S_DIRECT,
    GROUP_AT_MESSAGE_CREATE: _S_GROUP,
    GROUP_MESSAGE_CREATE: _S_GROUP,
    C2C_MESSAGE_CREATE: _S_DIRECT,
}

# 这些事件天然带 @，允许省略前缀
_NO_PREFIX_EVENTS = frozenset({AT_MESSAGE_CREATE, DIRECT_MESSAGE_CREATE, GROUP_AT_MESSAGE_CREATE})
# 公开场景无权限时不回复，避免刷屏
_SILENT_DENIED_EVENTS = frozenset({MESSAGE_CREATE})

COMMAND_DEFAULTS = {
    'enabled': True,
    'prefixes': ['/'],
    'at_without_prefix': True,
    'public_messages': False,
    'group_at_messages': True,
    'group_messages': True,
    'c2c_messages': True,
    'only_channel_handlers': False,
    'denied_reply': '暂无指令权限',
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
    group_at_messages = _as_bool(source.get('group_at_messages'), COMMAND_DEFAULTS['group_at_messages'])
    group_messages = _as_bool(source.get('group_messages'), COMMAND_DEFAULTS['group_messages'])
    c2c_messages = _as_bool(source.get('c2c_messages'), COMMAND_DEFAULTS['c2c_messages'])
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
        'group_at_messages': group_at_messages,
        'group_messages': group_messages,
        'c2c_messages': c2c_messages,
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


# ==================== 场景判定 ====================


def event_scene(event):
    """返回事件的场景位掩码。"""
    return _EVENT_SCENES.get(getattr(event, 'event_type', ''), 0)


def scene_name(event):
    """返回便于插件判断的场景名: channel / group / direct / unknown。"""
    scene = event_scene(event)
    if scene & _S_GROUP:
        return 'group'
    if scene & _S_CHANNEL:
        return 'channel'
    if scene & _S_DIRECT:
        return 'direct'
    return 'unknown'


def handler_scene(handler):
    """返回 handler 声明的场景位掩码；未声明表示全场景。"""
    if not isinstance(handler, dict):
        return 0
    return (
        (_S_GROUP if handler.get('group_only') else 0)
        | (_S_DIRECT if handler.get('direct_only') else 0)
        | (_S_CHANNEL if handler.get('channel_only') else 0)
    )


# ==================== 身份组放行 ====================
# 非频道主人执行 owner_only 指令时，先看 handler 上显式声明的 role_ids，
# 未声明时回退到这里的全局默认身份组：
#   4 = 频道主，2 = 管理员
# 即频道主 / 管理员即使不在 channel_owners 白名单里，也能执行 owner_only 指令；
# 想让某个 owner_only 指令只允许 channel_owners 时，请给该 handler 显式声明
# 空集合（() 或 []）。
_ELEVATED_ROLE_IDS = frozenset({'4', '2'})


def _event_roles(event):
    """读取事件里的身份标识: 频道 d.member.roles 与群聊 author.member_role。"""
    result = set()

    role = str(getattr(event, 'member_role', '') or '').strip()
    if role:
        result.add(role)

    raw = getattr(event, 'raw', None)
    if not isinstance(raw, dict):
        return result

    data = raw.get('d') if isinstance(raw.get('d'), dict) else raw
    member = data.get('member') if isinstance(data.get('member'), dict) else None
    if not member:
        return result

    roles = member.get('roles')
    if not isinstance(roles, (list, tuple, set)):
        return result

    for item in roles:
        text = str(item).strip() if item is not None else ''
        if text:
            result.add(text)
    return result


def _wanted_roles(handler):
    """返回 handler 声明的角色 ID 集合。

    优先级：handler['role_ids'] > handler['func']._role_ids > 全局 _ELEVATED_ROLE_IDS。
    显式声明空集合（() 或 []）表示该指令只允许主人执行。
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
    """非主人是否可以通过身份组执行该 owner_only 指令。"""
    wanted = _wanted_roles(handler)
    if not wanted:
        return False
    return bool(wanted & _event_roles(event))


# ==================== 主人判定 ====================


def _framework_owner(event):
    app = get_app()
    plugin_manager = getattr(app, 'plugin_manager', None) if app is not None else None
    check = getattr(plugin_manager, '_is_owner', None)
    if not callable(check):
        return False
    try:
        return bool(check(event))
    except Exception:
        return False


def _is_owner(event, owners):
    user_id = str(getattr(event, 'user_id', '') or '').strip()
    if not user_id:
        return False

    scene = event_scene(event)
    if scene & _S_CHANNEL and user_id in owners_for(owners, getattr(event, 'appid', '')):
        return True
    return _framework_owner(event)


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
    """通用指令拦截与主人权限控制。"""

    __slots__ = ('_adapter',)

    def __init__(self, adapter):
        self._adapter = adapter

    async def handle(self, event):
        """返回 True 表示事件已被接管，不再进入核心通用分发。"""
        event_type = getattr(event, 'event_type', '')
        if event_type not in COMMAND_EVENTS:
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
        if not self._scene_enabled(event_type, settings):
            return False
        if framework_cfg.get_bot_setting(appid, 'maintenance.enabled', False):
            return False

        # 全量群消息里被 @ 时，也可以像 @ 消息一样省略前缀
        at_optional = event_type in _NO_PREFIX_EVENTS or (
            event_type == GROUP_MESSAGE_CREATE and bool(getattr(event, 'is_at_self', False))
        )
        candidates, prefixed = _strip_prefix(content, settings['prefixes'])
        if not prefixed and (not at_optional or not settings['at_without_prefix']):
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
                report_error(EXTENSION, '通用指令', exc, context={'appid': appid, 'event_type': event_type})

        self._record_message(app, bot, event)

        if await self._run_interceptors(plugin_manager, event):
            event.finish_dispatch()
            return True

        log.debug(f'[{appid}] 指令拦截[{scene_name(event)}]: {content[:80]}')
        spawn(self._run(matched, denied, event, settings))
        return True

    @staticmethod
    def _scene_enabled(event_type, settings):
        if event_type == MESSAGE_CREATE:
            return bool(settings['public_messages'])
        if event_type == GROUP_MESSAGE_CREATE:
            return bool(settings['group_messages'])
        if event_type == GROUP_AT_MESSAGE_CREATE:
            return bool(settings['group_at_messages'])
        if event_type == C2C_MESSAGE_CREATE:
            return bool(settings['c2c_messages'])
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
                report_error(EXTENSION, '通用指令', exc, context={'appid': appid, 'event_type': event.event_type})

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
                    interceptor.get('_plugin') or '通用指令',
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
        scene = event_scene(event)
        only_channel = settings['only_channel_handlers']
        matched = []
        denied = False

        for handler in getattr(plugin_manager, '_all_handlers', []) or []:
            if handler.get('fallback'):
                continue

            if only_channel and scene & _S_CHANNEL:
                # 兼容旧行为：频道场景只匹配 channel_only 处理器
                if not handler.get('channel_only'):
                    continue
            elif handler_scene(handler) & ~scene:
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
                # 非主人：只有显式声明了 role_ids 且角色命中才放行
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
                    '通用指令',
                    f'指令处理器 [{handler.get("name") or plugin_name}] 超时({_HANDLER_TIMEOUT}s)',
                    context={'appid': event.appid, 'event_type': event.event_type},
                )
            except Exception as exc:
                report_error(
                    EXTENSION,
                    '通用指令',
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
        if not text or event.event_type in _SILENT_DENIED_EVENTS:
            return
        try:
            await event.reply(text)
        except Exception as exc:
            report_error(EXTENSION, '通用指令', exc, context={'appid': event.appid, 'event_type': event.event_type})
