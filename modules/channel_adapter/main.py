"""可选模块: QQ 频道事件适配

框架核心已处理频道消息收发。本模块补齐 GUILDS / GUILD_MEMBERS 等
intent 下的频道生命周期事件，并把事件按显式 event_types 分发给插件。
同时覆盖机器人频道列表、频道详情、子频道、成员、身份组与权限、接口授权、
禁言、公告、精华消息、日程、音频、论坛帖子、子频道图片与 Markdown 模板消息等服务端接口。
模块的指令拦截层同时支持频道、群聊（@ 与全量）和好友聊天，共用同一套指令。

启用模块后，频道事件会填充 Event 的 guild_id / channel_id / user_id /
content 等字段，插件可通过 event_types 显式订阅，例如:

    @handler(r'.*', event_types=['GUILD_CREATE'])
    async def on_guild_create(event, match):
        ...

接收成员事件需要在 bot.yaml 打开:
    websocket:
      subscribe_channel_events: true

私域机器人会额外订阅 GUILD_MESSAGES / FORUMS_EVENT；公域论坛
OPEN_FORUM_EVENT 与音视频/直播成员 AUDIO_LIVE_MEMBER 可在配置中开启。
网关拒绝未授权 intent 时按组自动回退，无需手动区分公域/私域。
"""

__module_meta__ = {
    'name': '频道事件适配',
    'description': 'QQ 频道全量管理接口与事件适配：子频道、成员、身份组、权限、发言、公告、精华、日程、音频、论坛、审核、单聊开关、图片与 Markdown 模板消息',
    'version': '2.10.0',
    'author': 'ElainaBot',
}

import asyncio
import inspect
import json
import re
import time

from core.application import get_app
from core.base.logger import EXTENSION, get_logger, report_error
from core.base.tasks import spawn

from .api import ChannelAPI
from .commands import COMMAND_EVENTS, ChannelCommandRouter, normalize_command_config, normalize_owners, owners_to_config, scene_name
from .context import appid_context

log = get_logger(EXTENSION, '频道适配')

GUILD_CREATE = 'GUILD_CREATE'
GUILD_UPDATE = 'GUILD_UPDATE'
GUILD_DELETE = 'GUILD_DELETE'
CHANNEL_CREATE = 'CHANNEL_CREATE'
CHANNEL_UPDATE = 'CHANNEL_UPDATE'
CHANNEL_DELETE = 'CHANNEL_DELETE'
GUILD_MEMBER_ADD = 'GUILD_MEMBER_ADD'
GUILD_MEMBER_UPDATE = 'GUILD_MEMBER_UPDATE'
GUILD_MEMBER_REMOVE = 'GUILD_MEMBER_REMOVE'
AUDIO_OR_LIVE_CHANNEL_MEMBER_ENTER = 'AUDIO_OR_LIVE_CHANNEL_MEMBER_ENTER'
AUDIO_OR_LIVE_CHANNEL_MEMBER_EXIT = 'AUDIO_OR_LIVE_CHANNEL_MEMBER_EXIT'
MESSAGE_DELETE = 'MESSAGE_DELETE'
PUBLIC_MESSAGE_DELETE = 'PUBLIC_MESSAGE_DELETE'
DIRECT_MESSAGE_DELETE = 'DIRECT_MESSAGE_DELETE'
MESSAGE_REACTION_ADD = 'MESSAGE_REACTION_ADD'
MESSAGE_REACTION_REMOVE = 'MESSAGE_REACTION_REMOVE'

AT_MESSAGE_CREATE = 'AT_MESSAGE_CREATE'
MESSAGE_CREATE = 'MESSAGE_CREATE'

MESSAGE_AUDIT_PASS = 'MESSAGE_AUDIT_PASS'
MESSAGE_AUDIT_REJECT = 'MESSAGE_AUDIT_REJECT'

FORUM_THREAD_CREATE = 'FORUM_THREAD_CREATE'
FORUM_THREAD_UPDATE = 'FORUM_THREAD_UPDATE'
FORUM_THREAD_DELETE = 'FORUM_THREAD_DELETE'
FORUM_POST_CREATE = 'FORUM_POST_CREATE'
FORUM_POST_DELETE = 'FORUM_POST_DELETE'
FORUM_REPLY_CREATE = 'FORUM_REPLY_CREATE'
FORUM_REPLY_DELETE = 'FORUM_REPLY_DELETE'
FORUM_PUBLISH_AUDIT_RESULT = 'FORUM_PUBLISH_AUDIT_RESULT'

AT_FORUM_THREAD_CREATE = 'AT_FORUM_THREAD_CREATE'
AT_FORUM_POST_CREATE = 'AT_FORUM_POST_CREATE'
AT_FORUM_REPLY_CREATE = 'AT_FORUM_REPLY_CREATE'

OPEN_FORUM_THREAD_CREATE = 'OPEN_FORUM_THREAD_CREATE'
OPEN_FORUM_THREAD_UPDATE = 'OPEN_FORUM_THREAD_UPDATE'
OPEN_FORUM_THREAD_DELETE = 'OPEN_FORUM_THREAD_DELETE'
OPEN_FORUM_POST_CREATE = 'OPEN_FORUM_POST_CREATE'
OPEN_FORUM_POST_DELETE = 'OPEN_FORUM_POST_DELETE'
OPEN_FORUM_REPLY_CREATE = 'OPEN_FORUM_REPLY_CREATE'
OPEN_FORUM_REPLY_DELETE = 'OPEN_FORUM_REPLY_DELETE'

AUDIO_START = 'AUDIO_START'
AUDIO_FINISH = 'AUDIO_FINISH'
AUDIO_ON_MIC = 'AUDIO_ON_MIC'
AUDIO_OFF_MIC = 'AUDIO_OFF_MIC'

C2C_MSG_RECEIVE = 'C2C_MSG_RECEIVE'
C2C_MSG_REJECT = 'C2C_MSG_REJECT'

AUDIT_EVENTS = frozenset({MESSAGE_AUDIT_PASS, MESSAGE_AUDIT_REJECT})

FORUM_THREAD_EVENTS = frozenset({
    FORUM_THREAD_CREATE,
    FORUM_THREAD_UPDATE,
    FORUM_THREAD_DELETE,
    AT_FORUM_THREAD_CREATE,
    OPEN_FORUM_THREAD_CREATE,
    OPEN_FORUM_THREAD_UPDATE,
    OPEN_FORUM_THREAD_DELETE,
})

