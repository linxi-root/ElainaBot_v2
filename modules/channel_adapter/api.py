"""QQ 频道服务端接口封装

覆盖官方文档「频道管理」下的子频道、成员、身份组与权限、接口授权、
发言管理、内容管理接口。小程序相关接口属于前端 SDK，不在此模块内。
"""

import json
import mimetypes
import os

from core.application import get_app

from .context import current_appid

try:
    import httpx as _httpx  # noqa: F401
    _HAS_HTTPX = True
except ImportError:
    _HAS_HTTPX = False


class ChannelAPIError(RuntimeError):
    """频道接口调用失败"""

    def __init__(self, action, data, *, method, endpoint, appid):
        self.action = action
        self.data = data if isinstance(data, dict) else {'message': str(data)}
        self.code = self.data.get('code', self.data.get('err_code'))
        self.method = method
        self.endpoint = endpoint
        self.appid = appid
        message = self.data.get('message') or self.data.get('msg') or '未知错误'
        detail = f'{action}失败: {message} ({method} {endpoint}'
        if self.code not in (None, ''):
            detail += f', code={self.code}'
        if appid:
            detail += f', appid={appid}'
        detail += ')'
        super().__init__(detail)


def _require_id(value, name):
    text = '' if value is None else str(value).strip()
    if not text:
        raise ValueError(f'{name} 不能为空')
    return text


def _optional(value):
    if value is None:
        return None
    return str(value).strip()


def _payload(**fields):
    return {key: value for key, value in fields.items() if value is not None}


def _require_payload(payload, message):
    if not payload:
        raise ValueError(message)
    return payload


def _page_limit(limit, *, default=None):
    if limit is None:
        return default
    try:
        value = int(limit)
    except (TypeError, ValueError) as exc:
        raise ValueError('limit 必须是整数') from exc
    if not 1 <= value <= 400:
        raise ValueError('limit 必须在 1-400 之间')
    return value


def _mute_payload(mute_end_timestamp=None, mute_seconds=None):
    payload = _payload(
        mute_end_timestamp=_optional(mute_end_timestamp),
        mute_seconds=_optional(mute_seconds),
    )
    return _require_payload(payload, 'mute_end_timestamp 与 mute_seconds 至少提供一个')


def _permission_payload(add=None, remove=None):
    payload = _payload(
        add=_optional(add),
        remove=_optional(remove),
    )
    return _require_payload(payload, 'add 与 remove 至少提供一个')


def _form_fields(fields):
    """把 multipart 字段序列化为字符串，对象/数组按文档要求转 JSON。"""
    result = {}
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, (dict, list, tuple)):
            result[key] = json.dumps(value, ensure_ascii=False)
        elif isinstance(value, bool):
            result[key] = 'true' if value else 'false'
        else:
            result[key] = str(value)
    return result


def _guess_mime(file_name):
    return mimetypes.guess_type(file_name or '')[0] or 'application/octet-stream'


def _read_image_file(value, file_name=None):
    """返回 (bytes, 文件名)；URL 由调用方单独处理。"""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value), file_name or 'image'
    if hasattr(value, 'read'):
        data = value.read()
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError('file_image 必须返回 bytes')
        name = file_name or os.path.basename(str(getattr(value, 'name', '') or '')) or 'image'
        return bytes(data), name
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError('file_image 不能为空')
        if text.startswith(('http://', 'https://')):
            raise ValueError('URL 图片请使用 image 参数')
        with open(text, 'rb') as fp:
            return fp.read(), file_name or os.path.basename(text)
    raise TypeError('file_image 仅支持 bytes、文件对象或本地路径')


def _markdown_values(value):
    values = value if isinstance(value, (list, tuple)) else [value]
    return ['' if item is None else str(item) for item in values]


