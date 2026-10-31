"""频道适配示例插件

演示如何通过模块管理器取得 ChannelAdapter 实例，调用它的 api 与快照方法。

前置条件：
1. 在 Web 面板启用「频道事件适配」模块（modules/channel_adapter）。
2. 需要接收频道事件时，在 config/bot.yaml 打开：
   websocket:
     subscribe_channel_events: true

本插件的指令由模块的通用指令拦截层接管并做权限判断；默认前缀是 /，
频道与群聊 @机器人 时也可以省略前缀。声明 channel_only=True 的指令只在频道
生效，不声明场景的通用指令可以在频道、群聊和好友聊天共用。

权限说明：
- 本文件的频道专属指令均声明 owner_only=True，只有模块配置里的
  channel_owners、频道主(role=4) 与管理员(role=2) 可执行。
- 想让特定身份组成员也执行 owner_only 指令，可在装饰器之后给函数挂
  role_ids（示例见文件末尾注释）。
"""

import re

from core.application import get_app
from core.base.logger import PLUGIN, get_logger, report_error
from core.plugin.decorators import handler

__plugin_meta__ = {
    'name': '频道适配示例',
    'description': '演示频道适配模块的 api、快照、Markdown 模板与成员事件',
    'version': '1.1.1',
    'author': 'ElainaBot',
}

log = get_logger(PLUGIN, '频道演示')

_MODULE_NAME = 'channel_adapter'


def _adapter():
    """取得已启用的 ChannelAdapter 实例；模块未启用时返回 None。"""
    app = get_app()
    if app is None or app.module_manager is None:
        return None
    return app.module_manager.get(_MODULE_NAME)


def _split_values(text):
    """按中文「，」或英文「,」拆分模板变量值。"""
    return [part.strip() for part in re.split(r'[，,]', text or '') if part.strip()]


# ==================== 通用指令（频道 / 群聊 / 好友聊天）====================


@handler(r'^(?:帮助|help)$', name='通用帮助', desc='三端通用的指令列表')
async def common_help(event, match):
    """不声明场景，频道、群聊和好友聊天都能触发。"""
    adapter = _adapter()
    if adapter is None:
        return await event.reply('❌ 频道事件适配模块未启用')

    scene_label = {
        'channel': '频道',
        'group': '群聊',
        'direct': '好友聊天',
    }.get(adapter.scene(event), '未知场景')
    text = (
        f'当前场景：{scene_label}\n'
        '可用指令：\n'
        '· 帮助 — 查看本列表\n'
        '· 频道信息 — 机器人已加入的频道（仅频道主人/管理员）\n'
        '· 子频道列表 — 当前频道的子频道（仅频道主人/管理员）\n'
        '· 发图 <URL> — 发送网络图片（仅频道主人/管理员）\n'
        '· 模板列表 / 模板消息 [模板ID] 值1，值2 — 频道模板（仅频道主人/管理员）'
    )
    await adapter.reply(event, text)


# ==================== 频道专属指令 ====================


@handler(r'^频道信息$', name='频道信息', desc='查看已加入的频道列表', channel_only=True, owner_only=True)
async def channel_info(event, match):
    adapter = _adapter()
    if adapter is None:
        return await event.reply('❌ 频道事件适配模块未启用')
    try:
        guilds = await adapter.api.fetch_bot_guilds(appid=event.appid)
    except Exception as exc:
        report_error(PLUGIN, '频道演示', exc, context={'appid': event.appid})
        return await event.reply(f'❌ 获取频道列表失败：{exc}')

    if isinstance(guilds, dict):
        guilds = guilds.get('guilds') or guilds.get('data') or []
    if not isinstance(guilds, list) or not guilds:
        return await event.reply('📭 机器人未加入任何频道')

    lines = ['📡 已加入的频道：']
    for item in guilds[:20]:
        if not isinstance(item, dict):
            continue
        gid = str(item.get('id') or '')
        name = str(item.get('name') or gid)
        mark = ' ← 当前' if gid == str(getattr(event, 'guild_id', '')) else ''
        lines.append(f'· {name}（{gid}）{mark}')
    await adapter.reply(event, '\n'.join(lines))


@handler(r'^子频道列表$', name='子频道列表', desc='查看当前频道的子频道', channel_only=True, owner_only=True)
async def channel_list(event, match):
    adapter = _adapter()
    if adapter is None:
        return await event.reply('❌ 频道事件适配模块未启用')
    guild_id = getattr(event, 'guild_id', '')
    if not guild_id:
        return await event.reply('❌ 当前消息未携带频道 ID')

    try:
        channels = await adapter.api.fetch_guild_channels(guild_id, appid=event.appid)
    except Exception as exc:
        report_error(PLUGIN, '频道演示', exc, context={'appid': event.appid})
        return await event.reply(f'❌ 获取子频道失败：{exc}')

    if isinstance(channels, dict):
        channels = channels.get('channels') or channels.get('data') or []
    if not isinstance(channels, list) or not channels:
        return await event.reply('📭 当前频道没有子频道')

    lines = ['🗂 子频道：']
    for ch in channels[:30]:
        if not isinstance(ch, dict):
            continue
        cid = str(ch.get('id') or '')
        name = str(ch.get('name') or cid)
        kind = adapter.channel_type_name(ch.get('type'))
        lines.append(f'· {name}（{cid}）[{kind}]')
    await adapter.reply(event, '\n'.join(lines))