FORUM_POST_EVENTS = frozenset({
    FORUM_POST_CREATE,
    FORUM_POST_DELETE,
    AT_FORUM_POST_CREATE,
    OPEN_FORUM_POST_CREATE,
    OPEN_FORUM_POST_DELETE,
})

FORUM_REPLY_EVENTS = frozenset({
    FORUM_REPLY_CREATE,
    FORUM_REPLY_DELETE,
    AT_FORUM_REPLY_CREATE,
    OPEN_FORUM_REPLY_CREATE,
    OPEN_FORUM_REPLY_DELETE,
})

FORUM_AUDIT_EVENTS = frozenset({FORUM_PUBLISH_AUDIT_RESULT})
FORUM_EVENTS = FORUM_THREAD_EVENTS | FORUM_POST_EVENTS | FORUM_REPLY_EVENTS | FORUM_AUDIT_EVENTS

AUDIO_ACTION_EVENTS = frozenset({AUDIO_START, AUDIO_FINISH, AUDIO_ON_MIC, AUDIO_OFF_MIC})
DIRECT_LIFECYCLE_EVENTS = frozenset({C2C_MSG_RECEIVE, C2C_MSG_REJECT})

CHANNEL_EVENTS = frozenset({
    GUILD_CREATE,
    GUILD_UPDATE,
    GUILD_DELETE,
    CHANNEL_CREATE,
    CHANNEL_UPDATE,
    CHANNEL_DELETE,
    GUILD_MEMBER_ADD,
    GUILD_MEMBER_UPDATE,
    GUILD_MEMBER_REMOVE,
    AUDIO_OR_LIVE_CHANNEL_MEMBER_ENTER,
    AUDIO_OR_LIVE_CHANNEL_MEMBER_EXIT,
    MESSAGE_DELETE,
    PUBLIC_MESSAGE_DELETE,
    DIRECT_MESSAGE_DELETE,
    MESSAGE_REACTION_ADD,
    MESSAGE_REACTION_REMOVE,
}) | AUDIT_EVENTS | FORUM_EVENTS | AUDIO_ACTION_EVENTS

CORE_SILENT_EVENTS = frozenset({
    GUILD_UPDATE,
    MESSAGE_REACTION_ADD,
    MESSAGE_REACTION_REMOVE,
})

GUILD_UPSERT_EVENTS = frozenset({GUILD_CREATE, GUILD_UPDATE})
CHANNEL_UPSERT_EVENTS = frozenset({CHANNEL_CREATE, CHANNEL_UPDATE})
GUILD_MEMBER_EVENTS = frozenset({GUILD_MEMBER_ADD, GUILD_MEMBER_UPDATE, GUILD_MEMBER_REMOVE})
AUDIO_MEMBER_EVENTS = frozenset({
    AUDIO_OR_LIVE_CHANNEL_MEMBER_ENTER,
    AUDIO_OR_LIVE_CHANNEL_MEMBER_EXIT,
})
MESSAGE_DELETE_EVENTS = frozenset({MESSAGE_DELETE, PUBLIC_MESSAGE_DELETE, DIRECT_MESSAGE_DELETE})
REACTION_EVENTS = frozenset({MESSAGE_REACTION_ADD, MESSAGE_REACTION_REMOVE})

_GUILD_KEYS = (
    'id',
    'name',
    'icon',
    'owner_id',
    'owner',
    'member_count',
    'max_members',
    'description',
    'joined_at',
)

_CHANNEL_KEYS = (
    'id',
    'guild_id',
    'name',
    'type',
    'sub_type',
    'position',
    'owner_id',
)

_CHANNEL_SEND_PREFIXES = ('/channels/', '/dms/')
_EVENT_TIMEOUT = 10
_TEMPLATES_FILE = 'templates.yaml'
_CONFIG_ORDER = (
    'dispatch_to_plugins',
    'persist_snapshots',
    'enhance_channel_mentions',
    'default_appid',
    'auto_private_intents',
    'subscribe_open_forum_events',
    'subscribe_audio_live_events',
    'log_show_name',
    'log_show_channel_name',
    'channel_commands',
    'channel_owners',
)
_CONFIG_COMMENTS = {
    'dispatch_to_plugins': '是否把频道事件分发给显式声明 event_types 的插件',
    'persist_snapshots': '是否把频道/子频道快照写入 data/snapshots.json',
    'enhance_channel_mentions': '是否补齐频道消息的 mentions/is_at_self 字段',
    'default_appid': '频道接口兜底 appid；留空时优先使用当前事件机器人，事件之外仅在单机器人时自动选择',
    'auto_private_intents': '频道事件开启时是否尝试私域 GUILD_MESSAGES/FORUMS_EVENT，未授权自动回退',
    'subscribe_open_forum_events': '是否尝试公域论坛 OPEN_FORUM_EVENT intent，未授权自动回退',
    'subscribe_audio_live_events': '是否尝试音视频/直播成员 AUDIO_LIVE_MEMBER intent，未授权自动回退',
    'log_show_name': '频道事件日志显示成员名称；关闭后仅显示成员 ID',
    'log_show_channel_name': '频道事件日志显示频道/子频道名称；关闭后仅显示 ID',
    'channel_commands': {
        '__desc__': '通用指令拦截设置；统一作用于频道、群聊与好友聊天',
        'enabled': '是否启用通用指令拦截',
        'prefixes': '指令前缀列表，可配置多个',
        'at_without_prefix': '频道/群 @机器人 时是否允许省略前缀',
        'public_messages': '频道是否处理未 @机器人 的普通消息（需私域 GUILD_MESSAGES）',
        'group_at_messages': '是否处理群聊 @机器人 消息（GROUP_AT_MESSAGE_CREATE）',
        'group_messages': '是否处理群聊全量消息（GROUP_MESSAGE_CREATE）',
        'c2c_messages': '是否处理好友聊天消息（C2C_MESSAGE_CREATE）',
        'only_channel_handlers': '频道是否只匹配声明 channel_only 的指令；关闭后频道也匹配通用指令',
        'denied_reply': '无权限时的回复内容；留空则不回复',
    },
    'channel_owners': '频道主人列表，按 appid 分组；* 表示所有机器人共用',
}
_TEMPLATES_TEMPLATE = """# 频道 Markdown 模板库（模块首次启动时自动生成）
# 模板 ID 形如 <机器人 appid>_<时间戳>，前缀必须与机器人 appid 一致
# 修改后调用 adapter.reload_templates() 或重启模块
#
# bots:
#   '102030405':
#     default_template_id: '102030405_1658748972'
#     templates:
#       '102030405_1658748972':
#         content: |
#           {{.title}}
#           {{.desc}}
#         params: [默认标题, 默认描述]
#
# 变量按 content 中 {{.变量}} 首次出现的顺序自动提取，无需手写 variables。
# params 用列表时按变量顺序绑定默认值；发送时传 (值1, 值2) 即按位置覆盖。
bots: {}
"""


