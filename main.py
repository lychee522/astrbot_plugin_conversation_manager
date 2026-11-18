"""AstrBot 对话管理插件（astrbot_plugin_conversation_manager）。

在 Dashboard 左侧提供「对话管理」页面：可视化浏览 / 编辑 / 删除 / 插入
任何平台会话的消息，支持自动快照撤销、JSONL 导入导出、手动分块压缩摘要。

设计约束（全部对照 AstrBot 4.29.3 源码核对）：
- 数据层只走官方 conversation_manager，不直接访问数据库，跨版本安全。
- Web API 走 context.register_web_api，最终挂载在 /api/plug/<route>，
  由 dashboard 统一鉴权；handler 为 Quart view（**kwargs），query/body
  经全局 quart.request 读取。
- 响应信封统一 {"success": True/False, "data"/"message"}：非标准信封会被
  父级 SPA 原样透传，由前端自行解包（与官方 message_recorder/cost_control 一致）。
- 任何 handler 异常都在内部消化，绝不影响 bot 主流程。
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
from astrbot.api.star import Context, Star, StarTools, register

PLUGIN_NAME = "astrbot_plugin_conversation_manager"
MAX_TEXT_PER_MSG_FOR_SUMMARY = 2400
SUMMARY_CHUNK_CHARS = 24000

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


def _now_ts() -> int:
    return int(time.time())


def est_tokens(history: list[dict]) -> int:
    """粗略估算上下文 token（与 AstrBot EstimateTokenCounter 同量级）。"""
    chars = 0
    for m in history:
        c = m.get("content")
        if isinstance(c, str):
            chars += len(c)
        elif isinstance(c, list):
            for p in c:
                if isinstance(p, dict):
                    chars += len(str(p.get("text") or p.get("think") or ""))
        chars += 8
    return int(chars / 4.5) + 4 * len(history)


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


def sanitize_for_summary(history: list[dict], max_chars: int = MAX_TEXT_PER_MSG_FOR_SUMMARY) -> list[dict]:
    """抽取纯文本用于摘要请求（跳过图片/工具等非文本内容）。"""
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
        body = "\n".join(texts).strip()
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
    "对话可视化管理：浏览/编辑/删除/插入消息、自动快照撤销、JSONL 导入导出、手动分块压缩摘要",
    "1.0.1",
)
class ConversationManagerPlugin(Star):
    def __init__(self, context: Context, config=None) -> None:
        super().__init__(context)
        self.context = context
        self.config = config if config is not None else {}
        self._compress_jobs: dict[str, dict] = {}
        self._compress_tasks: dict[str, asyncio.Task] = {}

    # ---------- helpers ----------

    def _cfg(self, key: str, default: Any) -> Any:
        try:
            v = self.config.get(key, default)
        except Exception:
            v = default
        return v

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
            (f"{prefix}/providers", self.api_providers, ["GET"], "可用模型列表"),
            (f"{prefix}/status/<cid>", self.api_status, ["GET"], "上下文压力估算"),
            (f"{prefix}/config", self.api_config_get, ["GET"], "读取面板配置"),
            (f"{prefix}/config", self.api_config_set, ["POST"], "保存面板配置"),
        ]
        for route, handler, methods, desc in routes:
            reg(route, handler, methods, desc)
        logger.info("[对话管理] Web API 注册完成（/api/plug/%s/...）" % PLUGIN_NAME)

    async def terminate(self):
        for t in self._compress_tasks.values():
            t.cancel()

    # ---------- API handlers ----------

    async def api_list(self, **kwargs) -> dict:
        try:
            convs = await self.context.conversation_manager.get_conversations()
            items = []
            for c in convs:
                hist = self._load_history(c)
                items.append({
                    "cid": getattr(c, "cid", ""),
                    "user_id": getattr(c, "user_id", ""),
                    "platform_id": getattr(c, "platform_id", ""),
                    "title": getattr(c, "title", None),
                    "persona_id": getattr(c, "persona_id", None),
                    "created_at": getattr(c, "created_at", 0),
                    "updated_at": getattr(c, "updated_at", 0),
                    "msg_count": len(hist),
                    "est_tokens": est_tokens(hist),
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
                "est_tokens": est_tokens(hist),
                "rounds": len(split_rounds(hist)),
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
            new_hist = self._load_history(await self._find_conv(cid))
            logger.info(f"[对话管理] 会话 {cid[:8]}… 已保存 {len(history)} 条消息（快照: {snap}）")
            return self._ok({
                "snapshot": snap,
                "updated_at": getattr(await self._find_conv(cid), "updated_at", 0),
                "msg_count": len(history),
                "est_tokens": est_tokens(history),
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
            logger.info(f"[对话管理] 会话 {cid[:8]}… 已导入 {len(history)} 条消息（快照: {snap}）")
            return self._ok({
                "snapshot": snap,
                "msg_count": len(history),
                "file_cid": file_cid,
                "est_tokens": est_tokens(history),
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
            est = est_tokens(hist)
            threshold = int(self._cfg("token_threshold", 12800))
            recent_lens = [len(self._msg_text(m)) for m in hist[-10:]]
            avg = sum(recent_lens) / max(1, len(recent_lens))
            chars_left = max(0, (threshold - est) * 4.5)
            msgs_left = max(0, int(chars_left / max(1, avg)))
            return self._ok({
                "est_tokens": est,
                "threshold": threshold,
                "msg_count": len(hist),
                "rounds": len(split_rounds(hist)),
                "est_msgs_left": msgs_left,
                "will_trigger": est >= threshold,
            })
        except Exception as e:
            return self._err(f"状态估算失败: {e}")

    async def api_compress(self, **kwargs) -> dict:
        try:
            body = await self._body()
            cid = body.get("cid")
            provider_id = body.get("provider_id")
            if not cid or not provider_id:
                return self._err("缺少 cid 或 provider_id")
            conv = await self._find_conv(cid)
            if not conv:
                return self._err("对话不存在")
            provider = self.context.get_provider_by_id(provider_id)
            if provider is None:
                return self._err(f"模型 {provider_id} 不存在或未启用")
            hist = self._load_history(conv)
            rounds = split_rounds(hist)
            if len(rounds) < 4:
                return self._err("对话轮次太少，无需压缩")
            if cid in self._compress_jobs and self._compress_jobs[cid].get("state") == "running":
                return self._err("该会话已有压缩任务在执行")
            ratio = float(body.get("keep_ratio", self._cfg("compress_keep_ratio", 0.15)))
            ratio = min(0.3, max(0.0, ratio))
            instruction = (body.get("instruction") or self._cfg(
                "compress_instruction", DEFAULT_COMPRESS_INSTRUCTION)) or DEFAULT_COMPRESS_INSTRUCTION
            job = {
                "state": "running",
                "cid": cid,
                "started_at": _now_ts(),
                "progress": "准备中",
                "total_chunks": 0,
                "done_chunks": 0,
            }
            self._compress_jobs[cid] = job
            base_updated = getattr(conv, "updated_at", 0)
            task = asyncio.create_task(
                self._run_compress(cid, provider, provider_id, hist, rounds, ratio, instruction, base_updated)
            )
            self._compress_tasks[cid] = task
            return self._ok({"started": True, "rounds": len(rounds), "msg_count": len(hist)})
        except Exception as e:
            return self._err(f"启动压缩失败: {e}")

    async def _run_compress(self, cid: str, provider, provider_id: str,
                            hist: list[dict], rounds: list[list[dict]],
                            ratio: float, instruction: str, base_updated: int) -> None:
        job = self._compress_jobs[cid]
        try:
            keep = max(1, int(len(rounds) * ratio))
            old_rounds = rounds[:-keep]
            recent_rounds = rounds[-keep:]
            old_msgs = sanitize_for_summary(flatten_rounds(old_rounds))
            chunks = pack_chunks(old_msgs)
            job["total_chunks"] = len(chunks)
            summaries: list[str] = []
            for i, chunk in enumerate(chunks, 1):
                job["progress"] = f"分块摘要 {i}/{len(chunks)}"
                job["done_chunks"] = i - 1
                convo_text = "\n\n".join(
                    f"【{'用户' if m['role'] == 'user' else '助手'}】{m['content']}" for m in chunk
                )
                resp = await provider.text_chat(
                    prompt=f"{instruction}\n\n===对话内容===\n{convo_text}",
                    request_max_retries=1,
                )
                text = getattr(resp, "completion_text", None) or getattr(resp, "result_chain", None)
                if isinstance(text, str) and text.strip():
                    summaries.append(text.strip())
                else:
                    raise RuntimeError(f"分块 {i} 摘要返回为空")
            if len(summaries) > 1:
                job["progress"] = "合并分块摘要"
                resp = await provider.text_chat(
                    prompt=f"{MERGE_INSTRUCTION}\n\n" + "\n\n".join(
                        f"===摘要 {i + 1}===\n{s}" for i, s in enumerate(summaries)
                    ),
                    request_max_retries=1,
                )
                summary = getattr(resp, "completion_text", "")
                if not str(summary).strip():
                    raise RuntimeError("合并摘要返回为空")
            else:
                summary = summaries[0]

            # 写回前乐观校验
            conv_now = await self._find_conv(cid)
            if conv_now is None or int(getattr(conv_now, "updated_at", 0)) != int(base_updated):
                raise RuntimeError("压缩期间会话有新消息，已安全中止（未做任何修改），请重新执行")

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
            job.update({
                "state": "done",
                "progress": "完成",
                "snapshot": snap,
                "summary_preview": summary[:300],
                "before": {"msg_count": len(hist), "est_tokens": est_tokens(hist)},
                "after": {"msg_count": len(new_history), "est_tokens": est_tokens(new_history)},
            })
            logger.info(f"[对话管理] 会话 {cid[:8]}… 压缩完成: {len(hist)} → {len(new_history)} 条（快照: {snap}）")
        except asyncio.CancelledError:
            job.update({"state": "error", "progress": "已取消", "error": "任务被取消"})
        except Exception as e:
            logger.error(f"[对话管理] 压缩失败: {e}")
            job.update({"state": "error", "progress": "失败", "error": str(e)})

    async def api_compress_status(self, cid: str = "", **kwargs) -> dict:
        job = self._compress_jobs.get(cid) or {"state": "idle"}
        return self._ok({"job": {k: v for k, v in job.items() if k != "cid"}})

    async def api_config_get(self, **kwargs) -> dict:
        try:
            return self._ok({
                "snapshot_keep": int(self._cfg("snapshot_keep", 20)),
                "poll_interval": int(self._cfg("poll_interval", 5)),
                "token_threshold": int(self._cfg("token_threshold", 12800)),
                "compress_keep_ratio": float(self._cfg("compress_keep_ratio", 0.15)),
                "compress_instruction": self._cfg("compress_instruction", DEFAULT_COMPRESS_INSTRUCTION),
            })
        except Exception as e:
            return self._err(f"读取配置失败: {e}")

    async def api_config_set(self, **kwargs) -> dict:
        try:
            body = await self._body()
            allowed = {
                "snapshot_keep": int, "poll_interval": int, "token_threshold": int,
                "compress_keep_ratio": float, "compress_instruction": str,
            }
            changed = {}
            for k, cast in allowed.items():
                if k in body:
                    try:
                        self.config[k] = cast(body[k])
                        changed[k] = self.config[k]
                    except Exception:
                        return self._err(f"配置项 {k} 类型错误")
            try:
                save = getattr(self.config, "save_config", None)
                if callable(save):
                    save()
            except Exception as e:
                logger.warning(f"[对话管理] 配置保存持久化失败（内存中已生效）: {e}")
            return self._ok({"changed": changed})
        except Exception as e:
            return self._err(f"保存配置失败: {e}")
