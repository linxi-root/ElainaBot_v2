"""可选频道 intent 自动协商

GUILD_MESSAGES(1<<9) 与 FORUMS_EVENT(1<<28) 仅私域机器人可用；公域论坛
OPEN_FORUM_EVENT(1<<18) 与音视频/直播成员 AUDIO_LIVE_MEMBER(1<<19) 也需要
对应权限。公域机器人携带未授权 intent 鉴权时，网关会以 4013/4014 关闭连接。

这里在模块内包装 WSClient 的鉴权与消息循环：按需附带可选 intent，未授权时
逐组回退并重连，不修改 core/ 代码。

被网关拒绝的 appid 与 intent 组会持久化到 data/intents_state.json，下次启动
不再为该 appid 传入这些 intent；需要重试时删除该文件或对应条目即可。
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

# {appid: set(intent_group)}，记录被网关拒绝过的组；由 install 注入的 loader/saver 同步到磁盘
_state = {}
_state_loader = None
_state_saver = None
_state_loaded = False


def _close_code(exc):
    code = getattr(exc, 'code', None)
    if code is None:
        received = getattr(exc, 'rcvd', None)
        code = getattr(received, 'code', None)
    try:
        return int(code) if code is not None else None
    except (TypeError, ValueError):
        return None


def _ensure_state_loaded():
    global _state_loaded
    if _state_loaded:
        return
    _state_loaded = True
    if _state_loader is None:
        return
    try:
        data = _state_loader() or {}
    except Exception as exc:
        if _log is not None:
            _log.warning(f'读取频道 intent 拒绝记录失败: {exc}')
        return
    if not isinstance(data, dict):
        return
    for appid, groups in data.items():
        text = str(appid).strip()
        if not text:
            continue
        if isinstance(groups, str):
            groups = [groups]
        if not isinstance(groups, (list, tuple, set)):
            continue
        entries = {str(item) for item in groups if item}
        if entries:
            _state.setdefault(text, set()).update(entries)


def _persist_state():
    if _state_saver is None:
        return
    payload = {appid: sorted(groups) for appid, groups in _state.items() if groups}
    try:
        _state_saver(payload)
    except Exception as exc:
        if _log is not None:
            _log.warning(f'保存频道 intent 拒绝记录失败: {exc}')


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
    _ensure_state_loaded()
    dropped = getattr(client, '_optional_intents_dropped', None)
    if dropped is None:
        appid = str(getattr(client, '_appid', '') or '')
        dropped = set(_state.get(appid, ()))
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


def install(
    auto_private_intents=True,
    *,
    open_forum=False,
    audio_live=False,
    log=None,
    state_loader=None,
    state_saver=None,
):
    """安装包装；重复调用只更新开关与日志对象。

    state_loader 返回 {appid: [intent_group, ...]}，state_saver 接收相同结构，
    用于把被网关拒绝的 intent 持久化，避免每次启动重复尝试。
    """
    global _auto_private, _open_forum, _audio_live, _log
    global _state_loader, _state_saver, _state_loaded

    _auto_private = bool(auto_private_intents)
    _open_forum = bool(open_forum)
    _audio_live = bool(audio_live)
    if log is not None:
        _log = log
    if state_loader is not None:
        _state_loader = state_loader
        # 换 loader 时强制重新读取，确保和磁盘状态一致
        _state_loaded = False
    if state_saver is not None:
        _state_saver = state_saver

    _ensure_state_loaded()

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

    def _fallback(client, detail):
        client._private_intents_probe = False
        dropped = _dropped(client)
        appid = str(getattr(client, '_appid', '') or '')
        for name, _bits in _extra_groups():
            if name in dropped:
                continue
            dropped.add(name)
            if appid:
                _state.setdefault(appid, set()).add(name)
            if name == 'GUILD_MESSAGES/FORUMS_EVENT':
                client._private_intents_rejected = True
            if _log is not None:
                _log.warning(f'[{client._appid}] {name} intent 未授权 ({detail})，已回退并重连')
            _persist_state()
            return
        if _log is not None:
            _log.warning(f'[{client._appid}] 频道事件 intent 被网关拒绝 ({detail})，继续使用基础 intent')

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

    WSClient._send_identify = _send_identify
    WSClient._handle_connection = _handle_connection
    if original_dispatch is not None:
        WSClient._on_dispatch = _on_dispatch
    setattr(WSClient, _PATCH_FLAG, True)