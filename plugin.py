"""群梗名片 (Group Meme Nickname)

让麦麦结合「这个群最近在聊什么」与「自己的长期记忆」，用人设驱动的语气
给自己起一个带群梗的群名片，并通过适配器已连接的 OneBot 通道写回 QQ
（适配器不可用时才回退到 NapCat 的 OneBot v11 HTTP 接口）。

数据流：
    宿主群聊流 / OneBot get_group_list    → 群号 / 群名
    chat.get_stream_by_group_id(群号)    → stream_id
    message.get_recent(stream_id)       → 最近消息
    message.build_readable(...)         → 群聊近况文本
    knowledge.search(group_id=...)      → 长期记忆片段
    config.get(bot.nickname / personality.*) → 人设
    ctx.call_capability("llm.generate", timeout_ms=120000)    → 群名片文本
    api.call(adapter.napcat.action.call, set_group_card) → 生效

走 call_capability 而不是 ctx.llm.generate，是为了传 timeout_ms：
SDK 包装层默认 30s，会把思考模型的生成截断成空结果。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParamType, ToolParameterInfo

# 生成结果里需要剥掉的首尾字符
_QUOTE_CHARS = "\"'“”‘’「」『』《》<>()（）[]【】〔〕 \t"
# 模型偶尔会带上的前缀
_PREFIXES = (
    "群名片：",
    "群名片:",
    "新名片：",
    "新名片:",
    "名片：",
    "名片:",
    "结果：",
    "结果:",
)


class PluginSection(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(
        default="1.3.0",
        description="配置版本；改动配置模型或默认值后递增，宿主据此重建已有的 config.toml",
    )


# 下面这些要么是内部管道、要么基本没人会改，就不做成配置项了，免得配置文件里全是噪音。
_SUFFIX_SEPARATOR = "丨"
_HTTP_TIMEOUT_SECONDS = 10.0
_ADAPTER_API_NAMESPACE = "adapter.napcat"
# 两个适配器暴露同一套 API 名，短名会「名称不唯一」，所以要拼限定名逐个试
_ADAPTER_PLUGIN_IDS: tuple[str, ...] = ("maibot-team.napcat-adapter", "maibot-team.snowluma-adapter")
_FILTER_BOT_MESSAGES = False
_TRANSCRIPT_MAX_CHARS = 1800
_MEMORY_LIMIT = 6
_MEMORY_QUERY_TEMPLATE = "{group_name} 群里反复出现的梗、口头禅、外号"
_GEN_RETRY_TIMES = 1
_GEN_TIMEOUT_SECONDS = 120.0
_REFRESH_ON_START = False
# NapCat HTTP 只是「适配器不可用」时的备选通道，正常走不到，所以这两项也不做成配置项。
_PREFER_ADAPTER_API = True
_NAPCAT_HTTP_BASE_URL = "http://127.0.0.1:3000"
_NAPCAT_HTTP_TOKEN = ""


class BotSection(PluginConfigBase):
    """机器人自身信息与名片格式。"""

    __ui_label__ = "名片格式"
    __ui_icon__ = "id-card"
    __ui_order__ = 1

    base_name: str = Field(
        default="",
        description="造词的词根，也就是群里怎么叫你（如「麦麦」）；留空则用宿主 bot_config.toml 的 bot.nickname",
    )
    apply_mode: str = Field(
        default="replace",
        description="replace=整张名片就是造出来的绰号，如「大麦脚」（推荐）；suffix=主名+分隔符+模型给的后缀",
    )
    max_length: int = Field(default=24, description="群名片总长度上限（字符，中文按 1 个算）")


_ADAPTER_API_MISSING_TOKENS: tuple[str, ...] = (
    "不存在",
    "未找到",
    "未注册",
    "未授权",
    "未公开",
    "不唯一",
    "not found",
    "unknown",
    "no such",
    "not registered",
    "ambiguous",
)


class PersonaSection(PluginConfigBase):
    """人设来源。默认直接读宿主 bot_config.toml，无需手抄。"""

    __ui_label__ = "人设"
    __ui_icon__ = "user"
    __ui_order__ = 3

    use_host_personality: bool = Field(
        default=True,
        description="从宿主配置读取 personality.personality / personality.reply_style / bot.nickname",
    )
    use_host_alias_names: bool = Field(
        default=True,
        description="把宿主 bot.alias_names 也当作可用的名字词根（群里可能用别的名字叫你）",
    )
    extra_personality: str = Field(
        default="",
        description="在宿主人设之外额外叠加的人格，例如「最近在群里自称脚神，说话爱用叠词」",
    )
    style_presets: list[str] = Field(
        default=["高冷寡言", "沙雕跳脱", "中二热血", "慵懒随性", "阴阳怪气", "一本正经地胡说八道"],
        description="语气池：每轮随机挑一个作为这次的用词口味；留空则不限定。只影响口味，不影响造词规则",
    )
    forbidden_words: list[str] = Field(
        default=[],
        description="名片里禁止出现的词或字（如「诗」「梦」「夜」），一旦出现就重新生成",
    )
    extra_instruction: str = Field(
        default="",
        description="附加要求，例如：不超过 4 个字、只用群里的梗、别太文艺、避免重复用过的梗",
    )


class ContextSection(PluginConfigBase):
    """「群里的事」与「记忆」的取材范围。"""

    __ui_label__ = "上下文"
    __ui_icon__ = "database"
    __ui_order__ = 4

    lookback_hours: float = Field(default=36.0, description="读取群聊记录的时间窗口（小时）")
    max_messages: int = Field(default=80, description="最多读取多少条群消息")
    use_memory: bool = Field(default=True, description="是否检索长期记忆（memory_service）")


class GenerationSection(PluginConfigBase):
    """生成参数。"""

    __ui_label__ = "生成"
    __ui_icon__ = "sparkles"
    __ui_order__ = 5

    task_name: str = Field(default="replyer", description="使用哪个模型任务生成（建议 replyer）")
    temperature: float = Field(default=0.95, description="采样温度，越高越放飞")
    model_name: str = Field(
        default="",
        description="可选：本插件单独指定具体模型；留空则用 task_name 那个任务配的模型",
    )
    style_hints: list[str] = Field(
        default=[
            "嵌字：主名后面直接接上梗字",
            "增字：主名前加 大/小/老/阿/王/神 之类的字",
            "倒装：把梗字放到主名前面当定语",
            "谐音：用同音字替换主名里的字",
            "叠字：把梗字叠起来",
            "合词：把主名和梗字拼成一个像词的组合",
        ],
        description="随机挑选的造词手法池，留空则不限定",
    )


class ScheduleSection(PluginConfigBase):
    """定时自动换名。"""

    __ui_label__ = "调度"
    __ui_icon__ = "clock"
    __ui_order__ = 6

    enabled: bool = Field(
        default=False,
        description="是否开启定时自动换名；默认关闭，开启后默认对所有群生效，可用 whitelist_groups 限定",
    )
    interval_minutes: int = Field(default=1440, description="多久检查一次（分钟），默认 1440 = 一天")
    trigger_probability: float = Field(
        default=0.3,
        description="每次检查真的换名的概率：0.3 表示约三成检查会换，其余保持原名片；填 1 就是每次必换",
    )
    quiet_hours: list[str] = Field(
        default=[],
        description='免打扰时段，例如 ["02:00-07:00"]；支持跨零点如 ["23:30-07:00"]',
    )
    whitelist_groups: list[str] = Field(default=[], description="只对这些群生效；留空=全部群")
    blacklist_groups: list[str] = Field(default=[], description="这些群绝不改动")


class ToolSection(PluginConfigBase):
    """是否允许麦麦在聊天里自己决定换名片。"""

    __ui_label__ = "工具"
    __ui_icon__ = "wrench"
    __ui_order__ = 7

    enabled: bool = Field(default=True, description="是否把 change_my_group_card 工具交给 Planner")


class PoeticGroupCardConfig(PluginConfigBase):
    """群梗名片插件配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    bot: BotSection = Field(default_factory=BotSection)
    persona: PersonaSection = Field(default_factory=PersonaSection)
    context: ContextSection = Field(default_factory=ContextSection)
    generation: GenerationSection = Field(default_factory=GenerationSection)
    schedule: ScheduleSection = Field(default_factory=ScheduleSection)
    tool: ToolSection = Field(default_factory=ToolSection)


