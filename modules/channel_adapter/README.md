# 频道适配模块

把 QQ 频道的生命周期事件接入 ElainaBot：归一化事件字段、维护频道/子频道快照，并按 `event_types` 分发给插件。频道、群聊与好友消息在进入核心通用分发前先由本模块的指令层匹配通用指令与主人权限，其余消息继续走框架核心；模块同时提供全量频道管理接口、子频道图片与 Markdown 模板消息发送，以及一个管理配置的 Web 页面。

## 启用

1. 在 Web 面板的模块页启用「频道事件适配」。
2. 如不需要插件分发，可在 `modules/channel_adapter/data/config.yaml` 关闭 `dispatch_to_plugins`。
3. 需要成员事件时，在 `config/bot.yaml` 打开频道事件订阅：

```yaml
bots:
  - appid: "xxx"
    websocket:
      subscribe_channel_events: true
```

`GUILD_CREATE`、`CHANNEL_CREATE` 等使用 `GUILDS (1<<0)`，默认已订阅。
`GUILD_MEMBER_*` 使用 `GUILD_MEMBERS (1<<1)`，`MESSAGE_REACTION_*` 使用 `GUILD_MESSAGE_REACTIONS (1<<10)`，`AUDIO_START/FINISH/ON_MIC/OFF_MIC` 使用 `AUDIO_ACTION (1<<29)`，这些都需要上面的开关。
`AUDIO_OR_LIVE_CHANNEL_MEMBER_*` 使用 `AUDIO_LIVE_MEMBER (1<<19)`，默认不订阅；在 `data/config.yaml` 打开 `subscribe_audio_live_events` 后尝试。
`MESSAGE_AUDIT_PASS/REJECT` 使用 `MESSAGE_AUDIT (1<<27)`，默认已订阅。
`MESSAGE_DELETE` 使用 `GUILD_MESSAGES (1<<9)`，仅私域机器人可申请。
`PUBLIC_MESSAGE_DELETE` 使用 `PUBLIC_GUILD_MESSAGES (1<<30)`，公域机器人可用。
`FORUM_*` 使用 `FORUMS_EVENT (1<<28)`，仅私域机器人可申请。
`OPEN_FORUM_*` 使用 `OPEN_FORUM_EVENT (1<<18)`，默认不订阅；在 `data/config.yaml` 打开 `subscribe_open_forum_events` 后尝试。

打开 `subscribe_channel_events` 后，模块会先附带私域 `GUILD_MESSAGES (1<<9)` 与 `FORUMS_EVENT (1<<28)` 鉴权；网关返回未授权时自动移除并重连，公域机器人无需额外配置。私域机器人可直接获得 `MESSAGE_CREATE`、`MESSAGE_DELETE` 与论坛事件。`OPEN_FORUM_EVENT` 与 `AUDIO_LIVE_MEMBER` 同样按组自动回退。

## 支持的事件

| 事件 | Intent | Event 字段 |
| --- | --- | --- |
| `GUILD_CREATE` / `GUILD_UPDATE` / `GUILD_DELETE` | `GUILDS` | `guild_id`、`user_id`、`content` |
| `CHANNEL_CREATE` / `CHANNEL_UPDATE` / `CHANNEL_DELETE` | `GUILDS` | `guild_id`、`channel_id`、`user_id`、`content` |
| `GUILD_MEMBER_ADD` / `UPDATE` / `REMOVE` | `GUILD_MEMBERS` | `guild_id`、`user_id`、`username`、`content` |
| `MESSAGE_DELETE` / `PUBLIC_MESSAGE_DELETE` / `DIRECT_MESSAGE_DELETE` | `GUILD_MESSAGES` / `PUBLIC_GUILD_MESSAGES` | `guild_id`、`channel_id`、`message_id`、`user_id` |
| `MESSAGE_REACTION_ADD` / `REMOVE` | `GUILD_MESSAGE_REACTIONS` | `guild_id`、`channel_id`、`message_id`、`user_id`、`content` |
| `MESSAGE_AUDIT_PASS` / `REJECT` | `MESSAGE_AUDIT` | `guild_id`、`channel_id`、`message_id`、`timestamp`、`content` |
| `AUDIO_START` / `FINISH` / `ON_MIC` / `OFF_MIC` | `AUDIO_ACTION` | `guild_id`、`channel_id`、`content` |
| `AUDIO_OR_LIVE_CHANNEL_MEMBER_ENTER` / `EXIT` | `AUDIO_LIVE_MEMBER` | `guild_id`、`channel_id`、`user_id`、`content` |
| `FORUM_THREAD_*` / `FORUM_POST_*` / `FORUM_REPLY_*` | `FORUMS_EVENT` | `guild_id`、`channel_id`、`user_id`、`message_id`、`content` |
| `AT_FORUM_THREAD_CREATE` / `AT_FORUM_POST_CREATE` / `AT_FORUM_REPLY_CREATE` | `FORUMS_EVENT` | 同上 |
| `OPEN_FORUM_THREAD_*` / `OPEN_FORUM_POST_*` / `OPEN_FORUM_REPLY_*` | `OPEN_FORUM_EVENT` | 同上 |
| `FORUM_PUBLISH_AUDIT_RESULT` | `FORUMS_EVENT` | `guild_id`、`channel_id`、`message_id`、`content` |
| `C2C_MSG_RECEIVE` / `C2C_MSG_REJECT` | 无（生命周期事件） | `user_id`、`timestamp`、`content` |

