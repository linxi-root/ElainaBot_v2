"""频道适配模块 Web 面板

注册频道适配模块的配置页面与 /api/ext 路由，用于管理频道指令开关、
前缀、事件日志显示名和频道主人列表。
"""

from __future__ import annotations

from aiohttp import web

from core.application import get_app
from core.base.config import cfg
from core.base.logger import EXTENSION, get_logger
from core.plugin.web_pages import register_page, register_route, unregister_page, unregister_route

from .commands import normalize_command_config, normalize_owners

log = get_logger(EXTENSION, '频道适配面板')

PAGE_KEY = 'channel-adapter'
_ROUTE_PREFIX = '/api/ext/channel-adapter'
_ROUTES = (
    ('GET', f'{_ROUTE_PREFIX}/config'),
    ('POST', f'{_ROUTE_PREFIX}/config'),
)


def _running_bots():
    """返回 {appid: 运行时名称}，来源是当前已启动的 bot registry。"""
    running = {}
    app = get_app()
    registry = getattr(app, 'bot_registry', None) if app is not None else None
    if registry is None:
        return running

    raw_bots = getattr(registry, 'bots', None)
    if isinstance(raw_bots, dict):
        iterable = raw_bots.values()
    elif isinstance(raw_bots, (list, tuple, set)):
        iterable = raw_bots
    else:
        iterable = []

    for bot in iterable:
        appid = str(getattr(bot, 'appid', '') or '').strip()
        if not appid:
            continue
        running[appid] = str(getattr(bot, 'name', '') or appid)
    return running


def _bot_list():
    """合并配置文件与运行中的机器人，尽量给出可用名称与运行状态。"""
    running = _running_bots()
    bots = []
    seen = set()

    for item in cfg.get_bot_configs() or []:
        if not isinstance(item, dict):
            continue
        appid = str(item.get('appid') or '').strip()
        if not appid or appid in seen:
            continue
        seen.add(appid)
        name = running.get(appid) or str(item.get('name') or '').strip() or appid
        bots.append({'appid': appid, 'name': name, 'running': appid in running})

    for appid, name in running.items():
        if appid not in seen:
            bots.append({'appid': appid, 'name': name, 'running': True})
    return bots


def _snapshot(adapter):
    config = adapter.get_config() or {}
    return {
        'channel_commands': normalize_command_config(config.get('channel_commands')),
        'channel_owners': normalize_owners(config.get('channel_owners')),
        'log_show_name': bool(config.get('log_show_name', True)),
    }


def register(ctx, adapter):
    async def get_config(request):
        return web.json_response({'ok': True, 'config': _snapshot(adapter), 'bots': _bot_list()})

    async def save_config(request):
        try:
            payload = await request.json()
        except Exception as exc:
            log.warning(f'频道适配配置保存：请求内容不是 JSON: {exc}')
            return web.json_response({'ok': False, 'error': '请求内容必须是 JSON'}, status=400)
        if not isinstance(payload, dict):
            return web.json_response({'ok': False, 'error': '请求内容必须是对象'}, status=400)

        values = {}
        if 'channel_commands' in payload:
            values['channel_commands'] = payload.get('channel_commands')
        if 'channel_owners' in payload:
            values['channel_owners'] = payload.get('channel_owners')
        if 'log_show_name' in payload:
            values['log_show_name'] = bool(payload.get('log_show_name'))
        if not values:
            return web.json_response({'ok': False, 'error': '没有可保存的字段'}, status=400)

        if 'channel_owners' in values:
            raw_owners = values['channel_owners']
            if raw_owners is not None and not isinstance(raw_owners, (dict, list)):
                return web.json_response(
                    {'ok': False, 'error': 'channel_owners 必须是对象或列表'},
                    status=400,
                )

        try:
            adapter.update_settings(values)
        except ValueError as exc:
            log.warning(f'频道适配配置校验失败: {exc}')
            return web.json_response({'ok': False, 'error': str(exc)}, status=400)
        except Exception as exc:
            log.exception('频道适配配置保存失败')
            return web.json_response({'ok': False, 'error': f'保存失败：{exc}'}, status=500)

        return web.json_response({'ok': True, 'config': _snapshot(adapter)})

    register_page(
        key=PAGE_KEY,
        label='频道适配',
        source='module',
        source_name='channel_adapter',
        html_file=ctx.get_resource_path('assets/panel.html'),
        icon='chatbubbles',
    )
    register_route('GET', f'{_ROUTE_PREFIX}/config', get_config)
    register_route('POST', f'{_ROUTE_PREFIX}/config', save_config)


def unregister():
    unregister_page(PAGE_KEY)
    for method, path in _ROUTES:
        unregister_route(method, path)