@handler(r'^发图\s+(\S+)$', name='发图', desc='发送一张网络图片', channel_only=True, owner_only=True)
async def send_image(event, match):
    adapter = _adapter()
    if adapter is None:
        return await event.reply('❌ 频道事件适配模块未启用')
    if not getattr(event, 'channel_id', ''):
        return await event.reply('❌ 当前私信场景暂不支持发送图片')

    url = match.group(1).strip()
    if not url.startswith(('http://', 'https://')):
        return await event.reply('❌ 图片地址必须是 http/https 链接')
    try:
        await adapter.send_image(event, url)
    except Exception as exc:
        report_error(PLUGIN, '频道演示', exc, context={'appid': event.appid})
        return await event.reply(f'❌ 发送图片失败：{exc}')
    await adapter.reply(event, '✅ 已发送图片')


@handler(r'^模板列表$', name='模板列表', desc='查看当前机器人的频道模板', channel_only=True, owner_only=True)
async def template_list(event, match):
    adapter = _adapter()
    if adapter is None:
        return await event.reply('❌ 频道事件适配模块未启用')

    templates = adapter.list_templates(appid=event.appid)
    if not templates:
        return await event.reply(
            '📭 当前机器人未配置频道模板，请编辑 modules/channel_adapter/data/templates.yaml'
        )

    lines = ['📋 模板列表：']
    for item in templates:
        desc = item.get('description') or ''
        suffix = f' — {desc}' if desc else ''
        lines.append(f'· {item["id"]}{suffix}')
    await adapter.reply(event, '\n'.join(lines))


@handler(r'^模板消息(?:\s+(.+))?$', name='模板消息', desc='发送频道 Markdown 模板消息', channel_only=True, owner_only=True)
async def send_template_cmd(event, match):
    adapter = _adapter()
    if adapter is None:
        return await event.reply('❌ 频道事件适配模块未启用')
    if not getattr(event, 'channel_id', ''):
        return await event.reply('❌ 当前私信场景暂不支持发送模板消息')

    args = (match.group(1) or '').strip()

    # 可选地以「模板ID 值1，值2」开头；省略模板 ID 时使用该机器人默认模板
    template_id = None
    value_text = args
    head, sep, rest = args.partition(' ')
    if sep and re.fullmatch(r'\d+_\S+', head):
        template_id = head
        value_text = rest.strip()

    values = _split_values(value_text)
    try:
        await adapter.send_template(
            event.channel_id,
            template_id,
            values if values else None,
            appid=event.appid,
        )
    except Exception as exc:
        report_error(PLUGIN, '频道演示', exc, context={'appid': event.appid})
        return await event.reply(f'❌ 发送模板消息失败：{exc}')
    await adapter.reply(event, '✅ 模板消息已发送')


# ==================== 频道生命周期事件 ====================


@handler(r'', name='成员加入', desc='新成员加入频道时读取快照', event_types=['GUILD_MEMBER_ADD'])
async def on_guild_member_add(event, match):
    adapter = _adapter()
    if adapter is None:
        return

    # 快照由模块在事件落地时维护，这里直接读取即可
    guild = adapter.get_guild(event.guild_id)
    guild_name = guild.get('name') or event.guild_id
    log.info(f'新成员 {event.user_id} 加入频道「{guild_name}」（{event.guild_id}）')

    # 需要主动通知时，找到第一个文字子频道发送欢迎；默认不发送以免打扰：
    # text_channels = [
    #     c for c in adapter.list_channels(event.guild_id)
    #     if str(c.get('type')) == '0'
    # ]
    # if text_channels:
    #     await adapter.api.send_channel_message(
    #         text_channels[0]['id'],
    #         f'欢迎新成员 {event.user_id} 加入 {guild_name}！',
    #     )


# ==================== 权限扩展示例（可选）====================
# 频道专属指令已声明 owner_only=True，默认允许 channel_owners、频道主(role=4)
# 与管理员(role=2)执行；若还想放行其它身份组（例如子频道管理员 role_id='5'），
# 装饰器之后追加：
#   send_image._role_ids = frozenset({'5'})
# 若要限制为「只允许 channel_owners 白名单」则声明空集合：
#   send_image._role_ids = frozenset()