这些频道事件都会设置 `event.is_channel = True`，因此 `channel_only=True` 的处理器可以匹配；`C2C_MSG_*` 会设置 `event.is_direct = True`。原始载荷仍保留在 `event.raw`。

生命周期事件会写入框架的 `lifecycle` 日志，并推送到 Web 面板的事件日志；模块不会把事件本身打印到控制台。写入日志时使用中文事件名，并把用户、频道、子频道、消息和内容整理成中文字段，Web 面板直接展示中文事件名和可展开的中文字段，不展示原始英文载荷。插件分发使用后台任务，单个指令失败不会阻塞后续频道事件；事件处理超过 10 秒会记录错误并放行。

## 插件订阅

插件必须显式声明 `event_types`，模块使用 `explicit_only=True` 分发：

```python
from core.plugin.decorators import handler

@handler(r'.*', event_types=['GUILD_CREATE'])
async def on_guild_create(event, match):
    print('已加入频道', event.guild_id)

@handler(r'.*', event_types=['CHANNEL_CREATE', 'CHANNEL_DELETE'])
async def on_channel_change(event, match):
    print(event.event_type, event.guild_id, event.channel_id)
```

`event.reply()` 在频道事件上不可用，事件本身没有可回复的消息。需要通知时使用 `event.send_to_channel(channel_id, content)`。

## 频道消息增强

对 `AT_MESSAGE_CREATE` 和 `MESSAGE_CREATE`，模块会补齐：

- `event.mentions`
- `event.is_at_self`
- `event.is_at_other_bot`
- `event.is_at_other_user`
- `event.is_at_all`

## 通用指令与主人权限

频道、群聊与好友聊天消息在进入核心分发之前，会先经过本模块的指令拦截层 `ChannelCommandRouter`。命中指令后由模块直接执行处理器并结束事件，不再进入核心分发；未命中则原样交回核心。

支持的场景：

| 场景 | 事件 | 默认是否处理 | 前缀 |
| --- | --- | --- | --- |
| 频道 @机器人 / 私信 | `AT_MESSAGE_CREATE`、`DIRECT_MESSAGE_CREATE` | 是 | 可省略 |
| 公开子频道消息 | `MESSAGE_CREATE` | 否，需要 `public_messages` | 必须 |
| 群聊 @机器人 | `GROUP_AT_MESSAGE_CREATE` | 是 | 可省略 |
| 群聊全量消息 | `GROUP_MESSAGE_CREATE` | 是，需要 `group_messages` | 被 @ 时可省略，否则必须 |
| 好友聊天 | `C2C_MESSAGE_CREATE` | 是，需要 `c2c_messages` | 必须 |

匹配规则：

- 只有本模块 `data/config.yaml` 中 `channel_commands.prefixes` 里的前缀（默认 `/`）才视为指令；`AT_MESSAGE_CREATE`、`DIRECT_MESSAGE_CREATE`、`GROUP_AT_MESSAGE_CREATE` 允许省略前缀（`at_without_prefix`）。
- 群聊全量消息默认必须带前缀，被 @ 时可省略；好友聊天默认必须带前缀，避免误触发。
- 处理器场景由装饰器声明：`channel_only`、`group_only`、`direct_only`，都不声明表示三端通用。
- `only_channel_handlers` 打开时，频道场景只分发给 `channel_only=True` 的处理器；群聊与好友聊天不受该开关影响。
- 仍然遵循处理器自身的 `event_types`、机器人白名单、`block`、拦截器与黑名单。

