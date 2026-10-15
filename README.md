# 群梗名片 (Group Meme Nickname)

群聊群名片管理插件，用于让麦麦结合群里的聊天内容和自己的记忆，给自己换一个符合人设的群名片。

## 功能

- 现场造名：读群里最近的聊天记录、检索长期记忆、取宿主人设，一起交给 replyer 现造一个名字。
- 词根校验：名字必须带上「麦麦」里的字，不带就重造（默认最多生成 2 回）。
- 定时换名：默认关闭；开启后每 1440 分钟掷一次骰子、30% 概率真的换，支持免打扰时段和群黑白名单。
- 手动命令：可用 `/麦片` 系列命令换名、预览、指定文本、恢复主名、查状态。
- 智能换名：可把 `change_my_group_card` 工具交给 Planner，让麦麦自己决定换不换。
- 权限保护：命令默认只对 `[plugin] permission` 里的账号开放，也可按命令 ID 单独放行。
- 免开 HTTP：复用 napcat-adapter / snowluma-adapter 已连的连接，不用另开 NapCat HTTP 服务。

## 配置

主要配置位于 config.toml：

`plugin.enabled`：是否启用插件。

`plugin.config_version`：配置版本，由宿主管理；升级时会按新默认值重建 config.toml 并保留注释。

`bot.base_name`：造词的词根，也就是群里怎么叫她，例如「麦麦」；留空则用宿主的 bot.nickname。

`bot.apply_mode`：replace = 整张名片就是造出来的名字；suffix = 主名 + 丨 + 模型给的后缀。

`bot.max_length`：名片总长度上限，默认 24。

`persona.use_host_personality`：是否直接读宿主的 bot_config.toml 人设，默认开启。

`persona.use_host_alias_names`：是否把宿主的 bot.alias_names 也当词根，默认开启。

`persona.extra_personality`：在宿主人设之上再叠一段人格，例如「最近在群里自称脚神，说话爱用叠词」。

`persona.style_presets`：语气池，每轮随机挑一个（高冷寡言 / 沙雕跳脱 / 中二热血 / 慵懒随性 / 阴阳怪气 / 一本正经地胡说八道）。

`persona.forbidden_words`：禁止出现在名片里的词或字，命中就重新生成。

`persona.extra_instruction`：附加要求，例如「不超过 4 个字」「只用群里的梗」「别太文艺」。

`context.lookback_hours`：读群聊记录的时间窗口，默认 36 小时。

`context.max_messages`：最多读多少条群消息，默认 80。

`context.use_memory`：是否检索长期记忆，默认开启。

`generation.task_name`：用哪个模型任务生成，默认 replyer。

`generation.temperature`：采样温度，默认 0.95。

`generation.model_name`：单独给本插件指定模型；留空则用 task_name 那个任务的模型。

`generation.style_hints`：造词手法池，每轮随机挑一个（嵌字 / 增字 / 倒装 / 谐音 / 叠字 / 合词）。

`schedule.enabled`：是否开启定时自动换名，默认关闭；开启后默认对所有群生效，只想在指定群生效请填 schedule.whitelist_groups。

`schedule.interval_minutes`：多久检查一次，默认 1440 分钟（一天）。

`schedule.trigger_probability`：每次检查真的换名的概率，默认 0.3，即约三成中签。

`schedule.quiet_hours`：免打扰时段，例如 `["02:00-07:00"]`，支持跨零点。

`schedule.whitelist_groups`：允许定时换名生效的群；空列表表示不限制，也就是所有群都会换。

`schedule.blacklist_groups`：这些群绝不改动。

`tool.enabled`：是否把 change_my_group_card 交给 Planner，默认开启。

群号填群号码本身，例如：

```toml
schedule.whitelist_groups = ["123456789"]
schedule.blacklist_groups = ["987654321"]
bot.base_name = "麦麦"
```

## 手动命令

在群里可以直接使用：

```
/麦片                  # 立刻换一张，后面跟文字则当成风格要求
/麦片 预览              # 只生成不写入
/麦片 现在              # 查当前群名片
/麦片 用 <文本>         # 跳过生成，直接写入指定文本
/麦片 恢复              # 恢复成主名
/麦片 状态              # 调度状态、OneBot 通道、上次刷新时间、本群上次名片
/麦片 探测              # 分别探测两条 OneBot 通道
```

示例：

```
/麦片
/麦片 用 大麦脚
/麦片 想要清冷一点
```

换名成功与失败都会在群里回一条文本（例如「新群名片：大麦脚」或「换名片失败：…」）；
预览、现在、状态、探测这类查询只回查询结果。

命令走宿主的受保护命令机制（permission="operator"），默认只有 config/bot_config.toml 里
[plugin] permission 列出的账号和 WebUI / 控制台的本地操作者能触发。

定时换名和 Planner 工具调用不受这条命令权限约束：schedule.enabled 打开后按时换名不经过命令权限；
tool.enabled 打开后 change_my_group_card 由 Planner 自己调用，模型就能改名片。
想让别的群成员也能用，在宿主配置里按命令 ID 放行：

```toml
[plugin]
command_permissions = { "geffnt.group-meme-nickname.group_card" = { allow_users = ["qq:123456"], allow_chats = [] } }
```

## 注意事项

本版本要求 MaiBot 1.3.0 或更高的 1.3.x 版本，以及 maibot-plugin-sdk 2.10.0 或更高版本。

config.toml 由宿主按插件的配置模型自动生成，不需要手写；config_version 升级时会按新默认值重建并保留注释。

从插件市场搜「群梗名片」安装即可，也可以把文件夹放进 MaiBot 根目录的 plugins/ 后重启 MaiBot。

群名片属于 QQ 平台侧数据，插件通过适配器的 OneBot 接口写入，麦麦需要具备修改自己群名片的权限（群若开启「仅管理员及群主可修改群名片」，麦麦得是管理员）。

插件优先复用 napcat-adapter / snowluma-adapter 已经连着的那条连接；两个适配器都没有或都不可用时，才回退到本机 http://127.0.0.1:3000 的 NapCat OneBot HTTP 服务。

定时换名默认关闭，装好后不会自己动名片；一旦打开 schedule.enabled 就是所有群都换（whitelist_groups 留空 = 不限制），
按默认值约每 1440 分钟掷一次骰子、30% 概率真的换，平均三天多换一次。

名字由模型现场生成，没有人审；群里不适合出现的字用 persona.forbidden_words 卡掉。

生成的名字太长或带引号时，调小 bot.max_length，或者把 generation.temperature 降到 0.8 左右。