def _is_channel_send_endpoint(endpoint):
    path = str(endpoint or '').split('?', 1)[0].rstrip('/')
    return path.endswith('/messages') and path.startswith(_CHANNEL_SEND_PREFIXES)


def _downgrade_channel_markdown(data):
    """频道默认不支持 markdown.content，回复时降级为纯文本。

    显式传入 custom_template_id 的模板消息保持不变。
    """
    if not isinstance(data, dict):
        return data
    payload = data.get('payload')
    if not isinstance(payload, dict) or not _is_channel_send_endpoint(data.get('endpoint')):
        return data

    markdown = payload.get('markdown')
    if not isinstance(markdown, dict) or markdown.get('custom_template_id'):
        return data

    text = markdown.get('content')
    if not text:
        text = data.get('content') or payload.get('content') or ''
    text = str(text)
    payload.pop('markdown', None)
    payload.pop('msg_type', None)
    payload['content'] = text
    data['content'] = text
    return data


_TEMPLATE_VAR_RE = re.compile(r'\{\{\s*\.?([A-Za-z_][A-Za-z0-9_]*)\s*\}\}')


def _is_keyed_params(params):
    return isinstance(params, (list, tuple)) and any(
        isinstance(item, dict) and item.get('key') for item in params
    )


def _params_map(params):
    if isinstance(params, dict):
        return dict(params)
    if isinstance(params, str):
        return {'content': params}
    result = {}
    if isinstance(params, (list, tuple)):
        for item in params:
            if not isinstance(item, dict) or not item.get('key'):
                continue
            key = _text(item['key']).strip()
            if key:
                result[key] = item.get('values')
    return result


def _template_variables(content, configured=None):
    variables = []
    if isinstance(configured, (list, tuple)):
        for item in configured:
            key = _text(item).strip()
            if key and key not in variables:
                variables.append(key)
    if variables:
        return variables
    for key in _TEMPLATE_VAR_RE.findall(_text(content)):
        if key not in variables:
            variables.append(key)
    return variables


def _bind_params(defaults, variables, params):
    bound = _params_map(defaults)
    if params is None:
        return bound
    if isinstance(params, dict):
        bound.update(params)
        return bound
    if isinstance(params, str) or not isinstance(params, (list, tuple)):
        params = [params]
    if _is_keyed_params(params):
        bound.update(_params_map(params))
        return bound
    if not variables:
        raise ValueError('模板未声明变量，无法按位置传参')
    if len(params) > len(variables):
        raise ValueError(f'参数数量 {len(params)} 超过模板变量数量 {len(variables)}')
    for key, value in zip(variables, params):
        bound[key] = value
    return bound


_EVENT_LABELS = {
    GUILD_CREATE: '加入频道',
    GUILD_UPDATE: '频道更新',
    GUILD_DELETE: '离开频道',
    CHANNEL_CREATE: '子频道创建',
    CHANNEL_UPDATE: '子频道更新',
    CHANNEL_DELETE: '子频道删除',
    GUILD_MEMBER_ADD: '成员加入',
    GUILD_MEMBER_UPDATE: '成员更新',
    GUILD_MEMBER_REMOVE: '成员离开',
    AUDIO_OR_LIVE_CHANNEL_MEMBER_ENTER: '进入音视频子频道',
    AUDIO_OR_LIVE_CHANNEL_MEMBER_EXIT: '离开音视频子频道',
    MESSAGE_DELETE: '消息撤回',
    PUBLIC_MESSAGE_DELETE: '消息撤回',
    DIRECT_MESSAGE_DELETE: '私信撤回',
    MESSAGE_REACTION_ADD: '添加表态',
    MESSAGE_REACTION_REMOVE: '取消表态',
    MESSAGE_AUDIT_PASS: '消息审核通过',
    MESSAGE_AUDIT_REJECT: '消息审核未通过',
    FORUM_PUBLISH_AUDIT_RESULT: '论坛审核结果',
    AUDIO_START: '音频开始播放',
    AUDIO_FINISH: '音频播放结束',
    AUDIO_ON_MIC: '机器人上麦',
    AUDIO_OFF_MIC: '机器人下麦',
    C2C_MSG_RECEIVE: '开启单聊消息接收',
    C2C_MSG_REJECT: '关闭单聊消息接收',
}


def _event_label(event_type):
    label = _EVENT_LABELS.get(event_type)
    if label:
        return label
    if event_type in FORUM_THREAD_EVENTS:
        return f'{_forum_scope(event_type)}主题{_forum_action(event_type)}'
    if event_type in FORUM_POST_EVENTS:
        return f'{_forum_scope(event_type)}帖子{_forum_action(event_type)}'
    if event_type in FORUM_REPLY_EVENTS:
        return f'{_forum_scope(event_type)}回复{_forum_action(event_type)}'
    return event_type


_CHANNEL_TYPE_NAMES = {
    0: '文字',
    2: '语音',
    4: '分组',
    10005: '直播',
    10006: '应用',
    10007: '论坛',
}


def _event_data(event):
    raw = event.raw
    if not isinstance(raw, dict):
        return None
    data = raw.get('d')
    if isinstance(data, dict):
        return data
    if raw.get('_qqbotRawEvent') == event.event_type:
        return raw
    return None


def _text(value):
    return '' if value is None else str(value)


def _snippet(value, limit=40):
    text = _text(value).replace('\n', ' ').strip()
    return f'「{text[:limit]}」' if text else ''


def _forum_scope(event_type):
    if event_type.startswith('OPEN_FORUM_'):
        return '公域论坛'
    if event_type.startswith('AT_FORUM_'):
        return '论坛@'
    return '论坛'


def _forum_action(event_type):
    if event_type.endswith('_CREATE'):
        return '创建'
    if event_type.endswith('_UPDATE'):
        return '更新'
    if event_type.endswith('_DELETE'):
        return '删除'
    return '变更'