频道主人是本模块独立维护的列表，存放在 `data/config.yaml` 的 `channel_owners`，与 `config/bot.yaml` 的 `owner_ids` 互不影响。群聊与好友聊天的 `owner_only` 判定沿用框架的 `owner_ids`：

```yaml
channel_owners:
  - appid: '*'
    user_ids:
      - user_openid_a
  - appid: '102030405'
    user_ids:
      - user_openid_b
```

`*` 组对所有机器人生效，按 appid 分组的主人只对该机器人生效，判定时会合并两组。声明了 `owner_only=True` 的频道指令默认允许 `channel_owners`、频道主（role=4）与管理员（role=2）执行，其他人会收到 `denied_reply`（公开子频道消息不回复）；给 handler 挂 `_role_ids` 可追加或收窄允许的身份组。`channel_owners` 也接受 `{appid: [user_id]}` 形式，面板保存时统一写成上面的列表形式。

### Web 管理页面

模块启用后会在 Web 面板注册「频道适配」页面（`source=module`），可以开关指令拦截、按场景开关、设置前缀与无权限回复，并按 appid 分组增删频道主人；事件日志可以分别控制成员名称与频道名称的显示。页面通过 `/api/ext/channel-adapter/config` 读写模块配置，保存后立即生效并写回 `data/config.yaml`。

该页面属于模块自身资产：`panel.py` 负责注册页面与路由，`assets/panel.html` 是界面，模块卸载时由 `teardown()` 注销。核心框架无需任何改动。

## 模块 API

```python
adapter = app.module_manager.get('channel_adapter')

# 事件侧快照
adapter.get_guild(guild_id)
adapter.get_channel(channel_id)
adapter.list_guilds()
adapter.list_channels(guild_id=None)
adapter.channel_type_name(channel)

# 主动拉取频道详情并更新快照
await adapter.sync_guild(guild_id)

# 插件开发辅助：统一回复、发图与场景判断，需 await
await adapter.reply(event, '你好')
await adapter.send_image(event, 'https://example.com/a.png', '看图')
adapter.scene(event)      # channel / group / direct / unknown
adapter.event_appid(event)
```

快照默认写入 `modules/channel_adapter/data/snapshots.json`。

## 频道管理接口

所有方法都是异步的，返回官方响应 JSON（对象或数组）。多机器人场景可传 `appid`；不传时优先使用当前事件来源机器人，其次使用 `default_appid`，仅有一个机器人时自动选择。在模块接管的指令处理器和频道事件处理器里调用时可以省略 `appid`；由框架核心分发的其他消息处理器建议显式传 `event.appid`。接口失败会抛出 `ChannelAPIError`，其中包含 `code`、`data`、`method`、`endpoint`、`appid`。

### 频道消息与图片

`send_channel_message()` 是通用发送入口，支持 `content`、`embed`、`ark`、`markdown`、`keyboard`、`image`、`file_image`、`message_reference`、`msg_id`、`event_id`。

| 方法 | 接口 |
| --- | --- |
| `send_channel_message(channel_id, content=None, ...)` | `POST /channels/{channel_id}/messages` |
| `send_channel_image(channel_id, image, content='', ...)` | `POST /channels/{channel_id}/messages` |
| `send_channel_markdown_template(channel_id, template_id, params)` | `POST /channels/{channel_id}/messages` |
| `adapter.send_template(channel_id, template_id=None, params=None, ..., appid=None)` | 按机器人 appid 与 `data/templates.yaml` 中的模板发送 |

- `image` 传公网 URL，平台会转存后下发，请求体为 JSON。
- `file_image` 传 `bytes`、文件对象或本地路径，请求体为 `multipart/form-data`，字段名 `file_image`。
- 被动回复传 `msg_id` 或 `event_id`；频道被动回复窗口为 5 分钟。
- 频道默认不支持原生 `markdown.content`，需要先在开放平台申请模板，再通过 `custom_template_id` 与 `params` 发送。
- 模块注册了 `before_send` 钩子，频道回复里的内联 markdown 会自动降级为纯文本；带 `custom_template_id` 的模板消息保持原样。
- 官方发送子频道消息文档只暴露图片字段 `image` / `file_image`。语音、视频、文件没有对应的频道发送字段；需要发送这些类型时请使用群聊或单聊接口。

