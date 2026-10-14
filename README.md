# 群梗名片 (Group Meme Nickname)

拿**麦麦自己的名字当词根**，嫁接**这个群最近真正在聊的梗**，造一个像群友互相起外号那样的 QQ 群名片。

不是写诗句，也不是从预设模板里挑后缀。假设麦麦就叫「麦麦」，而这个群这阵子天天在聊脚：

```
大麦脚
麦脚子
麦门拖鞋
```

整条链是：读这个群的真实聊天记录 → 本地统计出高频词当造词素材 → 检索麦麦关于这个群的长期记忆
→ 取宿主配置里的真实人设 → 一起丢给 **replyer** 任务现场造一个词 → 经 NapCat 写回 QQ。

造出来的绰号**必须含「麦麦」里的字**，否则插件自动打回让模型重造（默认重试 1 次，即最多生成 2 回）——就是为了
避免出现「雨声里翻书」这种跟群里毫无关系、还看不出这是谁的名字。

和「动态群名片」那类插件的区别：它不是从预设模板里挑一个后缀，而是每次现场取材
——读这个群的真实聊天记录、检索麦麦关于这个群的长期记忆、取宿主配置里的真实人设，
再把它们一起交给 **replyer** 模型任务现造一个。

## 数据流

| 步骤 | 用到的接口 |
|---|---|
| 1. 拿群列表 | `chat.get_group_streams`（宿主聊天流）；缺群名时才走 OneBot `get_group_list` |
| 2. 群号 → 聊天流 | `chat.get_stream_by_group_id` |
| 3. 读群里最近的事 | `message.get_recent` → `message.build_readable` |
| 4. 统计造词素材 | 本地字符 n-gram 词频统计（纯标准库，不额外调模型） |
| 5. 检索长期记忆 | `knowledge.search`（后端是 `src.services.memory_service`，按 `group_id` 过滤） |
| 6. 取人设 | `config.get` → `personality.personality` / `personality.reply_style` / `bot.nickname` |
| 7. 造名片 | `ctx.llm.generate(task_name="replyer")`，2~6 字绰号，必须含名字字根 |
| 8. 写回 QQ | OneBot `set_group_card`：优先 `api.call("adapter.napcat.action.call")`，复用适配器已连的 WebSocket；用不了才直连 NapCat HTTP |

## 依赖

- MaiBot `>= 1.3.0`
- `maibot-plugin-sdk >= 2.10.0`（开发时本机为 2.11.0）
- **`napcat-adapter` 插件**（官方适配器，装了就有）：它把整套 OneBot 动作注册成了公开
  API `adapter.napcat.action.call`，本插件直接复用**它已经连着的 WebSocket**，所以
  **不需要再单独开 NapCat 的 HTTP 服务器**
- 一个可用的 `replyer` 模型任务

没装 `napcat-adapter`（例如用 SnowLuma / Lagrange）也能用：此时回退到备选通道
**NapCat OneBot v11 HTTP 服务**，需要手动开（NapCat 默认不开，见安装第 3 步）。

## 安装

**插件市场（推荐）**：在 MaiBot WebUI 的插件市场里搜「群梗名片」安装即可。

**手动**：把整个 `maibot-poetic-group-card` 文件夹放进 MaiBot 根目录的 `plugins/` 里，然后重启 MaiBot，
或在 WebUI 的插件管理页加载本插件。首次加载时宿主会按插件的配置模型自动生成 `config.toml`
——**不用手写**；`config_version` 升级时宿主会按新默认值重建它，并保留你自己加的注释。

装好后在群里按这个顺序试：

1. `/麦片 探测` —— 确认通道。装了 `napcat-adapter` 的话会看到
   「① 适配器 WebSocket：可用（通道 maibot-team.napcat-adapter）」，此时**不需要任何额外配置**。
2. `/麦片 预览` —— 只调模型、不写 QQ，先看效果。
3. `/麦片` —— 满意了就真正写入。

> 只有适配器彻底不可用时，才会回退去直连 **NapCat HTTP 服务器**。插件**不提供** NapCat 配置项：
> 回退地址是代码常量 `_NAPCAT_HTTP_BASE_URL`（默认 `http://127.0.0.1:3000`），token 是
> `_NAPCAT_HTTP_TOKEN`。要开它就去 NapCat WebUI（`http://127.0.0.1:6099`）→
> **网络配置 → 新建 → HTTP 服务器**，设个端口（如 `3000`）并启用。那是 HTTP 服务器，
> 和适配器用的 WebSocket 是两套配置，互不影响。

## 群名片怎么读写的