def _template_owner(template_id):
    text = _text(template_id).strip()
    owner, sep, suffix = text.partition('_')
    if sep and owner.isdigit() and suffix:
        return owner
    return ''


def _normalize_template(value):
    variables = []
    description = ''
    if isinstance(value, str):
        content = value
        raw_params = None
    elif isinstance(value, dict):
        content = _text(value.get('content', value.get('template', '')))
        raw_params = value.get('params')
        variables = _template_variables(content, value.get('variables'))
        description = _text(value.get('description') or value.get('desc'))
    else:
        return {}

    if not variables:
        variables = _template_variables(content)
    if isinstance(raw_params, str):
        raw_params = {'content': raw_params}
    if _is_keyed_params(raw_params):
        params = _params_map(raw_params)
    elif isinstance(raw_params, dict):
        params = dict(raw_params)
    elif isinstance(raw_params, (list, tuple)):
        params = {
            key: item
            for key, item in zip(variables, raw_params)
        }
    else:
        params = {}
    return {
        'content': content,
        'params': params,
        'variables': variables,
        'description': description,
    }


def _normalize_template_book(value):
    book = {'default_template_id': '', 'templates': {}}
    if not isinstance(value, dict):
        return book
    book['default_template_id'] = _text(value.get('default_template_id')).strip()
    raw_templates = value.get('templates')
    if isinstance(raw_templates, dict):
        for template_id, item in raw_templates.items():
            tid = _text(template_id).strip()
            entry = _normalize_template(item)
            if tid and entry:
                book['templates'][tid] = entry
    return book


def _ensure_templates(ctx):
    """确保 data/templates.yaml 存在，并按机器人 appid 读取模板库。"""
    if not ctx.data_exists(_TEMPLATES_FILE):
        ctx.save_data(_TEMPLATES_FILE, _TEMPLATES_TEMPLATE)
        ctx.log.info(f'频道模板配置已自动生成: {_TEMPLATES_FILE}')

    raw_text = ctx.read_data(_TEMPLATES_FILE) or ''
    raw = ctx.read_config(_TEMPLATES_FILE)
    if isinstance(raw, dict) and 'bots' not in raw and not raw.get('templates') and not _text(raw.get('default_template_id')).strip():
        ctx.save_data(_TEMPLATES_FILE, _TEMPLATES_TEMPLATE)
        raw = ctx.read_config(_TEMPLATES_FILE)
    elif isinstance(raw, dict) and not raw.get('bots') and 'description: 机器人信息卡' in raw_text:
        # 把旧版自动生成的空模板文件整理为精简示例
        ctx.save_data(_TEMPLATES_FILE, _TEMPLATES_TEMPLATE)
        raw = ctx.read_config(_TEMPLATES_FILE)
    if not isinstance(raw, dict):
        return {}

    books = {}
    raw_bots = raw.get('bots')
    if isinstance(raw_bots, dict):
        for appid, value in raw_bots.items():
            aid = _text(appid).strip()
            if not aid:
                continue
            book = _normalize_template_book(value)
            if book['default_template_id'] or book['templates']:
                books[aid] = book

    legacy_templates = raw.get('templates')
    if isinstance(legacy_templates, dict):
        for template_id, value in legacy_templates.items():
            tid = _text(template_id).strip()
            owner = _template_owner(tid)
            entry = _normalize_template(value)
            if not (tid and owner and entry):
                continue
            book = books.setdefault(owner, {'default_template_id': '', 'templates': {}})
            book['templates'].setdefault(tid, entry)

    legacy_default = _text(raw.get('default_template_id')).strip()
    owner = _template_owner(legacy_default)
    if owner:
        book = books.setdefault(owner, {'default_template_id': '', 'templates': {}})
        if not book['default_template_id']:
            book['default_template_id'] = legacy_default
    return books


def _channel_type_name(value):
    try:
        return _CHANNEL_TYPE_NAMES.get(int(value), f'类型{value}')
    except (TypeError, ValueError):
        return '未知类型'


def _copy_keys(data, keys):
    return {key: data[key] for key in keys if key in data}