```python
api = adapter.api

# URL 图片
await api.send_channel_image(channel_id, 'https://example.com/a.png', '看图')

# 本地图片 / bytes
await api.send_channel_image(channel_id, '/data/a.png', '本地图')
await api.send_channel_image(channel_id, image_bytes, '二进制图', msg_id=message_id)

# 组合消息
await api.send_channel_message(
    channel_id,
    '说明文字',
    embed={'title': '标题', 'description': '内容'},
    msg_id=message_id,
)
```

### Markdown 模板库

QQ 频道的 Markdown 模板属于具体机器人，开放平台不存在跨机器人通用的模板。模板 ID 形如 `<机器人 appid>_<时间戳>`，例如 `102030405_1658748972`。本模块按机器人 appid 分组保存模板，发送时会使用与 appid 匹配的模板。

模块首次启动时会自动生成 `modules/channel_adapter/data/templates.yaml`。修改文件后调用 `adapter.reload_templates()` 热加载，或重启模块。

```yaml
bots:
  '102030405':
    default_template_id: '102030405_1658748972'
    templates:
      '102030405_1658748972':
        content: |
          {{.botinfo}}
          {{.name}}
          {{.desc}}
          {{.list1}}
        params: [默认介绍, 默认名称, 默认描述, 默认列表]
      '102030405_1658749999': |
        # {{.title}}
  '102030406':
    default_template_id: '102030406_1658749000'
    templates:
      '102030406_1658749000':
        content: |
          # {{.title}}
```

- `bots` 的键是机器人 appid；每个机器人有自己的 `default_template_id` 与 `templates`。
- 模板 ID 的前缀应与该机器人的 appid 一致，不匹配时发送会直接报错，避免把 A 机器人的模板发给 B 机器人。
- `default_template_id` 用于该机器人省略模板 ID 的调用。
- 变量按 `content` 中 `{{.变量}}` 首次出现的顺序自动提取，无需手写 `variables`。
- `params` 用列表时按变量顺序绑定默认值，例如 `[默认1, 默认2]` 对应前两个变量。
- `params` 也支持 `{key: value}` 与 `[{key: values}]`，便于精确指定变量。
- 只写字符串的简洁形式等价于 `{content: ..., params: {}}`。
- 旧版扁平的 `default_template_id` / `templates` 仍可读取，模块会按模板 ID 前缀自动归属到对应机器人。

发送时传元组或列表即可按位置覆盖模板变量，未传到的变量继续使用模板默认值：

```python
await adapter.send_template(
    channel_id,
    '102030405_1658748972',
    ('实际介绍', '实际名称', '实际描述'),
    appid='102030405',
)

await adapter.send_template(
    channel_id,
    '102030405_1658748972',
    {'name': '精确名称'},
    appid='102030405',
)
```

示例插件中可直接发送：

```text
模板消息 公告，今晚维护
模板消息 102030405_1658748972 公告，今晚维护
```

逗号支持中文 `，` 与英文 `,`，值按 `variables` 顺序映射到前几个变量；省略模板 ID 时使用该机器人的默认模板。

模块不会自动把普通 Markdown 回复套进模板，模板消息需要通过 `send_template` 或示例插件的「模板消息」指令显式发送。频道内联 Markdown 仍会自动降级为纯文本，显式带 `custom_template_id` 的模板消息保持原样。

```python
adapter.reload_templates()
adapter.get_template(appid='102030405')
adapter.get_template('102030405_1658748972', appid='102030405')
adapter.list_templates(appid='102030405')
await adapter.send_template(channel_id, appid='102030405')  # 全部使用模板默认值
```

### 子频道

| 方法 | 接口 |
| --- | --- |
| `fetch_bot_guilds(before=None, after=None, limit=None)` | `GET /users/@me/guilds` |
| `fetch_guild(guild_id)` | `GET /guilds/{guild_id}` |
| `fetch_guild_channels(guild_id)` | `GET /guilds/{guild_id}/channels` |
| `create_guild_channel(guild_id, ...)` | `POST /guilds/{guild_id}/channels` |
| `fetch_channel(channel_id)` | `GET /channels/{channel_id}` |
| `update_channel(channel_id, ...)` | `PATCH /channels/{channel_id}` |
| `delete_channel(channel_id)` | `DELETE /channels/{channel_id}` |

