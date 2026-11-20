/* astrbot_plugin_conversation_manager · 前端逻辑（原生 JS，无构建依赖） */
/* global AstrBotPluginPage */
(function () {
  "use strict";

  const PN = "/astrbot_plugin_conversation_manager";
  const PAGE_SIZE = 80;
  const CJK_RE = /[\u4e00-\u9fff\u3400-\u4dbf\u3000-\u303f\uff00-\uffef]/g;

  const DEFAULT_INSTR_FALLBACK =
    "你是对话历史压缩器。请把下面的多轮对话压缩成一份第三人称中文摘要，用于替换原始历史作为长期记忆。" +
    "必须保留：人物关系与固定称呼、关键事件与大致时间线、双方做出的约定与承诺、" +
    "重要的情绪转折、以及尚未回收的伏笔与悬而未决的话题。" +
    "不要发表评论，不要使用 Markdown 标题符号，直接输出摘要正文。";

  const state = {
    convs: [],
    current: null,
    history: [],
    baseUpdatedAt: 0,
    dirty: false,
    windowStart: 0,
    selectedIdx: null,
    settings: {
      snapshot_keep: 20,
      poll_interval: 5,
      token_threshold: 12800,
      auto_compress_enabled: false,
      auto_compress_rounds: 0,
      auto_compress_chars: 0,
      auto_compress_provider: "",
      auto_compress_fallback_provider: "",
      compress_keep_ratio: 0.15,
      compress_instruction: "",
      default_compress_instruction: DEFAULT_INSTR_FALLBACK,
    },
    providers: [],
    pollTimer: null,
    jobTimer: null,
  };

  const $ = (sel) => document.querySelector(sel);

  // ---------- API ----------
  const AP = window.AstrBotPluginPage;
  function unwrap(r) {
    if (r && r.success === false) {
      const e = new Error(r.message || "请求失败");
      e.extra = r;
      throw e;
    }
    return r && r.data !== undefined ? r.data : r;
  }
  function withTimeout(p, ms, tag) {
    return Promise.race([
      p,
      new Promise((_, rej) => setTimeout(() => rej(new Error(tag + " 超时(" + ms + "ms)")), ms)),
    ]);
  }
  async function viaBridge(ep, params, body, isPost) {
    if (!AP || typeof AP.apiGet !== "function") throw new Error("bridge 不可用（SDK 未加载）");
    const sub = String(ep).replace(/^\/+/, "");
    if (isPost) return await withTimeout(AP.apiPost(sub, body || {}), 30000, "bridge POST");
    return await withTimeout(AP.apiGet(sub, params || {}), 15000, "bridge GET");
  }
  async function viaFetch(ep, params, body, isPost) {
    let resp;
    if (isPost) {
      resp = await fetch("/api/plug" + PN + ep, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        credentials: "same-origin",
        body: JSON.stringify(body || {}),
      });
    } else {
      const u = new URL("/api/plug" + PN + ep, window.location.origin);
      if (params) for (const [k, v] of Object.entries(params)) u.searchParams.set(k, v);
      resp = await fetch(u.toString(), { headers: { Accept: "application/json" }, credentials: "same-origin" });
    }
    let r = null;
    try { r = await resp.json(); } catch (e) {
      throw new Error("HTTP " + resp.status + "（非 JSON 响应）");
    }
    if (!resp.ok && !(r && r.success !== undefined)) {
      throw new Error("HTTP " + resp.status + " " + ((r && r.message) || ""));
    }
    return unwrap(r);
  }
  async function apiCall(ep, params, body, isPost) {
    let bridgeErr = null;
    try {
      return unwrap(await viaBridge(ep, params, body, isPost));
    } catch (e) {
      bridgeErr = e;
      if (e && e.extra) throw e;
    }
    try {
      return unwrap(await viaFetch(ep, params, body, isPost));
    } catch (e2) {
      throw new Error("bridge: " + (bridgeErr ? bridgeErr.message : "?") + " ｜ fetch兜底: " + (e2 ? e2.message : "?"));
    }
  }
  function apiGet(ep, params) {
    return apiCall(ep, params, null, false);
  }
  function apiPost(ep, body) {
    return apiCall(ep, null, body, true);
  }

  // ---------- utils ----------
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
  function fmtTs(ts) {
    if (!ts) return "-";
    const d = new Date(ts * 1000);
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }
  function fmtWan(n) {
    const num = Number(n) || 0;
    if (num >= 10000) return (num / 10000).toFixed(1).replace(/\.0$/, "") + "万";
    return String(num);
  }
  function strTokens(s) {
    if (!s) return 0;
    const str = String(s);
    const m = str.match(CJK_RE);
    const cjk = m ? m.length : 0;
    const other = str.length - cjk;
    return cjk * 0.65 + other * 0.25;
  }
  function calcStats(hist) {
    let textChars = 0;
    let thinkChars = 0;
    let textTok = 0;
    let thinkTok = 0;
    let rounds = 0;
    let inRound = false;

    for (const m of hist) {
      if (!m) continue;
      if (m.role === "user" && inRound) {
        rounds++;
      }
      inRound = true;
      const c = m.content;
      if (typeof c === "string") {
        textChars += c.length;
        textTok += strTokens(c);
      } else if (Array.isArray(c)) {
        for (const p of c) {
          if (!p || typeof p !== "object") continue;
          if (p.type === "think") {
            const th = String(p.think || "");
            thinkChars += th.length;
            thinkTok += strTokens(th);
          } else {
            const tx = String(p.text || "");
            textChars += tx.length;
            textTok += strTokens(tx);
          }
        }
      }
    }
    if (inRound) rounds++;
    const overhead = 4 * hist.length;
    const textTokensInt = Math.round(textTok) + overhead;
    const thinkTokensInt = Math.round(thinkTok);
    return {
      msg_count: hist.length,
      rounds,
      text_chars: textChars,
      think_chars: thinkChars,
      total_chars: textChars + thinkChars,
      text_tokens: textTokensInt,
      think_tokens: thinkTokensInt,
      est_tokens: textTokensInt + thinkTokensInt,
    };
  }
  function extractTime(m) {
    const parts = m && m.content;
    const texts = [];
    if (typeof parts === "string") texts.push(parts);
    else if (Array.isArray(parts)) {
      for (const p of parts) if (p && p.type === "text" && p.text) texts.push(p.text);
    }
    for (const t of texts) {
      const m2 = t.match(/datetime:\s*(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})/);
      if (m2) return `${m2[1]} ${m2[2]}`;
    }
    return null;
  }
  function toast(msg, isErr) {
    let t = document.getElementById("__toast");
    if (!t) {
      t = document.createElement("div");
      t.id = "__toast";
      t.style.cssText =
        "position:fixed;top:14px;right:14px;z-index:9999;max-width:440px;padding:10px 14px;" +
        "border-radius:10px;box-shadow:0 4px 16px rgba(0,0,0,.3);font-size:13px;line-height:1.5;";
      document.body.appendChild(t);
    }
    t.style.background = isErr ? "#b3261e" : "#2fa46a";
    t.style.color = "#fff";
    t.textContent = msg;
    t.style.display = "block";
    clearTimeout(t.__timer);
    t.__timer = setTimeout(() => { t.style.display = "none"; }, isErr ? 6000 : 3000);
  }

  // ---------- modal ----------
  function openModal(title, bodyHtml, footHtml) {
    closeModal();
    const root = $("#modal-root");
    root.innerHTML = `
      <div class="modal-overlay">
        <div class="modal">
          <div class="modal-head"><span>${esc(title)}</span><button class="icon-btn" data-close>✕</button></div>
          <div class="modal-body">${bodyHtml}</div>
          <div class="modal-foot">${footHtml || '<button class="btn" data-close>关闭</button>'}</div>
        </div>
      </div>`;
    root.querySelectorAll("[data-close]").forEach((b) => (b.onclick = closeModal));
    root.querySelector(".modal-overlay").addEventListener("mousedown", (e) => {
      if (e.target === e.currentTarget) closeModal();
    });
    return root.querySelector(".modal");
  }
  function closeModal() { $("#modal-root").innerHTML = ""; }
  function confirmModal(text, danger) {
    return new Promise((resolve) => {
      const m = openModal("请确认", `<div style="line-height:1.6">${esc(text)}</div>`,
        `<button class="btn" data-no>取消</button><button class="btn ${danger ? "danger" : "primary"}" data-yes>确定</button>`);
      m.querySelector("[data-yes]").onclick = () => { closeModal(); resolve(true); };
      m.querySelector("[data-no]").onclick = () => { closeModal(); resolve(false); };
    });
  }

  // ---------- data ops ----------
  async function loadSettings() {
    try { state.settings = Object.assign(state.settings, await apiGet("/config")); } catch (e) { /* ignore */ }
  }
  async function loadProviders() {
    try {
      const d = await apiGet("/providers");
      state.providers = (d.providers || []).filter((p) => p.enable);
    } catch (e) { state.providers = []; }
  }
  async function loadList() {
    const d = await apiGet("/list");
    state.convs = d.conversations || [];
    renderConvList();
  }
  async function openConv(cid) {
    try {
      const d = await apiGet(`/conv/${cid}`);
      state.current = d;
      state.history = d.history || [];
      state.baseUpdatedAt = d.updated_at;
      state.dirty = false;
      state.selectedIdx = null;
      state.windowStart = Math.max(0, state.history.length - PAGE_SIZE);
      $("#placeholder").classList.add("hidden");
      $("#editor").classList.remove("hidden");
      $("#ed-user").textContent = d.user_id || d.cid;
      updateHeaderMeta();
      hideBanner();
      updateSaveBar();
      renderTimeline({ forceBottom: true });
      renderGuard();
      renderConvList();
      checkCompressJobOnce();
    } catch (e) {
      toast(e.message, true);
    }
  }
  function updateHeaderMeta() {
    if (!state.current) return;
    const d = state.current;
    $("#ed-meta").textContent =
      `${state.history.length} 条（${d.rounds || 0} 轮） · ${fmtTs(state.baseUpdatedAt)} · ${d.platform_id || ""}`;
  }
  async function reloadConv(silent) {
    if (!state.current) return;
    const keepIdx = state.selectedIdx;
    await openConv(state.current.cid);
    state.selectedIdx = keepIdx != null && keepIdx < state.history.length ? keepIdx : null;
    if (state.selectedIdx != null) renderTimeline();
    if (!silent) toast("已重新加载");
  }

  // ---------- render: conv list ----------
  function renderConvList() {
    const el = $("#conv-list");
    if (!state.convs.length) {
      el.innerHTML = '<div class="muted small" style="padding:10px">暂无对话</div>';
      return;
    }
    el.innerHTML = state.convs
      .map((c) => {
        const active = state.current && state.current.cid === c.cid ? "active" : "";
        const charsStr = c.total_chars != null ? `${fmtWan(c.total_chars)}字` : "";
        return `<div class="conv-item ${active}" data-cid="${esc(c.cid)}">
          <div class="ci-title">${esc(c.title || c.user_id || c.cid)}</div>
          <div class="ci-sub">
            <span>${c.msg_count} 条/${c.rounds || "-"} 轮</span>
            ${charsStr ? `<span>${charsStr}</span>` : ""}
            <span>~${fmtWan(c.est_tokens)} tok</span>
            <span>${fmtTs(c.updated_at)}</span>
          </div>
        </div>`;
      })
      .join("");
    el.querySelectorAll(".conv-item").forEach((n) => {
      n.onclick = () => {
        if (state.dirty && !window.confirm("当前有未保存的修改，切换将丢弃，确定？")) return;
        openConv(n.dataset.cid);
      };
    });
  }

  // ---------- render: timeline & guard ----------
  function renderGuard() {
    const g = $("#guard-card");
    if (!state.current) return;
    const cur = state.current;
    const s = state.settings;
    const rounds = cur.rounds || 0;
    const totalChars = cur.total_chars || 0;
    const thinkChars = cur.think_chars || 0;
    const estTok = cur.est_tokens || 0;

    const rLim = parseInt(s.auto_compress_rounds, 10) || 0;
    const cLim = parseInt(s.auto_compress_chars, 10) || 0;
    const tLim = parseInt(s.token_threshold, 10) || 0;

    const hit = [];
    const warn = [];
    if (rLim > 0) {
      if (rounds >= rLim) hit.push(`轮数 ${rounds}/${rLim}`);
      else if (rounds >= rLim * 0.8) warn.push(`轮数 ${rounds}/${rLim}`);
    }
    if (cLim > 0) {
      if (totalChars >= cLim) hit.push(`字数 ${fmtWan(totalChars)}/${fmtWan(cLim)}`);
      else if (totalChars >= cLim * 0.8) warn.push(`字数 ${fmtWan(totalChars)}/${fmtWan(cLim)}`);
    }
    if (tLim > 0) {
      if (estTok >= tLim) hit.push(`Token ~${fmtWan(estTok)}/${fmtWan(tLim)}`);
      else if (estTok >= tLim * 0.8) warn.push(`Token ~${fmtWan(estTok)}/${fmtWan(tLim)}`);
    }

    g.classList.remove("warn", "danger");
    const thinkNote = thinkChars > 0 ? `（含思考 ${fmtWan(thinkChars)}字）` : "";
    const autoBadge = s.auto_compress_enabled ? " · 自动压缩:开" : "";
    const baseSummary = `共 ${rounds} 轮 · ${fmtWan(totalChars)}字${thinkNote} · ~${fmtWan(estTok)} tok${autoBadge}`;

    if (hit.length) {
      g.classList.add("danger");
      g.textContent = `⚠ ${baseSummary}（已达阈值: ${hit.join("、")}）`;
    } else if (warn.length) {
      g.classList.add("warn");
      g.textContent = `${baseSummary}（接近阈值: ${warn.join("、")}）`;
    } else {
      g.textContent = baseSummary;
    }

    const limDesc = [
      rLim > 0 ? `轮数上限: ${rLim} 轮` : "轮数: 不限",
      cLim > 0 ? `字数上限: ${cLim} 字` : "字数: 不限",
      tLim > 0 ? `Token 上限: ${tLim} tok` : "Token: 不限",
    ].join(" ｜ ");
    g.title =
      `对话正文字数: ${(cur.text_chars || 0).toLocaleString()} 字\n` +
      `AI 思考字数: ${thinkChars.toLocaleString()} 字\n` +
      `总计字数: ${totalChars.toLocaleString()} 字（约 ${estTok.toLocaleString()} Token，含思考内容）\n` +
      `当前限制（${s.auto_compress_enabled ? "已开启自动压缩" : "仅预警，未开自动压缩"}）：${limDesc}`;
  }

  // 系统提醒仅在消息头显示时间，气泡内不再渲染；think 思考过程以折叠条展示
  function stripReminder(t) {
    return String(t == null ? "" : t).replace(/<system_reminder>[\s\S]*?<\/system_reminder>/g, "");
  }
  function renderText(t) {
    return esc(stripReminder(t));
  }
  function renderBubbleInner(m) {
    const c = m.content;
    if (typeof c === "string") {
      const s = stripReminder(c).trim();
      return s ? renderText(s) : "<em class='muted small'>(空消息)</em>";
    }
    if (Array.isArray(c)) {
      let thinkHtml = "";
      let bodyHtml = "";
      let hasBody = false;
      for (const p of c) {
        if (!p || typeof p !== "object") continue;
        if (p.type === "think") {
          const th = String(p.think || "").trim();
          if (th) {
            thinkHtml += `<details class="think"><summary>💭 思考过程（${th.length} 字）</summary><div class="think-body">${esc(th)}</div></details>`;
          }
        } else if (p.type === "text") {
          const s = stripReminder(p.text).trim();
          if (s) { bodyHtml += (hasBody ? "\n" : "") + renderText(s); hasBody = true; }
        } else if (p.type) {
          bodyHtml += (hasBody ? "\n" : "") + `<span class="part-img">[${esc(p.type)} · 只读]</span>`;
          hasBody = true;
        }
      }
      const total = thinkHtml + bodyHtml;
      return total || "<em class='muted small'>(空消息)</em>";
    }
    return `<em class='muted small'>(未知内容类型)</em>`;
  }

  function renderTimeline(opts) {
    const forceBottom = opts && opts.forceBottom;
    const tl = $("#timeline");
    const h = state.history;
    const start = Math.max(0, state.windowStart);
    const prevTop = tl.scrollTop;
    const wasBottom = tl.scrollHeight - tl.scrollTop - tl.clientHeight < 80;
    let html = "";
    if (start > 0) {
      html += `<div style="text-align:center;padding:6px 0"><button class="btn ghost" data-act="more">⬆ 加载更早消息（${start} 条未显示）</button></div>`;
    }
    for (let i = start; i < h.length; i++) {
      const m = h[i];
      const role = m.role === "user" ? "user" : m.role === "assistant" ? "assistant" : "system";
      const sel = state.selectedIdx === i ? "selected" : "";
      const t = extractTime(m);
      const readonly = role === "system" ? "readonly" : "";
      html += `<div class="msg-row ${role}" data-i="${i}"><div class="msg-body"><div class="msg-head"><span class="msg-idx">#${i}</span><span>${role === "user" ? "用户" : role === "assistant" ? "助手" : esc(m.role)}</span>${t ? `<span>${esc(t)}</span>` : ""}</div><div class="bubble ${sel} ${readonly}" data-act="select" data-i="${i}">${renderBubbleInner(m)}</div></div><div class="msg-ops"><button class="btn" data-act="edit" data-i="${i}" title="编辑">✏ 编辑</button><button class="btn" data-act="del" data-i="${i}" title="删除此条">🗑 删</button>${role === "user" ? `<button class="btn" data-act="delround" data-i="${i}" title="删除这一轮（含回复）">⏬ 删轮</button>` : ""}<button class="btn" data-act="insert" data-i="${i}" title="在此后插入">➕ 插入</button><button class="btn" data-act="trunc" data-i="${i}" title="截断：保留到这条为止">✂ 截断</button></div></div>`;
    }
    tl.innerHTML = `<div class="timeline-inner">${html}</div>`;
    tl.querySelectorAll("details.think").forEach((d) => {
      d.addEventListener("click", (ev) => ev.stopPropagation());
    });
    tl.querySelectorAll("[data-act]").forEach((b) => {
      const act = b.dataset.act;
      if (act === "more") {
        b.addEventListener("click", () => {
          state.windowStart = Math.max(0, state.windowStart - PAGE_SIZE);
          renderTimeline();
        });
      } else {
        b.addEventListener("click", onMsgAction);
      }
    });
    if (forceBottom || wasBottom) tl.scrollTop = tl.scrollHeight;
    else tl.scrollTop = prevTop;
  }

  function onMsgAction(e) {
    const i = parseInt(e.currentTarget.dataset.i, 10);
    const act = e.currentTarget.dataset.act;
    if (act === "select") {
      state.selectedIdx = state.selectedIdx === i ? null : i;
      renderTimeline();
      return;
    }
    if (act === "edit") editModal(i);
    if (act === "del") deleteMsg(i);
    if (act === "delround") deleteRound(i);
    if (act === "insert") insertModal(i + 1);
    if (act === "trunc") truncateTo(i);
  }

  // ---------- local edit ops ----------
  function markDirty() {
    state.dirty = true;
    if (state.current) {
      const st = calcStats(state.history);
      Object.assign(state.current, st);
      updateHeaderMeta();
    }
    updateSaveBar();
  }
  function updateSaveBar() {
    $("#save-bar").classList.toggle("hidden", !state.dirty);
  }
  function deleteMsg(i) {
    state.history.splice(i, 1);
    if (state.selectedIdx === i) state.selectedIdx = null;
    state.windowStart = Math.min(state.windowStart, Math.max(0, state.history.length - PAGE_SIZE));
    markDirty(); renderTimeline(); renderGuard();
  }
  function deleteRound(i) {
    let j = i + 1;
    while (j < state.history.length && state.history[j].role !== "user") j++;
    state.history.splice(i, j - i);
    state.selectedIdx = null;
    state.windowStart = Math.min(state.windowStart, Math.max(0, state.history.length - PAGE_SIZE));
    markDirty(); renderTimeline(); renderGuard();
  }
  function truncateTo(i) {
    state.history = state.history.slice(0, i + 1);
    state.selectedIdx = null;
    state.windowStart = Math.max(0, state.history.length - PAGE_SIZE);
    markDirty(); renderTimeline(); renderGuard();
  }
  function insertModal(at) {
    const m = openModal("插入消息", `
      <div class="field"><label>角色</label>
        <select id="ins-role"><option value="user">用户</option><option value="assistant">助手</option></select>
      </div>
      <div class="field"><label>💭 思考过程（可选，留空则无思考过程）</label><textarea id="ins-think" placeholder="可选：输入模型思考过程（think），留空则不添加" style="min-height:80px"></textarea></div>
      <div class="field"><label>内容（文本）</label><textarea id="ins-text" placeholder="在此输入消息正文文本"></textarea></div>
    `, '<button class="btn" data-close>取消</button><button class="btn primary" id="ins-ok">插入</button>');
    m.querySelector("#ins-ok").onclick = () => {
      const role = m.querySelector("#ins-role").value;
      const think = m.querySelector("#ins-think").value.trim();
      const text = m.querySelector("#ins-text").value;
      if (!text.trim() && !think) { toast("内容与思考过程不能同时为空", true); return; }
      const content = think
        ? [{ type: "think", think }, { type: "text", text }]
        : text;
      state.history.splice(at, 0, { role, content });
      state.selectedIdx = null;
      state.windowStart = Math.min(state.windowStart, Math.max(0, state.history.length - PAGE_SIZE));
      closeModal(); markDirty(); renderTimeline(); renderGuard();
    };
  }
  function editModal(i) {
    const m = state.history[i];
    const c = m.content;
    const stringMode = typeof c === "string";
    let existingThink = "";
    if (Array.isArray(c)) {
      const thinks = [];
      for (const p of c) {
        if (p && p.type === "think" && p.think != null) thinks.push(String(p.think));
      }
      existingThink = thinks.join("\n\n");
    }
    let partsHtml = "";
    if (stringMode) {
      partsHtml = `<div class="field"><label>回复/消息内容（文本）</label>
        <textarea class="edit-t" data-p="0" placeholder="输入消息正文内容">${esc(c)}</textarea></div>`;
    } else if (Array.isArray(c)) {
      let ti = 0;
      c.forEach((p, pi) => {
        if (p && p.type === "text") {
          partsHtml += `<div class="field"><label>文本片段 #${ti}（part ${pi}）</label>
            <textarea class="edit-t" data-p="${pi}" placeholder="输入消息正文内容">${esc(p.text || "")}</textarea></div>`;
          ti++;
        }
      });
      if (!ti) {
        partsHtml = `<div class="field"><label>补充文本内容（可选）</label>
          <textarea class="edit-t" data-p="new-text" placeholder="该消息当前无文本片段，可在此补充"></textarea></div>`;
      }
    } else {
      partsHtml = `<div class="muted small">未知内容类型，仅可修改角色</div>`;
    }
    const m2 = openModal(`编辑消息 #${i}`, `
      <div class="field"><label>角色</label>
        <select id="ed-role">
          <option value="user" ${m.role === "user" ? "selected" : ""}>用户</option>
          <option value="assistant" ${m.role === "assistant" ? "selected" : ""}>助手</option>
        </select>
      </div>
      <div class="field"><label>💭 思考过程（think · 留空表示无/删除思考过程）</label>
        <textarea id="ed-think" placeholder="当前无思考过程（可在此直接输入新增，或把已有内容清空以删除思考过程）" style="min-height:95px">${esc(existingThink)}</textarea>
      </div>
      <div class="edit-area">${partsHtml}</div>
    `, '<button class="btn" data-close>取消</button><button class="btn primary" id="ed-ok">应用（本地，稍后统一保存）</button>');
    m2.querySelector("#ed-ok").onclick = () => {
      const newRole = m2.querySelector("#ed-role").value;
      const thinkVal = m2.querySelector("#ed-think").value.trim();
      if (stringMode) {
        const textVal = m2.querySelector(".edit-t").value;
        if (thinkVal) {
          state.history[i] = {
            role: newRole,
            content: [{ type: "think", think: thinkVal }, { type: "text", text: textVal }],
          };
        } else {
          state.history[i] = { role: newRole, content: textVal };
        }
      } else if (Array.isArray(c)) {
        const nm = JSON.parse(JSON.stringify(m));
        nm.role = newRole;
        m2.querySelectorAll(".edit-t").forEach((ta) => {
          if (ta.dataset.p === "new-text") {
            if (ta.value) nm.content.push({ type: "text", text: ta.value });
          } else {
            const pi = parseInt(ta.dataset.p, 10);
            if (nm.content[pi] && nm.content[pi].type === "text") nm.content[pi].text = ta.value;
          }
        });
        const firstThink = nm.content.find((p) => p && p.type === "think");
        nm.content = nm.content.filter((p) => !(p && p.type === "think"));
        if (thinkVal) {
          const thinkPart = firstThink
            ? Object.assign({}, firstThink, { type: "think", think: thinkVal })
            : { type: "think", think: thinkVal };
          nm.content.unshift(thinkPart);
        }
        state.history[i] = nm;
      } else {
        state.history[i].role = newRole;
      }
      closeModal(); markDirty(); renderTimeline(); renderGuard();
    };
  }

  // ---------- save / discard ----------
  async function saveChanges() {
    try {
      const d = await apiPost(`/save/${state.current.cid}`, {
        history: state.history,
        base_updated_at: state.baseUpdatedAt,
      });
      state.baseUpdatedAt = d.updated_at != null ? d.updated_at : state.baseUpdatedAt;
      state.dirty = false;
      updateSaveBar();
      const st = calcStats(state.history);
      Object.assign(state.current, st, d);
      updateHeaderMeta();
      renderGuard();
      toast(`已保存 ${d.msg_count} 条${d.snapshot ? `（快照 ${d.snapshot}）` : ""}`);
      loadList();
    } catch (e) {
      if (e.extra && e.extra.conflict) {
        showBanner(`保存冲突：${e.message}`, "重新加载", () => reloadConv());
      } else {
        toast(e.message, true);
      }
    }
  }
  async function discardChanges() {
    if (!(await confirmModal("放弃当前所有本地修改并重新加载？", true))) return;
    reloadConv();
  }
  function showBanner(text, btnText, cb) {
    const b = $("#banner");
    b.classList.remove("hidden");
    b.innerHTML = `<span>${esc(text)}</span>` +
      (btnText ? `<button class="btn" id="banner-btn">${esc(btnText)}</button>` : "");
    if (btnText) b.querySelector("#banner-btn").onclick = cb;
  }
  function hideBanner() { $("#banner").classList.add("hidden"); }

  // ---------- snapshots ----------
  async function snapshotsModal() {
    let snaps = [];
    try { snaps = (await apiGet(`/snapshots/${state.current.cid}`)).snapshots || []; } catch (e) { /* ignore */ }
    const listHtml = snaps.length
      ? snaps.map((s) => `
          <div class="snap-item">
            <div>
              <div>${esc(s.name)}</div>
              <div class="muted small">${fmtTs(s.time)} · ${s.msg_count} 条</div>
            </div>
            <button class="btn" data-restore="${esc(s.name)}">恢复</button>
          </div>`).join("")
      : '<div class="muted small">暂无快照（每次保存/导入/手动或自动压缩前会自动创建）</div>';
    const m = openModal("快照管理", listHtml);
    m.querySelectorAll("[data-restore]").forEach((b) => {
      b.onclick = async () => {
        if (!(await confirmModal("恢复该快照将覆盖当前对话内容（当前内容会先自动快照），确定？", true))) return;
        try {
          const d = await apiPost(`/snapshots/restore/${state.current.cid}`, { name: b.dataset.restore });
          toast(`已恢复快照 ${d.restored}`);
          closeModal();
          reloadConv(true);
          loadList();
        } catch (e) { toast(e.message, true); }
      };
    });
  }

  // ---------- export / import ----------
  async function exportConv() {
    try {
      const d = await apiGet(`/export/${state.current.cid}`);
      const blob = new Blob([d.jsonl], { type: "application/jsonl" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = d.filename;
      a.click();
      URL.revokeObjectURL(a.href);
      toast(`已导出 ${d.msg_count} 条`);
    } catch (e) { toast(e.message, true); }
  }
  function importModal() {
    const m = openModal("导入 JSONL（整段替换当前对话）", `
      <div class="banner" style="margin:-4px 0 12px">⚠ 导入会整段替换当前对话内容（替换前自动快照，可随时回滚）</div>
      <div class="field"><label>选择 .jsonl 文件</label><input type="file" id="imp-file" accept=".jsonl,.json,.txt" /></div>
      <div class="field"><label>或直接粘贴 JSONL 内容</label><textarea id="imp-text" placeholder='{"cid": "...", "content": [...]} 每行一条记录'></textarea></div>
    `, '<button class="btn" data-close>取消</button><button class="btn primary" id="imp-ok">校验并导入</button>');
    m.querySelector("#imp-file").addEventListener("change", async (ev) => {
      const f = ev.target.files[0];
      if (f) m.querySelector("#imp-text").value = await f.text();
    });
    m.querySelector("#imp-ok").onclick = async () => {
      const text = m.querySelector("#imp-text").value;
      if (!text.trim()) { toast("内容为空", true); return; }
      closeModal();
      if (!(await confirmModal("确认用文件内容整段替换当前对话？", true))) return;
      try {
        const d = await apiPost(`/import/${state.current.cid}`, { jsonl: text });
        toast(`已导入 ${d.msg_count} 条（快照 ${d.snapshot}）`);
        reloadConv(true);
        loadList();
      } catch (e) { toast(e.message, true); }
    };
  }

  // ---------- compress ----------
  function buildProviderOptions(selectedId, firstLabel) {
    let html = `<option value="" ${!selectedId ? "selected" : ""}>${esc(firstLabel)}</option>`;
    for (const p of state.providers) {
      const sel = p.id === selectedId ? "selected" : "";
      html += `<option value="${esc(p.id)}" ${sel}>${esc(p.id)}${p.model ? " · " + esc(p.model) : ""}</option>`;
    }
    return html;
  }

  function compressModal() {
    const s = state.settings;
    const primaryOpts = buildProviderOptions(s.auto_compress_provider, "🌟 默认（跟随当前聊天模型）");
    const fbOpts = buildProviderOptions(s.auto_compress_fallback_provider, "不使用备用模型");
    const defaultInstr = s.default_compress_instruction || DEFAULT_INSTR_FALLBACK;
    const cur = state.current || {};
    const m = openModal("立即手动压缩对话历史", `
      <div class="banner" style="margin:-4px 0 12px">
        把旧对话浓缩为摘要并替换历史头部（压缩前自动创建快照；若因审核/断网失败，原聊天记录完好无损，绝不强行删除）。
      </div>
      <div class="row2">
        <div class="field">
          <label>首选压缩模型</label>
          <select id="cp-provider">${primaryOpts}</select>
          <div class="field-hint">默认用当前聊天模型；内容敏感建议选无审核模型</div>
        </div>
        <div class="field">
          <label>备用兜底模型（可选）</label>
          <select id="cp-fb-provider">${fbOpts}</select>
          <div class="field-hint">首选模型报错或被审核拦截时自动顶上</div>
        </div>
      </div>
      <div class="row2">
        <div class="field">
          <label>保留最近多少比例不压缩（0 ~ 0.5）</label>
          <input id="cp-ratio" type="number" step="0.05" min="0" max="0.5" value="${s.compress_keep_ratio}" placeholder="例：填 0.15 代表保留最近 15% 的原对话" />
          <div class="field-hint">例：填 0.15 代表保留最近 15% 的完整对话，压缩前 85%</div>
        </div>
        <div class="field">
          <label>当前会话统计（只读）</label>
          <input type="text" value="共 ${cur.rounds || 0} 轮 · ${fmtWan(cur.total_chars || 0)}字 · ~${fmtWan(cur.est_tokens || 0)} tok" readonly />
          <div class="field-hint">含思考过程 ${fmtWan(cur.think_chars || 0)} 字</div>
        </div>
      </div>
      <div class="field">
        <label>摘要提示词（留空则使用框内显示的默认提示词）</label>
        <textarea id="cp-instr" placeholder="默认提示词：${esc(defaultInstr)}">${esc(s.compress_instruction || "")}</textarea>
        <div class="field-hint">可在此写明需要重点保留的人物称呼、说话风格、场景设定与未完伏笔</div>
      </div>
    `, '<button class="btn" data-close>取消</button><button class="btn primary" id="cp-ok">开始后台压缩</button>');
    m.querySelector("#cp-ok").onclick = async () => {
      const provider_id = m.querySelector("#cp-provider").value;
      const fallback_provider_id = m.querySelector("#cp-fb-provider").value;
      const keep_ratio = parseFloat(m.querySelector("#cp-ratio").value);
      const instruction = m.querySelector("#cp-instr").value;
      closeModal();
      try {
        await apiPost("/compress", {
          cid: state.current.cid,
          provider_id,
          fallback_provider_id,
          keep_ratio: isNaN(keep_ratio) ? 0.15 : keep_ratio,
          instruction,
        });
        toast("压缩任务已在后台启动");
        pollJob();
      } catch (e) { toast(e.message, true); }
    };
  }

  function renderJobBanner(job) {
    if (!job || job.state === "idle") return;
    if (job.state === "running") {
      showBanner(`⏳ ${job.progress || "后台压缩中"}${job.total_chunks ? `（已完成 ${job.done_chunks || 0}/${job.total_chunks} 块）` : ""}`);
    } else if (job.state === "retry_wait") {
      showBanner(`⏳ ${job.error || job.progress}`, "取消重试并清除提示", async () => {
        if (!state.current) return;
        await apiPost(`/compress/reset/${state.current.cid}`, {});
        hideBanner();
      });
    } else if (job.state === "error") {
      showBanner(`⚠ ${job.error || "压缩失败（原记录完好无损）"}`, "我知道了", async () => {
        if (state.current) {
          try { await apiPost(`/compress/reset/${state.current.cid}`, {}); } catch (e) { /* ignore */ }
        }
        hideBanner();
      });
    }
  }

  async function checkCompressJobOnce() {
    if (!state.current) return;
    try {
      const d = await apiGet(`/compress/status/${state.current.cid}`);
      const job = d.job || {};
      if (job.state === "running") {
        renderJobBanner(job);
        pollJob();
      } else if (job.state === "retry_wait" || job.state === "error") {
        renderJobBanner(job);
      }
    } catch (e) { /* ignore */ }
  }

  function pollJob() {
    clearInterval(state.jobTimer);
    state.jobTimer = setInterval(async () => {
      if (!state.current) { clearInterval(state.jobTimer); return; }
      try {
        const d = await apiGet(`/compress/status/${state.current.cid}`);
        const job = d.job || {};
        if (job.state === "running") {
          renderJobBanner(job);
        } else {
          clearInterval(state.jobTimer);
          if (job.state === "done") {
            hideBanner();
            const tag = job.mode === "auto" ? "自动压缩完成" : "压缩完成";
            toast(`${tag}（${job.used_provider || ""}）：${job.before.msg_count} → ${job.after.msg_count} 条，~${job.before.est_tokens} → ~${job.after.est_tokens} tok`);
            reloadConv(true);
            loadList();
          } else if (job.state === "retry_wait" || job.state === "error") {
            renderJobBanner(job);
          }
        }
      } catch (e) { /* ignore */ }
    }, 2000);
  }

  // ---------- settings ----------
  async function settingsModal() {
    const s = state.settings;
    const primaryOpts = buildProviderOptions(s.auto_compress_provider, "🌟 默认（跟随当前聊天模型）");
    const fbOpts = buildProviderOptions(s.auto_compress_fallback_provider, "不使用备用模型");
    const defaultInstr = s.default_compress_instruction || DEFAULT_INSTR_FALLBACK;

    const m = openModal("⚙ 对话管理与自动压缩设置", `
      <div class="settings-section">
        <div class="sec-title">🤖 自动压缩上下文（后台无感执行 · 失败绝不删记录）</div>
        <label class="switch-row">
          <input id="st-auto-en" type="checkbox" ${s.auto_compress_enabled ? "checked" : ""} />
          <span><strong>开启自动压缩</strong>（当下方三个限制条件满足任意一个时，在机器人回复后自动后台压缩）</span>
        </label>
        <div class="row3" style="margin-top:10px">
          <div class="field">
            <label>① 对话轮数限制（轮）</label>
            <input id="st-rounds" type="number" min="0" value="${s.auto_compress_rounds || 0}" placeholder="例：80（填 0 代表不限制）" />
            <div class="field-hint">例：填 80 代表满 80 轮触发；填 0 不按轮数限制</div>
          </div>
          <div class="field">
            <label>② 消息总字数限制（字）</label>
            <input id="st-chars" type="number" min="0" step="1000" value="${s.auto_compress_chars || 0}" placeholder="例：50000（填 0 代表不限制）" />
            <div class="field-hint">含对话正文与 AI 思考字数；例：填 50000 代表 5 万字触发；填 0 不限制</div>
          </div>
          <div class="field">
            <label>③ Token 数限制 & 预警线</label>
            <input id="st-th" type="number" min="0" step="1000" value="${s.token_threshold || 0}" placeholder="例：30000（填 0 代表不限制）" />
            <div class="field-hint">含 AI 思考 Token；达到时右上角变红，开启自动压缩后也会触发压缩（填 0 不限制）</div>
          </div>
        </div>
        <div class="row2">
          <div class="field">
            <label>首选压缩模型</label>
            <select id="st-prov">${primaryOpts}</select>
            <div class="field-hint">默认跟随该会话当前的聊天模型；也可指定专用/无审核模型</div>
          </div>
          <div class="field">
            <label>备用兜底模型（保命防失败）</label>
            <select id="st-fb-prov">${fbOpts}</select>
            <div class="field-hint">首选模型因审核拦截、断网、欠费失败时自动顶上；若都失败则 5 分钟后重试（最多 3 次），绝不一刀切删历史</div>
          </div>
        </div>
      </div>

      <div class="settings-section">
        <div class="sec-title">📝 压缩参数与常规设置</div>
        <div class="row3">
          <div class="field">
            <label>压缩保留最近比例（0~0.5）</label>
            <input id="st-ratio" type="number" step="0.05" min="0" max="0.5" value="${s.compress_keep_ratio}" placeholder="例：0.15（保留最近 15% 对话）" />
            <div class="field-hint">例：填 0.15 代表保留最近 15% 的原对话不压缩</div>
          </div>
          <div class="field">
            <label>每会话快照保留份数</label>
            <input id="st-snap" type="number" min="1" value="${s.snapshot_keep}" placeholder="例：20（超出自动滚动淘汰旧快照）" />
            <div class="field-hint">每次修改或压缩前自动备份，随时可一键回滚</div>
          </div>
          <div class="field">
            <label>面板自动刷新间隔（秒）</label>
            <input id="st-poll" type="number" min="0" value="${s.poll_interval}" placeholder="例：5（填 0 关闭自动刷新）" />
            <div class="field-hint">填 5 代表每 5 秒同步一次最新消息；填 0 关闭</div>
          </div>
        </div>
        <div class="field" style="margin-bottom:4px">
          <label>压缩摘要提示词（留空则自动使用框内灰字显示的默认提示词）</label>
          <textarea id="st-instr" placeholder="默认提示词：${esc(defaultInstr)}">${esc(s.compress_instruction || "")}</textarea>
          <div class="field-hint">无论是手动压缩还是自动压缩，都会按此要求提炼摘要</div>
        </div>
      </div>
    `, '<button class="btn" data-close>取消</button><button class="btn primary" id="st-ok">保存设置（立即生效）</button>');

    m.querySelector("#st-ok").onclick = async () => {
      try {
        const d = await apiPost("/config", {
          auto_compress_enabled: m.querySelector("#st-auto-en").checked,
          auto_compress_rounds: parseInt(m.querySelector("#st-rounds").value, 10) || 0,
          auto_compress_chars: parseInt(m.querySelector("#st-chars").value, 10) || 0,
          token_threshold: parseInt(m.querySelector("#st-th").value, 10) || 0,
          auto_compress_provider: m.querySelector("#st-prov").value,
          auto_compress_fallback_provider: m.querySelector("#st-fb-prov").value,
          compress_keep_ratio: parseFloat(m.querySelector("#st-ratio").value) || 0.15,
          snapshot_keep: Math.max(1, parseInt(m.querySelector("#st-snap").value, 10) || 20),
          poll_interval: Math.max(0, parseInt(m.querySelector("#st-poll").value, 10) || 0),
          compress_instruction: m.querySelector("#st-instr").value,
        });
        Object.assign(state.settings, d.changed || {});
        toast("设置已保存并立即生效");
        closeModal();
        setupPoll();
        renderGuard();
      } catch (e) { toast(e.message, true); }
    };
  }

  // ---------- poll ----------
  function setupPoll() {
    clearInterval(state.pollTimer);
    const sec = parseInt(state.settings.poll_interval, 10) || 0;
    if (sec <= 0) return;
    state.pollTimer = setInterval(async () => {
      try {
        const d = await apiGet("/list");
        state.convs = d.conversations || [];
        renderConvList();
        if (state.current) {
          checkCompressJobOnce();
          const now = state.convs.find((c) => c.cid === state.current.cid);
          if (now && now.updated_at > state.baseUpdatedAt) {
            if (!state.dirty) {
              await reloadConv(true);
              renderGuard();
            } else {
              showBanner("会话有新消息进入，而你有未保存的本地修改——直接保存会覆盖新消息，建议先重新加载。",
                "重新加载", () => reloadConv());
            }
          }
        }
      } catch (e) { /* ignore */ }
    }, sec * 1000);
  }

  // ---------- boot ----------
  function showBootError(msg) {
    const el = $("#conv-list");
    if (el) el.innerHTML = '<div class="banner" style="margin:8px;white-space:pre-wrap">初始化失败：' + esc(msg) + "</div>";
    toast("初始化失败: " + msg, true);
  }
  async function boot() {
    await loadSettings();
    await loadProviders();
    await loadList();
    setupPoll();

    $("#btn-refresh").onclick = () => loadList().then(() => toast("已刷新")).catch((e) => toast(e.message, true));
    $("#btn-snapshots").onclick = snapshotsModal;
    $("#btn-export").onclick = exportConv;
    $("#btn-import").onclick = importModal;
    $("#btn-compress").onclick = compressModal;
    $("#btn-settings").onclick = settingsModal;
    $("#btn-save").onclick = saveChanges;
    $("#btn-discard").onclick = discardChanges;
    $("#btn-insert").onclick = () => insertModal(state.history.length);
  }

  window.addEventListener("error", (ev) => {
    toast("JS 错误: " + (ev.message || ev.type), true);
  });
  window.addEventListener("unhandledrejection", (ev) => {
    const r = ev.reason;
    toast("未处理异常: " + ((r && r.message) || String(r)), true);
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => boot().catch((e) => showBootError(e.message || String(e))));
  } else {
    boot().catch((e) => showBootError(e.message || String(e)));
  }
})();
