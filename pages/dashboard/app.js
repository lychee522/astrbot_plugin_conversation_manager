/* astrbot_plugin_conversation_manager · 前端逻辑（原生 JS，无构建依赖） */
/* global AstrBotPluginPage */
(function () {
  "use strict";

  const PN = "/astrbot_plugin_conversation_manager";
  const PAGE_SIZE = 80;

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
      compress_keep_ratio: 0.15,
      compress_instruction: "",
    },
    providers: [],
    pollTimer: null,
    jobTimer: null,
  };

  const $ = (sel) => document.querySelector(sel);

  // ---------- API ----------
  // 网络层：bridge 优先（官方机制，页面在 sandbox iframe 内为 opaque origin，
  // 直接 fetch 会被 CORS 拦截），fetch 仅作兜底；两路失败都把原因显示出来。
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
    // 父级自动拼 /api/v1/plugins/extensions/<插件名>/<endpoint>，endpoint 不能再带插件名前缀
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
      if (e && e.extra) throw e; // 业务错误（success:false）直接抛，不重试
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
        "position:fixed;top:14px;right:14px;z-index:9999;max-width:420px;padding:10px 14px;" +
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
      $("#ed-meta").textContent =
        `${d.msg_count || state.history.length} 条 · ${fmtTs(d.updated_at)} · ${d.platform_id || ""}`;
      hideBanner();
      updateSaveBar();
      renderTimeline({ forceBottom: true });
      renderGuard();
      renderConvList();
    } catch (e) {
      toast(e.message, true);
    }
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
        return `<div class="conv-item ${active}" data-cid="${esc(c.cid)}">
          <div class="ci-title">${esc(c.title || c.user_id || c.cid)}</div>
          <div class="ci-sub">
            <span>${c.msg_count} 条</span><span>~${c.est_tokens} tok</span>
            <span>${esc((c.user_id || "").split(":").pop())}</span>
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

  // ---------- render: timeline ----------
  function renderGuard() {
    const g = $("#guard-card");
    const est = state.current ? state.current.est_tokens : 0;
    const th = state.settings.token_threshold;
    g.classList.remove("warn", "danger");
    if (est >= th) { g.classList.add("danger"); g.textContent = `⚠ 上下文 ~${est} / ${th} tok，可能触发自动压缩`; }
    else if (est >= th * 0.8) { g.classList.add("warn"); g.textContent = `上下文 ~${est} / ${th} tok，接近阈值`; }
    else { g.textContent = `上下文 ~${est} / ${th} tok`; }
  }

  // 系统提醒仅在消息头显示时间，气泡内不再渲染；think 思考过程不展示
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
      let html = "";
      let hasVisible = false;
      for (const p of c) {
        if (!p || typeof p !== "object") continue;
        if (p.type === "text") {
          const s = stripReminder(p.text).trim();
          if (s) { html += (hasVisible ? "\n" : "") + renderText(s); hasVisible = true; }
        } else if (p.type === "think") {
          // 思考过程按需求不展示
        } else if (p.type) {
          html += `<span class="part-img">[${esc(p.type)} · 只读]</span>`;
          hasVisible = true;
        }
      }
      return html || "<em class='muted small'>(空消息)</em>";
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
    const inner = start > 0 ? `<div class="timeline-inner">${html}</div>` : `<div class="timeline-inner">${html}</div>`;
    tl.innerHTML = inner;
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
      <div class="field"><label>内容（文本）</label><textarea id="ins-text" placeholder="输入消息文本"></textarea></div>
    `, '<button class="btn" data-close>取消</button><button class="btn primary" id="ins-ok">插入</button>');
    m.querySelector("#ins-ok").onclick = () => {
      const role = m.querySelector("#ins-role").value;
      const text = m.querySelector("#ins-text").value;
      if (!text.trim()) { toast("内容不能为空", true); return; }
      state.history.splice(at, 0, { role, content: text });
      state.selectedIdx = null;
      state.windowStart = Math.min(state.windowStart, Math.max(0, state.history.length - PAGE_SIZE));
      closeModal(); markDirty(); renderTimeline(); renderGuard();
    };
  }
  function editModal(i) {
    const m = state.history[i];
    const c = m.content;
    const stringMode = typeof c === "string";
    let partsHtml = "";
    if (stringMode) {
      partsHtml = `<div class="field"><label>内容（字符串型消息）</label>
        <textarea class="edit-t" data-p="0">${esc(c)}</textarea></div>`;
    } else if (Array.isArray(c)) {
      let ti = 0;
      c.forEach((p, pi) => {
        if (p && p.type === "text") {
          partsHtml += `<div class="field"><label>文本片段 #${ti}（part ${pi}）</label>
            <textarea class="edit-t" data-p="${pi}">${esc(p.text || "")}</textarea></div>`;
          ti++;
        }
      });
      if (!ti) partsHtml = `<div class="muted small">该消息没有可编辑的文本片段（仅含图片/媒体等只读内容）</div>`;
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
      <div class="edit-area">${partsHtml}</div>
    `, '<button class="btn" data-close>取消</button><button class="btn primary" id="ed-ok">应用（本地，稍后统一保存）</button>');
    m2.querySelector("#ed-ok").onclick = () => {
      const newRole = m2.querySelector("#ed-role").value;
      if (stringMode) {
        state.history[i] = { role: newRole, content: m2.querySelector(".edit-t").value };
      } else if (Array.isArray(c)) {
        const nm = JSON.parse(JSON.stringify(m));
        nm.role = newRole;
        m2.querySelectorAll(".edit-t").forEach((ta) => {
          const pi = parseInt(ta.dataset.p, 10);
          if (nm.content[pi] && nm.content[pi].type === "text") nm.content[pi].text = ta.value;
        });
        state.history[i] = nm;
      } else {
        state.history[i].role = newRole;
      }
      closeModal(); markDirty(); renderTimeline();
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
      state.current.est_tokens = d.est_tokens;
      $("#ed-meta").textContent =
        `${state.history.length} 条 · ${fmtTs(state.baseUpdatedAt)} · ${state.current.platform_id || ""}`;
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
      : '<div class="muted small">暂无快照（每次保存/导入/压缩前会自动创建）</div>';
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
      <div class="banner" style="margin:-4px 0 12px">⚠ 导入会整段替换当前对话内容（替换前自动快照，可回滚）</div>
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
  function compressModal() {
    if (!state.providers.length) {
      toast("未读到可用模型，请检查 AstrBot 模型提供商配置", true);
      return;
    }
    const opts = state.providers
      .map((p) => `<option value="${esc(p.id)}">${esc(p.id)}${p.model ? " · " + esc(p.model) : ""}</option>`)
      .join("");
    const m = openModal("手动压缩对话历史", `
      <div class="banner" style="margin:-4px 0 12px">把旧对话交给所选模型生成摘要后替换（压缩前自动快照）。内容敏感时请选择无审核的本地模型。</div>
      <div class="field"><label>用于压缩的模型</label><select id="cp-provider">${opts}</select></div>
      <div class="row2">
        <div class="field"><label>保留最近比例（0~0.3）</label><input id="cp-ratio" type="number" step="0.05" min="0" max="0.3" value="${state.settings.compress_keep_ratio}" /></div>
        <div class="field"><label>当前轮次</label><input type="text" value="${(state.current && state.current.rounds) || "-"} 条（只读）" readonly /></div>
      </div>
      <div class="field"><label>摘要指令</label><textarea id="cp-instr">${esc(state.settings.compress_instruction || "")}</textarea></div>
      <div class="muted small">大历史会自动分块摘要再合并；压缩在服务器后台执行，可关闭本窗口后继续聊天。</div>
    `, '<button class="btn" data-close>取消</button><button class="btn primary" id="cp-ok">开始压缩</button>');
    m.querySelector("#cp-ok").onclick = async () => {
      const provider_id = m.querySelector("#cp-provider").value;
      const keep_ratio = parseFloat(m.querySelector("#cp-ratio").value) || 0.15;
      const instruction = m.querySelector("#cp-instr").value;
      closeModal();
      try {
        await apiPost("/compress", {
          cid: state.current.cid, provider_id, keep_ratio, instruction,
        });
        toast("压缩任务已启动");
        pollJob();
      } catch (e) { toast(e.message, true); }
    };
  }
  function pollJob() {
    clearInterval(state.jobTimer);
    state.jobTimer = setInterval(async () => {
      if (!state.current) { clearInterval(state.jobTimer); return; }
      try {
        const d = await apiGet(`/compress/status/${state.current.cid}`);
        const job = d.job || {};
        if (job.state === "running") {
          showBanner(`⏳ ${job.progress || "压缩中"}${job.total_chunks ? `（${job.done_chunks || 0}/${job.total_chunks} 块）` : ""}`);
        } else {
          clearInterval(state.jobTimer);
          if (job.state === "done") {
            hideBanner();
            toast(`压缩完成：${job.before.msg_count} → ${job.after.msg_count} 条，est ${job.before.est_tokens} → ${job.after.est_tokens} tok`);
            reloadConv(true);
            loadList();
          } else if (job.state === "error") {
            showBanner(`压缩失败：${job.error || "未知错误"}`, "关闭", hideBanner);
          }
        }
      } catch (e) { /* ignore */ }
    }, 2000);
  }

  // ---------- settings ----------
  async function settingsModal() {
    const s = state.settings;
    const m = openModal("设置", `
      <div class="row2">
        <div class="field"><label>快照保留份数</label><input id="st-snap" type="number" min="1" value="${s.snapshot_keep}" /></div>
        <div class="field"><label>自动刷新间隔（秒，0=关）</label><input id="st-poll" type="number" min="0" value="${s.poll_interval}" /></div>
      </div>
      <div class="row2">
        <div class="field"><label>上下文压力阈值（est token）</label><input id="st-th" type="number" min="100" value="${s.token_threshold}" /></div>
        <div class="field"><label>压缩默认保留比例</label><input id="st-ratio" type="number" step="0.05" min="0" max="0.3" value="${s.compress_keep_ratio}" /></div>
      </div>
      <div class="field"><label>压缩摘要指令（留空用内置默认）</label><textarea id="st-instr">${esc(s.compress_instruction || "")}</textarea></div>
      <div class="muted small">设置保存在 AstrBot 插件配置中，热生效。</div>
    `, '<button class="btn" data-close>取消</button><button class="btn primary" id="st-ok">保存设置</button>');
    m.querySelector("#st-ok").onclick = async () => {
      try {
        const d = await apiPost("/config", {
          snapshot_keep: parseInt(m.querySelector("#st-snap").value, 10),
          poll_interval: parseInt(m.querySelector("#st-poll").value, 10),
          token_threshold: parseInt(m.querySelector("#st-th").value, 10),
          compress_keep_ratio: parseFloat(m.querySelector("#st-ratio").value),
          compress_instruction: m.querySelector("#st-instr").value,
        });
        Object.assign(state.settings, d.changed || {});
        toast("设置已保存");
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
        const prev = state.convs;
        state.convs = d.conversations || [];
        renderConvList();
        if (state.current) {
          const now = state.convs.find((c) => c.cid === state.current.cid);
          if (now && now.updated_at > state.baseUpdatedAt) {
            if (!state.dirty) {
              await reloadConv(true);
              renderGuard();
            } else {
              showBanner("会话有新消息，而你有未保存的修改——保存会覆盖新消息，建议先保存或重新加载。",
                "重新加载", () => reloadConv());
            }
          }
        }
        void prev;
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