群名片（群昵称）属于 **QQ 平台侧数据**，SDK 的 `ctx.*` 能力代理**不提供**读写接口
（能力清单里没有 `chat.get_group_card` / `chat.set_group_card`）。但它提供了 `api.call`
——可以调用**其他插件公开的 API**，而 `napcat-adapter` 恰好把整套 OneBot 动作注册成了
公开 API：

| 需求 | 走哪条 |
|---|---|
| 读机器人 QQ 号 | `adapter.napcat.action.call` → `get_login_info` |
| **读当前群名片** | 同上 → `get_group_member_info`（响应里的 `card` 字段） |
| **写群名片** | 同上 → `set_group_card` |
| 拿群列表 | 同上 → `get_group_list` |

也就是说：**读写都能只用 MaiBot 自己的东西解决**，复用的是适配器本来就连着的那条
WebSocket，不必额外开 HTTP 端口。只有适配器缺失/不可用时，插件才回退到直连 NapCat HTTP。
同类插件通常要求你先去开 HTTP 服务器，本插件把这一步变成了可选项。

> 一个容易踩的坑：`napcat-adapter` 和 `snowluma-adapter` 暴露的是**同一套 API 名**。宿主解析
> API 名的规则是「全表按 `name` / `full_name` 精确匹配，命中多个就报『名称不唯一』」，所以
> 两个适配器同时加载时短名必然失败。插件的做法是依次尝试 `插件ID.API名` 限定名
> （适配器 ID 列表写在代码常量 `_ADAPTER_PLUGIN_IDS` 里），**谁真的返回了 OneBot 响应就用谁**并记住它，
> 之后直接走这条——因为两个适配器里通常只有一个真连着。

## 配置说明

> 下面列出的就是 `config.toml` 的全部字段（这个文件由插件按配置模型生成：SDK 的 `build_default_config()` 导出默认值，宿主在文件缺失时写入、`config_version` 升级时按新默认值重建）；**不需要的字段也不会藏在代码里**。
> 有些纯内部管道（HTTP 超时、记忆条数、重试次数、适配器插件 ID、是否过滤自己发言等）已经收进代码常量，不再作为配置项。

### `[bot]`

| 字段 | 默认 | 说明 |
|---|---|---|
| `base_name` | `""` | **造词的词根**，也就是群里怎么叫你（如 `麦麦`）；留空则用宿主配置的 `bot.nickname` |
| `apply_mode` | `replace` | `replace`=整张名片就是造出来的绰号（推荐，如「大麦脚」）；`suffix`=主名+分隔符+模型给的后缀 |
| `max_length` | `24` | 名片总长度上限（字符） |

### 没有 `[napcat]` 这一栏

群名片的读写走适配器插件（napcat-adapter / snowluma-adapter）已经连好的那条 WebSocket，所以插件**不提供任何 NapCat
相关的配置项**：它会先试适配器，只有适配器彻底不可用时才回退去直连本机 `http://127.0.0.1:3000` 的 NapCat HTTP 服务
（这个地址和 token 是代码常量 `_NAPCAT_HTTP_BASE_URL` / `_NAPCAT_HTTP_TOKEN`）。

### `[persona]`

| 字段 | 默认 | 说明 |
|---|---|---|
| `use_host_personality` | `true` | **直接从宿主 bot_config.toml 读人设，不用手抄** |
| `use_host_alias_names` | `true` | 把宿主 `bot.alias_names` 也当词根（别名 `麦惠` 里的「麦」正好能造出「大麦脚」） |
| `extra_personality` | `""` | **在宿主人设之上再叠加一段人格**，例如「最近在群里自称脚神，说话爱用叠词」 |
| `style_presets` | 六种语气 | **语气池：每轮随机挑一个当这次的用词口味**（高冷寡言 / 沙雕跳脱 / 中二热血 / 慵懒随性 / 阴阳怪气 / 一本正经地胡说八道），让换出来的名字不总是同一种调调 |
| `forbidden_words` | `[]` | 名片里禁止出现的词或字，出现就重新生成，例如 `["诗", "梦", "夜"]` |
| `extra_instruction` | `""` | 附加要求，例如「不超过 4 个字」「只用群里的梗」「别太文艺」 |

### `[context]`

| 字段 | 默认 | 说明 |
|---|---|---|
| `lookback_hours` | `36.0` | 读群聊记录的时间窗口 |
| `max_messages` | `80` | 最多读多少条 |
| `use_memory` | `true` | 是否检索长期记忆 |

### `[generation]`

