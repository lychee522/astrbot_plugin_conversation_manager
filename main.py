"""AstrBot 对话管理插件（astrbot_plugin_conversation_manager）。

在 Dashboard 左侧提供「对话管理」页面：可视化浏览 / 编辑 / 删除 / 插入
任何平台会话的消息（含模型 think 思考过程），支持自动快照撤销、JSONL 导入导出、
手动与按阈值（轮数/字数/Token）自动分块压缩摘要（带当场重试、备用模型切换与延迟重试保命机制）。

设计约束（全部对照 AstrBot 4.29.3 源码核对）：
- 数据层只走官方 conversation_manager，不直接访问数据库，跨版本安全。
- Web API 走 context.register_web_api，最终挂载在 /api/plug/<route>，
  由 dashboard 统一鉴权；handler 为 Quart view（**kwargs），query/body
  经全局 quart.request 读取。
- 响应信封统一 {"success": True/False, "data"/"message"}。
- 任何 handler 异常都在内部消化，压缩失败绝不删改原历史记录，绝不影响 bot 主流程。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:  # dashboard 基于 Quart，handler 运行在其应用上下文中
    from quart import request as _quart_request
except Exception:  # pragma: no cover - 理论上不会发生
    _quart_request = None

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

PLUGIN_NAME = "astrbot_plugin_conversation_manager"
MAX_TEXT_PER_MSG_FOR_SUMMARY = 2400
SUMMARY_CHUNK_CHARS = 16000
AUTO_RETRY_INTERVAL_SEC = 300  # 自动压缩失败后等待 5 分钟再试
AUTO_MAX_RETRIES = 3           # 自动压缩最多连续尝试 3 次

DEFAULT_COMPRESS_INSTRUCTION = (
    "你是对话历史压缩器。请把下面的多轮对话压缩成一份第三人称中文摘要，用于替换原始历史作为长期记忆。"
    "必须保留：人物关系与固定称呼、关键事件与大致时间线、双方做出的约定与承诺、"
    "重要的情绪转折、以及尚未回收的伏笔与悬而未决的话题。"
    "不要发表评论，不要使用 Markdown 标题符号，直接输出摘要正文。"
)
MERGE_INSTRUCTION = (
    "下面是对同一段对话分块压缩出的多份摘要。请把它们合并成一份连贯的中文摘要，"
    "按时间顺序整合事件、约定与伏笔，去除重复，保留所有关键信息。直接输出摘要正文。"
)
ACK_TEXT = "Acknowledged the summary of our previous conversation history."

_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\u3000-\u303f\uff00-\uffef]")


def _now_ts() -> int:
    return int(time.time())


def _str_tokens(s: str) -> float:
    """区分中英文估算单个字符串的 Token 数（贴合 DeepSeek/Qwen/OpenAI 分词器）。"""
    if not s:
        return 0.0
    cjk = len(_CJK_RE.findall(s))
    other = len(s) - cjk
    return cjk * 0.65 + other * 0.25


def split_rounds(history: list[dict]) -> list[list[dict]]:
    """按 user 消息切分轮次；开头非 user 的消息并入第一轮。"""
    rounds: list[list[dict]] = []
    cur: list[dict] = []
    for m in history:
        if m.get("role") == "user" and cur:
            rounds.append(cur)
            cur = [m]
        else:
            cur.append(m)
    if cur:
        rounds.append(cur)
    return rounds


def flatten_rounds(rounds: list[list[dict]]) -> list[dict]:
    out: list[dict] = []
    for r in rounds:
        out.extend(r)
    return out


def calc_stats(history: list[dict]) -> dict[str, int]:
    """统一统计会话的轮数、正文字数、思考字数、总字数及包含思考内容的 Token 估算。"""
    text_chars = 0
    think_chars = 0
    text_tok = 0.0
    think_tok = 0.0

    for m in history:
        c = m.get("content")
        if isinstance(c, str):
            text_chars += len(c)
            text_tok += _str_tokens(c)
        elif isinstance(c, list):
            for p in c:
                if not isinstance(p, dict):
                    continue
                ptype = p.get("type")
                if ptype == "think":
                    th = str(p.get("think") or "")
                    think_chars += len(th)
                    think_tok += _str_tokens(th)
                else:
                    tx = str(p.get("text") or "")
                    text_chars += len(tx)
                    text_tok += _str_tokens(tx)

    overhead = 4 * len(history)
    text_tokens_int = int(round(text_tok)) + overhead
    think_tokens_int = int(round(think_tok))
    return {
        "msg_count": len(history),
        "rounds": len(split_rounds(history)),
        "text_chars": text_chars,
        "think_chars": think_chars,
        "total_chars": text_chars + think_chars,
        "text_tokens": text_tokens_int,
        "think_tokens": think_tokens_int,
        "est_tokens": text_tokens_int + think_tokens_int,
    }


def est_tokens(history: list[dict]) -> int:
    """估算包含正文与 think 思考内容的总上下文 Token。"""
    return calc_stats(history)["est_tokens"]


SUMMARY_PREFIX = "Our previous history conversation summary:"
_END_PUNCT = set("。！？….”』」）】.!?'\"~～)）]】》>：:")


def _extract_prev_summary(rounds: list[list[dict]]) -> tuple[str, list[list[dict]]]:
    """检查开头是否已有前一次压缩生成的长期摘要；若有则独立剥离出来用于增量合并，避免被丢弃或截断。"""
    if not rounds:
        return "", rounds
    first_round = rounds[0]
    if not first_round:
        return "", rounds
    first_msg = first_round[0]
    c = first_msg.get("content")
    text = ""
    if isinstance(c, str):
        text = c
    elif isinstance(c, list):
        text = "\n".join(str(p.get("text") or "") for p in c if isinstance(p, dict) and p.get("type") == "text")
    text = text.strip()
    if text.startswith(SUMMARY_PREFIX):
        prev_body = text[len(SUMMARY_PREFIX):].strip()
        return prev_body, rounds[1:]
    return "", rounds


def _check_not_truncated(resp: Any, text: str, pid: str) -> None:
    """检查模型输出是否因 max_tokens 被中途截断，杜绝把半截腰斩的摘要写回数据库。"""
    raw = getattr(resp, "raw_completion", None)
    try:
        choices = getattr(raw, "choices", None)
        if choices and getattr(choices[0], "finish_reason", None) == "length":
            raise RuntimeError(f"模型 {pid} 输出触发了 max_tokens 长度上限被中途截断，请在模型配置中调大 max_tokens 或更换模型")
    except RuntimeError:
        raise
    except Exception:
        pass
    # 兜底检测：较长摘要末尾停在半个词（无句末标点）
    if len(text) >= 120 and text[-1] not in _END_PUNCT:
        raise RuntimeError(
            f"模型 {pid} 生成的摘要疑似中途戛然而止（末尾停在“{text[-10:]}”，可能受限于 max_tokens），已拒绝写回以保护原记录"
        )


def _normalize_contexts_for_chat(msgs: list[dict]) -> list[dict]:
    """将消息规范为严格 user/assistant 交替且以 assistant 结尾的上下文，便于末尾追加 user 总结指令。"""
    merged: list[dict] = []
    for m in msgs:
        role = "assistant" if m.get("role") == "assistant" else "user"
        text = str(m.get("content") or "").strip()
        if not text:
            continue
        if merged and merged[-1]["role"] == role:
            merged[-1]["content"] += "\n" + text
        else:
            merged.append({"role": role, "content": text})
    if not merged:
        return []
    if merged[0]["role"] != "user":
        merged.insert(0, {"role": "user", "content": "（以下为历史对话片段）"})
    if merged[-1]["role"] != "assistant":
        merged.append({"role": "assistant", "content": "好的，我了解了。"})
    return merged


def sanitize_for_summary(history: list[dict], max_chars: int = MAX_TEXT_PER_MSG_FOR_SUMMARY) -> list[dict]:
    """抽取纯文本用于摘要请求（跳过图片/工具/think等非对话正文内容，移除 system_reminder）。"""
    out: list[dict] = []
    for m in history:
        role = m.get("role")
        c = m.get("content")
        texts: list[str] = []
        if isinstance(c, str):
            texts.append(c)
        elif isinstance(c, list):
            for p in c:
                if isinstance(p, dict) and p.get("type") == "text":
                    t = p.get("text") or ""
                    if t:
                        texts.append(t)
        body = re.sub(r"<system_reminder>[\s\S]*?</system_reminder>", "", "\n".join(texts)).strip()
        if not body:
            continue
        if len(body) > max_chars:
            body = body[:max_chars] + "…（超长截断）"
        out.append({"role": role if role in ("user", "assistant") else "user", "content": body})
    return out


def pack_chunks(msgs: list[dict], max_chars: int = SUMMARY_CHUNK_CHARS) -> list[list[dict]]:
    """把消息贪心打包成若干块，每块总字符不超过 max_chars（至少一条）。"""
    chunks: list[list[dict]] = []
    cur: list[dict] = []
    size = 0
    for m in msgs:
        n = len(m.get("content") or "")
        if cur and size + n > max_chars:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(m)
        size += n
    if cur:
        chunks.append(cur)
    return chunks


def validate_history(history: Any) -> tuple[bool, str]:
    if not isinstance(history, list):
        return False, "history 必须是数组"
    for m in history:
        if not isinstance(m, dict) or "role" not in m or "content" not in m:
            return False, "存在缺少 role/content 的消息"
    return True, ""


@register(
    PLUGIN_NAME,
    "lychee522",
    "对话可视化管理：浏览/编辑/删除/插入消息与思考过程、自动快照撤销、JSONL 导入导出、手动与按阈值自动分块压缩摘要",
    "1.1.1",
)
class ConversationManagerPlugin(Star):
    def __init__(self, context: Context, config=None) -> None:
        super().__init__(context)
        self.context = context
        self.config = config if config is not None else {}
        self._compress_jobs: dict[str, dict] = {}
        self._compress_tasks: dict[str, asyncio.Task] = {}
        self._auto_check_tasks: dict[str, asyncio.Task] = {}
        self._retry_timer_tasks: dict[str, asyncio.Task] = {}
        # {cid: {"retry_count": int, "paused": bool, "last_error": str, "next_retry_at": int}}
        self._auto_retry_state: dict[str, dict] = {}

    # ---------- helpers ----------

    def _cfg(self, key: str, default: Any) -> Any:
        try:
            v = self.config.get(key, default)
            return default if v is None else v
        except Exception:
            return default

    def _snap_dir(self, cid: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", cid)[:64] or "unknown"
        d = Path(StarTools.get_data_dir(PLUGIN_NAME)) / "snapshots" / safe
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _param(self, name: str, default: Any = None) -> Any:
        try:
            if _quart_request is not None:
                return _quart_request.args.get(name, default)
        except Exception:
            pass
        return default

    async def _body(self) -> dict:
        try:
            if _quart_request is not None:
                data = await _quart_request.get_json(silent=True)
                return data if isinstance(data, dict) else {}
        except Exception:
            pass
        return {}

    def _ok(self, data: Any = None) -> dict:
        return {"success": True, "data": data}

    def _err(self, message: str, extra: dict | None = None) -> dict:
        out = {"success": False, "message": str(message)}
        if extra:
            out.update(extra)
        return out

    async def _find_conv(self, cid: str):
        """按 cid 找对话（官方 API）。返回 Conversation 或 None。"""
        if not cid:
            return None
        convs = await self.context.conversation_manager.get_conversations()
        for c in convs:
            if getattr(c, "cid", None) == cid:
                return c
        return None

    async def _snapshot(self, cid: str, conv) -> str:
        """保存当前历史的快照，返回快照名。"""
        snap_dir = self._snap_dir(cid)
        name = time.strftime("%Y%m%d-%H%M%S") + f"-{uuid.uuid4().hex[:4]}"
        payload = {
            "cid": cid,
            "user_id": getattr(conv, "user_id", ""),
            "platform_id": getattr(conv, "platform_id", ""),
            "title": getattr(conv, "title", None),
            "persona_id": getattr(conv, "persona_id", None),
            "created_at": getattr(conv, "created_at", 0),
            "updated_at": getattr(conv, "updated_at", 0),
            "token_usage": getattr(conv, "token_usage", 0),
            "history": json.loads(conv.history or "[]"),
            "snapshotted_at": _now_ts(),
        }
        path = snap_dir / f"{name}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        await self._prune_snapshots(cid)
        return name

    async def _prune_snapshots(self, cid: str) -> None:
        try:
            keep = int(self._cfg("snapshot_keep", 20))
            snap_dir = self._snap_dir(cid)
            files = sorted(snap_dir.glob("*.json"))
            if keep <= 0:
                keep = 1
            for f in files[:-keep] if len(files) > keep else []:
                f.unlink(missing_ok=True)
        except Exception as e:
            logger.warning(f"[对话管理] 清理快照失败: {e}")

    def _load_history(self, conv) -> list[dict]:
        try:
            h = json.loads(conv.history or "[]")
            return h if isinstance(h, list) else []
        except Exception:
            return []

    def _check_thresholds(self, stats: dict[str, int]) -> tuple[list[str], list[str]]:
        """检查当前统计数据是否达到或接近任一限制条件。返回 (已达标原因列表, 接近80%原因列表)。"""
        triggered: list[str] = []
        warnings: list[str] = []

        r_lim = int(self._cfg("auto_compress_rounds", 0) or 0)
        c_lim = int(self._cfg("auto_compress_chars", 0) or 0)
        t_lim = int(self._cfg("token_threshold", 12800) or 0)

        if r_lim > 0:
            if stats["rounds"] >= r_lim:
                triggered.append(f"轮数 {stats['rounds']}/{r_lim} 轮")
            elif stats["rounds"] >= r_lim * 0.8:
                warnings.append(f"轮数接近上限 ({stats['rounds']}/{r_lim})")

        if c_lim > 0:
            if stats["total_chars"] >= c_lim:
                triggered.append(f"字数 {stats['total_chars']}/{c_lim} 字")
            elif stats["total_chars"] >= c_lim * 0.8:
                warnings.append(f"字数接近上限 ({stats['total_chars']}/{c_lim})")

        if t_lim > 0:
            if stats["est_tokens"] >= t_lim:
                triggered.append(f"Token ~{stats['est_tokens']}/{t_lim}")
            elif stats["est_tokens"] >= t_lim * 0.8:
                warnings.append(f"Token 接近上限 (~{stats['est_tokens']}/{t_lim})")

        return triggered, warnings

    async def _resolve_compress_providers(self, umo: str, override_primary_id: str = "") -> tuple[Any, str, Any, str]:
        """解析首选压缩模型与备用兜底模型：(primary_inst, primary_id, fallback_inst, fallback_id)。"""
        primary_id = (override_primary_id or str(self._cfg("auto_compress_provider", "") or "")).strip()
        primary = self.context.get_provider_by_id(primary_id) if primary_id else None

        if primary is None:
            # 默认跟随当前对话模型
            try:
                get_async = getattr(self.context, "get_using_provider_async", None)
                if callable(get_async):
                    primary = await get_async(umo)
                else:
                    primary = self.context.get_using_provider(umo)
            except Exception:
                primary = None
            if primary is not None:
                cfg = getattr(primary, "provider_config", {}) or {}
                primary_id = str(cfg.get("id") or "当前聊天模型")

        fb_id = str(self._cfg("auto_compress_fallback_provider", "") or "").strip()
        fallback = self.context.get_provider_by_id(fb_id) if fb_id else None
        if fallback is primary or (fb_id and fb_id == primary_id):
            fallback = None
            fb_id = ""

        return primary, primary_id, fallback, fb_id

    # ---------- lifecycle ----------

    async def initialize(self):
        prefix = f"/{PLUGIN_NAME}"
        reg = self.context.register_web_api
        routes = [
            (f"{prefix}/list", self.api_list, ["GET"], "会话列表"),
            (f"{prefix}/conv/<cid>", self.api_conv, ["GET"], "读取对话历史"),
            (f"{prefix}/save/<cid>", self.api_save, ["POST"], "保存编辑后的历史"),
            (f"{prefix}/export/<cid>", self.api_export, ["GET"], "导出 JSONL"),
            (f"{prefix}/import/<cid>", self.api_import, ["POST"], "导入 JSONL 替换"),
            (f"{prefix}/snapshots/<cid>", self.api_snapshots, ["GET"], "快照列表"),
            (f"{prefix}/snapshots/restore/<cid>", self.api_restore, ["POST"], "恢复快照"),
            (f"{prefix}/compress", self.api_compress, ["POST"], "手动压缩（后台执行）"),
            (f"{prefix}/compress/status/<cid>", self.api_compress_status, ["GET"], "压缩进度"),
            (f"{prefix}/compress/reset/<cid>", self.api_compress_reset, ["POST"], "重置自动压缩暂停状态"),
            (f"{prefix}/providers", self.api_providers, ["GET"], "可用模型列表"),
            (f"{prefix}/status/<cid>", self.api_status, ["GET"], "上下文压力估算"),
            (f"{prefix}/config", self.api_config_get, ["GET"], "读取面板配置"),
            (f"{prefix}/config", self.api_config_set, ["POST"], "保存面板配置"),
        ]
        for route, handler, methods, desc in routes:
            reg(route, handler, methods, desc)
        logger.info("[对话管理] Web API 注册完成（/api/plug/%s/...）" % PLUGIN_NAME)

    async def terminate(self):
        for task_dict in (self._compress_tasks, self._auto_check_tasks, self._retry_timer_tasks):
            for t in task_dict.values():
                t.cancel()

    # ---------- Auto-compress event hook ----------

    @filter.after_message_sent()
    async def on_after_message_sent(self, event: AstrMessageEvent):
        """每条回复发送完成后，若开启了自动压缩，延迟 2.5 秒检查是否达到压缩阈值。"""
        try:
            if not bool(self._cfg("auto_compress_enabled", False)):
                return
            umo = getattr(event, "unified_msg_origin", "") or ""
            if not umo:
                return
            prev = self._auto_check_tasks.get(umo)
            if prev and not prev.done():
                prev.cancel()
            self._auto_check_tasks[umo] = asyncio.create_task(
                self._delayed_auto_check(umo, delay=2.5)
            )
        except Exception as e:
            logger.debug(f"[对话管理] 自动压缩钩子异常（已忽略）: {e}")

    async def _delayed_auto_check(self, umo: str, delay: float = 2.5) -> None:
        try:
            await asyncio.sleep(delay)
            if not bool(self._cfg("auto_compress_enabled", False)):
                return
            cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
            if not cid:
                return
            await self._try_trigger_auto_compress(cid, umo)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning(f"[对话管理] 自动压缩检查失败: {e}")

    async def _try_trigger_auto_compress(self, cid: str, umo: str, is_retry: bool = False) -> None:
        if not bool(self._cfg("auto_compress_enabled", False)):
            return
        job = self._compress_jobs.get(cid)
        if job and job.get("state") == "running":
            return

        rstate = self._auto_retry_state.get(cid) or {}
        if rstate.get("paused"):
            return
        if not is_retry and rstate.get("next_retry_at", 0) > _now_ts():
            return

        conv = await self._find_conv(cid)
        if not conv:
            return
        hist = self._load_history(conv)
        stats = calc_stats(hist)
        if stats["rounds"] < 4:
            return

        triggered, _ = self._check_thresholds(stats)
        if not triggered:
            return

        primary, primary_id, fallback, fb_id = await self._resolve_compress_providers(umo)
        if primary is None:
            logger.warning(f"[对话管理] 会话 {cid[:8]}… 已达自动压缩阈值（{'、'.join(triggered)}），但未找到可用压缩模型")
            return

        ratio = float(self._cfg("compress_keep_ratio", 0.15))
        ratio = min(0.5, max(0.0, ratio))
        instruction = (self._cfg("compress_instruction", DEFAULT_COMPRESS_INSTRUCTION) or "").strip() or DEFAULT_COMPRESS_INSTRUCTION
        reason_str = "、".join(triggered)
        logger.info(
            f"[对话管理] 会话 {cid[:8]}… 触发自动压缩（原因: {reason_str}，模型: {primary_id}"
            + (f"，备用: {fb_id}" if fb_id else "")
            + "）"
        )
        rounds = split_rounds(hist)
        base_updated = getattr(conv, "updated_at", 0)
        new_job = {
            "state": "running",
            "mode": "auto",
            "trigger_reason": reason_str,
            "cid": cid,
            "started_at": _now_ts(),
            "progress": f"自动压缩准备中（触发: {reason_str}）",
            "total_chunks": 0,
            "done_chunks": 0,
        }
        self._compress_jobs[cid] = new_job
        task = asyncio.create_task(
            self._run_compress(
                cid=cid,
                umo=umo,
                provider=primary,
                provider_id=primary_id,
                fallback_provider=fallback,
                fallback_id=fb_id,
                hist=hist,
                rounds=rounds,
                ratio=ratio,
                instruction=instruction,
                base_updated=base_updated,
                is_auto=True,
            )
        )
        self._compress_tasks[cid] = task

    def _schedule_delayed_retry(self, cid: str, umo: str, delay_sec: int = AUTO_RETRY_INTERVAL_SEC) -> None:
        prev = self._retry_timer_tasks.get(cid)
        if prev and not prev.done():
            prev.cancel()

        async def _timer():
            try:
                await asyncio.sleep(delay_sec)
                await self._try_trigger_auto_compress(cid, umo, is_retry=True)
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning(f"[对话管理] 延迟重试自动压缩异常: {e}")

        self._retry_timer_tasks[cid] = asyncio.create_task(_timer())

    # ---------- API handlers ----------

    async def api_list(self, **kwargs) -> dict:
        try:
            convs = await self.context.conversation_manager.get_conversations()
            items = []
            for c in convs:
                hist = self._load_history(c)
                st = calc_stats(hist)
                items.append({
                    "cid": getattr(c, "cid", ""),
                    "user_id": getattr(c, "user_id", ""),
                    "platform_id": getattr(c, "platform_id", ""),
                    "title": getattr(c, "title", None),
                    "persona_id": getattr(c, "persona_id", None),
                    "created_at": getattr(c, "created_at", 0),
                    "updated_at": getattr(c, "updated_at", 0),
                    "msg_count": st["msg_count"],
                    "rounds": st["rounds"],
                    "total_chars": st["total_chars"],
                    "think_chars": st["think_chars"],
                    "est_tokens": st["est_tokens"],
                    "last_preview": self._preview(hist),
                })
            items.sort(key=lambda x: x["updated_at"], reverse=True)
            return self._ok({"conversations": items})
        except Exception as e:
            return self._err(f"读取会话列表失败: {e}")

    def _preview(self, hist: list[dict], limit: int = 60) -> str:
        for m in reversed(hist):
            t = self._msg_text(m)
            if t:
                return t[:limit]
        return ""

    def _msg_text(self, m: dict) -> str:
        c = m.get("content")
        if isinstance(c, str):
            return c
        parts = []
        if isinstance(c, list):
            for p in c:
                if isinstance(p, dict) and p.get("type") == "text":
                    parts.append(p.get("text") or "")
        return "\n".join(parts)

    async def api_conv(self, cid: str = "", **kwargs) -> dict:
        try:
            conv = await self._find_conv(cid)
            if not conv:
                return self._err("对话不存在")
            hist = self._load_history(conv)
            st = calc_stats(hist)
            return self._ok({
                "cid": cid,
                "user_id": getattr(conv, "user_id", ""),
                "platform_id": getattr(conv, "platform_id", ""),
                "title": getattr(conv, "title", None),
                "persona_id": getattr(conv, "persona_id", None),
                "created_at": getattr(conv, "created_at", 0),
                "updated_at": getattr(conv, "updated_at", 0),
                "token_usage": getattr(conv, "token_usage", 0),
                "history": hist,
                "est_tokens": st["est_tokens"],
                "text_tokens": st["text_tokens"],
                "think_tokens": st["think_tokens"],
                "total_chars": st["total_chars"],
                "text_chars": st["text_chars"],
                "think_chars": st["think_chars"],
                "rounds": st["rounds"],
                "msg_count": st["msg_count"],
            })
        except Exception as e:
            return self._err(f"读取对话失败: {e}")

    async def api_save(self, cid: str = "", **kwargs) -> dict:
        try:
            body = await self._body()
            history = body.get("history")
            ok, msg = validate_history(history)
            if not ok:
                return self._err(msg)
            conv = await self._find_conv(cid)
            if not conv:
                return self._err("对话不存在")
            base_updated = body.get("base_updated_at")
            cur_updated = getattr(conv, "updated_at", 0)
            if base_updated is not None and int(base_updated) != int(cur_updated) and not body.get("force"):
                return self._err(
                    "保存冲突：编辑期间会话有新消息。请刷新后合并再保存。",
                    extra={"conflict": True, "current_updated_at": cur_updated},
                )
            if history != self._load_history(conv):
                snap = await self._snapshot(cid, conv)
            else:
                snap = None
            umo = getattr(conv, "user_id", "")
            await self.context.conversation_manager.update_conversation(
                unified_msg_origin=umo,
                conversation_id=cid,
                history=history,
            )
            st = calc_stats(history)
            logger.info(f"[对话管理] 会话 {cid[:8]}… 已保存 {len(history)} 条消息（快照: {snap}）")
            return self._ok({
                "snapshot": snap,
                "updated_at": getattr(await self._find_conv(cid), "updated_at", 0),
                "msg_count": st["msg_count"],
                "rounds": st["rounds"],
                "total_chars": st["total_chars"],
                "think_chars": st["think_chars"],
                "est_tokens": st["est_tokens"],
            })
        except Exception as e:
            return self._err(f"保存失败: {e}")

    async def api_export(self, cid: str = "", **kwargs) -> dict:
        try:
            conv = await self._find_conv(cid)
            if not conv:
                return self._err("对话不存在")
            hist = self._load_history(conv)
            record = {
                "cid": cid,
                "user_id": getattr(conv, "user_id", ""),
                "platform_id": getattr(conv, "platform_id", ""),
                "title": getattr(conv, "title", None),
                "persona_id": getattr(conv, "persona_id", None),
                "created_at": getattr(conv, "created_at", 0),
                "updated_at": getattr(conv, "updated_at", 0),
                "content": hist,
            }
            iso = datetime.fromtimestamp(_now_ts(), tz=timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%fZ")[:-3]
            line = json.dumps(record, ensure_ascii=False)
            return self._ok({
                "filename": f"astrbot_conversations_{iso}.jsonl",
                "jsonl": line,
                "msg_count": len(hist),
            })
        except Exception as e:
            return self._err(f"导出失败: {e}")

    async def api_import(self, cid: str = "", **kwargs) -> dict:
        try:
            body = await self._body()
            text = body.get("jsonl")
            if not text or not isinstance(text, str):
                return self._err("缺少 jsonl 内容")
            history = None
            file_cid = None
            for line in text.strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                cand = obj.get("content", obj.get("history"))
                if isinstance(cand, list) and cand:
                    ok, msg = validate_history(cand)
                    if ok:
                        history = cand
                        file_cid = obj.get("cid")
                        break
            if history is None:
                return self._err("未在文件中找到有效的对话内容（content/history 数组）")
            conv = await self._find_conv(cid)
            if not conv:
                return self._err("对话不存在")
            snap = await self._snapshot(cid, conv)
            umo = getattr(conv, "user_id", "")
            await self.context.conversation_manager.update_conversation(
                unified_msg_origin=umo,
                conversation_id=cid,
                history=history,
            )
            st = calc_stats(history)
            logger.info(f"[对话管理] 会话 {cid[:8]}… 已导入 {len(history)} 条消息（快照: {snap}）")
            return self._ok({
                "snapshot": snap,
                "msg_count": st["msg_count"],
                "rounds": st["rounds"],
                "total_chars": st["total_chars"],
                "think_chars": st["think_chars"],
                "file_cid": file_cid,
                "est_tokens": st["est_tokens"],
                "updated_at": getattr(await self._find_conv(cid), "updated_at", 0),
            })
        except Exception as e:
            return self._err(f"导入失败: {e}")

    async def api_snapshots(self, cid: str = "", **kwargs) -> dict:
        try:
            snap_dir = self._snap_dir(cid)
            out = []
            for f in sorted(snap_dir.glob("*.json"), reverse=True):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    h = data.get("history") or []
                    out.append({
                        "name": f.stem,
                        "time": data.get("snapshotted_at", 0),
                        "msg_count": len(h),
                        "updated_at": data.get("updated_at", 0),
                    })
                except Exception:
                    continue
            return self._ok({"snapshots": out})
        except Exception as e:
            return self._err(f"读取快照失败: {e}")

    async def api_restore(self, cid: str = "", **kwargs) -> dict:
        try:
            body = await self._body()
            name = body.get("name")
            if not name:
                return self._err("缺少快照名")
            path = self._snap_dir(cid) / f"{re.sub(r'[^A-Za-z0-9_-]', '', name)}.json"
            if not path.exists():
                return self._err("快照不存在")
            data = json.loads(path.read_text(encoding="utf-8"))
            history = data.get("history") or []
            ok, msg = validate_history(history)
            if not ok:
                return self._err(f"快照数据异常: {msg}")
            conv = await self._find_conv(cid)
            if not conv:
                return self._err("对话不存在")
            snap = await self._snapshot(cid, conv)
            await self.context.conversation_manager.update_conversation(
                unified_msg_origin=getattr(conv, "user_id", ""),
                conversation_id=cid,
                history=history,
            )
            logger.info(f"[对话管理] 会话 {cid[:8]}… 已恢复快照 {name}（保存前快照: {snap}）")
            return self._ok({"restored": name, "snapshot": snap, "msg_count": len(history)})
        except Exception as e:
            return self._err(f"恢复失败: {e}")

    async def api_providers(self, **kwargs) -> dict:
        try:
            pm = getattr(self.context, "provider_manager", None)
            insts = getattr(pm, "provider_insts", []) or []
            items = []
            for p in insts:
                cfg = getattr(p, "provider_config", {}) or {}
                model = cfg.get("model")
                if isinstance(model, list):
                    model = ", ".join(str(x) for x in model)
                items.append({
                    "id": str(cfg.get("id", "")),
                    "model": str(model or ""),
                    "enable": bool(cfg.get("enable", True)),
                    "max_context_tokens": int(cfg.get("max_context_tokens") or 0),
                })
            items = [x for x in items if x["id"] and x["enable"]]
            return self._ok({"providers": items})
        except Exception as e:
            return self._err(f"读取模型列表失败: {e}")

    async def api_status(self, cid: str = "", **kwargs) -> dict:
        try:
            conv = await self._find_conv(cid)
            if not conv:
                return self._err("对话不存在")
            hist = self._load_history(conv)
            st = calc_stats(hist)
            triggered, warnings = self._check_thresholds(st)
            return self._ok({
                **st,
                "threshold": int(self._cfg("token_threshold", 12800) or 0),
                "rounds_threshold": int(self._cfg("auto_compress_rounds", 0) or 0),
                "chars_threshold": int(self._cfg("auto_compress_chars", 0) or 0),
                "auto_compress_enabled": bool(self._cfg("auto_compress_enabled", False)),
                "triggered_reasons": triggered,
                "warn_reasons": warnings,
                "will_trigger": len(triggered) > 0,
            })
        except Exception as e:
            return self._err(f"状态估算失败: {e}")

    async def api_compress_reset(self, cid: str = "", **kwargs) -> dict:
        """清除某个会话的自动压缩失败暂停状态。"""
        self._auto_retry_state.pop(cid, None)
        t = self._retry_timer_tasks.pop(cid, None)
        if t and not t.done():
            t.cancel()
        if cid in self._compress_jobs and self._compress_jobs[cid].get("state") in ("error", "retry_wait"):
            self._compress_jobs.pop(cid, None)
        return self._ok({"reset": True})

    async def api_compress(self, **kwargs) -> dict:
        try:
            body = await self._body()
            cid = body.get("cid")
            provider_id = (body.get("provider_id") or "").strip()
            fallback_id = (body.get("fallback_provider_id") or "").strip()
            if not cid:
                return self._err("缺少 cid")
            conv = await self._find_conv(cid)
            if not conv:
                return self._err("对话不存在")
            umo = getattr(conv, "user_id", "")

            primary, resolved_primary_id, default_fb, default_fb_id = await self._resolve_compress_providers(
                umo, override_primary_id=provider_id
            )
            if primary is None:
                return self._err(f"模型 {provider_id or '默认聊天模型'} 不存在或未启用")

            if fallback_id:
                fb_provider = self.context.get_provider_by_id(fallback_id)
                fb_id = fallback_id if fb_provider else ""
            else:
                fb_provider, fb_id = default_fb, default_fb_id
            if fb_provider is primary or (fb_id and fb_id == resolved_primary_id):
                fb_provider, fb_id = None, ""

            hist = self._load_history(conv)
            rounds = split_rounds(hist)
            if len(rounds) < 4:
                return self._err("对话轮次太少（不足 4 轮），无需压缩")
            if cid in self._compress_jobs and self._compress_jobs[cid].get("state") == "running":
                return self._err("该会话已有压缩任务在后台执行")

            # 手动触发压缩时清除之前的自动重试/暂停状态
            self._auto_retry_state.pop(cid, None)
            prev_timer = self._retry_timer_tasks.pop(cid, None)
            if prev_timer and not prev_timer.done():
                prev_timer.cancel()

            ratio = float(body.get("keep_ratio", self._cfg("compress_keep_ratio", 0.15)))
            ratio = min(0.5, max(0.0, ratio))
            instruction = (
                body.get("instruction")
                or self._cfg("compress_instruction", DEFAULT_COMPRESS_INSTRUCTION)
                or DEFAULT_COMPRESS_INSTRUCTION
            ).strip() or DEFAULT_COMPRESS_INSTRUCTION

            job = {
                "state": "running",
                "mode": "manual",
                "cid": cid,
                "started_at": _now_ts(),
                "progress": f"准备中（使用模型: {resolved_primary_id}）",
                "total_chunks": 0,
                "done_chunks": 0,
            }
            self._compress_jobs[cid] = job
            base_updated = getattr(conv, "updated_at", 0)
            task = asyncio.create_task(
                self._run_compress(
                    cid=cid,
                    umo=umo,
                    provider=primary,
                    provider_id=resolved_primary_id,
                    fallback_provider=fb_provider,
                    fallback_id=fb_id,
                    hist=hist,
                    rounds=rounds,
                    ratio=ratio,
                    instruction=instruction,
                    base_updated=base_updated,
                    is_auto=False,
                )
            )
            self._compress_tasks[cid] = task
            return self._ok({
                "started": True,
                "provider_id": resolved_primary_id,
                "fallback_id": fb_id,
                "rounds": len(rounds),
                "msg_count": len(hist),
            })
        except Exception as e:
            return self._err(f"启动压缩失败: {e}")

    async def _call_llm_with_retry_and_fallback(
        self,
        job: dict,
        prompt_text: str,
        stage_label: str,
        active_provider: Any,
        active_id: str,
        fallback_provider: Any,
        fallback_id: str,
        contexts: list[dict] | None = None,
        system_prompt: str | None = None,
    ) -> tuple[str, Any, str]:
        """调用大模型生成摘要（优先采用仿正常聊天的多轮 contexts 结构，降低网关误拦率）：
        1. 首选模型失败时，等待 3 秒当场重试 1 次；
        2. 若仍失败且配置了备用兜底模型，自动切换到备用模型（同样带当场重试）；
        返回 (summary_text, current_active_provider, current_active_id)。
        """
        async def _try_once(prov, pid: str) -> str:
            kwargs: dict[str, Any] = {
                "prompt": prompt_text,
                "request_max_retries": 1,
            }
            if contexts:
                kwargs["contexts"] = contexts
            if system_prompt:
                kwargs["system_prompt"] = system_prompt
            resp = await prov.text_chat(**kwargs)
            text = getattr(resp, "completion_text", None) or getattr(resp, "result_chain", None)
            if isinstance(text, str) and text.strip():
                cleaned = text.strip()
                _check_not_truncated(resp, cleaned, pid)
                return cleaned
            raise RuntimeError(f"模型 {pid} 返回内容为空（可能触发了内容安全过滤）")

        # 第 1 步：当前模型首次尝试 + 3秒后当场重试
        first_err: Exception | None = None
        try:
            text = await _try_once(active_provider, active_id)
            return text, active_provider, active_id
        except asyncio.CancelledError:
            raise
        except Exception as e1:
            first_err = e1
            logger.warning(f"[对话管理] {stage_label} 模型 {active_id} 首次调用失败: {e1}，3 秒后当场重试…")
            job["progress"] = f"{stage_label}（{active_id} 响应异常，3 秒后当场重试…）"
            await asyncio.sleep(3)
            try:
                text = await _try_once(active_provider, active_id)
                return text, active_provider, active_id
            except asyncio.CancelledError:
                raise
            except Exception as e2:
                first_err = e2

        # 第 2 步：自动切换到备用兜底模型
        if fallback_provider is not None and fallback_provider is not active_provider:
            logger.warning(
                f"[对话管理] {stage_label} 首选模型 {active_id} 重试仍失败({first_err})，"
                f"正在自动切换备用模型 {fallback_id}…"
            )
            job["progress"] = f"{stage_label}（首选模型失败，已切换备用模型 {fallback_id} 继续…）"
            try:
                text = await _try_once(fallback_provider, fallback_id)
                return text, fallback_provider, fallback_id
            except asyncio.CancelledError:
                raise
            except Exception as fb_err1:
                logger.warning(f"[对话管理] 备用模型 {fallback_id} 首次调用失败: {fb_err1}，3 秒后重试…")
                await asyncio.sleep(3)
                try:
                    text = await _try_once(fallback_provider, fallback_id)
                    return text, fallback_provider, fallback_id
                except asyncio.CancelledError:
                    raise
                except Exception as fb_err2:
                    raise RuntimeError(
                        f"首选模型({active_id})失败: {first_err}；备用模型({fallback_id})也失败: {fb_err2}"
                    )

        raise RuntimeError(f"模型({active_id})请求失败: {first_err}")

    async def _run_compress(
        self,
        cid: str,
        umo: str,
        provider: Any,
        provider_id: str,
        fallback_provider: Any,
        fallback_id: str,
        hist: list[dict],
        rounds: list[list[dict]],
        ratio: float,
        instruction: str,
        base_updated: int,
        is_auto: bool = False,
    ) -> None:
        job = self._compress_jobs[cid]
        active_prov = provider
        active_id = provider_id
        prefix_tag = "自动压缩" if is_auto else "手动压缩"
        try:
            keep = max(1, int(len(rounds) * ratio))
            # 防止压缩保留的轮数本身仍高于自动压缩轮数上限，导致刚压完发一句话又连环触发二次压缩
            auto_r_lim = int(self._cfg("auto_compress_rounds", 0) or 0)
            if auto_r_lim > 0 and keep >= auto_r_lim:
                keep = max(1, min(keep, int(auto_r_lim * 0.6)))
            old_rounds = rounds[:-keep]
            recent_rounds = rounds[-keep:]

            # 若开头已有上一次压缩的长期记忆，独立剥离出来并在合并阶段强制增量继承，绝不丢弃早期记忆
            prev_summary, new_old_rounds = _extract_prev_summary(old_rounds)
            old_msgs = sanitize_for_summary(flatten_rounds(new_old_rounds))
            if not old_msgs and not prev_summary:
                raise RuntimeError("旧对话中没有可压缩的文本内容")

            chunks = pack_chunks(old_msgs) if old_msgs else []
            job["total_chunks"] = len(chunks)
            summaries: list[str] = []

            for i, chunk in enumerate(chunks, 1):
                job["progress"] = f"{prefix_tag}：分块摘要 {i}/{len(chunks)}（模型: {active_id}）"
                job["done_chunks"] = i - 1
                ctx_msgs = _normalize_contexts_for_chat(chunk)
                text, active_prov, active_id = await self._call_llm_with_retry_and_fallback(
                    job=job,
                    prompt_text="请按系统要求，将以上历史对话总结为一份客观、连贯、完整的中文摘要，直接输出正文（务必写完整，不要中途截断）。",
                    stage_label=f"分块 {i}/{len(chunks)}",
                    active_provider=active_prov,
                    active_id=active_id,
                    fallback_provider=fallback_provider,
                    fallback_id=fallback_id,
                    contexts=ctx_msgs,
                    system_prompt=instruction,
                )
                summaries.append(text)

            # 组合「既有长期记忆（若有）」与「新产生的分块摘要」
            all_parts: list[tuple[str, str]] = []
            if prev_summary:
                all_parts.append(("此前已存档的长期历史记忆（必须完整继承，严禁丢弃早期核心设定与重要事件）", prev_summary))
            for idx_s, s in enumerate(summaries, 1):
                all_parts.append((f"新增的第 {idx_s} 阶段对话摘要", s))

            if len(all_parts) > 1:
                job["progress"] = f"{prefix_tag}：增量融合 {len(all_parts)} 份历史摘要（模型: {active_id}）"
                merge_ctx: list[dict] = []
                for label, body in all_parts:
                    merge_ctx.append({"role": "user", "content": f"【{label}】\n{body}"})
                    merge_ctx.append({"role": "assistant", "content": f"已完整记录{label}。"})
                merge_sys = (
                    f"{MERGE_INSTRUCTION}\n"
                    f"同时请严格遵循用户的核心摘要要求：\n{instruction}\n"
                    "【铁律】必须完整保留“此前已存档的长期历史记忆”中的关键人物、称呼、规矩设定与重要早期事件，"
                    "并将后续新增阶段摘要按时间顺序自然衔接在后面，绝不可丢弃早期记忆，也不可中途截断。"
                )
                summary, active_prov, active_id = await self._call_llm_with_retry_and_fallback(
                    job=job,
                    prompt_text="请将以上既有长期记忆与后续新增摘要合并为一份完整、连贯的中文长期记忆，按时间顺序整合，直接输出正文。",
                    stage_label="增量融合摘要",
                    active_provider=active_prov,
                    active_id=active_id,
                    fallback_provider=fallback_provider,
                    fallback_id=fallback_id,
                    contexts=merge_ctx,
                    system_prompt=merge_sys,
                )
            else:
                summary = all_parts[0][1]

            # 写回前乐观锁校验：绝不覆盖压缩期间产生的新消息
            conv_now = await self._find_conv(cid)
            if conv_now is None or int(getattr(conv_now, "updated_at", 0)) != int(base_updated):
                if is_auto:
                    logger.info(f"[对话管理] 会话 {cid[:8]}… 自动压缩期间有新聊天消息进入，安全礼让中止（下一轮再压）")
                    job.update({
                        "state": "idle",
                        "progress": "已礼让新消息",
                    })
                    return
                raise RuntimeError("压缩期间会话有新消息进入，已安全中止（未改动任何记录），请刷新后重试")

            new_history = [
                {"role": "user", "content": f"Our previous history conversation summary: {summary}"},
                {"role": "assistant", "content": ACK_TEXT},
            ] + flatten_rounds(recent_rounds)

            conv_latest = await self._find_conv(cid)
            snap = await self._snapshot(cid, conv_latest)
            await self.context.conversation_manager.update_conversation(
                unified_msg_origin=getattr(conv_latest, "user_id", ""),
                conversation_id=cid,
                history=new_history,
            )

            # 压缩成功：清空失败重试计数
            self._auto_retry_state.pop(cid, None)
            before_st = calc_stats(hist)
            after_st = calc_stats(new_history)
            job.update({
                "state": "done",
                "mode": "auto" if is_auto else "manual",
                "used_provider": active_id,
                "progress": "完成",
                "snapshot": snap,
                "summary_preview": summary[:300],
                "before": {
                    "msg_count": before_st["msg_count"],
                    "rounds": before_st["rounds"],
                    "total_chars": before_st["total_chars"],
                    "est_tokens": before_st["est_tokens"],
                },
                "after": {
                    "msg_count": after_st["msg_count"],
                    "rounds": after_st["rounds"],
                    "total_chars": after_st["total_chars"],
                    "est_tokens": after_st["est_tokens"],
                },
            })
            logger.info(
                f"[对话管理] 会话 {cid[:8]}… {prefix_tag}成功（模型: {active_id}）: "
                f"{before_st['msg_count']} → {after_st['msg_count']} 条, "
                f"{before_st['total_chars']} → {after_st['total_chars']} 字, "
                f"~{before_st['est_tokens']} → ~{after_st['est_tokens']} tok（快照: {snap}）"
            )
        except asyncio.CancelledError:
            job.update({"state": "error", "progress": "已取消", "error": "压缩任务被取消（原记录未改动）"})
        except Exception as e:
            err_msg = str(e)
            logger.error(f"[对话管理] 会话 {cid[:8]}… {prefix_tag}失败（原聊天记录完好无损）: {err_msg}")
            if is_auto:
                rstate = self._auto_retry_state.setdefault(
                    cid, {"retry_count": 0, "paused": False, "last_error": "", "next_retry_at": 0}
                )
                rstate["retry_count"] = int(rstate.get("retry_count", 0)) + 1
                rstate["last_error"] = err_msg
                cnt = rstate["retry_count"]
                if cnt < AUTO_MAX_RETRIES:
                    next_ts = _now_ts() + AUTO_RETRY_INTERVAL_SEC
                    rstate["next_retry_at"] = next_ts
                    job.update({
                        "state": "retry_wait",
                        "mode": "auto",
                        "retry_count": cnt,
                        "max_retries": AUTO_MAX_RETRIES,
                        "next_retry_at": next_ts,
                        "progress": f"自动压缩第 {cnt}/{AUTO_MAX_RETRIES} 次失败，5 分钟后自动重试",
                        "error": (
                            f"自动压缩第 {cnt}/{AUTO_MAX_RETRIES} 次失败（{err_msg}）。"
                            "原聊天记录完好无损，系统将在 5 分钟后自动重试。"
                        ),
                    })
                    self._schedule_delayed_retry(cid, umo, AUTO_RETRY_INTERVAL_SEC)
                else:
                    rstate["paused"] = True
                    rstate["next_retry_at"] = 0
                    job.update({
                        "state": "error",
                        "mode": "auto",
                        "paused": True,
                        "retry_count": cnt,
                        "progress": "自动压缩连续失败 3 次已暂停",
                        "error": (
                            f"自动压缩连续 {AUTO_MAX_RETRIES} 次失败，已自动暂停（原因：{err_msg}）。"
                            "原聊天记录完好无损！建议在左下角「⚙ 设置」中更换无审核的压缩模型或设置备用模型。"
                        ),
                    })
            else:
                job.update({
                    "state": "error",
                    "mode": "manual",
                    "progress": "失败",
                    "error": f"{err_msg}（原聊天记录完好无损，未做任何删改）",
                })

    async def api_compress_status(self, cid: str = "", **kwargs) -> dict:
        job = self._compress_jobs.get(cid) or {"state": "idle"}
        rstate = self._auto_retry_state.get(cid) or {}
        out = {k: v for k, v in job.items() if k != "cid"}
        if rstate.get("paused"):
            out["paused"] = True
            out.setdefault("error", rstate.get("last_error", "自动压缩已暂停"))
        return self._ok({"job": out})

    async def api_config_get(self, **kwargs) -> dict:
        try:
            return self._ok({
                "snapshot_keep": int(self._cfg("snapshot_keep", 20)),
                "poll_interval": int(self._cfg("poll_interval", 5)),
                "token_threshold": int(self._cfg("token_threshold", 12800)),
                "auto_compress_enabled": bool(self._cfg("auto_compress_enabled", False)),
                "auto_compress_rounds": int(self._cfg("auto_compress_rounds", 0)),
                "auto_compress_chars": int(self._cfg("auto_compress_chars", 0)),
                "auto_compress_provider": str(self._cfg("auto_compress_provider", "") or ""),
                "auto_compress_fallback_provider": str(self._cfg("auto_compress_fallback_provider", "") or ""),
                "compress_keep_ratio": float(self._cfg("compress_keep_ratio", 0.15)),
                "compress_instruction": str(self._cfg("compress_instruction", "") or ""),
                "default_compress_instruction": DEFAULT_COMPRESS_INSTRUCTION,
            })
        except Exception as e:
            return self._err(f"读取配置失败: {e}")

    async def api_config_set(self, **kwargs) -> dict:
        try:
            body = await self._body()
            allowed = {
                "snapshot_keep": int,
                "poll_interval": int,
                "token_threshold": int,
                "auto_compress_enabled": bool,
                "auto_compress_rounds": int,
                "auto_compress_chars": int,
                "auto_compress_provider": str,
                "auto_compress_fallback_provider": str,
                "compress_keep_ratio": float,
                "compress_instruction": str,
            }
            changed = {}
            for k, cast in allowed.items():
                if k in body:
                    try:
                        val = body[k]
                        if cast is bool:
                            self.config[k] = bool(val)
                        else:
                            self.config[k] = cast(val)
                        changed[k] = self.config[k]
                    except Exception:
                        return self._err(f"配置项 {k} 类型错误")
            # 修改设置后重置所有会话的自动压缩失败暂停状态，让新配置立即生效
            self._auto_retry_state.clear()
            for cid_k, j in list(self._compress_jobs.items()):
                if j.get("state") in ("error", "retry_wait") and j.get("mode") == "auto":
                    self._compress_jobs.pop(cid_k, None)
            try:
                save = getattr(self.config, "save_config", None)
                if callable(save):
                    save()
            except Exception as e:
                logger.warning(f"[对话管理] 配置保存持久化失败（内存中已生效）: {e}")
            return self._ok({"changed": changed})
        except Exception as e:
            return self._err(f"保存配置失败: {e}")