### 成员

| 方法 | 接口 |
| --- | --- |
| `fetch_channel_online_nums(channel_id)` | `GET /channels/{channel_id}/online_nums` |
| `fetch_guild_members(guild_id, after='0', limit=None)` | `GET /guilds/{guild_id}/members` |
| `fetch_role_members(guild_id, role_id, ...)` | `GET /guilds/{guild_id}/roles/{role_id}/members` |
| `fetch_guild_member(guild_id, user_id)` | `GET /guilds/{guild_id}/members/{user_id}` |
| `delete_guild_member(guild_id, user_id, ...)` | `DELETE /guilds/{guild_id}/members/{user_id}` |

### 身份组与子频道权限

| 方法 | 接口 |
| --- | --- |
| `fetch_guild_roles(guild_id)` | `GET /guilds/{guild_id}/roles` |
| `create_guild_role(guild_id, ...)` | `POST /guilds/{guild_id}/roles` |
| `update_guild_role(guild_id, role_id, ...)` | `PATCH /guilds/{guild_id}/roles/{role_id}` |
| `delete_guild_role(guild_id, role_id)` | `DELETE /guilds/{guild_id}/roles/{role_id}` |
| `add_guild_member_role(guild_id, user_id, role_id, ...)` | `PUT /guilds/{guild_id}/members/{user_id}/roles/{role_id}` |
| `remove_guild_member_role(guild_id, user_id, role_id, ...)` | `DELETE /guilds/{guild_id}/members/{user_id}/roles/{role_id}` |
| `fetch_channel_member_permissions(channel_id, user_id)` | `GET /channels/{channel_id}/members/{user_id}/permissions` |
| `update_channel_member_permissions(channel_id, user_id, ...)` | `PUT /channels/{channel_id}/members/{user_id}/permissions` |
| `fetch_channel_role_permissions(channel_id, role_id)` | `GET /channels/{channel_id}/roles/{role_id}/permissions` |
| `update_channel_role_permissions(channel_id, role_id, ...)` | `PUT /channels/{channel_id}/roles/{role_id}/permissions` |

### 接口授权与发言

| 方法 | 接口 |
| --- | --- |
| `fetch_bot_api_permissions(guild_id)` | `GET /guilds/{guild_id}/api_permission` |
| `create_api_permission_demand(guild_id, ...)` | `POST /guilds/{guild_id}/api_permission/demand` |
| `fetch_message_setting(guild_id)` | `GET /guilds/{guild_id}/message/setting` |
| `mute_guild(guild_id, ...)` | `PATCH /guilds/{guild_id}/mute` |
| `mute_guild_member(guild_id, user_id, ...)` | `PATCH /guilds/{guild_id}/members/{user_id}/mute` |
| `mute_guild_members(guild_id, user_ids, ...)` | `PATCH /guilds/{guild_id}/mute` |

### 内容管理

| 方法 | 接口 |
| --- | --- |
| `create_announcement(guild_id, ...)` | `POST /guilds/{guild_id}/announces` |
| `delete_announcement(guild_id, message_id='all')` | `DELETE /guilds/{guild_id}/announces/{message_id}` |
| `fetch_pins(channel_id)` | `GET /channels/{channel_id}/pins` |
| `add_pin(channel_id, message_id)` | `PUT /channels/{channel_id}/pins/{message_id}` |
| `delete_pin(channel_id, message_id='all')` | `DELETE /channels/{channel_id}/pins/{message_id}` |
| `fetch_schedules(channel_id, since=None)` | `GET /channels/{channel_id}/schedules` |
| `fetch_schedule(channel_id, schedule_id)` | `GET /channels/{channel_id}/schedules/{schedule_id}` |
| `create_schedule(channel_id, schedule)` | `POST /channels/{channel_id}/schedules` |
| `update_schedule(channel_id, schedule_id, schedule)` | `PATCH /channels/{channel_id}/schedules/{schedule_id}` |
| `delete_schedule(channel_id, schedule_id)` | `DELETE /channels/{channel_id}/schedules/{schedule_id}` |
| `control_audio(channel_id, payload=None, ...)` | `POST /channels/{channel_id}/audio` |
| `join_mic(channel_id)` | `PUT /channels/{channel_id}/mic` |
| `leave_mic(channel_id)` | `DELETE /channels/{channel_id}/mic` |
| `fetch_threads(channel_id)` | `GET /channels/{channel_id}/threads` |
| `fetch_thread(channel_id, thread_id)` | `GET /channels/{channel_id}/threads/{thread_id}` |
| `create_thread(channel_id, title, content, format=1)` | `PUT /channels/{channel_id}/threads` |
| `delete_thread(channel_id, thread_id)` | `DELETE /channels/{channel_id}/threads/{thread_id}` |

