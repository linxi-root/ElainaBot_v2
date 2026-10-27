"""可选频道 intent 自动协商

GUILD_MESSAGES(1<<9) 与 FORUMS_EVENT(1<<28) 仅私域机器人可用；公域论坛
OPEN_FORUM_EVENT(1<<18) 与音视频/直播成员 AUDIO_LIVE_MEMBER(1<<19) 也需要
对应权限。公域机器人携带未授权 intent 鉴权时，网关会以 4013/4014 关闭连接。

这里在模块内包装 WSClient 的鉴权与消息循环：按需附带可选 intent，未授权时
逐组回退并重连，不修改 core/ 代码。
"""

from websockets.exceptions import ConnectionClosed

_PRIVATE_BITS = (1 << 9) | (1 << 28)
_OPEN_FORUM_BITS = 1 << 18
_AUDIO_LIVE_BITS = 1 << 19
_DENIED_CLOSE_CODES = frozenset({4013, 4014})
_PATCH_FLAG = '_elaina_private_intents_patched'

_auto_private = True
_open_forum = False
_audio_live = False
_log = None


def _close_code(exc):
    code = getattr(exc, 'code', None)
    if code is None:
        received = getattr(exc, 'rcvd', None)
        code = getattr(received, 'code', None)
    try:
        return int(code) if code is not None else None
    except (TypeError, ValueError):
        return None


def _extra_groups():
    groups = []
    if _open_forum:
        groups.append(('OPEN_FORUM_EVENT', _OPEN_FORUM_BITS))
    if _audio_live:
        groups.append(('AUDIO_LIVE_MEMBER', _AUDIO_LIVE_BITS))
    if _auto_private:
        groups.append(('GUILD_MESSAGES/FORUMS_EVENT', _PRIVATE_BITS))
    return groups


def _dropped(client):
    dropped = getattr(client, '_optional_intents_dropped', None)
    if dropped is None:
        dropped = set()
        client._optional_intents_dropped = dropped
    return dropped


def _extra_bits(client):
    bits = 0
    dropped = _dropped(client)
    for name, value in _extra_groups():
        if name not in dropped:
            bits |= value
    return bits


def _probe_intents(client):
    if not getattr(client, '_subscribe_channel_events', False):
        return None
    extra = _extra_bits(client)
    if not extra:
        return None
    current = getattr(client, '_intents', 0)
    probe = current | extra
    return None if probe == current else probe


def install(auto_private_intents=True, *, open_forum=False, audio_live=False, log=None):
    """安装包装；重复调用只更新开关与日志对象。"""
    global _auto_private, _open_forum, _audio_live, _log
    _auto_private = bool(auto_private_intents)
    _open_forum = bool(open_forum)
    _audio_live = bool(audio_live)
    if log is not None:
        _log = log

    from core.network.websocket import WSClient

    if getattr(WSClient, _PATCH_FLAG, False):
        return

    original_identify = WSClient._send_identify
    original_handle = WSClient._handle_connection
    original_dispatch = getattr(WSClient, '_on_dispatch', None)

    async def _send_identify(self):
        probe = _probe_intents(self)
        if probe is None or probe == getattr(self, '_intents', 0):
            await original_identify(self)
            return

        current = self._intents
        self._intents = probe
        self._private_intents_probe = True
        try:
            await original_identify(self)
        finally:
            self._intents = current

    async def _on_dispatch(self, payload):
        await original_dispatch(self, payload)
        if isinstance(payload, dict) and payload.get('t') == 'READY':
            self._private_intents_probe = False

    async def _handle_connection(self):
        try:
            result = await original_handle(self)
        except ConnectionClosed as exc:
            code = _close_code(exc)
            if code in _DENIED_CLOSE_CODES and getattr(self, '_private_intents_probe', False):
                _fallback(self, f'close={code}')
                return 'unauthorized_intents'
            raise
        if result == 'invalid_session' and getattr(self, '_private_intents_probe', False):
            _fallback(self, 'invalid_session')
            return 'unauthorized_intents'
        return result

    def _fallback(client, detail):
        client._private_intents_probe = False
        dropped = _dropped(client)
        for name, _bits in _extra_groups():
            if name in dropped:
                continue
            dropped.add(name)
            if name == 'GUILD_MESSAGES/FORUMS_EVENT':
                client._private_intents_rejected = True
            if _log is not None:
                _log.warning(f'[{client._appid}] {name} intent 未授权 ({detail})，已回退并重连')
            return
        if _log is not None:
            _log.warning(f'[{client._appid}] 频道事件 intent 被网关拒绝 ({detail})，继续使用基础 intent')

    WSClient._send_identify = _send_identify
    WSClient._handle_connection = _handle_connection
    if original_dispatch is not None:
        WSClient._on_dispatch = _on_dispatch
    setattr(WSClient, _PATCH_FLAG, True)