def _guild_entry(data):
    entry = _copy_keys(data, _GUILD_KEYS)
    entry['updated_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
    return entry


def _channel_entry(data):
    entry = _copy_keys(data, _CHANNEL_KEYS)
    entry['updated_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
    return entry


class ChannelAdapter:
    """频道事件归一化、插件分发与频道/子频道快照"""

    __slots__ = (
        '_ctx',
        '_cfg',
        '_guilds',
        '_channels',
        '_dirty',
        '_templates',
        'api',
        'commands',
    )

    def __init__(self, ctx, cfg):
        self._ctx = ctx
        self._cfg = cfg
        self._guilds = {}
        self._channels = {}
        self._dirty = False
        self._templates = {}
        self.api = ChannelAPI(self)
        self.commands = ChannelCommandRouter(self)
        self._load_snapshots()
        self.reload_templates()

    async def handle(self, event):
        # 绑定事件来源机器人；指令处理任务会继承该上下文，
        # 插件不传 appid 时 ChannelAPI 自动使用收到消息的机器人。
        with appid_context(getattr(event, 'appid', '')):
            return await self._handle(event)

    async def _handle(self, event):
        event_type = getattr(event, 'event_type', '')
        if event_type in COMMAND_EVENTS:
            if self._cfg.get('enhance_channel_mentions', True):
                self._enhance_message(event)
            try:
                if await self.commands.handle(event):
                    return None
            except Exception as exc:
                report_error(
                    EXTENSION,
                    '通用指令',
                    exc,
                    context={'appid': event.appid, 'event_type': event_type},
                )
            return event
        try:
            async with asyncio.timeout(_EVENT_TIMEOUT):
                return await self._process_event(event)
        except TimeoutError:
            report_error(
                EXTENSION,
                '频道事件适配',
                f'频道事件处理超时({_EVENT_TIMEOUT}s)',
                context={'appid': event.appid, 'event_type': event.event_type},
            )
            return event
        except Exception as exc:
            report_error(
                EXTENSION,
                '频道事件适配',
                exc,
                context={'appid': event.appid, 'event_type': event.event_type},
            )
            return event

    async def _process_event(self, event):
        event_type = event.event_type
        if event_type not in CHANNEL_EVENTS and event_type not in DIRECT_LIFECYCLE_EVENTS:
            return event

        data = _event_data(event)
        if not isinstance(data, dict):
            return event

        if event_type in DIRECT_LIFECYCLE_EVENTS:
            self._fill_direct_event(event, event_type, data)
        else:
            self._fill_event(event, event_type, data)
            self._update_snapshots(event_type, data)
            await self._persist_snapshots()

        if event_type not in CORE_SILENT_EVENTS:
            self._log_event(event)

        if self._cfg.get('dispatch_to_plugins', True):
            spawn(self._dispatch(event))

        return event if event_type in CORE_SILENT_EVENTS else None

    def _fill_event(self, event, event_type, data):
        event.is_channel = True
        event.is_group = False
        event.is_direct = False

        guild_id = ''
        channel_id = ''
        user_id = ''
        username = ''
        message_id = ''

        if event_type in GUILD_UPSERT_EVENTS or event_type == GUILD_DELETE:
            guild_id = _text(data.get('id'))
            user_id = _text(data.get('op_user_id') or data.get('owner_id'))
        elif event_type in CHANNEL_UPSERT_EVENTS or event_type == CHANNEL_DELETE:
            guild_id = _text(data.get('guild_id'))
            channel_id = _text(data.get('id'))
            user_id = _text(data.get('op_user_id') or data.get('owner_id'))
        elif event_type in GUILD_MEMBER_EVENTS:
            guild_id = _text(data.get('guild_id'))
            member = data.get('user')
            member = member if isinstance(member, dict) else {}
            user_id = _text(member.get('id') or data.get('user_id') or data.get('op_user_id'))
            username = _text(member.get('username') or data.get('username'))
        elif event_type in AUDIO_MEMBER_EVENTS:
            guild_id = _text(data.get('guild_id'))
            channel_id = _text(data.get('channel_id'))
            user_id = _text(data.get('user_id'))
        elif event_type in MESSAGE_DELETE_EVENTS:
            guild_id = _text(data.get('guild_id'))
            channel_id = _text(data.get('channel_id'))
            message_id = _text(data.get('id'))
            user_id = _text(data.get('op_user_id') or data.get('user_id'))
        elif event_type in REACTION_EVENTS:
            guild_id = _text(data.get('guild_id'))
            channel_id = _text(data.get('channel_id'))
            target = data.get('target')
            target = target if isinstance(target, dict) else {}
            message_id = _text(target.get('id') or data.get('message_id'))
            user_id = _text(data.get('user_id') or data.get('op_user_id'))
        elif event_type in AUDIT_EVENTS:
            guild_id = _text(data.get('guild_id'))
            channel_id = _text(data.get('channel_id'))
            message_id = _text(data.get('message_id'))
            user_id = _text(data.get('op_user_id') or data.get('user_id'))
        elif event_type in FORUM_THREAD_EVENTS:
            info = data.get('thread_info')
            info = info if isinstance(info, dict) else {}
            guild_id = _text(data.get('guild_id'))
            channel_id = _text(data.get('channel_id'))
            user_id = _text(data.get('author_id') or info.get('author_id'))
            message_id = _text(info.get('thread_id'))
        elif event_type in FORUM_POST_EVENTS:
            info = data.get('post_info')
            info = info if isinstance(info, dict) else {}
            guild_id = _text(data.get('guild_id'))
            channel_id = _text(data.get('channel_id'))
            user_id = _text(data.get('author_id') or info.get('author_id'))
            message_id = _text(info.get('post_id'))
        elif event_type in FORUM_REPLY_EVENTS:
            info = data.get('reply_info')
            info = info if isinstance(info, dict) else {}
            guild_id = _text(data.get('guild_id'))
            channel_id = _text(data.get('channel_id'))
            user_id = _text(data.get('author_id') or info.get('author_id'))
            message_id = _text(info.get('reply_id'))
        elif event_type in FORUM_AUDIT_EVENTS:
            guild_id = _text(data.get('guild_id'))
            channel_id = _text(data.get('channel_id'))
            user_id = _text(data.get('author_id'))
            message_id = _text(
                data.get('reply_id') or data.get('post_id') or data.get('thread_id') or data.get('task_id')
            )
        elif event_type in AUDIO_ACTION_EVENTS:
            guild_id = _text(data.get('guild_id'))
            channel_id = _text(data.get('channel_id'))

        event.guild_id = guild_id or None
        event.channel_id = channel_id or None
        event.user_id = user_id or None
        event.raw_user_id = event.user_id
        if username:
            event.username = username
        if message_id:
            event.message_id = message_id
        if not event.timestamp:
            event.timestamp = (
                data.get('timestamp')
                or data.get('joined_at')
                or data.get('audit_time')
                or data.get('create_time')
                or data.get('date_time')
                or data.get('apply_at')
                or ''
            )
        event.content = self._describe(event_type, data, guild_id, channel_id, user_id, username, message_id)

    def _fill_direct_event(self, event, event_type, data):
        event.is_channel = False
        event.is_group = False
        event.is_direct = True
        event.is_lifecycle = True
        event.group_id = ''

        user_id = _text(
            data.get('openid')
            or data.get('user_openid')
            or data.get('op_member_openid')
            or data.get('user_id')
        )
        event.user_id = event.raw_user_id = user_id or None
        if not event.timestamp:
            event.timestamp = data.get('timestamp') or ''
        action = '开启' if event_type == C2C_MSG_RECEIVE else '关闭'
        event.content = f'用户 {user_id or "未知"} {action}单聊消息接收'

    @staticmethod
    def _describe(event_type, data, guild_id, channel_id, user_id, username, message_id):
        name = _text(data.get('name'))
        label = f'「{name}」' if name else ''
        if event_type == GUILD_CREATE:
            return f'机器人加入频道{label} {guild_id}'
        if event_type == GUILD_UPDATE:
            return f'频道{label} {guild_id} 信息更新'
        if event_type == GUILD_DELETE:
            return f'频道{label} {guild_id} 已解散或机器人被移除'
        if event_type == CHANNEL_CREATE:
            return f'子频道{label} {channel_id} 在频道 {guild_id} 创建'
        if event_type == CHANNEL_UPDATE:
            return f'子频道{label} {channel_id} 在频道 {guild_id} 更新'
        if event_type == CHANNEL_DELETE:
            return f'子频道{label} {channel_id} 在频道 {guild_id} 删除'
        if event_type == GUILD_MEMBER_ADD:
            return f'用户 {username or user_id} 加入频道 {guild_id}'
        if event_type == GUILD_MEMBER_UPDATE:
            return f'频道成员 {username or user_id} 资料更新'
        if event_type == GUILD_MEMBER_REMOVE:
            return f'用户 {username or user_id} 离开频道 {guild_id}'
        if event_type == AUDIO_OR_LIVE_CHANNEL_MEMBER_ENTER:
            return f'用户 {user_id} 进入音视频/直播子频道 {channel_id}'
        if event_type == AUDIO_OR_LIVE_CHANNEL_MEMBER_EXIT:
            return f'用户 {user_id} 离开音视频/直播子频道 {channel_id}'
        if event_type in (MESSAGE_DELETE, PUBLIC_MESSAGE_DELETE):
            return f'频道消息 {message_id} 被撤回'
        if event_type == DIRECT_MESSAGE_DELETE:
            return f'频道私信消息 {message_id} 被撤回'
        if event_type in REACTION_EVENTS:
            emoji = data.get('emoji')
            emoji = emoji if isinstance(emoji, dict) else {}
            emoji_id = _text(emoji.get('id'))
            action = '添加' if event_type == MESSAGE_REACTION_ADD else '移除'
            suffix = f' 表情 {emoji_id}' if emoji_id else '表情'
            return f'用户 {user_id} 在频道 {channel_id} 对消息 {message_id} {action}{suffix}表态'
        if event_type in AUDIT_EVENTS:
            action = '通过' if event_type == MESSAGE_AUDIT_PASS else '未通过'
            return f'频道消息 {message_id} 审核{action}'
        if event_type in FORUM_THREAD_EVENTS:
            info = data.get('thread_info')
            info = info if isinstance(info, dict) else {}
            label = _snippet(info.get('title'))
            return f'{_forum_scope(event_type)}主题 {message_id}{label} {_forum_action(event_type)}'
        if event_type in FORUM_POST_EVENTS:
            info = data.get('post_info')
            info = info if isinstance(info, dict) else {}
            label = _snippet(info.get('content'))
            return f'{_forum_scope(event_type)}帖子 {message_id}{label} {_forum_action(event_type)}'
        if event_type in FORUM_REPLY_EVENTS:
            info = data.get('reply_info')
            info = info if isinstance(info, dict) else {}
            label = _snippet(info.get('content'))
            return f'{_forum_scope(event_type)}回复 {message_id}{label} {_forum_action(event_type)}'
        if event_type in FORUM_AUDIT_EVENTS:
            err = _text(data.get('err_msg'))
            suffix = f': {err}' if err else ''
            return f'论坛内容审核结果 {data.get("result", "")}{suffix}'
        if event_type in AUDIO_ACTION_EVENTS:
            labels = {
                AUDIO_START: '音频开始播放',
                AUDIO_FINISH: '音频播放结束',
                AUDIO_ON_MIC: '机器人上麦',
                AUDIO_OFF_MIC: '机器人下麦',
            }
            label = labels.get(event_type, event_type)
            return f'{label} 子频道 {channel_id}{_snippet(data.get("text"))}'
        return event_type

    def _update_snapshots(self, event_type, data):
        if event_type in GUILD_UPSERT_EVENTS:
            guild_id = _text(data.get('id'))
            if guild_id:
                self._guilds[guild_id] = _guild_entry(data)
                self._dirty = True
        elif event_type == GUILD_DELETE:
            guild_id = _text(data.get('id'))
            if guild_id:
                self._guilds.pop(guild_id, None)
                for channel_id, channel in list(self._channels.items()):
                    if _text(channel.get('guild_id')) == guild_id:
                        self._channels.pop(channel_id, None)
                self._dirty = True
        elif event_type in CHANNEL_UPSERT_EVENTS:
            channel_id = _text(data.get('id'))
            if channel_id:
                self._channels[channel_id] = _channel_entry(data)
                self._dirty = True
        elif event_type == CHANNEL_DELETE:
            channel_id = _text(data.get('id'))
            if channel_id:
                self._channels.pop(channel_id, None)
                self._dirty = True

    def _load_snapshots(self):
        if not self._cfg.get('persist_snapshots', True):
            return
        raw = self._ctx.read_data('snapshots.json')
        if not raw:
            return
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(data, dict):
            return
        guilds = data.get('guilds')
        channels = data.get('channels')
        if isinstance(guilds, dict):
            self._guilds = {str(key): value for key, value in guilds.items() if isinstance(value, dict)}
        if isinstance(channels, dict):
            self._channels = {str(key): value for key, value in channels.items() if isinstance(value, dict)}

    async def _persist_snapshots(self):
        if not self._dirty or not self._cfg.get('persist_snapshots', True):
            return
        self._dirty = False
        payload = json.dumps(
            {'guilds': self._guilds, 'channels': self._channels},
            ensure_ascii=False,
            indent=2,
        )
        try:
            await self._ctx.save_data_async('snapshots.json', payload)
        except Exception as exc:
            self._dirty = True
            log.warning(f'保存频道快照失败: {exc}')

    def _event_names(self, event):
        data = _event_data(event) or {}
        guild = self._guilds.get(_text(event.guild_id)) or {}
        channel = self._channels.get(_text(event.channel_id)) or {}
        guild_name = _text(guild.get('name'))
        channel_name = _text(channel.get('name'))
        if event.event_type in GUILD_UPSERT_EVENTS or event.event_type == GUILD_DELETE:
            guild_name = _text(data.get('name')) or guild_name
        if event.event_type in CHANNEL_UPSERT_EVENTS or event.event_type == CHANNEL_DELETE:
            channel_name = _text(data.get('name')) or channel_name
        return guild_name, channel_name

    def _log_event(self, event):
        app = get_app()
        bot = app.get_bot(event.appid) if app is not None else None
        if bot is None:
            return

        code = event.event_type
        label = _event_label(code)
        guild_name, channel_name = self._event_names(event)
        guild_id = _text(getattr(event, 'guild_id', ''))
        channel_id = _text(getattr(event, 'channel_id', ''))
        username = _text(getattr(event, 'username', ''))
        user_id = _text(getattr(event, 'user_id', ''))
        # 开关打开时优先显示名称（框架里 event.username 通常是「名称(ID)」格式），
        # 关闭时只显示成员 ID；两者都为空时互相回退，避免出现空括号。
        if self._cfg.get('log_show_name', True):
            display_user = username or user_id
        else:
            display_user = user_id or username
        # 频道名称开关：打开时显示频道/子频道名称，关闭时显示 ID。
        if self._cfg.get('log_show_channel_name', True):
            display_group = ' / '.join(part for part in (guild_name or guild_id, channel_name or channel_id) if part)
        else:
            display_group = ' / '.join(part for part in (guild_id, channel_id) if part)
        content = _text(getattr(event, 'content', ''))
        extra = json.dumps(event.raw, ensure_ascii=False)
        entry = {
            'type': label,
            'user_id': display_user,
            'group_id': display_group,
            'extra': extra,
        }
        try:
            bot.log_service.add_sync('lifecycle', entry)
        except Exception:
            pass

        push = getattr(app, '_push_web_log', None)
        if push is None:
            return
        try:
            push(
                'event',
                {
                    'appid': event.appid,
                    'bot_name': bot.name,
                    'event_type': label,
                    'event_code': code,
                    'user_id': display_user,
                    'group_id': display_group,
                    'guild_id': guild_id,
                    'channel_id': channel_id,
                    'channel_name': channel_name,
                    'content': content,
                    'raw_message': extra,
                },
            )
        except Exception:
            pass


    async def _dispatch(self, event):
        try:
            app = get_app()
            if app is None:
                return
            plugin_manager = app.plugin_manager
            bot = app.get_bot(event.appid)
            if plugin_manager is None or bot is None:
                return
            await plugin_manager.dispatch(event, bot.sender, explicit_only=True)
        except Exception as exc:
            report_error(
                EXTENSION,
                '频道事件分发',
                exc,
                context={'appid': event.appid, 'event_type': event.event_type},
            )

    def _enhance_message(self, event):
        data = _event_data(event)
        if not isinstance(data, dict):
            return
        mentions = data.get('mentions')
        if not isinstance(mentions, list):
            return
        event.mentions = [item for item in mentions if isinstance(item, dict)]

        bot = get_app().get_bot(event.appid) if get_app() is not None else None
        bot_id = _text(getattr(bot, 'bot_id', ''))
        for mention in event.mentions:
            mention_id = _text(mention.get('id'))
            is_you = mention.get('is_you') is True or bool(bot_id and mention_id == bot_id)
            if is_you:
                event.is_at_self = True
            elif mention.get('scope') == 'all':
                event.is_at_all = True
            elif mention.get('bot') is True:
                event.is_at_other_bot = True
            else:
                event.is_at_other_user = True

        if event.event_type == AT_MESSAGE_CREATE:
            event.is_at_self = True

    def _template_appid(self, template_id=None, appid=None):
        target = _text(appid).strip()
        if target:
            return target
        owner = _template_owner(template_id)
        if owner:
            return owner
        try:
            return self.api.resolve_appid(None)
        except (RuntimeError, ValueError):
            if len(self._templates) == 1:
                return next(iter(self._templates))
            raise

    def _template_book(self, appid=None, template_id=None):
        target = self._template_appid(template_id, appid)
        book = self._templates.get(target)
        if book is None:
            book = {'default_template_id': '', 'templates': {}}
        return target, book

    def _template_entry(self, template_id=None, appid=None):
        target, book = self._template_book(appid, template_id)
        tid = _text(template_id or book.get('default_template_id')).strip()
        if not tid and len(book['templates']) == 1:
            tid = next(iter(book['templates']))
        if not tid:
            return target, '', {}
        owner = _template_owner(tid)
        if owner and owner != target:
            raise ValueError(f'模板 {tid} 属于机器人 {owner}，当前机器人是 {target}')
        return target, tid, book['templates'].get(tid) or {}

    def reload_templates(self):
        """重新读取 data/templates.yaml，返回模板总数。"""
        self._templates = _ensure_templates(self._ctx)
        return sum(len(book['templates']) for book in self._templates.values())

    def get_template(self, template_id=None, *, appid=None):
        """按机器人 appid 与模板 ID 读取模板；不传模板 ID 时使用该机器人的默认模板。"""
        if not self._templates and not template_id:
            return {}
        target, tid, entry = self._template_entry(template_id, appid)
        if not entry:
            return {}
        return {'id': tid, 'appid': target, **entry}

    def default_template(self, *, appid=None):
        """返回该机器人的默认模板；没有默认值时使用唯一的模板。"""
        if not self._templates:
            return {}
        target, book = self._template_book(appid)
        tid = _text(book.get('default_template_id')).strip()
        if not tid and len(book['templates']) == 1:
            tid = next(iter(book['templates']))
        if not tid:
            return {}
        _, tid, entry = self._template_entry(tid, target)
        if not entry:
            return {}
        return {'id': tid, 'appid': target, **entry}

    def list_templates(self, *, appid=None):
        if not self._templates:
            return []
        target, book = self._template_book(appid)
        items = []
        for tid, entry in book['templates'].items():
            items.append(
                {
                    'id': tid,
                    'appid': target,
                    'content': entry.get('content', ''),
                    'params': dict(entry.get('params') or {}),
                    'variables': list(entry.get('variables') or []),
                    'description': entry.get('description', ''),
                }
            )
        return items

    async def send_template(self, channel_id, template_id=None, params=None, *, content=None, appid=None, **kwargs):
        """按机器人 appid 与模板 ID 发送频道 Markdown 模板消息。

        params 支持 dict、[{key, values}]，也支持按 variables 顺序传元组或
        列表；未覆盖的变量继续使用模板默认值。kwargs 透传给
        send_channel_markdown_template。
        """
        target, tid, entry = self._template_entry(template_id, appid)
        if not entry:
            raise ValueError(f'机器人 {target} 未配置频道模板: {tid or "<默认模板>"}')
        values = _bind_params(entry.get('params'), entry.get('variables') or [], params)
        return await self.api.send_channel_markdown_template(
            channel_id,
            tid,
            values,
            content,
            appid=target,
            **kwargs,
        )

    def get_config(self, key=None, default=None):
        if key is None:
            return dict(self._cfg)
        return self._cfg.get(key, default)

    def update_settings(self, values):
        """更新并持久化模块配置，返回更新后的配置副本。"""
        if not isinstance(values, dict):
            raise ValueError('配置必须是字典')

        config = dict(self._cfg)
        config.update(values)
        config['channel_commands'] = normalize_command_config(config.get('channel_commands'))
        # 内存里统一用 dict 形式，和 setup() 保持一致
        normalized_owners = normalize_owners(config.get('channel_owners'))
        config['channel_owners'] = normalized_owners

        # 落盘时用便于 YAML 阅读的 list 形式
        save_payload = dict(config)
        save_payload['channel_owners'] = owners_to_config(normalized_owners)
        try:
            self._ctx.save_config(save_payload, comments=_CONFIG_COMMENTS)
        except TypeError:
            # 兼容不支持 comments 参数的框架版本
            self._ctx.save_config(save_payload)

        self._cfg.clear()
        self._cfg.update(config)
        return dict(self._cfg)

    # ==================== 插件开发辅助 ====================

    @staticmethod
    def scene(event):
        """返回事件场景: channel / group / direct / unknown。"""
        return scene_name(event)

    @staticmethod
    def event_appid(event):
        """返回事件来源机器人 appid。"""
        return _text(getattr(event, 'appid', ''))

    @staticmethod
    def _channel_target(event):
        return (
            getattr(event, 'is_channel', False)
            and bool(getattr(event, 'channel_id', ''))
            and getattr(event, 'event_type', '') in (AT_MESSAGE_CREATE, MESSAGE_CREATE)
        )

    def reply(self, event, content=None, **kwargs):
        """统一回复入口，需要 await。

        频道消息走 send_channel_message（自动带上被动回复 msg_id），
        群聊与好友聊天走框架的 event.reply。
        """
        if self._channel_target(event):
            kwargs.setdefault('msg_id', getattr(event, 'message_id', '') or None)
            return self.api.send_channel_message(event.channel_id, content, appid=self.event_appid(event), **kwargs)
        return event.reply(content, **kwargs)

    def send_image(self, event, image, content='', **kwargs):
        """统一发图入口，需要 await。

        image 支持公网 URL、bytes、文件对象或本地路径；频道走频道图片接口，
        群聊与好友聊天走框架的 reply_image。
        """
        if self._channel_target(event):
            kwargs.setdefault('msg_id', getattr(event, 'message_id', '') or None)
            return self.api.send_channel_image(event.channel_id, image, content, appid=self.event_appid(event), **kwargs)
        return event.reply_image(image, content=content, **kwargs)

    async def sync_guild(self, guild_id, *, appid=None):
        """拉取频道详情并合并到本地快照"""
        data = await self.api.fetch_guild(guild_id, appid=appid)
        gid = _text(data.get('id') or guild_id)
        if gid:
            self._guilds[gid] = _guild_entry(data)
            self._dirty = True
            await self._persist_snapshots()
        return dict(data)

    def get_guild(self, guild_id):
        return dict(self._guilds.get(_text(guild_id)) or {})

    def get_channel(self, channel_id):
        return dict(self._channels.get(_text(channel_id)) or {})

    def list_guilds(self):
        return [dict(item) for item in self._guilds.values()]

    def list_channels(self, guild_id=None):
        if guild_id is None:
            return [dict(item) for item in self._channels.values()]
        target = _text(guild_id)
        return [dict(item) for item in self._channels.values() if _text(item.get('guild_id')) == target]

    def channel_type_name(self, channel):
        value = channel.get('type') if isinstance(channel, dict) else channel
        return _channel_type_name(value)


async def setup(ctx):
    cfg = ctx.ensure_config(
        {
            'dispatch_to_plugins': True,
            'persist_snapshots': True,
            'enhance_channel_mentions': True,
            'default_appid': '',
            'auto_private_intents': True,
            'subscribe_open_forum_events': False,
            'subscribe_audio_live_events': False,
            'log_show_name': True,
            'log_show_channel_name': True,
            'channel_commands': {
                'enabled': True,
                'prefixes': ['/'],
                'at_without_prefix': True,
                'public_messages': False,
                'group_at_messages': True,
                'group_messages': True,
                'c2c_messages': True,
                'only_channel_handlers': False,
                'denied_reply': '暂无指令权限',
            },
            'channel_owners': [{'appid': '*', 'user_ids': []}],
        },
        comments=_CONFIG_COMMENTS,
    )
    ordered_keys = [key for key in _CONFIG_ORDER if key in cfg]
    ordered_keys += [key for key in cfg if key not in _CONFIG_ORDER]
    if list(cfg) != ordered_keys:
        cfg = {key: cfg[key] for key in ordered_keys}
        ctx.save_config(cfg, comments=_CONFIG_COMMENTS)
        ctx.log.info('配置文件已按新布局整理: config.yaml')
    cfg['channel_commands'] = normalize_command_config(cfg.get('channel_commands'))
    cfg['channel_owners'] = normalize_owners(cfg.get('channel_owners'))

    from .intents import install as install_private_intents

    try:
        install_params = inspect.signature(install_private_intents).parameters
    except (TypeError, ValueError):
        install_params = {}

    def _load_intent_state():
        raw = ctx.read_data('intents_state.json')
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_intent_state(payload):
        try:
            ctx.save_data('intents_state.json', json.dumps(payload, ensure_ascii=False, indent=2))
        except Exception as exc:
            log.warning(f'保存频道 intent 拒绝记录失败: {exc}')

    install_kwargs = {}
    if 'log' in install_params:
        install_kwargs['log'] = log
    if 'state_loader' in install_params:
        install_kwargs['state_loader'] = _load_intent_state
    if 'state_saver' in install_params:
        install_kwargs['state_saver'] = _save_intent_state

    optional_flags = {
        'open_forum': bool(cfg.get('subscribe_open_forum_events', False)),
        'audio_live': bool(cfg.get('subscribe_audio_live_events', False)),
    }
    for name, enabled in optional_flags.items():
        if name in install_params:
            install_kwargs[name] = enabled
        elif enabled:
            log.warning(f'channel_adapter/intents.py 版本过旧，{name} 开关暂不生效，请同步该文件')

    install_private_intents(bool(cfg.get('auto_private_intents', True)), **install_kwargs)

    adapter = ChannelAdapter(ctx, cfg)

    @ctx.hook('before_event', priority=10)
    async def on_before_event(event):
        return await adapter.handle(event)

    @ctx.hook('before_send', priority=20)
    async def on_before_send(data):
        return _downgrade_channel_markdown(data)

    from . import panel as channel_panel

    channel_panel.register(ctx, adapter)

    log.info('频道事件适配已启用')
    return adapter


async def teardown():
    from . import panel as channel_panel

    channel_panel.unregister()