示例：

```python
api = adapter.api

channels = await api.fetch_guild_channels(guild_id)
channel = await api.create_guild_channel(guild_id, name='公告区', channel_type=0, sub_type=1)
await api.update_channel(channel['id'], name='公告区（已改名）', position=5)
await api.delete_channel(channel['id'])

members = await api.fetch_guild_members(guild_id, limit=100)
roles = await api.fetch_guild_roles(guild_id)
await api.mute_guild_member(guild_id, user_id, mute_seconds=600)

await api.create_announcement(guild_id, channel_id=channel_id, message_id=message_id)
await api.add_pin(channel_id, message_id)
await api.create_schedule(channel_id, {
    'name': '上王者',
    'start_timestamp': '1642076453000',
    'end_timestamp': '1642083653000',
    'jump_channel_id': '0',
    'remind_type': '0',
})
await api.create_thread(channel_id, '帖子标题', '<p>正文</p>', format=2)

# 新接口或未封装字段可走通用请求
await api.request('GET', f'/guilds/{guild_id}/channels')
```


## 配置

`modules/channel_adapter/data/config.yaml`：

```yaml
dispatch_to_plugins: true
persist_snapshots: true
enhance_channel_mentions: true
default_appid: ""
auto_private_intents: true
subscribe_open_forum_events: false
subscribe_audio_live_events: false
log_show_name: true
log_show_channel_name: true
channel_commands:
  enabled: true
  prefixes: ["/"]
  at_without_prefix: true
  public_messages: false
  group_at_messages: true
  group_messages: true
  c2c_messages: true
  only_channel_handlers: false
  denied_reply: 暂无指令权限
channel_owners:
  - appid: '*'
    user_ids: []
```

`auto_private_intents`、`subscribe_open_forum_events`、`subscribe_audio_live_events` 打开的 intent 如果被网关拒绝，模块会自动移除并重连，不需要手动改回配置。

`group_at_messages`、`group_messages`、`c2c_messages` 分别控制群聊 @ 消息、群聊全量消息与好友聊天的指令拦截。`channel_commands` 与 `channel_owners` 可以直接改文件，也可以在 Web 面板的「频道适配」页面维护；两者互不覆盖。`channel_owners` 里 `*` 表示所有机器人共用，其余是 appid。

## 插件调用示例

完整示例见 `plugins/channel_demo/main.py`。插件通过模块管理器取得实例，再调用 `api`：

```python
from core.application import get_app

adapter = get_app().module_manager.get('channel_adapter')
if adapter is None:
    raise RuntimeError('频道事件适配模块未启用')

guild = await adapter.api.fetch_guild(event.guild_id, appid=event.appid)
await adapter.api.send_channel_image(event.channel_id, image_url, msg_id=event.message_id)
```

示例插件注册了「频道信息」「子频道列表」「发图 <URL>」「模板列表」「模板消息 [模板ID] 值1，值2」五个频道命令，均声明 `owner_only=True`，只有 `channel_owners`、频道主（role=4）与管理员（role=2）可执行；同时演示了 `GUILD_MEMBER_ADD` 事件中读取快照。模板命令会按 `event.appid` 读取该机器人对应的模板；省略模板 ID 时使用默认模板。

## 不在本模块范围

- 频道语音、视频、文件发送（官方发送子频道消息接口只提供图片字段）
- 小程序前端的 `qq.getGuildInfo` 与开放数据解密

语音、视频、文件可使用群聊或单聊富媒体接口发送。私域、公域论坛与音视频成员 intent 在打开 `subscribe_channel_events` 后按配置尝试，未授权会自动回退；关闭对应开关可完全禁用这次尝试。