def _markdown_params(params):
    """把模板变量转换成官方要求的 [{key, values}] 结构。"""
    if params is None:
        return []
    if isinstance(params, dict):
        items = params.items()
    elif isinstance(params, (list, tuple)):
        result = []
        for item in params:
            if not isinstance(item, dict):
                raise TypeError('markdown params 的每一项必须是字典')
            key = str(item.get('key') or '').strip()
            if not key:
                raise ValueError('markdown params 缺少 key')
            result.append({'key': key, 'values': _markdown_values(item.get('values'))})
        return result
    else:
        raise TypeError('markdown params 必须是字典或列表')

    result = []
    for key, value in items:
        key = str(key).strip()
        if not key:
            raise ValueError('markdown params 缺少 key')
        result.append({'key': key, 'values': _markdown_values(value)})
    return result


def _schedule_payload(schedule):
    if not isinstance(schedule, dict):
        raise TypeError('schedule 必须是字典')
    if not schedule:
        raise ValueError('schedule 不能为空')
    return {'schedule': dict(schedule)}


class ChannelAPI:
    """按机器人 appid 调用频道管理接口"""

    __slots__ = ('_adapter',)

    def __init__(self, adapter):
        self._adapter = adapter

    def _resolve_bot(self, appid=None):
        app = get_app()
        if app is None:
            raise RuntimeError('应用尚未启动，无法调用频道接口')

        target = '' if appid is None else str(appid).strip()
        # 优先级: 显式 appid > 当前事件来源机器人 > 配置 default_appid > 唯一机器人。
        # 这样多机器人时，收到指令的机器人会自动成为接口调用方。
        if not target:
            target = current_appid()
        if not target:
            target = str(self._adapter.get_config('default_appid', '') or '').strip()

        if target:
            bot = app.get_bot(target)
            if bot is None:
                raise RuntimeError(f'机器人未启动: appid={target}')
            return bot

        registry = app.bot_registry
        bots = list(registry.bots.values()) if registry is not None else []
        if not bots:
            raise RuntimeError('没有已启动的机器人，无法调用频道接口')
        if len(bots) > 1:
            raise RuntimeError('当前有多个机器人，请传入 appid 或配置 default_appid')
        return bots[0]

    def resolve_appid(self, appid=None):
        """解析实际使用的机器人 appid"""
        return str(self._resolve_bot(appid).appid)

    async def request(self, method, endpoint, payload=None, *, params=None, appid=None):
        """调用任意频道接口，返回响应 JSON（对象或数组）"""
        verb = str(method or 'GET').strip().upper()
        path = str(endpoint or '').strip()
        if not path:
            raise ValueError('endpoint 不能为空')
        if not path.startswith('/'):
            path = f'/{path}'

        bot = self._resolve_bot(appid)
        kwargs = {}
        if params:
            kwargs['params'] = params
        if payload is not None:
            kwargs['json'] = payload

        ok, data = await bot.sender._request(verb, path, **kwargs)
        if not ok:
            raise ChannelAPIError(
                '频道接口调用',
                data,
                method=verb,
                endpoint=path,
                appid=bot.appid,
            )
        if isinstance(data, (dict, list)):
            return data
        return {'data': data}

    async def _post_channel_form(self, channel_id, fields, file_bytes, file_name, *, appid, action):
        """multipart 发送子频道图片，兼容 httpx 与 aiohttp。"""
        bot = self._resolve_bot(appid)
        path = f'/channels/{channel_id}/messages'
        if _HAS_HTTPX:
            files = {'file_image': (file_name, file_bytes, _guess_mime(file_name))}
            ok, data = await bot.sender._request('POST', path, data=fields, files=files)
        else:
            from aiohttp import FormData

            form = FormData()
            for key, value in fields.items():
                form.add_field(key, value)
            form.add_field(
                'file_image',
                file_bytes,
                filename=file_name,
                content_type=_guess_mime(file_name),
            )
            ok, data = await bot.sender._request('POST', path, data=form)
        if not ok:
            raise ChannelAPIError(
                action,
                data,
                method='POST',
                endpoint=path,
                appid=bot.appid,
            )
        if isinstance(data, (dict, list)):
            return data
        return {'data': data}

    # ==================== 频道与子频道 ====================

    async def fetch_bot_guilds(self, *, before=None, after=None, limit=None, appid=None):
        """获取机器人加入的频道列表"""
        params = _payload(before=_optional(before) or None, after=_optional(after) or None)
        if limit is not None:
            value = int(limit)
            if not 1 <= value <= 100:
                raise ValueError('limit 必须在 1-100 之间')
            params['limit'] = value
        return await self.request('GET', '/users/@me/guilds', params=params, appid=appid)

    async def fetch_guild(self, guild_id, *, appid=None):
        """获取频道详情"""
        gid = _require_id(guild_id, 'guild_id')
        return await self.request('GET', f'/guilds/{gid}', appid=appid)

    async def fetch_guild_channels(self, guild_id, *, appid=None):
        """获取子频道列表"""
        gid = _require_id(guild_id, 'guild_id')
        return await self.request('GET', f'/guilds/{gid}/channels', appid=appid)

    async def create_guild_channel(
        self,
        guild_id,
        *,
        name=None,
        channel_type=None,
        sub_type=None,
        position=None,
        parent_id=None,
        private_type=None,
        private_user_ids=None,
        speak_permission=None,
        application_id=None,
        appid=None,
    ):
        """创建子频道"""
        gid = _require_id(guild_id, 'guild_id')
        if private_user_ids is not None:
            if not isinstance(private_user_ids, (list, tuple)):
                raise TypeError('private_user_ids 必须是列表')
            private_user_ids = [str(item) for item in private_user_ids]
        payload = _require_payload(
            _payload(
                name=name,
                type=channel_type,
                sub_type=sub_type,
                position=position,
                parent_id=parent_id,
                private_type=private_type,
                private_user_ids=private_user_ids,
                speak_permission=speak_permission,
                application_id=application_id,
            ),
            '至少需要提供一个子频道字段',
        )
        return await self.request('POST', f'/guilds/{gid}/channels', payload, appid=appid)

    async def fetch_channel(self, channel_id, *, appid=None):
        """获取子频道详情"""
        cid = _require_id(channel_id, 'channel_id')
        return await self.request('GET', f'/channels/{cid}', appid=appid)

    async def update_channel(
        self,
        channel_id,
        *,
        name=None,
        position=None,
        parent_id=None,
        private_type=None,
        speak_permission=None,
        appid=None,
    ):
        """修改子频道"""
        cid = _require_id(channel_id, 'channel_id')
        payload = _require_payload(
            _payload(
                name=name,
                position=position,
                parent_id=parent_id,
                private_type=private_type,
                speak_permission=speak_permission,
            ),
            '至少需要提供一个要修改的字段',
        )
        return await self.request('PATCH', f'/channels/{cid}', payload, appid=appid)

    async def delete_channel(self, channel_id, *, appid=None):
        """删除子频道"""
        cid = _require_id(channel_id, 'channel_id')
        return await self.request('DELETE', f'/channels/{cid}', appid=appid)

    # ==================== 频道消息与图片 ====================

    async def send_channel_message(
        self,
        channel_id,
        content=None,
        *,
        embed=None,
        ark=None,
        markdown=None,
        image=None,
        file_image=None,
        file_name=None,
        message_reference=None,
        msg_id=None,
        event_id=None,
        keyboard=None,
        appid=None,
    ):
        """发送子频道消息

        image 传图片 URL，file_image 传 bytes、文件对象或本地路径。
        file_image 走 multipart/form-data，其余字段按 docs 序列化。
        频道默认不支持内联 markdown；需要 Markdown 时使用
        send_channel_markdown_template() 传模板 ID 与变量。
        """
        cid = _require_id(channel_id, 'channel_id')
        if image is not None and file_image is not None:
            raise ValueError('image 与 file_image 不能同时使用')
        if isinstance(file_image, str) and file_image.strip().startswith(('http://', 'https://')):
            image = file_image
            file_image = None
        if not any((content, embed, ark, markdown, image, file_image)):
            raise ValueError('content、embed、ark、markdown、image、file_image 至少提供一个')

        payload = _payload(
            content=content,
            embed=embed,
            ark=ark,
            markdown=markdown,
            image=str(image) if image is not None else None,
            message_reference=message_reference,
            msg_id=msg_id,
            event_id=event_id,
            keyboard=keyboard,
        )

        if file_image is None:
            return await self.request('POST', f'/channels/{cid}/messages', payload, appid=appid)

        data, name = _read_image_file(file_image, file_name)
        if not data:
            raise ValueError('file_image 不能为空')
        return await self._post_channel_form(
            cid,
            _form_fields(payload),
            data,
            name,
            appid=appid,
            action='发送子频道图片',
        )

    async def send_channel_image(
        self,
        channel_id,
        image,
        content='',
        *,
        msg_id=None,
        event_id=None,
        file_name=None,
        appid=None,
    ):
        """发送子频道图片；URL 直接传给 image，本地数据走 multipart。"""
        cid = _require_id(channel_id, 'channel_id')
        if isinstance(image, str) and image.strip().startswith(('http://', 'https://')):
            return await self.send_channel_message(
                cid,
                content,
                image=image.strip(),
                msg_id=msg_id,
                event_id=event_id,
                appid=appid,
            )
        return await self.send_channel_message(
            cid,
            content,
            file_image=image,
            file_name=file_name,
            msg_id=msg_id,
            event_id=event_id,
            appid=appid,
        )

    async def send_channel_markdown_template(
        self,
        channel_id,
        template_id,
        params=None,
        content=None,
        *,
        msg_id=None,
        event_id=None,
        keyboard=None,
        appid=None,
    ):
        """发送频道 Markdown 模板消息

        频道默认不支持 markdown.content，需要通过开放平台申请模板后传
        custom_template_id 与变量。params 支持 {key: value} 或
        [{'key': ..., 'values': [...]}]。
        """
        cid = _require_id(channel_id, 'channel_id')
        template = _require_id(template_id, 'template_id')
        markdown = {
            'custom_template_id': template,
            'params': _markdown_params(params),
        }
        return await self.send_channel_message(
            cid,
            content,
            markdown=markdown,
            msg_id=msg_id,
            event_id=event_id,
            keyboard=keyboard,
            appid=appid,
        )

    # ==================== 频道成员 ====================

    async def fetch_channel_online_nums(self, channel_id, *, appid=None):
        """获取音视频/直播子频道在线成员数，返回 int"""
        cid = _require_id(channel_id, 'channel_id')
        data = await self.request('GET', f'/channels/{cid}/online_nums', appid=appid)
        try:
            return int(data.get('online_nums', 0))
        except (AttributeError, TypeError, ValueError):
            return 0

    async def fetch_guild_members(self, guild_id, *, after='0', limit=None, appid=None):
        """获取频道成员列表"""
        gid = _require_id(guild_id, 'guild_id')
        params = {'after': _optional(after) or '0'}
        size = _page_limit(limit)
        if size is not None:
            params['limit'] = size
        return await self.request('GET', f'/guilds/{gid}/members', params=params, appid=appid)

    async def fetch_role_members(self, guild_id, role_id, *, start_index='0', limit=None, appid=None):
        """获取频道身份组成员列表"""
        gid = _require_id(guild_id, 'guild_id')
        rid = _require_id(role_id, 'role_id')
        params = {'start_index': _optional(start_index) or '0'}
        size = _page_limit(limit)
        if size is not None:
            params['limit'] = size
        return await self.request('GET', f'/guilds/{gid}/roles/{rid}/members', params=params, appid=appid)

    async def fetch_guild_member(self, guild_id, user_id, *, appid=None):
        """获取频道成员详情"""
        gid = _require_id(guild_id, 'guild_id')
        uid = _require_id(user_id, 'user_id')
        return await self.request('GET', f'/guilds/{gid}/members/{uid}', appid=appid)

    async def delete_guild_member(
        self,
        guild_id,
        user_id,
        *,
        add_blacklist=None,
        delete_history_msg_days=None,
        appid=None,
    ):
        """删除频道成员"""
        gid = _require_id(guild_id, 'guild_id')
        uid = _require_id(user_id, 'user_id')
        if delete_history_msg_days is not None:
            days = int(delete_history_msg_days)
            if days not in (-1, 0, 3, 7, 15, 30):
                raise ValueError('delete_history_msg_days 仅支持 -1、0、3、7、15、30')
            delete_history_msg_days = days
        payload = _payload(
            add_blacklist=add_blacklist,
            delete_history_msg_days=delete_history_msg_days,
        )
        return await self.request(
            'DELETE',
            f'/guilds/{gid}/members/{uid}',
            payload or None,
            appid=appid,
        )

    # ==================== 身份组 ====================

    async def fetch_guild_roles(self, guild_id, *, appid=None):
        """获取频道身份组列表"""
        gid = _require_id(guild_id, 'guild_id')
        return await self.request('GET', f'/guilds/{gid}/roles', appid=appid)

    async def create_guild_role(self, guild_id, *, name=None, color=None, hoist=None, appid=None):
        """创建频道身份组"""
        gid = _require_id(guild_id, 'guild_id')
        payload = _require_payload(
            _payload(name=name, color=color, hoist=hoist),
            '至少需要提供一个身份组字段',
        )
        return await self.request('POST', f'/guilds/{gid}/roles', payload, appid=appid)

    async def update_guild_role(
        self,
        guild_id,
        role_id,
        *,
        name=None,
        color=None,
        hoist=None,
        appid=None,
    ):
        """修改频道身份组"""
        gid = _require_id(guild_id, 'guild_id')
        rid = _require_id(role_id, 'role_id')
        payload = _require_payload(
            _payload(name=name, color=color, hoist=hoist),
            '至少需要提供一个要修改的字段',
        )
        return await self.request('PATCH', f'/guilds/{gid}/roles/{rid}', payload, appid=appid)

    async def delete_guild_role(self, guild_id, role_id, *, appid=None):
        """删除频道身份组"""
        gid = _require_id(guild_id, 'guild_id')
        rid = _require_id(role_id, 'role_id')
        return await self.request('DELETE', f'/guilds/{gid}/roles/{rid}', appid=appid)

    async def add_guild_member_role(
        self,
        guild_id,
        user_id,
        role_id,
        *,
        channel_id=None,
        appid=None,
    ):
        """创建频道身份组成员"""
        gid = _require_id(guild_id, 'guild_id')
        uid = _require_id(user_id, 'user_id')
        rid = _require_id(role_id, 'role_id')
        payload = {}
        if channel_id is not None:
            payload['channel'] = {'id': _require_id(channel_id, 'channel_id')}
        return await self.request(
            'PUT',
            f'/guilds/{gid}/members/{uid}/roles/{rid}',
            payload,
            appid=appid,
        )

    async def remove_guild_member_role(
        self,
        guild_id,
        user_id,
        role_id,
        *,
        channel_id=None,
        appid=None,
    ):
        """删除频道身份组成员"""
        gid = _require_id(guild_id, 'guild_id')
        uid = _require_id(user_id, 'user_id')
        rid = _require_id(role_id, 'role_id')
        payload = {}
        if channel_id is not None:
            payload['channel'] = {'id': _require_id(channel_id, 'channel_id')}
        return await self.request(
            'DELETE',
            f'/guilds/{gid}/members/{uid}/roles/{rid}',
            payload or None,
            appid=appid,
        )

    # ==================== 子频道权限 ====================

    async def fetch_channel_member_permissions(self, channel_id, user_id, *, appid=None):
        """获取子频道用户权限"""
        cid = _require_id(channel_id, 'channel_id')
        uid = _require_id(user_id, 'user_id')
        return await self.request('GET', f'/channels/{cid}/members/{uid}/permissions', appid=appid)

    async def update_channel_member_permissions(
        self,
        channel_id,
        user_id,
        *,
        add=None,
        remove=None,
        appid=None,
    ):
        """修改子频道用户权限"""
        cid = _require_id(channel_id, 'channel_id')
        uid = _require_id(user_id, 'user_id')
        payload = _permission_payload(add, remove)
        return await self.request('PUT', f'/channels/{cid}/members/{uid}/permissions', payload, appid=appid)

    async def fetch_channel_role_permissions(self, channel_id, role_id, *, appid=None):
        """获取子频道身份组权限"""
        cid = _require_id(channel_id, 'channel_id')
        rid = _require_id(role_id, 'role_id')
        return await self.request('GET', f'/channels/{cid}/roles/{rid}/permissions', appid=appid)

    async def update_channel_role_permissions(
        self,
        channel_id,
        role_id,
        *,
        add=None,
        remove=None,
        appid=None,
    ):
        """修改子频道身份组权限"""
        cid = _require_id(channel_id, 'channel_id')
        rid = _require_id(role_id, 'role_id')
        payload = _permission_payload(add, remove)
        return await self.request('PUT', f'/channels/{cid}/roles/{rid}/permissions', payload, appid=appid)

    # ==================== 接口授权 ====================

    async def fetch_bot_api_permissions(self, guild_id, *, appid=None):
        """获取机器人在频道可用权限列表"""
        gid = _require_id(guild_id, 'guild_id')
        return await self.request('GET', f'/guilds/{gid}/api_permission', appid=appid)

    async def create_api_permission_demand(
        self,
        guild_id,
        channel_id,
        path,
        method,
        desc,
        *,
        appid=None,
    ):
        """发送机器人在频道接口权限的授权链接"""
        gid = _require_id(guild_id, 'guild_id')
        payload = {
            'channel_id': _require_id(channel_id, 'channel_id'),
            'api_identify': {
                'path': _require_id(path, 'path'),
                'method': str(method or '').strip().upper(),
            },
            'desc': str(desc or ''),
        }
        if not payload['api_identify']['method']:
            raise ValueError('method 不能为空')
        return await self.request('POST', f'/guilds/{gid}/api_permission/demand', payload, appid=appid)

    # ==================== 发言管理 ====================

    async def fetch_message_setting(self, guild_id, *, appid=None):
        """获取频道消息频率设置详情"""
        gid = _require_id(guild_id, 'guild_id')
        return await self.request('GET', f'/guilds/{gid}/message/setting', appid=appid)

    async def mute_guild(self, guild_id, *, mute_end_timestamp=None, mute_seconds=None, appid=None):
        """频道全员禁言或解除禁言"""
        gid = _require_id(guild_id, 'guild_id')
        payload = _mute_payload(mute_end_timestamp, mute_seconds)
        return await self.request('PATCH', f'/guilds/{gid}/mute', payload, appid=appid)

    async def mute_guild_member(
        self,
        guild_id,
        user_id,
        *,
        mute_end_timestamp=None,
        mute_seconds=None,
        appid=None,
    ):
        """频道指定成员禁言或解除禁言"""
        gid = _require_id(guild_id, 'guild_id')
        uid = _require_id(user_id, 'user_id')
        payload = _mute_payload(mute_end_timestamp, mute_seconds)
        return await self.request('PATCH', f'/guilds/{gid}/members/{uid}/mute', payload, appid=appid)

    async def mute_guild_members(
        self,
        guild_id,
        user_ids,
        *,
        mute_end_timestamp=None,
        mute_seconds=None,
        appid=None,
    ):
        """频道批量成员禁言或解除禁言"""
        gid = _require_id(guild_id, 'guild_id')
        if not isinstance(user_ids, (list, tuple)) or not user_ids:
            raise ValueError('user_ids 必须是非空列表')
        payload = _mute_payload(mute_end_timestamp, mute_seconds)
        payload['user_ids'] = [_require_id(item, 'user_ids') for item in user_ids]
        return await self.request('PATCH', f'/guilds/{gid}/mute', payload, appid=appid)

    # ==================== 公告 ====================

    async def create_announcement(
        self,
        guild_id,
        *,
        channel_id=None,
        message_id=None,
        announces_type=None,
        recommend_channels=None,
        appid=None,
    ):
        """创建频道公告

        message_id 与 recommend_channels 互斥:
        - 消息公告: 传 message_id + channel_id
        - 推荐子频道公告: 传 announces_type=1 + recommend_channels
        """
        gid = _require_id(guild_id, 'guild_id')
        message = '' if message_id is None else str(message_id).strip()
        channel = '' if channel_id is None else str(channel_id).strip()
        recommends = recommend_channels

        if recommends is not None and message:
            raise ValueError('message_id 与 recommend_channels 不能同时使用')
        if message and not channel:
            raise ValueError('创建消息公告时必须提供 channel_id')

        if recommends is not None:
            if not isinstance(recommends, (list, tuple)):
                raise TypeError('recommend_channels 必须是列表')
            if len(recommends) > 3:
                raise ValueError('推荐子频道最多 3 条')
            normalized = []
            for item in recommends:
                if not isinstance(item, dict):
                    raise TypeError('recommend_channels 的每一项必须是字典')
                normalized.append({
                    'channel_id': _require_id(item.get('channel_id'), 'recommend_channels.channel_id'),
                    'introduce': '' if item.get('introduce') is None else str(item.get('introduce')),
                })
            recommends = normalized

        payload = {}
        if message:
            payload['message_id'] = message
        if channel:
            payload['channel_id'] = channel
        if announces_type is not None:
            value = int(announces_type)
            if value not in (0, 1):
                raise ValueError('announces_type 只能为 0 (成员公告) 或 1 (欢迎公告)')
            payload['announces_type'] = value
        if recommends is not None:
            payload['recommend_channels'] = recommends
        if not payload:
            raise ValueError('至少需要提供 message_id 或 recommend_channels')

        return await self.request('POST', f'/guilds/{gid}/announces', payload, appid=appid)

    async def delete_announcement(self, guild_id, message_id='all', *, appid=None):
        """删除频道公告，message_id 设为 all 可删除推荐子频道公告"""
        gid = _require_id(guild_id, 'guild_id')
        mid = _require_id(message_id, 'message_id')
        return await self.request('DELETE', f'/guilds/{gid}/announces/{mid}', appid=appid)

    # ==================== 精华消息 ====================

    async def fetch_pins(self, channel_id, *, appid=None):
        """获取子频道精华消息"""
        cid = _require_id(channel_id, 'channel_id')
        return await self.request('GET', f'/channels/{cid}/pins', appid=appid)

    async def add_pin(self, channel_id, message_id, *, appid=None):
        """添加精华消息"""
        cid = _require_id(channel_id, 'channel_id')
        mid = _require_id(message_id, 'message_id')
        return await self.request('PUT', f'/channels/{cid}/pins/{mid}', appid=appid)

    async def delete_pin(self, channel_id, message_id='all', *, appid=None):
        """删除精华消息，message_id 设为 all 可删除全部"""
        cid = _require_id(channel_id, 'channel_id')
        mid = _require_id(message_id, 'message_id')
        return await self.request('DELETE', f'/channels/{cid}/pins/{mid}', appid=appid)

    # ==================== 日程 ====================

    async def fetch_schedules(self, channel_id, *, since=None, appid=None):
        """获取频道日程列表"""
        cid = _require_id(channel_id, 'channel_id')
        params = {}
        if since is not None:
            params['since'] = int(since)
        return await self.request('GET', f'/channels/{cid}/schedules', params=params, appid=appid)

    async def fetch_schedule(self, channel_id, schedule_id, *, appid=None):
        """获取日程详情"""
        cid = _require_id(channel_id, 'channel_id')
        sid = _require_id(schedule_id, 'schedule_id')
        return await self.request('GET', f'/channels/{cid}/schedules/{sid}', appid=appid)

    async def create_schedule(self, channel_id, schedule, *, appid=None):
        """创建日程"""
        cid = _require_id(channel_id, 'channel_id')
        payload = _schedule_payload(schedule)
        return await self.request('POST', f'/channels/{cid}/schedules', payload, appid=appid)

    async def update_schedule(self, channel_id, schedule_id, schedule, *, appid=None):
        """修改日程"""
        cid = _require_id(channel_id, 'channel_id')
        sid = _require_id(schedule_id, 'schedule_id')
        payload = _schedule_payload(schedule)
        return await self.request('PATCH', f'/channels/{cid}/schedules/{sid}', payload, appid=appid)

    async def delete_schedule(self, channel_id, schedule_id, *, appid=None):
        """删除日程"""
        cid = _require_id(channel_id, 'channel_id')
        sid = _require_id(schedule_id, 'schedule_id')
        return await self.request('DELETE', f'/channels/{cid}/schedules/{sid}', appid=appid)

    # ==================== 音频 ====================

    async def control_audio(
        self,
        channel_id,
        payload=None,
        *,
        audio_url=None,
        text=None,
        status=None,
        appid=None,
    ):
        """音频控制"""
        cid = _require_id(channel_id, 'channel_id')
        if payload is not None and not isinstance(payload, dict):
            raise TypeError('payload 必须是字典')
        body = dict(payload) if isinstance(payload, dict) else {}
        body.update(_payload(audio_url=audio_url, text=text, status=status))
        if not body:
            raise ValueError('音频控制参数不能为空')
        return await self.request('POST', f'/channels/{cid}/audio', body, appid=appid)

    async def join_mic(self, channel_id, *, appid=None):
        """机器人上麦"""
        cid = _require_id(channel_id, 'channel_id')
        return await self.request('PUT', f'/channels/{cid}/mic', {}, appid=appid)

    async def leave_mic(self, channel_id, *, appid=None):
        """机器人下麦"""
        cid = _require_id(channel_id, 'channel_id')
        return await self.request('DELETE', f'/channels/{cid}/mic', appid=appid)

    # ==================== 论坛帖子 ====================

    async def fetch_threads(self, channel_id, *, appid=None):
        """获取帖子列表"""
        cid = _require_id(channel_id, 'channel_id')
        return await self.request('GET', f'/channels/{cid}/threads', appid=appid)

    async def fetch_thread(self, channel_id, thread_id, *, appid=None):
        """获取帖子详情"""
        cid = _require_id(channel_id, 'channel_id')
        tid = _require_id(thread_id, 'thread_id')
        return await self.request('GET', f'/channels/{cid}/threads/{tid}', appid=appid)

    async def create_thread(self, channel_id, title, content, *, format=1, appid=None):
        """发表帖子"""
        cid = _require_id(channel_id, 'channel_id')
        value = int(format)
        if value not in (1, 2, 3, 4):
            raise ValueError('format 仅支持 1(文本)、2(HTML)、3(Markdown)、4(JSON)')
        payload = {
            'title': str(title or '').strip(),
            'content': str(content or ''),
            'format': value,
        }
        if not payload['title']:
            raise ValueError('title 不能为空')
        if not payload['content']:
            raise ValueError('content 不能为空')
        return await self.request('PUT', f'/channels/{cid}/threads', payload, appid=appid)

    async def delete_thread(self, channel_id, thread_id, *, appid=None):
        """删除帖子"""
        cid = _require_id(channel_id, 'channel_id')
        tid = _require_id(thread_id, 'thread_id')
        return await self.request('DELETE', f'/channels/{cid}/threads/{tid}', appid=appid)