class PoeticGroupCardPlugin(MaiBotPlugin):
    """结合群内近况与长期记忆，给麦麦自己换一个带群梗的群名片。"""

    config_model = PoeticGroupCardConfig

    def __init__(self) -> None:
        super().__init__()
        self._scheduler: asyncio.Task[None] | None = None
        self._startup_task: asyncio.Task[Any] | None = None
        self._state: dict[str, Any] = {"groups": {}, "last_run": 0.0, "last_roll": {}}
        self._bot_qq_cache = ""
        self._last_gen_error = ""
        self._adapter_api_ok: bool | None = None
        self._adapter_api_winner = ""

    # ==================== 生命周期 ====================

    async def on_load(self) -> None:
        """加载插件并按配置启动调度。"""
        self._load_state()
        if not self.config.plugin.enabled:
            self.ctx.logger.info("群梗名片：配置中已禁用，不启动调度")
            return

        if self.config.schedule.enabled:
            self.ctx.logger.info(
                "群梗名片：已加载（任务=%s，每 %s 分钟检查一次，换名概率 %.2f）",
                self.config.generation.task_name,
                self.config.schedule.interval_minutes,
                self._trigger_chance(),
            )
        else:
            self.ctx.logger.info(
                "群梗名片：已加载（任务=%s，定时换名已关闭，可用 /麦片 手动换）",
                self.config.generation.task_name,
            )
        if self.config.schedule.enabled:
            self._restart_scheduler()
        if _REFRESH_ON_START:
            self._startup_task = asyncio.create_task(self._refresh_all(trigger="startup"))

    async def on_unload(self) -> None:
        """取消调度并落盘状态。"""
        await self._cancel_tasks()
        self._save_state()
        self.ctx.logger.info("群梗名片：已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """配置热重载后重建调度。"""
        del config_data
        self.ctx.logger.info("群梗名片：配置已更新（scope=%s，version=%s）", scope, version)
        self._bot_qq_cache = ""
        self._adapter_api_ok = None
        self._adapter_api_winner = ""
        await self._cancel_tasks()
        if self.config.plugin.enabled and self.config.schedule.enabled:
            self._restart_scheduler()

    async def _cancel_tasks(self) -> None:
        for task in (self._startup_task, self._scheduler):
            if task is not None and not task.done():
                task.cancel()
        for task in (self._startup_task, self._scheduler):
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 卸载阶段吞掉一切
                    pass
        self._startup_task = None
        self._scheduler = None

    def _restart_scheduler(self) -> None:
        if self._scheduler is not None and not self._scheduler.done():
            self._scheduler.cancel()
        self._scheduler = asyncio.create_task(self._schedule_loop())

    # ==================== 调度 ====================

    async def _schedule_loop(self) -> None:
        while True:
            delay = max(1, int(self.config.schedule.interval_minutes)) * 60.0
            chance = self._trigger_chance()
            self.ctx.logger.info(
                "群梗名片：%.1f 分钟后掷一次骰子（换名概率 %.2f）", delay / 60.0, chance
            )
            await asyncio.sleep(delay)
            if not self.config.plugin.enabled:
                continue
            if self._in_quiet_hours():
                self.ctx.logger.info("群梗名片：当前处于免打扰时段，跳过本轮")
                continue
            hit = random.random() < chance
            self._state["last_roll"] = {"at": time.time(), "hit": hit, "chance": chance}
            self._save_state()
            if not hit:
                self.ctx.logger.info("群梗名片：这次没中签（%.2f），名片保持不变", chance)
                continue
            self.ctx.logger.info("群梗名片：中签（%.2f），开始换名", chance)
            try:
                await self._refresh_all(trigger="schedule")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 定时任务不能因单次失败退出
                self.ctx.logger.error("群梗名片：定时换名失败: %s", exc)

    def _trigger_chance(self) -> float:
        """把配置里的概率夹在 0~1，避免填出界导致行为诡异。"""

        try:
            value = float(self.config.schedule.trigger_probability)
        except (TypeError, ValueError):
            return 1.0
        return min(1.0, max(0.0, value))

    def _last_roll_text(self) -> str:
        roll = self._state.get("last_roll")
        if not isinstance(roll, dict) or not roll:
            return "上次掷骰子：还没掷过"
        verdict = "中签（换名）" if roll.get("hit") else "没中签（保持原名片）"
        return f"上次掷骰子：{verdict}（{self._fmt_time(roll.get('at'))}）"

    def _in_quiet_hours(self) -> bool:
        now = datetime.now()
        now_minutes = now.hour * 60 + now.minute
        for span in self.config.schedule.quiet_hours:
            start, end = self._parse_span(str(span or ""))
            if start is None or end is None:
                continue
            if start <= end:
                if start <= now_minutes <= end:
                    return True
            elif now_minutes >= start or now_minutes <= end:
                return True
        return False

    @staticmethod
    def _parse_span(text: str) -> tuple[int | None, int | None]:
        match = re.match(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$", text)
        if not match:
            return None, None
        hour_a, minute_a, hour_b, minute_b = (int(match.group(i)) for i in range(1, 5))
        if hour_a > 23 or hour_b > 23 or minute_a > 59 or minute_b > 59:
            return None, None
        return hour_a * 60 + minute_a, hour_b * 60 + minute_b

    # ==================== 主流程 ====================

    async def _refresh_all(self, *, trigger: str) -> list[str]:
        groups = await self._target_groups()
        if not groups:
            self.ctx.logger.info("群梗名片：没有符合条件的群，跳过")
            return []
        results: list[str] = []
        for group in groups:
            try:
                ok, text = await self._refresh_one(group, trigger=trigger)
                results.append(f"{group.get('group_name') or group.get('group_id')}: {text}{'' if ok else '（失败）'}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 单群失败不影响其他群
                self.ctx.logger.error("群梗名片：群 %s 换名异常: %s", group.get("group_id"), exc)
            await asyncio.sleep(random.uniform(1.5, 6.0))
        self._state["last_run"] = time.time()
        self._save_state()
        return results

    async def _refresh_one(self, group: dict[str, str], *, trigger: str, mood: str = "") -> tuple[bool, str]:
        group_id = str(group.get("group_id") or "")
        group_name = str(group.get("group_name") or "")
        if not group_id:
            return False, "群号为空"

        stream_id = await self._stream_id_of(group_id)
        transcript = await self._transcript(stream_id) if stream_id else ""
        if not transcript:
            self.ctx.logger.info("群梗名片：群 %s 近期没有聊天记录，只按人设生成", group_id)
        memory = await self._memory(group_name, group_id, self._recent_hint(transcript))
        card = await self._generate(group_name, transcript, memory, mood=mood)
        if not card:
            return False, f"生成失败（{self._last_gen_error or '模型没有返回可用文本'}）"

        if not await self._set_card(group_id, card):
            return False, f"已生成「{card}」但写入 QQ 失败"

        self._state.setdefault("groups", {})[group_id] = {
            "card": card,
            "at": time.time(),
            "trigger": trigger,
            "group_name": group_name,
        }
        self._save_state()
        self.ctx.logger.info("群梗名片：群 %s 新名片「%s」（来源 %s）", group_id, card, trigger)
        return True, card

    # ==================== 取材 ====================

    async def _target_groups(self) -> list[dict[str, str]]:
        allow = {str(item).strip() for item in self.config.schedule.whitelist_groups if str(item).strip()}
        deny = {str(item).strip() for item in self.config.schedule.blacklist_groups if str(item).strip()}
        picked: list[dict[str, str]] = []
        for item in await self._list_groups():
            group_id = str(item.get("group_id") or "").strip()
            if not group_id or group_id in deny:
                continue
            if allow and group_id not in allow:
                continue
            picked.append({"group_id": group_id, "group_name": str(item.get("group_name") or "")})
        return picked

    async def _list_groups(self) -> list[dict[str, Any]]:
        resp = await self._onebot("get_group_list")
        data = resp.get("data") if isinstance(resp, dict) else None
        if isinstance(data, list) and data:
            return [item for item in data if isinstance(item, dict)]

        # 兜底：用宿主已经建立的群聊流（只能覆盖麦麦见过的群）
        self.ctx.logger.warning("群梗名片：NapCat get_group_list 不可用，回退到宿主群聊流")
        try:
            streams = await self.ctx.chat.get_group_streams()
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.error("群梗名片：获取群列表失败: %s", exc)
            return []
        if not isinstance(streams, list):
            return []
        return [
            {"group_id": str(item.get("group_id") or ""), "group_name": str(item.get("group_name") or "")}
            for item in streams
            if isinstance(item, dict) and item.get("group_id")
        ]

    async def _stream_id_of(self, group_id: str) -> str:
        try:
            stream = await self.ctx.chat.get_stream_by_group_id(str(group_id))
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("群梗名片：查询群 %s 的聊天流失败: %s", group_id, exc)
            return ""
        if isinstance(stream, dict):
            return str(stream.get("stream_id") or stream.get("session_id") or "")
        return ""

    async def _group_id_of_stream(self, stream_id: str) -> str:
        if not stream_id:
            return ""
        try:
            streams = await self.ctx.chat.get_group_streams()
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("群梗名片：反查群号失败: %s", exc)
            return ""
        if not isinstance(streams, list):
            return ""
        for item in streams:
            if not isinstance(item, dict):
                continue
            if str(item.get("stream_id") or item.get("session_id") or "") == str(stream_id):
                return str(item.get("group_id") or "")
        return ""

    async def _group_name_of(self, group_id: str) -> str:
        """优先问宿主已有的聊天流，问不到再去 NapCat 拉群列表。"""

        try:
            stream = await self.ctx.chat.get_stream_by_group_id(str(group_id))
        except Exception as exc:  # noqa: BLE001
            self._log_warning("群梗名片：查询群 %s 的名称失败: %s", group_id, exc)
            stream = None
        if isinstance(stream, dict) and str(stream.get("group_name") or "").strip():
            return str(stream["group_name"]).strip()
        for item in await self._list_groups():
            if str(item.get("group_id") or "") == str(group_id):
                return str(item.get("group_name") or "")
        return ""

    async def _transcript(self, stream_id: str) -> str:
        cfg = self.context_config()
        try:
            messages = await self.ctx.call_capability(
                "message.get_recent",
                chat_id=stream_id,
                hours=float(cfg.lookback_hours),
                limit=int(cfg.max_messages),
                filter_mai=_FILTER_BOT_MESSAGES,
            )
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("群梗名片：读取群消息失败: %s", exc)
            return ""
        if not isinstance(messages, list) or not messages:
            return ""
        try:
            text = await self.ctx.call_capability(
                "message.build_readable",
                messages=messages,
                timestamp_mode="relative",
                truncate=True,
            )
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("群梗名片：消息转文本失败: %s", exc)
            return ""
        if not isinstance(text, str) or not text.strip():
            return ""
        text = text.strip()
        limit = max(200, _TRANSCRIPT_MAX_CHARS)
        if len(text) > limit:
            text = "（更早的消息已省略）\n" + text[-limit:]
        return text

    @staticmethod
    def _recent_hint(transcript: str) -> str:
        for line in reversed(transcript.splitlines()):
            clean = line.strip()
            if clean and not clean.startswith("（"):
                return clean[:40]
        return ""

    async def _memory(self, group_name: str, group_id: str, recent: str) -> str:
        cfg = self.context_config()
        if not cfg.use_memory:
            return ""
        template = _MEMORY_QUERY_TEMPLATE
        try:
            query = template.format(group_name=group_name or group_id, group_id=group_id, recent=recent)
        except (KeyError, IndexError, ValueError):
            query = template
        if not query.strip():
            return ""
        try:
            content = await self.ctx.call_capability(
                "knowledge.search",
                query=query.strip(),
                limit=max(1, _MEMORY_LIMIT),
                group_id=str(group_id),
                mode="search",
            )
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("群梗名片：长期记忆检索失败: %s", exc)
            return ""
        if not isinstance(content, str):
            return ""
        content = content.strip()
        if not content or content.startswith("你不太了解"):
            return ""
        return content[:600]

    async def _persona(self) -> dict[str, Any]:
        cfg = self.config.persona
        bundle: dict[str, Any] = {"nickname": "", "personality": "", "reply_style": "", "aliases": []}
        if cfg.use_host_personality:
            bundle["nickname"] = await self._host_config("bot.nickname")
            bundle["personality"] = await self._host_config("personality.personality")
            bundle["reply_style"] = await self._host_config("personality.reply_style")
            if cfg.use_host_alias_names:
                bundle["aliases"] = await self._host_alias_names()
        if not bundle["nickname"]:
            bundle["nickname"] = str(self.config.bot.base_name or "").strip()
        return bundle

    def _log_warning(self, message: str, *args: Any) -> None:
        """写日志；Runner 尚未注入 ctx 时静默忽略，避免在异常处理里二次抛错。"""

        try:
            self.ctx.logger.warning(message, *args)
        except Exception:  # noqa: BLE001
            pass

    async def _host_alias_names(self, limit: int = 6) -> list[str]:
        """读取宿主 bot.alias_names，作为额外的名字词根。"""

        try:
            value = await self.ctx.config.get("bot.alias_names", [])
        except Exception as exc:  # noqa: BLE001
            self._log_warning("群梗名片：读取宿主配置 bot.alias_names 失败: %s", exc)
            return []
        if isinstance(value, str):
            raw: list[Any] = re.split(r"[,，、;；\s]+", value)
        elif isinstance(value, (list, tuple, set)):
            raw = list(value)
        else:
            return []
        names: list[str] = []
        for item in raw:
            name = str(item or "").strip()
            if name and name not in names:
                names.append(name)
        return names[:limit]

    async def _host_config(self, key: str) -> str:
        try:
            value = await self.ctx.config.get(key, "")
        except Exception as exc:  # noqa: BLE001
            self._log_warning("群梗名片：读取宿主配置 %s 失败: %s", key, exc)
            return ""
        return str(value or "").strip()

    # ==================== 生成 ====================

    _HOT_STOP_CHARS = frozenset(
        "的了是我你他她它们这那在有不就都和跟也还要会能说个上下面里吗吧呢啊哦嗯呀嘛把被给让对从到于与及或但而而且所以如果因为可以没什么一二三四五六七八九十"
        "哈嘻嘿呃呜咦唉哇"
    )

    @classmethod
    def _hot_words(cls, transcript: str, limit: int = 6) -> list[str]:
        """从群聊记录里统计高频词，作为造词的素材（纯统计，不额外调用模型）。"""

        if not transcript:
            return []
        counts: dict[str, int] = {}
        for line in transcript.splitlines():
            text = re.sub(r"^\s*\[[^\]]{0,24}\]\s*", "", line.strip())
            text = re.sub(r"^\s*[^\s:：]{1,24}\s*[:：]\s*", "", text)
            for run in re.findall(r"[\u4e00-\u9fff]+", text):
                for size in (1, 2, 3):
                    for start in range(0, len(run) - size + 1):
                        word = run[start : start + size]
                        if any(char in cls._HOT_STOP_CHARS for char in word):
                            continue
                        counts[word] = counts.get(word, 0) + 1
        ranked = sorted(counts.items(), key=lambda item: (-(item[1] * len(item[0])), -len(item[0]), item[0]))
        picked: list[str] = []
        for word, seen in ranked:
            # 短词多半是巧合，要求出现更多次；长词几乎是真梗，出现两次就算
            if seen < (3 if len(word) == 1 else 2):
                continue
            if any(word in chosen or chosen in word for chosen in picked):
                continue
            picked.append(word)
            if len(picked) >= limit:
                break
        return picked

    async def _generate(self, group_name: str, transcript: str, memory: str, *, mood: str = "") -> str:
        cfg = self.config.generation
        self._last_gen_error = ""
        persona = await self._persona()
        base_name = str(self.config.bot.base_name or "").strip() or persona["nickname"] or "麦麦"
        hot_words = self._hot_words(transcript)
        roots: list[str] = []
        for name in [base_name, *list(persona.get("aliases") or [])]:
            text = str(name or "").strip()
            if text and text not in roots:
                roots.append(text)
        root_chars = {char for char in "".join(roots) if char.strip()}
        feedback = ""
        attempts = max(1, _GEN_RETRY_TIMES + 1)
        for index in range(attempts):
            prompt = self._build_prompt(
                persona=persona,
                base_name=base_name,
                roots=roots,
                group_name=group_name,
                transcript=transcript,
                memory=memory,
                hot_words=hot_words,
                mood=mood,
                feedback=feedback,
            )
            payload: dict[str, Any] = {
                "prompt": prompt,
                "temperature": float(cfg.temperature),
            }
            # 刻意不给模型设 max_tokens：思考型模型会把思考也计进额度，设小了会出现正文为空。
            task = str(cfg.task_name or "replyer").strip()
            if task:
                payload["task_name"] = task
            model_override = str(cfg.model_name or "").strip()
            if model_override:
                payload["model_name"] = model_override
            try:
                # 这里刻意绕开 ctx.llm.generate，只为了能传 timeout_ms：
                # SDK 封装没暴露超时参数，走的是默认 30 秒，模型链切换模型时会被直接掐断。
                result = await self.ctx.call_capability(
                    "llm.generate",
                    timeout_ms=int(_GEN_TIMEOUT_SECONDS * 1000),
                    **payload,
                )
            except Exception as exc:  # noqa: BLE001
                reason = str(exc)
                self._last_gen_error = (
                    f"模型调用超时（{_GEN_TIMEOUT_SECONDS:.0f} 秒）"
                    if "TIMEOUT" in reason.upper() or "超时" in reason
                    else f"模型调用失败: {reason}"
                )
                self.ctx.logger.error("群梗名片：调用模型失败: %s", exc)
                feedback = f"上一次调用出错了（{exc}），请直接给出群名片。"
                continue

            raw = ""
            if isinstance(result, dict):
                raw = str(result.get("response") or result.get("content") or "")
                if not raw.strip():
                    self._last_gen_error = self._describe_empty_result(result)
            elif isinstance(result, str):
                raw = result
            candidate = self._finalize(raw, base_name)
            if candidate:
                banned = self._hit_forbidden(candidate)
                if banned:
                    self.ctx.logger.warning("群梗名片：第 %s 次结果含禁用词 %r: %r", index + 1, banned, candidate)
                    self._last_gen_error = f"模型给的「{candidate}」里带了不让用的词「{banned}」"
                    feedback = f"「{banned}」是禁止出现的词，换个说法重新给一个群名片。"
                    continue
                if not root_chars or any(char in candidate for char in root_chars):
                    return candidate
                self.ctx.logger.warning(
                    "群梗名片：第 %s 次结果没带上名字词根（%s）: %r",
                    index + 1,
                    "、".join(roots),
                    candidate,
                )
                self._last_gen_error = f"模型给的「{candidate}」没带上你的名字"
                sample_root = roots[0] if roots else "麦"
                feedback = (
                    f"「{candidate}」里没有带上你的名字，群友认不出这是你。"
                    f"必须把名字里的字嵌进去（{'、'.join(roots)}），"
                    f"像「大{sample_root[0]}脚」这种结构，重新给一个。"
                )
                continue
            self.ctx.logger.warning("群梗名片：第 %s 次生成结果不合法: %r", index + 1, raw[:80])
            feedback = "上一次输出不合法（可能带引号、换行、解释或超长），请只输出一行群名片文本。"
        return ""

    def _hit_forbidden(self, candidate: str) -> str:
        """返回名片里命中的第一个禁用词；都没命中则返回空串。"""

        for item in list(self.config.persona.forbidden_words or []):
            word = str(item or "").strip()
            if word and word in candidate:
                return word
        return ""

    def _build_prompt(
        self,
        *,
        persona: dict[str, Any],
        base_name: str,
        roots: list[str],
        group_name: str,
        transcript: str,
        memory: str,
        hot_words: list[str],
        mood: str,
        feedback: str,
    ) -> str:
        cfg = self.config.generation
        mode = str(self.config.bot.apply_mode or "replace").strip().lower()
        separator = _SUFFIX_SEPARATOR
        max_length = max(4, int(self.config.bot.max_length))

        root = roots[0] if roots else (base_name or "麦麦")
        first = root[0]
        name_list = "、".join(roots) if roots else root
        lines: list[str] = [
            f"群里的人都叫你「{name_list}」。现在要给你换一个群名片（群昵称）。",
            "它得像群友互相起的外号：拿你的名字当词根，嫁接这个群最近真正在聊的梗。",
            "你的名字有好几个，挑哪个当词根都行，哪个跟群里的梗最容易拼成词就用哪个。",
            "不要诗句、不要网名、不要意境短语。",
            "",
            "【怎么才算合格】",
            f"· 合格：你叫{root}、群里天天在聊脚 → 「大{first}脚」「{first}脚子」「{first}门拖鞋」，"
            "一眼看出是拿你的名字在玩梗。",
            "· 不合格：「雨声里翻书」「夜色温柔」——那是诗句，跟群里的梗没关系，也看不出你是谁。",
            "",
            "【你的人格设定】",
            persona["personality"] or "（未配置，按你自己的性格来）",
            "",
            "【你的说话风格】",
            persona["reply_style"] or "（未配置）",
        ]
        if extra_persona := str(self.config.persona.extra_personality or "").strip():
            lines += ["", "【额外人格（叠加在上面的人设之上）】", extra_persona]
        tone_pool = [
            str(item).strip() for item in (self.config.persona.style_presets or []) if str(item).strip()
        ]
        if tone_pool:
            lines += ["", f"【这次的语气】{random.choice(tone_pool)}（只影响用词口味，不改造词规则）"]
        if group_name:
            lines += ["", f"【你所在的群】{group_name}"]
        if hot_words:
            lines += [
                "",
                "【这个群最近反复出现的高频词（造词素材，优先从这里挑梗）】",
                "、".join(hot_words),
            ]
        if transcript:
            lines += ["", "【这个群最近的聊天记录】", transcript]
        if memory:
            lines += ["", "【你对这个群的长期记忆】", memory]
        if mood:
            lines += ["", f"【本次特别要求】{mood}"]
        hints = [str(item).strip() for item in cfg.style_hints if str(item).strip()]
        if hints:
            lines += ["", f"【这次的造词手法】{random.choice(hints)}"]
        if persona_extra := str(self.config.persona.extra_instruction or "").strip():
            lines += ["", f"【额外要求】{persona_extra}"]

        lines += ["", "【任务要求】"]
        if mode == "replace":
            lines.append(
                f"1. 只给一个绰号：2~6 个字，必须包含名字里的字（{name_list}），硬上限 {max_length} 个字。"
            )
        else:
            lines.append(
                f"1. 群名片格式固定为「{root}{separator}后缀」，"
                f"其中「{root}{separator}」是固定前缀，你只创作后面的后缀；整张名片不超过 {max_length} 个字。"
            )
        lines += [
            "2. 绰号要能听出梗的来处（群里最近在聊什么就借什么），但别写成一句完整的话。",
            "3. 不要出现具体群友的昵称或 QQ 号，不要暴露隐私。",
            "4. 不要引号、不要换行、不要解释、不要 @、不要 emoji、不要括号注解。",
        ]
        banned = [str(item).strip() for item in (self.config.persona.forbidden_words or []) if str(item).strip()]
        if banned:
            lines.append("5. 绝对不要出现这些词/字：" + "、".join(banned) + "。")
        lines.append(f"{6 if banned else 5}. 只输出群名片这一行文本，不要输出任何别的内容。")
        if feedback:
            lines += ["", f"【纠正】{feedback}"]
        return "\n".join(lines)

    @staticmethod
    def _describe_empty_result(result: dict[str, Any]) -> str:
        """模型没给正文时，说清到底是哪种原因，好让人直接改配置。"""

        if result.get("success") is False:
            return f"模型调用失败：{str(result.get('error') or '未说明原因')[:120]}"
        reasoning = str(result.get("reasoning") or "").strip()
        if reasoning:
            return (
                f"模型把输出额度全用在思考上了，正文为空（思考 {len(reasoning)} 字）"
                "——多半是模型配置里的 max_tokens 太小，或用 generation.model_name 换个非思考模型"
            )
        return "模型没有返回可用文本"

    def _finalize(self, raw: str, base_name: str) -> str:
        """清洗模型输出，返回可写入的群名片；不合法时返回空串。"""
        if not raw or not str(raw).strip():
            return ""

        text = ""
        for line in str(raw).splitlines():
            if line.strip():
                text = line.strip()
                break
        for prefix in _PREFIXES:
            if text.startswith(prefix):
                text = text[len(prefix) :].strip()
        text = text.strip(_QUOTE_CHARS)
        text = re.sub(r"\s+", " ", text).strip()
        text = re.sub(r"@\S+", "", text).strip()
        if not text:
            return ""

        cfg = self.config.bot
        mode = str(cfg.apply_mode or "suffix").strip().lower()
        max_length = max(4, int(cfg.max_length))
        head = ""
        phrase = text
        room = max_length

        if mode != "replace" and base_name:
            candidate_head = f"{base_name}{_SUFFIX_SEPARATOR}"
            if len(candidate_head) < max_length - 1:
                head = candidate_head
                room = max_length - len(head)
                if phrase.startswith(head):
                    phrase = phrase[len(head) :].strip()

        phrase = phrase.strip(_QUOTE_CHARS + "-_|丨·•,，。.、!！?？~～")
        if not phrase:
            return ""
        if len(phrase) > room:
            phrase = phrase[:room].strip(_QUOTE_CHARS + "-_|丨·•,，。.、")
        if not phrase:
            return ""

        card = f"{head}{phrase}" if head else phrase
        card = re.sub(r"\s+", " ", card).strip()
        return card[:max_length]

    # ==================== NapCat HTTP ====================

    def _adapter_api_candidates(self) -> list[str]:
        """按「短名 → 各适配器限定名」的顺序给出候选 API 名。"""

        namespace = _ADAPTER_API_NAMESPACE
        names = [f"{namespace}.action.call"]
        for plugin_id in _ADAPTER_PLUGIN_IDS:
            names.append(f"{plugin_id}.{namespace}.action.call")
        return names

    @staticmethod
    def _unwrap_adapter_result(result: Any) -> dict[str, Any] | None:
        """把 adapter API 的返回值归一成 OneBot 响应字典。"""

        if result is None:
            return None
        if isinstance(result, dict):
            if "status" in result or "retcode" in result:
                return result
            if result.get("success") is False:
                # 适配器这一层就失败了（例如 WebSocket 断开）：当作通道不可用，交给调用方回退直连 HTTP。
                return None
            inner = result.get("result")
            if isinstance(inner, dict):
                return inner
            return result
        return {"status": "failed", "message": str(result)[:200], "retcode": -1}

    async def _adapter_action(self, action: str, params: dict[str, Any]) -> dict[str, Any] | None:
        """通过适配器插件的公开 API 调用 OneBot 动作，复用它已经连着的 WebSocket。

        候选顺序是「短名 → 各适配器限定名」。宿主解析 API 名的规则是「全表按 name /
        full_name 精确匹配，命中多个就报『名称不唯一』」，而 napcat-adapter 与
        snowluma-adapter 暴露的是同一套 `adapter.napcat.*` 名字；两个都装着时短名必然
        不唯一，得用 `插件ID.API名` 的限定名指定提供方。而两个适配器里往往只有一个真
        连着，所以这里逐个试，谁真的返回了 OneBot 响应就用谁，并记下来下次直接用。

        Returns:
            dict: OneBot 响应字典；返回 None 表示这条通道用不了，调用方应回退到 HTTP。
        """

        if self._adapter_api_ok is False:
            return None
        candidates: list[str] = []
        if self._adapter_api_winner:
            candidates.append(self._adapter_api_winner)
        for name in self._adapter_api_candidates():
            if name not in candidates:
                candidates.append(name)
        if not candidates:
            return None

        last_error = ""
        missing = 0
        for api_name in candidates:
            try:
                result = await self.ctx.api.call(api_name, action_name=action, params=params)
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                if self._adapter_api_winner == api_name:
                    self._adapter_api_winner = ""
                if any(token in last_error.lower() for token in _ADAPTER_API_MISSING_TOKENS):
                    missing += 1
                continue
            response = self._unwrap_adapter_result(result)
            if response is None:
                # 这个提供方自己那层失败了（多半是它的 WebSocket 没连），换下一个候选。
                last_error = f"没有返回可用的 OneBot 响应: {result!r}"
                if self._adapter_api_winner == api_name:
                    self._adapter_api_winner = ""
                continue
            if self._adapter_api_winner != api_name:
                self._adapter_api_winner = api_name
                self.ctx.logger.info("群梗名片：已改用适配器通道 %s，无需 NapCat HTTP", api_name)
            self._adapter_api_ok = True
            return response

        if missing and missing == len(candidates):
            self._adapter_api_ok = False
            self.ctx.logger.info(
                "群梗名片：找不到可用的适配器 API（%s），改用直连 NapCat HTTP: %s",
                "、".join(candidates),
                last_error,
            )
        else:
            self._log_warning("群梗名片：适配器通道都不可用，本次回退直连 HTTP: %s", last_error)
        return None

    async def _onebot(self, action: str, **payload: Any) -> dict[str, Any]:
        """调用 OneBot 动作：优先走 napcat-adapter 已连接的 WebSocket，用不了再直连 NapCat HTTP。"""

        if _PREFER_ADAPTER_API:
            via_adapter = await self._adapter_action(action, dict(payload))
            if via_adapter is not None:
                return via_adapter
        return await self._http_action(action, dict(payload))

    async def _http_action(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        """直连 NapCat HTTP 服务器调用 OneBot 动作（备选通道）。"""

        base = _NAPCAT_HTTP_BASE_URL.strip().rstrip("/")
        if not base:
            return {"status": "failed", "message": "未配置 NapCat HTTP 地址", "retcode": -1}
        url = f"{base}/{action}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        token = _NAPCAT_HTTP_TOKEN.strip()
        timeout = _HTTP_TIMEOUT_SECONDS

        def _post() -> str:
            request = urllib.request.Request(url, data=body, method="POST")
            request.add_header("Content-Type", "application/json")
            if token:
                request.add_header("Authorization", f"Bearer {token}")
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", errors="replace")

        try:
            raw = await asyncio.to_thread(_post)
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("群梗名片：调用 NapCat %s 失败: %s", action, exc)
            return {"status": "failed", "message": f"{type(exc).__name__}: {exc}", "retcode": -1}
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return {"status": "failed", "message": raw[:200], "retcode": -1}
        return parsed if isinstance(parsed, dict) else {"status": "failed", "message": str(parsed)[:200]}

    @staticmethod
    def _as_id(value: str) -> int | str:
        text = str(value or "").strip()
        return int(text) if text.isdigit() else text

    async def _bot_qq(self) -> str:
        if self._bot_qq_cache:
            return self._bot_qq_cache
        resp = await self._onebot("get_login_info")
        data = resp.get("data") if isinstance(resp, dict) else None
        if isinstance(data, dict):
            self._bot_qq_cache = str(data.get("user_id") or "").strip()
        if not self._bot_qq_cache:
            self.ctx.logger.error("群梗名片：拿不到机器人 QQ 号（get_login_info 没返回），请检查 OneBot 通道")
        return self._bot_qq_cache

    async def _set_card(self, group_id: str, card: str) -> bool:
        qq = await self._bot_qq()
        if not qq:
            return False
        resp = await self._onebot(
            "set_group_card",
            group_id=self._as_id(group_id),
            user_id=self._as_id(qq),
            card=card,
        )
        if isinstance(resp, dict) and str(resp.get("status")) == "ok":
            return True
        self.ctx.logger.error("群梗名片：set_group_card 失败: %s", resp)
        return False

    async def _current_card(self, group_id: str) -> str:
        qq = await self._bot_qq()
        if not qq:
            return ""
        resp = await self._onebot(
            "get_group_member_info",
            group_id=self._as_id(group_id),
            user_id=self._as_id(qq),
            no_cache=True,
        )
        data = resp.get("data") if isinstance(resp, dict) else None
        if isinstance(data, dict):
            return str(data.get("card") or "").strip() or str(data.get("nickname") or "").strip()
        return ""

    # ==================== 状态 ====================

    def _state_path(self) -> Path:
        return Path(self.ctx.paths.data_dir) / "state.json"

    def _load_state(self) -> None:
        try:
            path = self._state_path()
            if not path.exists():
                return
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                groups = data.get("groups")
                roll = data.get("last_roll")
                self._state = {
                    "groups": groups if isinstance(groups, dict) else {},
                    "last_run": float(data.get("last_run") or 0.0),
                    "last_roll": roll if isinstance(roll, dict) else {},
                }
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("群梗名片：读取状态失败: %s", exc)

    def _save_state(self) -> None:
        try:
            path = self._state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("群梗名片：写入状态失败: %s", exc)

    def context_config(self) -> ContextSection:
        """读取上下文配置（便于集中取默认值）。"""
        return self.config.context

    # ==================== 组件：命令 ====================

    @Command(
        "group_card",
        description="生成/预览/恢复自己的群名片",
        pattern=r"^/(?:麦片|名片)(?:\s+(?P<arg>.+))?$",  # 主名字 /麦片，/名片 保留为兼容写法
        permission="operator",
    )
    async def handle_group_card(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        """处理 /麦片（兼容 /名片）系列命令。"""
        if not self.config.plugin.enabled:
            return False, "群梗名片插件未启用", True

        arg = self._command_arg(kwargs)
        keyword, _, rest = arg.partition(" ")
        keyword = keyword.strip().lower()
        rest = rest.strip()

        group_id = str(kwargs.get("group_id") or "").strip() or await self._group_id_of_stream(stream_id)
        if not group_id:
            await self.ctx.send.text("这个会话不是群聊，改不了群名片。", stream_id)
            return False, "非群聊会话", True
        group_name = await self._group_name_of(group_id)

        if keyword in {"状态", "status"}:
            await self.ctx.send.text(await self._status_text(group_id), stream_id)
            return True, "已输出名片状态", True

        if keyword in {"探测", "通道", "probe"}:
            text = await self._probe_text(group_id)
            await self.ctx.send.text(text, stream_id)
            return True, "已输出通道探测结果", True

        if keyword in {"现在", "看看", "show"}:
            card = await self._current_card(group_id)
            await self.ctx.send.text(f"当前群名片：{card or '（空，显示的是账号昵称）'}", stream_id)
            return True, "已输出当前群名片", True

        if keyword in {"预览", "preview"}:
            ok, text = await self._preview(group_id, group_name, mood=rest)
            await self.ctx.send.text(f"预览（未写入）：{text}" if ok else f"生成失败：{text}", stream_id)
            return ok, text, True

        if keyword in {"用", "use"}:
            if not rest:
                await self.ctx.send.text("用法：/麦片 用 <名片文本>", stream_id)
                return False, "缺少名片文本", True
            card = self._finalize(rest, "") or rest[: int(self.config.bot.max_length)]
            if await self._set_card(group_id, card):
                self._state.setdefault("groups", {})[group_id] = {
                    "card": card,
                    "at": time.time(),
                    "trigger": "command",
                    "group_name": group_name,
                }
                self._save_state()
                await self.ctx.send.text(f"已把群名片改成：{card}", stream_id)
                return True, card, True
            await self.ctx.send.text("写入失败：OneBot 通道不可用或没有群管理权限（发 /麦片 探测 查通道）。", stream_id)
            return False, "写入失败", True

        if keyword in {"恢复", "reset"}:
            persona = await self._persona()
            base = str(self.config.bot.base_name or "").strip() or persona["nickname"] or "麦麦"
            if await self._set_card(group_id, base):
                await self.ctx.send.text(f"已把群名片恢复成：{base}", stream_id)
                return True, base, True
            await self.ctx.send.text("恢复失败：OneBot 通道不可用或没有群管理权限（发 /麦片 探测 查通道）。", stream_id)
            return False, "恢复失败", True

        # 默认：立即换一张（可以把多余文字当成风格要求）
        ok, text = await self._refresh_with_reply(group_id, group_name, mood=arg)
        await self.ctx.send.text(f"新群名片：{text}" if ok else f"换名片失败：{text}", stream_id)
        return ok, text, True

    @staticmethod
    def _command_arg(kwargs: dict[str, Any]) -> str:
        matched = kwargs.get("matched_groups")
        if isinstance(matched, dict) and matched.get("arg"):
            return str(matched["arg"]).strip()
        for key in ("arg", "text", "raw_message", "plain_text"):
            value = kwargs.get(key)
            if isinstance(value, str) and value.strip():
                found = re.match(r"^/(?:麦片|名片)\s*(.*)$", value.strip(), re.DOTALL)
                if found:
                    return found.group(1).strip()
        return ""

    async def _refresh_with_reply(self, group_id: str, group_name: str, *, mood: str = "") -> tuple[bool, str]:
        ok, text = await self._refresh_one({"group_id": group_id, "group_name": group_name}, trigger="command", mood=mood)
        return ok, text

    async def _preview(self, group_id: str, group_name: str, *, mood: str = "") -> tuple[bool, str]:
        stream_id = await self._stream_id_of(group_id)
        transcript = await self._transcript(stream_id) if stream_id else ""
        memory = await self._memory(group_name, group_id, self._recent_hint(transcript))
        card = await self._generate(group_name, transcript, memory, mood=mood)
        if not card:
            return False, self._last_gen_error or "模型没有返回可用文本"
        return True, card

    @staticmethod
    def _resp_hint(resp: Any) -> str:
        if isinstance(resp, dict):
            return str(resp.get("message") or resp.get("wording") or resp.get("status") or "无响应")[:60]
        return str(resp or "无响应")[:60]

    async def _probe_text(self, group_id: str) -> str:
        """分别探测两条 OneBot 通道，方便定位到底卡在哪。"""

        candidates = self._adapter_api_candidates()
        lines: list[str] = []

        adapter_resp = await self._adapter_action("get_login_info", {})
        if isinstance(adapter_resp, dict) and str(adapter_resp.get("status")) == "ok":
            data = adapter_resp.get("data")
            data = data if isinstance(data, dict) else {}
            lines.append(
                f"① 适配器 WebSocket：可用（通道 {self._adapter_api_winner or '?'}，"
                f"机器人 QQ={data.get('user_id') or '未知'}）"
            )
        else:
            lines.append(f"① 适配器 WebSocket：不可用（{self._resp_hint(adapter_resp)}）")

        http_resp = await self._http_action("get_login_info", {})
        if isinstance(http_resp, dict) and str(http_resp.get("status")) == "ok":
            data = http_resp.get("data")
            data = data if isinstance(data, dict) else {}
            lines.append(f"② 直连 NapCat HTTP（{_NAPCAT_HTTP_BASE_URL}）：可用（机器人 QQ={data.get('user_id') or '未知'}）")
        else:
            lines.append(f"② 直连 NapCat HTTP（{_NAPCAT_HTTP_BASE_URL}）：不可用（{self._resp_hint(http_resp)}）")

        card = await self._current_card(group_id)
        lines.append(f"本群当前真实名片：{card or '（读不到：要么没设过，要么两条通道都不通）'}")
        state_text = {True: "可用", False: "不可用（已回退）", None: "尚未验证"}.get(self._adapter_api_ok, "尚未验证")
        lines.insert(0, f"适配器 API 候选：{'、'.join(candidates) or '（未配置）'}｜{state_text}")
        return "\n".join(lines)

    async def _status_text(self, group_id: str) -> str:
        cfg = self.config.schedule
        info = (self._state.get("groups") or {}).get(group_id)
        if self._adapter_api_ok is True and self._adapter_api_winner:
            channel = f"适配器 WebSocket（{self._adapter_api_winner}）"
        else:
            channel = {True: "适配器 WebSocket", False: "直连 NapCat HTTP", None: "未验证"}.get(
                self._adapter_api_ok, "未验证"
            )
        lines = [
            f"调度：{'开启' if cfg.enabled else '关闭'}（每 {cfg.interval_minutes} 分钟检查一次，换名概率 {cfg.trigger_probability:g}）",
            f"生成任务：{self.config.generation.task_name}",
            f"OneBot 通道：{channel}",
            f"上次全量刷新：{self._fmt_time(self._state.get('last_run'))}",
            self._last_roll_text(),
        ]
        if isinstance(info, dict):
            lines.append(
                f"本群上次名片：「{info.get('card')}」（{self._fmt_time(info.get('at'))}，来源 {info.get('trigger')}）"
            )
        else:
            lines.append("本群还没有换名记录")
        return "\n".join(lines)

    @staticmethod
    def _fmt_time(value: Any) -> str:
        try:
            stamp = float(value or 0.0)
        except (TypeError, ValueError):
            return "无记录"
        if stamp <= 0:
            return "无记录"
        return datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M:%S")

    # ==================== 组件：工具 ====================

    @Tool(
        "change_my_group_card",
        description="结合当前群的近况和自己的长期记忆，给自己换一个符合人设的群名片",
        parameters=[
            ToolParameterInfo(
                name="mood",
                param_type=ToolParamType.STRING,
                description="可选：想要的风格或心情，例如 清冷、俏皮、古风",
                required=False,
            )
        ],
    )
    async def tool_change_group_card(
        self, mood: str = "", stream_id: str = "", chat_id: str = "", **kwargs: Any
    ) -> dict[str, Any]:
        """供 Planner 调用：给自己换群名片。"""
        del kwargs
        if not (self.config.plugin.enabled and self.config.tool.enabled):
            return {"name": "change_my_group_card", "content": "换名片能力当前未开启"}
        group_id = await self._group_id_of_stream(str(stream_id or chat_id or ""))
        if not group_id:
            return {"name": "change_my_group_card", "content": "当前不在群聊里，改不了群名片"}
        group_name = await self._group_name_of(group_id)
        ok, text = await self._refresh_one(
            {"group_id": group_id, "group_name": group_name},
            trigger="tool",
            mood=str(mood or "").strip(),
        )
        if ok:
            return {"name": "change_my_group_card", "content": f"已经把群名片换成「{text}」"}
        return {"name": "change_my_group_card", "content": f"换群名片没成功：{text}"}


def create_plugin() -> PoeticGroupCardPlugin:
    """创建群梗名片插件实例。"""
    return PoeticGroupCardPlugin()