| 字段 | 默认 | 说明 |
|---|---|---|
| `task_name` | `replyer` | 用哪个模型任务（想在 WebUI 里换模型就改这里） |
| `temperature` | `0.95` | 采样温度，越高越放飞 |
| `model_name` | `""` | 可选：本插件单独指定具体模型；留空则用 `task_name` 那个任务配的模型（replyer 若是思考型模型，正文可能被思考吃空，可在这里换个更快的） |
| `style_hints` | 六种造词手法 | 每轮随机挑一个塞进提示词（嵌字 / 增字 / 倒装 / 谐音 / 叠字 / 合词），可以自己加新手法 |

### `[schedule]`

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 定时自动换名 |
| `interval_minutes` | `1440` | **多久检查一次，默认 1440 = 一天** |
| `trigger_probability` | `0.3` | **每次检查真的换名的概率**：`0.3` ≈ 三成中签、其余保持原名片；`1` = 每次必换。中签结果会写进状态，`/麦片 状态` 能看到上次是中了还是没中 |
| `quiet_hours` | `[]` | 免打扰时段，如 `["02:00-07:00"]`，支持跨零点 |
| `whitelist_groups` | `[]` | 只对这些群生效；留空=全部群 |
| `blacklist_groups` | `[]` | 这些群绝不改动 |

### `[tool]`

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 是否把 `change_my_group_card` 工具交给 Planner，让麦麦**自己**决定换名片 |

## 命令

命令主名是 `/麦片`（麦麦的名片）；`/名片` 作为旧写法仍然可用。

| 命令 | 作用 |
|---|---|
| `/麦片` | 立即现场取材换一张（后面跟文字则当成风格要求，如 `/麦片 想要清冷一点`） |
| `/麦片 预览` | 只生成不写入，先看效果 |
| `/麦片 现在` | 查当前群名片 |
| `/麦片 用 <文本>` | 跳过生成，直接写成指定文本 |
| `/麦片 恢复` | 恢复成主名 |
| `/麦片 状态` | 调度状态、当前 OneBot 通道、上次刷新时间、本群上次名片 |
| `/麦片 探测` | 分别探测两条 OneBot 通道，排查为什么写不进去 |

`/麦片` 走宿主自己的受保护命令机制（装饰器里写了 `permission="operator"`），所以只有两类人能触发：
`config/bot_config.toml` 里 `[plugin] permission` 列出的账号（格式 `qq:123456`），以及 WebUI / 控制台的
本地操作者。定时自动换名片和 Planner 工具调用**不受**这条限制。

想让别的群成员也能用，在宿主配置里按命令 ID 放行即可（`allow_users` 用 `qq:123456` 格式，
`allow_chats` 匹配会话 ID，也就是 `/麦片 状态` 里显示的 stream 值）：

```toml
[plugin]
command_permissions = { "geffnt.poetic-group-card.group_card" = { allow_users = ["qq:123456"], allow_chats = [] } }
```

## 常见问题

**改不了名片，日志里 set_group_card 失败**
群设置若开启「仅管理员及群主可修改群名片」而麦麦不是管理员，`set_group_card` 会被拒绝。
把麦麦设为管理员，或允许普通成员修改自己的群名片。

**改不了名片 / 连接被拒**
先在群里发 `/麦片 探测`，它会分别报告两条通道：

- ① 显示「napcat-adapter WebSocket：可用」却仍写不进去 → 问题在群权限（见上一条）。
- ① 和 ② 都不可用 → 检查 `napcat-adapter` 有没有加载、WebSocket 有没有连着；或者去
  开 NapCat 的 HTTP 服务器，端口要和代码常量 `_NAPCAT_HTTP_BASE_URL`（默认
  `http://127.0.0.1:3000`）一致；NapCat 不在本机时改这个常量里的 IP。

**生成的名字总是很长或带引号**
说明模型不听话。调小 `bot.max_length`、把 `generation.temperature` 降到 `0.8` 左右，
或在 `persona.extra_instruction` 里写死「不超过 8 个字」。

**造出来的名字没带上群里的梗 / 太文艺**
先在群里用 `/麦片 预览` 看实际输出。插件默认把「绰号必须含词根」当硬性检查，不含就打回重造；
若模型仍然跑偏，把 `generation.temperature` 降到 `0.7` 左右，在 `persona.extra_instruction`
里写「只许用高频词里的词」，或调大 `context.lookback_hours` 让它看到更多群聊。

**想完全不自动换、只在需要时手动触发**
`schedule.enabled = false`；要不要保留 Planner 工具看 `tool.enabled`。

**记忆检索没效果**
`knowledge.search` 走的是长期记忆服务，需要该群已经积累了记忆。群太新、或
`use_memory` 为 false 时自然取不到，插件会只靠人设 + 群聊近况生成。

## 许可

MIT
