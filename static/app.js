/* 标本信息管理系统 - 前端逻辑 */
"use strict";

/* ---------------- 全局状态 ---------------- */
const S = {
  me: null,            // {username, admin, role}
  meta: null,          // {system_name, tables}
  t: "specimens",      // 当前表 / 'history' / 'checkup' / 'stats'
  q: "", page: 1, per: 100, sort: "", dir: "asc",
  rows: [], total: 0, columns: [],
  showDeleted: false,  // 回收站视图
  preset: "",          // loaned = 借出中
  filters: {},         // 列级筛选 {col: {mode, values|text}}
  cur: null,           // 键盘导航当前格 {rid, col}
  cascade: {},         // 各表的分类级联树缓存 {tname: tree}
  detEditing: null,    // 详情抽屉正在编辑的字段状态
  prefs: {},           // 服务端用户偏好（列隐藏等）
  histPage: 1,
  sel: new Set(),      // 勾选待批量操作的行 id
  docs: {},            // 数据字典 {字段名: 含义说明}
  undoStack: [],       // 最近可撤销批次
  _lastCkIdx: null, _lastCkChecked: false, _ckDown: null,  // Shift 范围选择基准
  colwSaveTimer: null,
};
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtV = (v) => v === null || v === undefined || v === ""
  ? '<span class="none" title="未记录（空值，不当作 0）">—</span>' : esc(v);
/* 学名单元格：属名+种加词斜体，cf./aff./sp./nov. 等限定词与命名人保持正体（遵循学名书写规范） */
const _SCI_QUAL = /^(cf\.?|aff\.?|sp\.?|spp\.?|nov\.?|gen\.?|sp\.nov|nr\.?|near|group|s\.|sensu|str\.)$/i;
function sciV(name) {
  const s = String(name ?? "").trim();
  if (!s) return fmtV(null);
  let seenEpithet = false;
  const out = s.split(/(\s+)/).map((tok) => {
    if (/^\s+$/.test(tok)) return tok;
    const clean = tok.replace(/[^A-Za-z.\-]/g, "");
    if (!clean || _SCI_QUAL.test(clean) || tok.startsWith("(")) return esc(tok);
    if (!seenEpithet) { seenEpithet = true; return `<i>${esc(tok)}</i>`; }   // 属名
    if (/^[a-z][a-z\-]+$/.test(clean)) return `<i>${esc(tok)}</i>`;          // 种加词/亚种加词
    return esc(tok);                                                        // 命名人等
  }).join("");
  return `<span class="sci">${out}</span>`;
}
const SCI_COLS = new Set(["FinalSpeciesName", "TemporarySpeciesName", "IdentifiedSpeciesName",
  "Species name", "Temporary Name", "Final Name", "Final name"]);
/* 单元格展示：学名按语义斜体；cf./aff. 加「待确认」文字状态；日期分量越界加「错误」 */
function cellHtml(colName, v, row) {
  let html = SCI_COLS.has(colName) ? sciV(v) : fmtV(v);
  const s = String(v ?? "").trim();
  const low = s.toLowerCase();
  if (s && (low.startsWith("cf.") || low.startsWith("cf ") || s.includes(" aff.")
      || / sp\. nov/.test(low) || /\bsp\.$/.test(low))) {
    html += ' <span class="st st-pending" title="鉴定限定词：该名称尚未完全确定，需复核">待确认</span>';
  }
  if (/^(DD|MM|YY){1,2}\d?$/.test(colName) || ["YYYY1", "YYYY2"].includes(colName)) {
    const n = Number(s);
    if (s && !isNaN(n)) {
      const bad = (colName.startsWith("MM") && !(1 <= n && n <= 12))
        || (colName.startsWith("DD") && !(1 <= n && n <= 31))
        || (colName.startsWith("YYYY") && !(1700 <= n && n <= 2200));
      if (bad) html += ' <span class="st st-risk" title="取值越界，属危险日期，需人工改正">错误</span>';
    }
  }
  const lkey = curCfg().linked_key;
  if (row && row.miss && (colName === curCfg().key || (lkey && colName === lkey))) {
    html += ' <span class="st st-risk" title="引用的事件号在采集地信息中不存在">失配</span>';
  }
  // 鉴定联动差异徽标（v4.31）：本格与权威表（DNA 提取台账）不一致，点击整行以权威值为准同步
  if (row && row.dna_diff && row.dna_diff[colName] !== undefined) {
    const peer = (curCfg().ident_sync || {}).peer_label || "DNA 提取台账";
    const av = String(row.dna_diff[colName] ?? "").trim() || "（空）";
    html += ` <span class="st st-dnadiff dna-diff-badge" data-dnasync-rid="${row.id}" title="与「${esc(peer)}」不一致：权威值为「${esc(av)}」，点击以权威值为准同步本行">异</span>`;
  }
  return html;
}
const ROLE_TXT = { admin: "管理员", group_admin: "类群管理员", editor: "编辑者", specimen_editor: "标本编辑", viewer: "只读" };

/* 单色线条图标（与界面文字同色，保持视觉统一） */
const _SVG = (p) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${p}</svg>`;
const ICONS = {
  home: _SVG('<path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V21h14V9.5"/>'),
  table: _SVG('<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18M9 10v10"/>'),
  clock: _SVG('<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>'),
  check: _SVG('<rect x="5" y="3" width="14" height="18" rx="2"/><path d="m9 12 2 2 4-4"/>'),
  chart: _SVG('<path d="M4 20v-9M10 20V4M16 20v-6"/><path d="M2.5 20h19"/>'),
  trash: _SVG('<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/>'),
  tag: _SVG('<path d="M3 12V4h8l9 9-8 8-9-9z"/><circle cx="8" cy="8" r="1.5"/>'),
  user: _SVG('<circle cx="12" cy="8" r="4"/><path d="M4 21c1.5-4 5-6 8-6s6.5 2 8 6"/>'),
  paperclip: _SVG('<path d="m21 12-8.5 8.5a5 5 0 0 1-7-7L14 5a3.5 3.5 0 0 1 5 5l-9 9a2 2 0 0 1-3-3l8-8"/>'),
  copy: _SVG('<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>'),
  view: _SVG('<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/>'),
  upload: _SVG('<path d="M12 15V3m0 0 4 4m-4-4L8 7"/><path d="M4 15v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4"/>'),
  download: _SVG('<path d="M12 3v12m0 0 4-4m-4 4-4-4"/><path d="M4 15v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4"/>'),
  restore: _SVG('<path d="M3 12a9 9 0 1 0 2.64-6.36"/><path d="M3 3v6h6"/>'),
  geo: _SVG('<path d="M12 21s-7-5.5-7-11a7 7 0 0 1 14 0c0 5.5-7 11-7 11z"/><circle cx="12" cy="10" r="2.5"/>'),
  plus: _SVG('<path d="M12 5v14M5 12h14"/>'),
  box: _SVG('<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.2"/><circle cx="15.5" cy="8.5" r="1.2"/><circle cx="8.5" cy="15.5" r="1.2"/><circle cx="15.5" cy="15.5" r="1.2"/>'),
  linked: _SVG('<path d="M9 15 15 9"/><path d="M10.5 5.5 12 4a4 4 0 0 1 5.66 5.66l-1.6 1.6"/><path d="M13.5 18.5 12 20a4 4 0 0 1-5.66-5.66l1.6-1.6"/>'),
};
const icon = (name) => `<span class="ic">${ICONS[name] || ""}</span>`;
function canWriteTable(t) {
  if (!S.me) return false;
  if (S.me.admin) return true;
  const cfg = (S.meta && S.meta.tables && S.meta.tables[t]) || null;
  return cfg ? cfg.perm === "rw" : false;
}
function permListLabel(tables) {
  const tb = tables || [];
  if (tb.includes("*")) return "全部表可编辑";
  if (!tb.length) return "只读";
  return "可编辑 " + tb.map((t) => tblName(S.meta.tables[t]) || t).join("、");
}
function permLabel(me) {
  if (me.admin) return " (管理员)";
  if (me.group_admin) return " (类群管理员)";
  const tb = me.tables || [];
  if (tb.includes("*")) return "";
  if (!tb.length) return " (只读)";
  return " (可编辑 " + tb.length + " 表)";
}
function canWrite() { return canWriteTable(S.t); }
function canWriteAnyTable() {
  return !!(S.me && (S.me.admin || (S.me.tables || []).includes("*") || (S.me.tables || []).length > 0));
}

function token() { return localStorage.getItem("sm_token") || ""; }
function setToken(t) { t ? localStorage.setItem("sm_token", t) : localStorage.removeItem("sm_token"); }
function hideColumn(colName) {
  const set = getHidden();
  set.add(colName);
  setHidden(set);
  const hid = getHidden();
  if ($("hide-count")) $("hide-count").textContent = [...hid].length || "";
  renderGrid();
}
function getHidden() {
  return new Set((S.prefs.hidden || {})[S.t] || []);
}
function setHidden(set) {
  if (!S.prefs.hidden) S.prefs.hidden = {};
  S.prefs.hidden[S.t] = [...set];
  saveHiddenDebounced();
}
let _saveTimer = null;
function saveHiddenDebounced() {
  clearTimeout(_saveTimer);
  _saveTimer = setTimeout(() => {
    savePrefs(["hidden"]);   // 局部合并：只传 hidden，不丢 views/colorder
  }, 600);
}

function getColw() {
  if (!S.prefs.colw) S.prefs.colw = {};
  if (!S.prefs.colw[S.t]) S.prefs.colw[S.t] = {};
  return S.prefs.colw[S.t];
}
function setColw(col, px) {
  const w = getColw();
  if (px == null) delete w[col];
  else w[col] = Math.round(px);
  clearTimeout(S.colwSaveTimer);
  S.colwSaveTimer = setTimeout(() => {
    savePrefs(["hidden", "colw"]);   // 局部合并：列宽不再清空 views
  }, 700);
}

/* 采集事件号联想：input 上挂 datalist，按前缀检索 */
function attachEventSuggest(input) {
  let dl = input._evdl;
  if (!dl) {
    dl = document.createElement("datalist");
    dl.id = "evdl-" + Math.random().toString(36).slice(2, 8);
    input.after(dl);
    input.setAttribute("list", dl.id);
    input._evdl = dl;
  }
  let timer = null;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const q = input.value.trim();
      if (!q || q.length < 1) return;
      try {
        const r = await api(`/api/events-suggest?q=${encodeURIComponent(q)}`);
        dl.innerHTML = (r.items || []).map((it) =>
          `<option value="${esc(it.key)}">${esc(it.summary)}</option>`).join("");
      } catch { }
    }, 250);
  });
}

/* 必填项标红：按服务端规则在标签上加红星；"至少一项"规则加提示 */
function markRequired(scope) {
  const req = curCfg().required_fields || [];
  const oneOf = curCfg().require_one_of || [];
  scope.querySelectorAll("input[data-col], select[data-col]").forEach((input) => {
    const name = input.dataset.col;
    if (req.includes(name)) {
      const lb = input.closest("label") || input.parentElement;
      // 已有星号（构建器 .imp / 之前插入的 .req-star）→ 不重复；
      // 星号插在标签文字行内（input 之前），绝不落到输入框下方（flex 纵向 label 会另起一行）
      if (lb && !lb.querySelector(".req-star") && !lb.querySelector(".imp")) {
        const star = document.createElement("span");
        star.className = "req-star";
        star.textContent = " *";
        star.title = "必填字段";
        let head = lb.querySelector(".lb-head");
        if (!head) {   // 非构建器生成的标签：把首段文字收编进行内容器，星号紧跟文字
          head = document.createElement("span");
          head.className = "lb-head";
          const first = lb.childNodes[0];
          if (first && first.nodeType === 3) { head.textContent = first.textContent.trim(); first.remove(); }
          lb.insertBefore(head, lb.firstChild);
        }
        head.appendChild(star);
      }
    }
  });
  if (oneOf.length) {
    const tip = document.createElement("p");
    tip.className = "hint req-one";
    tip.innerHTML = `<span class="req-star">*</span> 以下字段至少填写一项：${oneOf.map(esc).join(" / ")}`;
    const first = scope.querySelector(`input[data-col="${CSS.escape(oneOf[0])}"]`);
    if (first) {
      const lb = first.closest("label");
      if (lb) lb.after(tip);
    }
  }
}

/* 按编号带出物种信息（UCE Sample Code / 原蛛 MHBU #）：
   在 scopeEl 容器内查找 data-col 输入框，只填空字段 */
function wireNumberLink(scopeEl, tname) {
  const codeCol = tname === "uce" ? "Sample Code" : "MHBU #";
  const ci = scopeEl.querySelector(`input[data-col="${CSS.escape(codeCol)}"]`);
  if (!ci) return;
  const handler = async () => {
    const code = ci.value.trim();
    if (!code || code.length < 3) return;
    try {
      const r = await api(`/api/specimen-link?table=${tname}&code=${encodeURIComponent(code)}`);
      if (!r.found) return;
      let filled = [];
      for (const [k, v] of Object.entries(r.fill || {})) {
        const el = scopeEl.querySelector(`input[data-col="${CSS.escape(k)}"]`);
        if (el && !el.value.trim()) { el.value = v; filled.push(k); }
      }
      if (filled.length) toast(`已按编号带出物种：${r.species}（${filled.join("、")}）`, "ok");
    } catch { }
  };
  let timer = null;
  ci.addEventListener("input", () => {  // 输入中防抖实时匹配
    clearTimeout(timer);
    timer = setTimeout(handler, 450);
  });
  ci.addEventListener("change", handler);
  ci.addEventListener("blur", handler);
}

/* NCBI SRA 直接联动：Sample Code 提交后查唯一命中，回填 Accession number */
async function sraTryFill(scopeGet, code, species, rowObj) {
  if (!code || code.length < 4) return;
  try {
    const r = await api(`/api/sra-lookup?sample=${encodeURIComponent(code)}&species=${encodeURIComponent(species || "")}`);
    if (!r.candidates || !r.candidates.length) return;
    const m = r.candidates.filter((c) => c.code_hit && c.genus_match);
    if (m.length !== 1) return;  // 0 条或歧义交由「SRA 自动匹配」批量工具处理
    const acc = (m[0].runs || []).join("; ") || m[0].srx || "";
    if (!acc) return;
    if (rowObj) {
      if ((rowObj.data["Accession number"] ?? "").toString().trim()) return;
      const changes = { "Accession number": acc };
      await api(`/api/rows/${S.t}/${rowObj.id}`, { method: "PUT", body: JSON.stringify({ changes }) });
      Object.assign(rowObj.data, changes);
      renderGrid();
      toast(`已从 NCBI 带出 SRA：${acc}`, "ok");
    } else {
      const el = scopeGet && scopeGet("Accession number");
      if (el && !el.value.trim()) { el.value = acc; toast(`已从 NCBI 带出 SRA：${acc}`, "ok"); }
    }
  } catch { }
}

/* WAC 伪蝎联想：属名联想 / 在属语境下联想种加词 */
function attachWacGenus(input) {
  let dl = input._wacdl;
  if (!dl) {
    dl = document.createElement("datalist");
    dl.id = "wacdl-" + Math.random().toString(36).slice(2, 8);
    input.after(dl);
    input.setAttribute("list", dl.id);
    input._wacdl = dl;
  }
  let timer = null;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const q = input.value.trim();
      if (!q) return;
      try {
        const r = await api(`/api/wac-suggest?q=${encodeURIComponent(q)}`);
        dl.innerHTML = (r.items || []).map((it) =>
          `<option value="${esc(it.genus)}">${it.syn ? "（异名）" : ""}${esc(it.family || "")}</option>`).join("");
      } catch { }
    }, 250);
  });
}

function attachWacEpithet(input, getGenus) {
  let dl = input._wacdl;
  if (!dl) {
    dl = document.createElement("datalist");
    dl.id = "wacdl-" + Math.random().toString(36).slice(2, 8);
    input.after(dl);
    input.setAttribute("list", dl.id);
    input._wacdl = dl;
  }
  let timer = null;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const q = input.value.trim();
      const genus = (getGenus() || "").trim();
      if (!q || !genus) return;
      try {
        const r = await api(`/api/wac-suggest?q=${encodeURIComponent(q)}&genus=${encodeURIComponent(genus)}`);
        dl.innerHTML = (r.items || []).map((it) =>
          `<option value="${esc(it.epithet)}">${it.syn ? "（异名→" + esc(it.full) + "）" : esc(it.family || "")}</option>`).join("");
      } catch { }
    }, 250);
  });
}

/* 数据字典悬停提示 */
const colDisp = (name) => {
  const c = (S.columns || []).find((x) => x.name === name);
  return (c && c.label) || name;
};

/* 表名：跳蛛/伪蝎/原蛛三张标本表显示名相同，跨表清单一律用带类群后缀的 display_label */
const tblName = (cfg) => (cfg && (cfg.display_label || cfg.label)) || "";

function detTitle(name) {
  return S.docs[name] ? name + " — " + S.docs[name] : name;
}

/* ---------------- 撤销 / 重做 ---------------- */
async function refreshUndo() {
  try {
    const r = await api("/api/undostack");
    S.undoStack = r.items || [];
  } catch { return; }
  const bu = $("btn-undo"), br = $("btn-redo");
  if (!bu || !br) return;
  const may = canWriteAnyTable();
  bu.disabled = !may || !S.undoStack.some((x) => !x.undone);
  br.disabled = !may || !S.undoStack.some((x) => x.undone);
  bu.title = may ? "撤销上一步（Ctrl+Z）" : "只读账号无操作可撤销";
  br.title = may ? "重做（Ctrl+Y）" : "只读账号无操作可重做";
}

async function _applyUndo(mode) {
  const it = S.undoStack.find((x) => mode === "undo" ? !x.undone : x.undone);
  if (!it) return toast(mode === "undo" ? "没有可撤销的操作" : "没有可重做的操作", "err");
  try {
    const r = await api(`/api/undo/${it.batch}`, { method: "POST" }).catch(async (e) => {
      const msg = e.message || "";
      if (mode === "undo" && msg.includes("已撤销过"))
        return await api(`/api/redo/${it.batch}`, { method: "POST" });
      if (msg.includes("整批未执行"))
        throw new Error(msg + "（存在后续修改的冲突行；如需仅回退无冲突行，请通过 API 传 skip_conflicts）");
      throw e;
    });
    const skippedN = (r.skipped_conflicts?.length || 0) + (r.skipped?.length || 0);
    toast(`${mode === "undo" ? "已撤销" : "已重做"}：${it.label} · 恢复 ${r.applied} 处` +
          (skippedN ? `（跳过 ${skippedN} 处冲突/缺失）` : ""), "ok");
    if (S.meta.tables[S.t]) loadTable();
    refreshMetaCounts();
  } catch (e) { toast(e.message, "err"); }
  refreshUndo();
}

function doUndo() { _applyUndo("undo"); }
function doRedo() { _applyUndo("redo"); }

$("btn-undo").onclick = doUndo;
$("btn-redo").onclick = doRedo;
$("btn-undo-list").onclick = (e) => {
  e.stopPropagation();
  const m = $("undo-menu");
  m.hidden = !m.hidden;
  if (m.hidden) return;
  m.innerHTML = S.undoStack.length
    ? S.undoStack.map((it) => `
      <div class="undo-item">
        <div class="ui-info"><b>${esc(it.label)}</b> <span class="hint">${it.n} 行 · ${relTime(it.ts)}</span></div>
        <button class="btn ${it.undone ? "" : "danger"}" data-ub="${esc(it.batch)}" data-umode="${it.undone ? "redo" : "undo"}">${it.undone ? "重做" : "撤销"}</button>
      </div>`).join("")
    : `<p class="hint" style="padding:8px 10px">暂无可撤销的操作</p>`;
  m.querySelectorAll("button[data-ub]").forEach((b) => b.onclick = () => {
    m.hidden = true;
    const mode = b.dataset.umode;
    const it = S.undoStack.find((x) => x.batch === b.dataset.ub);
    if (!it) return;
    api(`/api/${mode}/${it.batch}`, { method: "POST" })
      .then((r) => { toast(`${mode === "undo" ? "已撤销" : "已重做"}：${it.label} · 恢复 ${r.applied} 处`, "ok"); if (S.meta.tables[S.t]) loadTable(); refreshMetaCounts(); refreshUndo(); })
      .catch((e) => toast(e.message, "err"));
  });
};
document.addEventListener("click", (e) => {
  const um = $("undo-menu"); if (um && !e.target.closest(".undo-wrap")) um.hidden = true;
});
document.addEventListener("keydown", (e) => {
  if (e.key === "?" && !e.ctrlKey && !e.metaKey && !e.altKey) {
    const t0 = document.activeElement;
    const typing0 = t0 && (t0.tagName === "INPUT" || t0.tagName === "TEXTAREA" || t0.tagName === "SELECT" || t0.isContentEditable);
    if (!typing0 && $("modal").hidden && $("drawer").hidden) {
      openModal("键盘快捷键", `
        <table class="about-table" style="width:100%">
          <thead><tr><th style="width:190px">按键</th><th>作用</th></tr></thead>
          <tbody>
            <tr><td><b>Enter</b></td><td>提交当前单元格并移到下一行</td></tr>
            <tr><td><b>Tab / Shift+Tab</b></td><td>提交并右移 / 左移一列</td></tr>
            <tr><td><b>↑ ↓ ← →</b></td><td>编辑中提交并上下移动；浏览态移动光标格</td></tr>
            <tr><td><b>F2 / Enter（浏览态）</b></td><td>进入当前格编辑</td></tr>
            <tr><td><b>Esc</b></td><td>取消编辑 / 清空搜索框 / 清除光标</td></tr>
            <tr><td><b>Ctrl+C</b></td><td>复制当前格内容</td></tr>
            <tr><td><b>Ctrl+Shift+C</b></td><td>复制整行（制表符分隔，可粘贴进 Excel）</td></tr>
            <tr><td><b>Ctrl+Z / Ctrl+Y</b></td><td>撤销 / 重做（整批）</td></tr>
            <tr><td><b>Shift+点击勾选框</b></td><td>从上次勾选到本次的区间批量勾选</td></tr>
            <tr><td><b>Ctrl+K</b></td><td>全局搜索：跨所有有权限的表查找关键字</td></tr>
            <tr><td><b>?</b></td><td>打开本帮助</td></tr>
          </tbody>
        </table>`);
      return;
    }
  }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    if ($("modal").hidden && $("drawer").hidden) openGlobalSearch();
    return;
  }
  const t = document.activeElement;
  const typing = t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable);
  const modalOpen = !document.getElementById("modal").hidden || !document.getElementById("drawer").hidden;
  if (e.key === "Tab" && modalOpen) {
    const scope = !document.getElementById("modal").hidden ? $("modal") : $("drawer");
    _trapTabInModal(e, scope);
    return;
  }
  // 详情抽屉连续处理：PageUp/PageDeny 上一条/下一条（输入框/弹窗/组合输入期间不触发）
  if (e.key === "Escape" && !typing) {
    if (!document.getElementById("modal").hidden) { e.preventDefault(); closeModal(); return; }
    if (!document.getElementById("drawer").hidden) {
      e.preventDefault();
      (async () => { if (await closeGate("关闭详情")) closeDrawer(); })();
      return;
    }
  }
  if (!$("drawer").hidden && !typing && document.getElementById("modal").hidden
      && !(e.ctrlKey || e.metaKey) && !e.altKey) {
    if (e.key === "PageUp") { e.preventDefault(); if (typeof navRecord === "function") navRecord(-1); return; }
    if (e.key === "PageDown") { e.preventDefault(); if (typeof navRecord === "function") navRecord(1); return; }
  }
  if (!(e.ctrlKey || e.metaKey) || typing || modalOpen || !canWriteAnyTable()) return;
  const k = e.key.toLowerCase();
  if (k === "z" && !e.shiftKey) { e.preventDefault(); doUndo(); }
  else if (k === "y" || (k === "z" && e.shiftKey)) { e.preventDefault(); doRedo(); }
});

/* 刷新/关闭标签：存在未保存内容时浏览器原生提示（配合表单草稿 500ms 防抖落盘，刷新可恢复） */
window.addEventListener("beforeunload", (e) => {
  if (S.detEditing || (S.editing && dirtyEditing()) || anyFormGuardDirty()) {
    e.preventDefault();
    e.returnValue = "";
  }
});

/* ---------------- API ---------------- */
async function api(path, opts = {}) {
  const headers = Object.assign({ "X-Auth": token() }, opts.headers || {});
  if (opts.body && !(opts.body instanceof FormData)) headers["Content-Type"] = "application/json";
  // 写请求自动携带幂等键：同 ID 同请求体服务端返回原结果（覆盖提交成功但响应丢失），
  // 同 ID 不同请求体被服务端拒绝；网络中断时用同一 ID 重试一次
  if ((opts.method === "PUT" || opts.method === "POST" || opts.method === "DELETE") && !headers["X-Request-Id"]) {
    try { headers["X-Request-Id"] = crypto.randomUUID(); } catch { headers["X-Request-Id"] = String(Date.now()) + Math.random(); }
  }
  api.lastReqId = headers["X-Request-Id"] || null;   // 供「结果待确认」幂等查询
  let r;
  try {
    r = await fetch(path, { ...opts, headers });
  } catch (netErr) {
    if (opts.method === "PUT" || opts.method === "POST") {
      try { r = await fetch(path, { ...opts, headers }); }
      catch (netErr2) { api.lastNetError = true; throw netErr2; }
    } else throw netErr;
  }
  api.lastNetError = false;
  let data = null;
  try { data = await r.json(); } catch { /* 空响应 */ }
  if (r.status === 401) { showLogin(); throw new Error(data?.error || "请先登录"); }
  if (!r.ok) {
    if (r.status === 409 && data?.conflict) {
      const e = new Error(data.error);
      e.conflict = data.conflict;
      throw e;
    }
    if (r.status === 409 && data?.duplicate) {
      const e = new Error(data.error);
      e.duplicate = data.duplicate;
      throw e;
    }
    throw new Error(data?.error || `请求失败 (${r.status})`);
  }
  return data;
}

async function downloadFile(url) {
  const r = await fetch(url, { headers: { "X-Auth": token() } });
  if (r.status === 401) { showLogin(); throw new Error("请先登录"); }
  if (!r.ok) {
    let msg = `下载失败 (${r.status})`;
    try { msg = (await r.json()).error || msg; } catch { }
    throw new Error(msg);
  }
  const blob = await r.blob();
  const cd = r.headers.get("Content-Disposition") || "";
  let name = "导出.xlsx";
  const m = /filename\*=(?:UTF-8'')?([^;]+)/i.exec(cd) || /filename="?([^";]+)"?/i.exec(cd);
  if (m) { try { name = decodeURIComponent(m[1].trim().replace(/^"|"$/g, "")); } catch { name = m[1]; } }
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 8000);
}

const IMG_EXT = /\.(png|jpe?g|gif|webp|bmp)$/i;
const fileUrl = (fid, inline) =>
  `/api/files/${fid}/download?${inline ? "inline=1&" : ""}token=${encodeURIComponent(token())}`;
function lightbox(url, name) {
  const ov = document.createElement("div");
  ov.style.cssText = "position:fixed;inset:0;background:rgba(20,25,35,.85);z-index:500;display:flex;align-items:center;justify-content:center;cursor:zoom-out";
  ov.innerHTML = `<div style="max-width:92vw;max-height:92vh;text-align:center">
    <img src="${url}" style="max-width:90vw;max-height:86vh;border-radius:8px;box-shadow:0 12px 50px rgba(0,0,0,.5)">
    <div style="color:#dfe3ea;font-size:13px;margin-top:10px">${esc(name || "")}（点击任意处关闭）</div></div>`;
  ov.onclick = () => ov.remove();
  document.body.appendChild(ov);
}

function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(() => toast("已复制", "ok")).catch(() => fallbackCopy(text));
  } else fallbackCopy(text);
}
function fallbackCopy(text) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand("copy"); toast("已复制", "ok"); }
  catch { toast("复制失败，请手动选择复制", "err"); }
  ta.remove();
}

/* ---------------- Toast / 确认框 ---------------- */
function toast(msg, type = "") {
  const d = document.createElement("div");
  d.className = "toast " + type;
  d.textContent = msg;
  $("toast-box").appendChild(d);
  setTimeout(() => {
    d.style.animation = "fadeout .22s ease forwards";
    setTimeout(() => d.remove(), 240);
  }, type === "err" ? 5200 : 3200);
}

let _drawerScrollPreModal = null;
const modalLayers = [];   // [{title, node, wide, guard}] —— 嵌套弹窗只关闭当前层
let _modalRestoreFocus = null;
function _modalSetAria() {
  const m = $("modal");
  m.setAttribute("role", "dialog");
  m.setAttribute("aria-modal", "true");
  m.setAttribute("aria-labelledby", "modal-title");
}
function _setAppInert(on) {
  const el = document.getElementById("app-view");
  if (el) { try { el.inert = on; } catch { } }
}
function _focusFirstIn(scope) {
  const el = scope.querySelector(
    'input:not([disabled]):not([readonly]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), button:not([disabled])');
  if (el) { try { el.focus(); } catch { } return true; }
  return false;
}
function _trapTabInModal(e) {
  const focusables = $("modal").querySelectorAll(
    'button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])');
  if (!focusables.length) return;
  const first = focusables[0], last = focusables[focusables.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
}
function _modalFullyClosed() { return !modalLayers.length; }
const formGuards = new Map();  // guardId → {dirty(), canSaveLeave()?, saveLeave()?, keepDraft(), discard()}
function registerFormGuard(id, g) { formGuards.set(id, g); }
function unregisterFormGuard(id) { formGuards.delete(id); }
function anyFormGuardDirty() { for (const g of formGuards.values()) if (g.dirty && g.dirty()) return true; return false; }
function openModal(title, html, opts = {}) {
  const titleEl = $("modal-title"), bodyEl = $("modal-body");
  const prevNode = document.createDocumentFragment();
  while (bodyEl.firstChild) prevNode.appendChild(bodyEl.firstChild);
  modalLayers.push({ title: titleEl.textContent, node: prevNode,
                     wide: $("modal").classList.contains("modal-wide"),
                     guard: opts.guard || null });
  titleEl.textContent = title;
  bodyEl.innerHTML = html;
  if (opts.wide) $("modal").classList.add("modal-wide");
  else $("modal").classList.remove("modal-wide");
  $("modal").hidden = false;
  $("modal-overlay").hidden = false;
  _modalSetAria();
  _setAppInert(true);
  if (!$("drawer").hidden) _drawerScrollPreModal = $("drawer-body").scrollTop;
  if (modalLayers.length === 1 && !_modalRestoreFocus) _modalRestoreFocus = document.activeElement;
  setTimeout(() => { if (modalLayers.length) _focusFirstIn(bodyEl); }, 0);   // 初始焦点；输入框原生按键/输入法不受影响
}
function closeModal() {
  // 关闭顶层；被守卫的层（长表单）先过关闭门——遮罩/Esc/按钮都走这一条契约
  if (!modalLayers.length) return;
  const top = modalLayers[modalLayers.length - 1];
  const tryPop = () => {
    modalLayers.pop();
    const prev = modalLayers[modalLayers.length - 1];
    if (top.guard && top.guard.closed) unregisterFormGuard(top.guard.id);
    if (prev && prev.node.childNodes.length) {
      $("modal-title").textContent = prev.title;
      const bodyEl = $("modal-body");
      bodyEl.innerHTML = "";
      bodyEl.appendChild(prev.node);
      if (prev.wide) $("modal").classList.add("modal-wide");
      else $("modal").classList.remove("modal-wide");
    } else {
      $("modal").hidden = true;
      $("modal-overlay").hidden = true;
      $("modal").classList.remove("modal-wide");
      _setAppInert(false);
      if (_modalRestoreFocus) { try { _modalRestoreFocus.focus(); } catch { } _modalRestoreFocus = null; }
    }
    if (!$("drawer").hidden && _drawerScrollPreModal !== null) {
      $("drawer-body").scrollTop = _drawerScrollPreModal;
      _drawerScrollPreModal = null;
    }
  };
  if (top.guard && typeof top.guard.dirty === "function" && top.guard.dirty()) {
    formGuardConfirm(top.guard, "关闭此弹窗").then((ok) => { if (ok) tryPop(); });
    return;
  }
  tryPop();
}
function setTopModalGuard(guard) {
  if (modalLayers.length) modalLayers[modalLayers.length - 1].guard = guard;
}
function formGuardConfirm(g, action) {
  /* 表单关闭三选一：继续编辑 / 保存并离开（仅当表单支持提交且校验通过；等待结果明确）/
     保留草稿并关闭 / 放弃。弹在受守卫层之上，只关自己这一层。 */
  return new Promise((resolve) => {
    const canSave = typeof g.canSaveLeave === "function" && !!g.canSaveLeave();
    openModal("有未保存的表单内容", `
      <p style="line-height:1.8">表单内容尚未提交（${esc(action)}会离开）。</p>
      <p class="hint">「保留草稿」会把全部已填内容存放在本机浏览器（按账号/表单类型/目标隔离，留存 7 天，不上传服务器，不含令牌/密码/文件内容），下次打开同一表单时会展示保存时间并询问恢复。</p>
      <div class="form-actions">
        <button class="btn primary" id="fg-cont">继续编辑</button>
        ${canSave ? `<button class="btn primary" id="fg-save">保存并离开</button>` : ""}
        <button class="btn" id="fg-keep">保留草稿并关闭</button>
        <button class="btn danger" id="fg-drop">放弃修改</button>
      </div>`);
    $("fg-cont").onclick = () => { closeModal(); resolve(false); };
    if (canSave) {
      $("fg-save").onclick = async () => {
        const ok = await g.saveLeave();        // 等待提交结果明确（成功才关闭）
        resolve(!!ok);
      };
    }
    $("fg-keep").onclick = () => { g.keepDraft(); closeModal(); resolve(true); };
    $("fg-drop").onclick = () => { g.discard(); closeModal(); resolve(true); };
  });
}
async function closeGate(action) {
  /* 统一关闭门：详情字段编辑（阻塞）→ 表格单元格编辑（既有三选一）→ 表单弹窗草稿守卫。
     所有离开路径（✕/遮罩/Esc/切模块/上一条下一条/浏览器导航/退出）都经过这里。 */
  if (S.detEditing) { toast("详情字段修改尚未提交——先提交或按 Esc 放弃，再" + action, "err"); return false; }
  for (const [, g] of [...formGuards.entries()].reverse()) {
    if (!g.dirty || !g.dirty()) continue;
    const ok = await formGuardConfirm(g, action);
    if (!ok) return false;
  }
  if (S.editing && dirtyEditing()) {
    await leaveGuardConfirm(action);
    if (S.editing && dirtyEditing()) return false;
  }
  return true;
}
$("modal-close").onclick = closeModal;
$("modal-overlay").onclick = closeModal;

function confirmBox(msg, { okText = "确定", danger = true } = {}) {
  return new Promise((resolve) => {
    openModal("请确认", `
      <div style="line-height:1.7;white-space:pre-wrap">${msg}</div>
      <div class="form-actions">
        <button class="btn" id="cf-cancel">取消</button>
        <button class="btn ${danger ? "danger" : "primary"}" id="cf-ok">${esc(okText)}</button>
      </div>`);
    $("cf-cancel").onclick = () => { closeModal(); resolve(false); };
    $("cf-ok").onclick = () => { closeModal(); resolve(true); };
  });
}

/* ---------------- 登录 ---------------- */
let loginMode = "login";
let needsSetup = false;
function showLogin() {
  setToken(S.me = null);
  $("app-view").hidden = true;
  $("login-view").hidden = false;
  $("f-username").focus();
  api("/api/auth/me").then((m) => {
    needsSetup = !!m.needs_setup;
    $("f-invite-label").hidden = needsSetup;
    $("login-setup-hint").hidden = !needsSetup;
    if (!needsSetup) switchLoginMode(loginMode);
  }).catch(() => { });
}
function switchLoginMode(m) {
  loginMode = m;
  $("tab-login").classList.toggle("active", m === "login");
  $("tab-register").classList.toggle("active", m === "register");
  $("f-password2-label").hidden = m === "login";
  $("f-invite-label").hidden = m === "login" || needsSetup;
  $("login-setup-hint").hidden = m !== "register" || !needsSetup;
  $("login-submit").textContent = m === "login" ? "登 录" : "注 册";
  $("login-err").hidden = true;
}
$("tab-login").onclick = () => switchLoginMode("login");
$("tab-register").onclick = () => switchLoginMode("register");
$("login-form").onsubmit = async (e) => {
  e.preventDefault();
  const username = $("f-username").value.trim();
  const password = $("f-password").value;
  const invite = $("f-invite").value.trim();
  if (loginMode === "register" && password !== $("f-password2").value) {
    return loginErr("两次输入的密码不一致");
  }
  if (loginMode === "register" && !needsSetup && !invite) {
    return loginErr("注册需要邀请码，请向管理员索取");
  }
  try {
    const r = await api(`/api/auth/${loginMode}`, {
      method: "POST",
      body: JSON.stringify({ username, password, invite }),
    });
    setToken(r.token);
    await enterApp();
    const dl = parseRecordHash();
    if (dl) openDrawerRecord(dl.t, dl.rid).catch((e) => toast(e.message, "err"));
  } catch (err) { loginErr(err.message); }
};
function loginErr(msg) {
  const d = $("login-err");
  d.textContent = msg;
  d.hidden = false;
}

/* ---------------- 应用初始化 ---------------- */
async function enterApp() {
  const me = await api("/api/auth/me");
  if (!me.user) return showLogin();
  try {
    const saved = sessionStorage.getItem("sm_lastview");
    if (saved) S._lastView = JSON.parse(saved);
  } catch { }
  S.me = { username: me.user, admin: me.admin, role: me.role || "editor", group_admin: !!me.group_admin, tables: me.tables || [], group: me.group || "", can_boxes: !!me.can_boxes };
  // 加载服务端偏好（列隐藏等）
  try {
    const pr = await api("/api/prefs");
    if (pr.prefs && pr.prefs.hidden) S.prefs.hidden = pr.prefs.hidden;
    if (pr.prefs && pr.prefs.colw) S.prefs.colw = pr.prefs.colw;
    if (pr.prefs && pr.prefs.views) S.prefs.views = pr.prefs.views;
    if (pr.prefs && pr.prefs.colorder) S.prefs.colorder = pr.prefs.colorder;
    if (pr.prefs && pr.prefs.freeze) S.prefs.freeze = pr.prefs.freeze;
    if (pr.prefs && pr.prefs.view_sel) S.prefs.view_sel = pr.prefs.view_sel;
  } catch { }
  S.meta = await api("/api/meta");
  S.docs = S.meta.docs || {};
  // 组合表定义（v4.07）：已发布且授权给我的 +（构建者）自己的 +（管理员）全部
  try { const ctr = await api("/api/composite/definitions"); S.ctDefs = ctr.definitions || [];
        S.ctCanBuild = !!ctr.can_build; } catch { S.ctDefs = []; S.ctCanBuild = false; }
  // 长开页面检测服务器已更新（v4.01）：运行中的旧脚本会错过新功能/修复（如附件预览条），
  // 且 index 的 ?v= 参数只在页面刷新时生效。发现版本不一致 → 顶部提示条一键刷新。
  {
    const srvVer = String(S.meta.app_version || "");
    const runVer = window.__APP_VERSION__ || "";
    if (runVer && srvVer && runVer !== srvVer && !window.__verBarShown) {
      window.__verBarShown = true;
      try {
        const bar = document.createElement("div");
        bar.style.cssText = "position:sticky;top:0;z-index:450;background:var(--warn-soft,#fdf3e3);color:#8a6420;border-bottom:1px solid #e7dcc0;padding:6px 14px;font-size:13px;text-align:center";
        bar.innerHTML = `系统已更新到 v${srvVer}（当前页面仍是 v${runVer} 旧版本）——<a href="javascript:location.reload()" style="color:var(--primary);font-weight:600">刷新页面</a> 以启用全部功能`;
        document.body.prepend(bar);
      } catch { }
    }
  }
  if (S._lastView && S.meta.tables[S._lastView.t]) {
    setTimeout(async () => {
      switchView(S._lastView.t);
      if (S._lastView.page > 1) { S.page = S._lastView.page; await loadTable(); }
      // 刷新后恢复滚动/勾选/焦点位置（不回到左上角）
      const lv = S._lastView;
      if (lv.sel && lv.sel.length) { lv.sel.forEach((id) => S.sel.add(id)); renderGrid(); }
      if (lv.scroll) {
        const gw = document.querySelector(".grid-wrap");
        if (gw) { gw.scrollLeft = lv.scroll.left || 0; gw.scrollTop = lv.scroll.top || 0; }
      }
      if (lv.cur) {
        const td = document.querySelector(`#grid-body td[data-rid="${lv.cur.rid}"][data-col="${CSS.escape(lv.cur.col)}"]`);
        if (td) { td.classList.add("cur"); S.cur = { rid: lv.cur.rid, col: lv.cur.col }; }
      }
    }, 60);
  }
  refreshUndo();
  setInterval(refreshUndo, 60000);
  document.title = S.meta.system_name;
  $("brand-name").textContent = S.meta.system_name;
  $("login-title").textContent = S.meta.system_name;
  $("user-btn").innerHTML = icon("user") + " " + esc(S.me.username) + permLabel(S.me);
  $("mi-users").hidden = !(S.me.admin || S.me.group_admin);
  $("mi-backup").hidden = !S.me.admin;
    $("ck-geo").hidden = !S.me.admin;
  $("ck-coord").hidden = !S.me.admin;
  $("ck-wsc").hidden = !S.me.admin;
  $("ck-evmerge").hidden = !S.me.admin;
  $("ck-refs").hidden = !S.me.admin;
  $("ck-fields").hidden = !S.me.admin;
  $("ck-wscrefresh").hidden = !S.me.admin;
  $("ck-att").hidden = !S.me.admin;
  $("ck-vacuum").hidden = !S.me.admin;
  $("mi-boxes").hidden = !(S.me.admin || (S.me.can_boxes && (!S.me.group || S.me.group === "spider")));
  $("login-view").hidden = true;
  $("app-view").hidden = false;
  buildTabs();
  buildHistoryFilters();
  switchView("home");
}

function myTables() {
  // meta 已按三态 perms 过滤（不可见的表服务端不返回）
  return S.meta.tables;
}

function renderAbout() {
  const tables = S.meta.tables;
  const total = Object.values(tables).reduce((s, t) => s + t.total, 0);
  const g = S.me.group || "";
  const aboutTableEntries = g ? Object.entries(tables).filter(([t, c]) => !c.taxon_group || c.taxon_group === g || c.taxon_group === "shared") : Object.entries(tables);
  const taxonGroups = S.meta.taxon_groups || [];
  const schema = S.meta.schema || {};
  const appVer = S.meta.app_version || "";
  const schemaVer = schema.version || "";
  const schemaDate = schema.updated || "";
  const wscSync = schema.wsc_sync || "";
  const docs = S.docs || {};
  const fieldEntries = (tn) => (((tables[tn] || {}).columns) || []).map((c) => ({
    name: c.name, label: c.label || c.name, doc: docs[c.name] || "", linked: !!c.linked,
  }));
  const allTables = Object.entries(tables).map(([t, c]) => ({ t, label: tblName(c), key: c.key, total: c.total, group: c.taxon_group || "" }));
  const changelog = S.meta.app_changelog || [];   // 全部版本历史（v3.99 展开浏览）

  /* ---- 目录 ---- */
  const toc = [
    ["quick", "快速上手"],
    ["structure", "数据库结构"],
    ["specimen", "标本录入与管理"],
    ["events", "采集地信息"],
    ["taxonomy", "分类信息"],
    ["dna", "DNA 提取与 UCE 文库"],
    ["other-tables", "其他数据表"],
    ["search", "搜索与筛选"],
    ["edit", "编辑与保存"],
    ["detail", "详情抽屉"],
    ["determination", "鉴定管理"],
    ["lineage", "样本链"],
    ["attachment", "附件"],
    ["movement", "流转与借出"],
    ["lab", "实验板"],
    ["biblio", "文献与引证"],
    ["project", "项目工作集"],
    ["template", "模板导出"],
    ["collab", "协作编辑包"],
    ["recent", "最近操作"],
    ["checkup", "数据体检"],
    ["version", "版本管理与回退"],
    ["history", "变更历史"],
    ["stats", "统计"],
    ["security", "权限与安全"],
    ["deploy", "部署与备份"],
    ["faq", "常见问题"],
    ["changelog", "系统版本"],
  ];
  const tocHtml = `<div class="about-toc">${toc.map(([id, label], i) =>
    `<a href="javascript:void 0" class="about-toc-link" data-toc="${id}"><i>${String(i + 1).padStart(2, "0")}</i>${label}</a>`).join("")}</div>`;

  /* ---- 数据库结构（从 meta 动态构建） ---- */
  const structHtml = allTables.map(({ t, label, key, total: n, group }) => {
    const cols = fieldEntries(t);
    const colRows = cols.map((c) => {
      const tags = [];
      if (c.name === key) tags.push('<span class="st st-saved">主键</span>');
      if (c.linked) tags.push('<span class="st st-readonly">关联只读</span>');
      if (c.doc) tags.push(`<span class="hint">${esc(c.doc.slice(0, 80))}</span>`);
      return `<tr><td><code>${esc(c.name)}</code></td><td>${esc(c.label)}</td><td>${tags.join(" ")}</td></tr>`;
    }).join("");
    return `<details${cols.length <= 20 ? " open" : ""}><summary style="cursor:pointer;font-weight:bold">${esc(label)}（${t}，${n} 行，主键：${esc(key)}）</summary>
      <table class="about-table" style="width:100%;margin:4px 0"><thead><tr><th>字段名</th><th>显示名</th><th>说明</th></tr></thead><tbody>${colRows}</tbody></table></details>`;
  }).join("");

  $("view-about").innerHTML = `
  <div class="about-wrap">
    <div class="about-hero">
      <img src="/static/logo.png" style="width:56px;height:56px;border-radius:12px" alt="实验室徽标">
      <div class="about-hero-txt">
        <h1>标本信息管理系统</h1>
        <p class="login-sub">多人协作 · 全程可追溯 · 版本可回退</p>
      </div>
    </div>

    <div style="display:flex;gap:6px;flex-wrap:wrap;margin:10px 0;padding:8px 0;border-bottom:1px solid var(--border)">
      ${tocHtml}
    </div>

    <!-- ========== 快速上手 ========== -->
    <div class="about-card" id="about-quick">
      <h2 style="font-size:16px;margin:0 0 10px">快速上手（3 步开始使用）</h2>
      <table class="about-table" style="width:100%">
        <thead><tr><th style="width:60px">步骤</th><th style="width:180px">做什么</th><th>具体操作</th></tr></thead>
        <tbody>
          <tr><td><b>1</b></td><td>找到标本</td>
            <td>点击左侧「跳蛛: 河北大学标本信息」进入标本表格 → 在顶部搜索框输入编号或物种名 → 表格实时筛选</td></tr>
          <tr><td><b>2</b></td><td>录入新标本</td>
            <td>点击工具栏「+ 新增行」→ 在新行中逐列填写（编号自动补全前缀）→ 离开单元格自动保存 → 保存后颜色变绿</td></tr>
          <tr><td><b>3</b></td><td>查看详情</td>
            <td>点击行尾 👁 图标 → 右侧弹出详情抽屉，可查看/编辑所有字段、修改历史、历次鉴定、附件、流转记录</td></tr>
        </tbody></table>
      <p class="hint" style="margin-top:6px">更多操作：勾选行 → 导出菜单「导出向导」/「创建协作包」/「加入项目」；「管理」菜单 → 版本管理/列显示/回收站；工具栏「任务视图」一键切换列布局。</p>
    </div>

    <!-- ========== 数据库结构 ========== -->
    <div class="about-card" id="about-structure">
      <h3>数据库结构（共 ${allTables.length} 张表，${total.toLocaleString()} 行数据）</h3>
      <p class="hint">每张表是一个独立的数据集合；表之间通过「采集事件号」「DNA 凭证编号」等字段关联。
      下方按表逐一列出全部字段，标注了主键和关联只读字段。字段名是数据库内部使用的键，界面上显示的是中文语义名。</p>
      ${structHtml}
    </div>

    <!-- ========== 标本录入与管理 ========== -->
    <div class="about-card" id="about-specimen">
      <h3>标本录入与管理</h3>
      <table class="about-table" style="width:100%"><thead><tr><th>功能</th><th>操作方法</th></tr></thead><tbody>
        <tr><td>新增标本</td><td>工具栏「+ 新增行」→ 在新行逐列填写。编号输入数字自动补前缀（如 497 → MHBU-ARA-00000497）。离开单元格自动保存，绿色=已保存</td></tr>
        <tr><td>编辑标本</td><td>直接点击单元格修改 → 离开自动保存；或行尾 👁 打开详情抽屉逐字段编辑。保存冲突时系统提示三选一（保存/保留草稿/放弃）</td></tr>
        <tr><td>删除标本</td><td>行尾 🗑 图标 → 移入回收站（可还原）。彻底删除在回收站视图操作</td></tr>
        <tr><td>属名自动补全</td><td>填属名（Genus）时 WSC 联想 + 自动带出科/亚科/族/亚族等上级分类；分类表未覆盖的属自动从 WSC 属名录补科</td></tr>
        <tr><td>种名自动补全</td><td>填种名（FinalSpeciesName）时 WSC 种名录联想；异名自动转接受名；未收录名称拒绝保存；属自动联动</td></tr>
        <tr><td>暂定名（cf./sp.）</td><td>暂定名联想支持 sp. nov. 场景；cf. 按钮标注鉴定不确定（淡红底色）；未收录名称保存前弹提醒并列出相近名称</td></tr>
        <tr><td>编号查重</td><td>输入已有编号将被拦截；服务端同步拦截</td></tr>
        <tr><td>标本盒自动匹配</td><td>填入编号自动匹配对应标本盒；盒位示意图显示 6×6 或 10×10 孔位</td></tr>
        <tr><td>Shift 范围选择</td><td>勾选某行后按住 Shift 点击另一行，两行之间全部自动勾选（适合批量操作前快速选中）</td></tr>
        <tr><td>以此行新增</td><td>详情抽屉「以此行新增」：复制该行全部非空字段，编号自动 +1，适合同批标本连续录入</td></tr>
      </tbody></table>
    </div>

    <!-- ========== 采集地信息 ========== -->
    <div class="about-card" id="about-events">
      <h3>采集地信息</h3>
      <table class="about-table" style="width:100%"><thead><tr><th>功能</th><th>操作方法</th></tr></thead><tbody>
        <tr><td>采集事件</td><td>每条采集事件包含：事件号、国家/省/市/区县/地点、经纬度、采集人、采集日期、栖息地等。标本通过「采集事件号」字段关联到事件，省份、经纬度等自动带出</td></tr>
        <tr><td>事件号规则</td><td>格式如 YNDH-19-06、CQCK-22-1101（地区缩写-年份-序号）；标本表填入事件号后自动带出省份、经纬度、采集人、日期</td></tr>
        <tr><td>关联只读</td><td>标本表中省份、经纬度等采集地字段是<b>关联只读</b>——需要修改请到「采集地信息」表修改事件本身</td></tr>
      </tbody></table>
    </div>

    <!-- ========== 分类信息 ========== -->
    <div class="about-card" id="about-taxonomy">
      <h3>分类信息</h3>
      <p class="hint">按 WSC（世界蜘蛛名录）骨架维护：目→科→亚科→族→亚族→属。标本表填属名后自动带出上级分类。未收录的属自动从 WSC 补科。属名修改后标本的种名自动联动。</p>
    </div>

    <!-- ========== DNA 提取与 UCE 文库 ========== -->
    <div class="about-card" id="about-dna">
      <h3>DNA 提取与 UCE 文库</h3>
      <table class="about-table" style="width:100%"><thead><tr><th>功能</th><th>操作方法</th></tr></thead><tbody>
        <tr><td>DNA 提取台账</td><td>记录每管提取的凭证号（DNAVoucherID）、组织来源、保存浓度、Qubit 定量、提取时间等。凭证号是关联标本与文库的核心键</td></tr>
        <tr><td>UCE 文库</td><td>每条记录含 Sample Key（内部主键）、UCE library Code、Sample Code（= DNAVoucherID）、SRR 号等；同凭证可重复建库（一对多）</td></tr>
        <tr><td>SRA 自动匹配</td><td>UCE 工具栏「SRA 自动匹配」：按 Sample Code 在 NCBI SRA 检索，属名一致且唯一命中时自动填 Accession number；多候选供挑选；整批可撤销</td></tr>
        <tr><td>附件跨表共享</td><td>UCE 与 DNA 提取按 DNAVoucherID 关联——UCE 样本上传的照片，DNA 提取对应记录也能看到</td></tr>
      </tbody></table>
    </div>

    <!-- ========== 其他数据表 ========== -->
    <div class="about-card" id="about-other-tables">
      <h3>其他数据表</h3>
      <table class="about-table" style="width:100%"><thead><tr><th>表</th><th>用途</th></tr></thead><tbody>
        <tr><td>伪蝎等: 河北大学标本信息</td><td>伪蝎类群主表，仅伪蝎组用户可见和操作</td></tr>
        <tr><td>伪蝎等: DNA 提取信息</td><td>伪蝎类群的 DNA 提取台账（结构同跳蛛 DNA 提取表，凭证标本位置对应伪蝎标本位置编号），仅伪蝎组用户可见和操作</td></tr>
        <tr><td>原蛛: 河北大学标本信息</td><td>原蛛类群主表（KYUARA # 编号），逻辑同跳蛛表</td></tr>
        <tr><td>表名的类群前缀</td><td>数据录入各表按「类群: 机构/用途」命名（跳蛛/伪蝎等/原蛛），同机构的标本表靠前缀区分；后续新增其它类群的 FDNA 提取等表沿用同一惯例</td></tr>
        <tr><td>UDI i5 / i7 条码</td><td>UDI 条码序列对照表（96 行/表）；实验板模块按这些序列校验混池组合</td></tr>
      </tbody></table>
    </div>

    <!-- ========== 搜索与筛选 ========== -->
    <div class="about-card" id="about-search">
      <h3>搜索与筛选</h3>
      <table class="about-table" style="width:100%"><thead><tr><th>功能</th><th>操作方法</th></tr></thead><tbody>
        <tr><td>全表搜索</td><td>顶部搜索框：输入编号/物种名/地名/事件号等，所有列（含关联带出）实时匹配；编号查询精确/前缀命中优先排序</td></tr>
        <tr><td>列筛选</td><td>点击列头漏斗图标 → 勾选要保留的值（支持「（空）」=空值）、输入包含文本、或筛选为空/不为空；多列筛选可叠加</td></tr>
        <tr><td>预设视图</td><td>工具栏「任务视图」下拉一键切换列集合与排序（如跳蛛鉴定、采集信息、借出中）</td></tr>
        <tr><td>筛选标签</td><td>筛选条显示命中数/全表数/可清除的条件标签/分享视图入口；点击标签可快速移除单个条件</td></tr>
        <tr><td>视图分享</td><td>「分享视图」生成定义文本（只含筛选+列集合+列序），对方粘贴后按自身权限重算生效</td></tr>
      </tbody></table>
    </div>

    <!-- ========== 编辑与保存 ========== -->
    <div class="about-card" id="about-edit">
      <h3>编辑与保存</h3>
      <table class="about-table" style="width:100%"><thead><tr><th>功能</th><th>操作方法</th></tr></thead><tbody>
        <tr><td>表格编辑</td><td>直接点击单元格修改 → 离开自动保存；颜色反馈：黄色=保存中，绿色=已保存，红色=保存失败</td></tr>
        <tr><td>保存冲突</td><td>另一人先改了同一单元格时系统提示三选一：保存修改（覆盖）/ 保留草稿 / 放弃修改</td></tr>
        <tr><td>草稿</td><td>未提交修改按 账号/表/记录/字段/版本 隔离存放在本机浏览器（7 天，不上传）；回同一单元格会询问恢复；登出清除</td></tr>
        <tr><td>撤销/重做</td><td>Ctrl+Z 撤销 / Ctrl+Y 重做（工具栏 ↶↷ 同）；只影响自己的操作</td></tr>
        <tr><td>批量修改</td><td>勾选多行 → 工具栏批量按钮 → 对选中行统一修改同一字段；逐行以行版本条件更新</td></tr>
        <tr><td>请求超时/断网</td><td>提交后网络中断时显示「结果待确认」→ 系统凭确定性请求 ID 查询服务器真实结果，已成功的不会重复提交；网络恢复后可安全重试</td></tr>
        <tr><td>输入法</td><td>中文输入法组合输入期间不触发保存和 Enter 移动——组合结束后正常操作</td></tr>
      </tbody></table>
    </div>

    <!-- ========== 详情抽屉 ========== -->
    <div class="about-card" id="about-detail">
      <h3>详情抽屉</h3>
      <p class="hint">行尾 👁 打开右侧抽屉，显示全部字段（分组排版）、附件、历次鉴定、被引文献、流转记录、样本链等。</p>
      <table class="about-table" style="width:100%"><thead><tr><th>功能</th><th>操作方法</th></tr></thead><tbody>
        <tr><td>字段快速定位</td><td>顶部搜索框输入字段名/说明关键字，匹配行高亮其余隐藏</td></tr>
        <tr><td>字段编辑</td><td>点击字段值直接编辑（与表格同一保存链路）；Esc 放弃</td></tr>
        <tr><td>上一条/下一条</td><td>顶部固定头「↑」「↓」按钮或 PageUp/PageDown 键在当前筛选范围内连续切换；显示位置「第 i / N 条」</td></tr>
        <tr><td>深链</td><td>浏览器地址栏含 #/r/表/ID 可直接打开该记录并分享给有权限同事；刷新/前进/后退均可用</td></tr>
        <tr><td>修改历史</td><td>「修改历史」查看此行每次修改的操作人/时间/字段/旧值→新值</td></tr>
        <tr><td>历次鉴定</td><td>「历次鉴定」按序展示每条鉴定的名称原文/接受名/鉴定人/日期/依据/名录收录状态</td></tr>
        <tr><td>附件</td><td>「附件」上传/下载/预览；支持拖入文件或 Ctrl+V 粘贴图片（多选）；上传自动重命名为「标本编号_原文件名」；跨表共享标注实际归属</td></tr>
        <tr><td>被引文献</td><td>「被引文献」展示引用此标本的文献列表（标题/DOI/原文引用/原文编号）</td></tr>
        <tr><td>流转记录</td><td>「登记流转」和「流转记录」查看入库/移盒/取样/借出/归还/转赠/损耗全链路</td></tr>
        <tr><td>样本链</td><td>UCE/DNA 提取行显示「样本链」：凭证→提取→文库→SRR 全链路；未匹配分五类处理</td></tr>
      </tbody></table>
    </div>

    <!-- ========== 鉴定管理 ========== -->
    <div class="about-card" id="about-determination">
      <h3>鉴定管理</h3>
      <table class="about-table" style="width:100%"><thead><tr><th>功能</th><th>操作方法</th></tr></thead><tbody>
        <tr><td>鉴定工作台</td><td>侧边栏「鉴定工作台」：按「暂定/待鉴定/cf. 待复查/缺采集信息/DNA 断链/借出中」分类队列，逐条处理</td></tr>
        <tr><td>名录校准</td><td>数据体检 →「按 WSC 校准物种名」：异名→接受名预览→逐条/成批接受（受保护行跳过并列原因）；接受行可随批次撤销</td></tr>
        <tr><td>DetStatus</td><td>鉴定状态字段：待鉴定/暂定/已复核/有争议；改为「已复核」自动登记确认人</td></tr>
        <tr><td>DetLocked</td><td>人工锁定后名录自动校准不覆盖；已有确认/依据的鉴定不被覆盖</td></tr>
      </tbody></table>
    </div>

    <!-- ========== 样本链 ========== -->
    <div class="about-card" id="about-lineage">
      <h3>样本链</h3>
      <p class="hint">UCE/DNA 提取详情页「样本链」：呈现 凭证号→同标本其他提取（一对多）→UCE 文库→SRR accession 与标本侧引用。UCE 未匹配分五类（可追溯/编号差异候选/待补历史/外部来源/未明），候选绝不自动选择——人工确认别名后参与匹配。</p>
    </div>

    <!-- ========== 附件 ========== -->
    <div class="about-card" id="about-attachment">
      <h3>附件（标本照片与文件）</h3>
      <p class="hint" style="margin:0 0 6px"><b>这是什么：</b>给标本挂照片、图谱、论文 PDF 等文件的地方。照片直接显示缩略图可点击放大，PDF 可在系统内预览。</p>
      <p class="hint" style="margin:0 0 6px"><b>什么时候用：</b>拍了解剖图、 habitat 照、玻片照想永远跟着标本走的时候；把鉴定依据的文献页扫描件挂在标本上的时候。文件跟着标本走，不是散落在某台电脑的文件夹里。</p>
      <p class="hint" style="margin:0 0 8px"><b>怎么用：</b>打开标本详情 → 点「附件」→ 把文件拖进投放区、直接 Ctrl+V 粘贴截图，或点击选择文件（可多选；自动改名为「标本编号_原文件名」，重名自动加序号；剪贴板图片按「粘贴_日期_时间」命名）。删错了不用慌：删除只是移入回收站，30 天内可还原。同一凭证号（JXZ…）的标本、DNA 提取、UCE 文库三处共享附件——在标本行传的照片，DNA 提取和 UCE 的详情里也能看到，标有「来自标本」。</p>
      <table class="about-table" style="width:100%"><thead><tr><th>细节</th><th>说明</th></tr></thead><tbody>
        <tr><td>上传安全</td><td>暂存→校验（SHA-256 摘要）→发布；失败自动清理可重试，不会留下坏文件</td></tr>
        <tr><td>谁可以删</td><td>按文件实际归属表判权；跨表共享的文件只能由归属方删除</td></tr>
      </tbody></table>
    </div>

    <!-- ========== 流转与借出 ========== -->
    <div class="about-card" id="about-movement">
      <h3>流转与借出</h3>
      <p class="hint" style="margin:0 0 6px"><b>这是什么：</b>标本的「行程记录本」。入库、换盒子、取样、借给合作者、归还、送人、损耗——凡是标本发生了移动或状态变化，都在这里记一笔。它不改动标本数据本身，只是叠加一层历史。</p>
      <p class="hint" style="margin:0 0 6px"><b>什么时候用：</b>合作馆借走一批模式标本半年（借出→到期提醒→归还，随借随还部分归还都支持）；标本从旧盒搬到新盒（移盒会自动更新盒号/孔位字段并检查孔位占用）；取样做破坏性实验（记清楚取了多少，剩下的数量系统帮你算）。</p>
      <p class="hint" style="margin:0 0 8px"><b>怎么用：</b>「流转与借出」页 →「登记流转」→ 选类型 → 粘贴一批标本编号（实体选择器会逐条核对）→ 填对方/期限/凭据 → 登记。借出去的东西在借出台账一眼可见谁借的、到期没还；记错了单条可「回退」（如果后面已经有相关改动，系统会阻止并建议登记纠正事件，保证历史不说不清）。</p>
      <table class="about-table" style="width:100%"><thead><tr><th>细节</th><th>说明</th></tr></thead><tbody>
        <tr><td>七种类型</td><td>入库 / 移盒 / 取样 / 借出 / 归还 / 转赠 / 损耗；借出未归还时不能重复借出、移盒需确认</td></tr>
        <tr><td>数量口径</td><td>未知数量保持未知不当 0；归还可部分归还，系统计算仍在外数量</td></tr>
      </tbody></table>
    </div>

    <!-- ========== 实验板 ========== -->
    <div class="about-card" id="about-lab">
      <h3>实验板</h3>
      <p class="hint" style="margin:0 0 6px"><b>这是什么：</b>分子实验的台面账本。哪块板哪个孔放了哪只标本、混池里混了谁、用的什么条码组合、送测时 Quant 结果多少——从标本变成数据之前的实验室过程都在这里。</p>
      <p class="hint" style="margin:0 0 6px"><b>什么时候用：</b>提取完 DNA 准备建 UCE 文库，要把 96 孔板的排布记录下来；给一批标本建混池送测（同池条码组合不能重复，系统自动校验）；送出去测序时把批次「标记送测」绑定版本——之后板子再怎么改，送测时那一刻的状态永远可查。</p>
      <p class="hint" style="margin:0 0 8px"><b>怎么用：</b>按流程走：创建实验批次 → 建板（96/384 孔）→ 在孔位图上点格子占孔（样本/对照/空白，粘标本编号即自动解析）→ 需要混池就建混池加成员（UDI5+UDI7）→ 登记条码对与定量 QC → 送测。孔位支持批量预览后一次提交，错位冲突会提前拦下。</p>
      <table class="about-table" style="width:100%"><thead><tr><th>细节</th><th>说明</th></tr></thead><tbody>
        <tr><td>条码原样</td><td>i5/i7 源序列照录不覆盖；导出时按实验室配置转换格式</td></tr>
        <tr><td>定量多次</td><td>同一孔多次测量可都保存，采用哪次需写明理由</td></tr>
      </tbody></table>
    </div>

    <!-- ========== 文献与引证 ========== -->
    <div class="about-card" id="about-biblio">
      <h3>文献与引证</h3>
      <p class="hint" style="margin:0 0 6px"><b>这是什么：</b>论文和标本之间的双向桥。哪篇论文引用了哪些标本（引证）、哪只标本被哪些论文提过（被引），随时双向可查；登记引证那一刻标本的现状会被「冻结」，将来名字改了也能看到论文发表时写的是什么。</p>
      <p class="hint" style="margin:0 0 6px"><b>什么时候用：</b>审稿人/读者问「这篇论文的材料清单到底有哪些标本」；整理新论文的 Material examined 段落（系统按分类学惯例自动聚合生成可直接粘贴的文本）；新种描述发表后把原文记述挂到对应标本上，十年后也能对上。</p>
      <p class="hint" style="margin:0 0 8px"><b>怎么用：</b>「文献与引证」页 →「新增文献」录一篇（老文献没有 DOI 就空着，系统不虚构标识）→ 文献行「登记引证」粘贴标本编号、填原文的材料记述和原文编号 → 需要时「材料清单」查看全部引证，或一键生成 Material examined 文本。反过来：打开任何一只标本的详情，能看到它被哪些论文引用过。</p>
      <table class="about-table" style="width:100%"><thead><tr><th>细节</th><th>说明</th></tr></thead><tbody>
        <tr><td>冻结与并列</td><td>引证时冻结标本版本；材料清单里原文编号与当前接受名并列，历史与现状都看得见</td></tr>
        <tr><td>防重复</td><td>同一文献重复引用同一标本会被唯一约束拦下</td></tr>
      </tbody></table>
    </div>

    <!-- ========== 项目工作集 ========== -->
    <div class="about-card" id="about-project">
      <h3>项目工作集</h3>
      <p class="hint" style="margin:0 0 6px"><b>这是什么：</b>为一件具体的事（一篇论文、一个课题、一次送测）圈出来的一组标本，配上项目内的备注和状态。它不改标本本身的任何数据——相当于一个轻量的「工作夹」。</p>
      <p class="hint" style="margin:0 0 6px"><b>什么时候用：</b>论文要投稿了，把涉及的几百号标本圈成一个项目（圈定后导出向导直接取这批，不用每次重新筛选）；形态学连续测量跨几周（成员行上记「已测/未测」进度，写在项目里不弄脏主库）；做系统发育分析想固定一个明确的标本集合不随入库新增而变化（固化成员版本）。</p>
      <p class="hint" style="margin:0 0 8px"><b>怎么用：</b>「项目工作集」→「新建项目」：固定项目（逐批粘贴编号纳入）或动态项目（用筛选条件定义，符合的自动进）。详情里「纳入成员」随时补号、成员行可写项目内备注。数据定稿后「冻结」生成受保护版本——之后导出时可选「当前数据」或「冻结版本」，投稿版与后续修订永不混淆。</p>
      <table class="about-table" style="width:100%"><thead><tr><th>细节</th><th>说明</th></tr></thead><tbody>
        <tr><td>动态 vs 固定</td><td>动态项目随入库自动扩展；「固化成员版本」把当时结果定下来不再变</td></tr>
        <tr><td>冻结口径</td><td>只含确认纳入的成员（已移除/排除不算）；采集地/分类关联内容一并冻结</td></tr>
      </tbody></table>
    </div>

    <!-- ========== 模板导出 ========== -->
    <div class="about-card" id="about-template">
      <h3>模板导出</h3>
      <p class="hint" style="margin:0 0 6px"><b>这是什么：</b>把库里的数据变成能交出去的表格。期刊要求什么列、什么格式，就按什么模板导——导一次存一个模板，以后一键复用。</p>
      <p class="hint" style="margin:0 0 6px"><b>什么时候用：</b>论文投稿要 Material examined 表（内置模板）；合作者要演化分析元数据（另一套列）；期刊排版要求特殊列序与命名（自建模板，可视化编辑字段不用懂任何代码）。每次导出前系统会先「体检」：哪些行缺关键数据、缺什么，先看清楚再决定导不导。</p>
      <p class="hint" style="margin:0 0 8px"><b>怎么用：</b>表格勾选若干行（或从项目/导出菜单进）→「导出向导」三步走：①选范围（勾选的/当前筛选全部/项目成员）②选模板（不选则默认全部列）③检查预览（缺什么一目了然，确认后生成）。支持 XLSX/CSV/TSV，文件自带 README 说明数据口径与导出时点。</p>
      <table class="about-table" style="width:100%"><thead><tr><th>细节</th><th>说明</th></tr></thead><tbody>
        <tr><td>起始模板</td><td>三套通用模板开箱即用；「以此新建」复制一份改成自己的最快</td></tr>
        <tr><td>防呆</td><td>范围固化：预览后数据变化不会悄悄混入导出；编号按文本保存防丢前导零；公式注入防护</td></tr>
      </tbody></table>
    </div>

    <!-- ========== 协作编辑包 ========== -->
    <div class="about-card" id="about-collab">
      <h3>协作编辑包</h3>
      <p class="hint" style="margin:0 0 6px"><b>这是什么：</b>给没有账号的合作者（外校专家、研究生）发一份 Excel，让他们在熟悉的表格里改数据，收回后系统自动合并——不用开账号，也不用担心覆盖别人的修改。</p>
      <p class="hint" style="margin:0 0 6px"><b>什么时候用：</b>请外部专家批量鉴定/修订一批标本的字段；让研究生在本机离线整理采集信息再一次性交回。你能精确控制对方能改哪些列（身份列和关联列天然锁定），其余列想开就开。</p>
      <p class="hint" style="margin:0 0 8px"><b>怎么用：</b>「协作编辑包」→「新建协作包」→ 粘贴标本编号、勾选允许编辑的列 → 下载工作簿发给对方 → 对方改完传回 →「回传合并」。两边都改过的字段会进入冲突审阅：每个冲突显示 基线/当前/回传 三个值，逐项决定「保留当前」还是「接受回传」，绝不静默覆盖。</p>
      <table class="about-table" style="width:100%"><thead><tr><th>细节</th><th>说明</th></tr></thead><tbody>
        <tr><td>安全性</td><td>工作簿只含包 ID 与数据，不含任何账号/令牌；管理员可收回包（收回后回传被拒）</td></tr>
        <tr><td>冲突持久</td><td>冲突审阅未处理完可以放着，随时继续；提交时逐项重检，审阅期间的新修改不会被旧决策覆盖</td></tr>
      </tbody></table>
    </div>

    <!-- ========== 最近操作 ========== -->
    <div class="about-card" id="about-recent">
      <h3>最近操作</h3>
      <p class="hint" style="margin:0 0 6px"><b>这是什么：</b>你做过的批量操作的「已发送邮件箱」。导入、协作合并、批量修改、撤销——每件事的结果都在这里留一张卡，错过了页面提示也能回来找。</p>
      <p class="hint" style="margin:0 0 6px"><b>什么时候用：</b>导入 200 行后关了页面才想起没看清结果；网络断了不确定那次保存到底成没成；想撤销昨天那次批量修改但记不清动了哪些行。</p>
      <p class="hint" style="margin:0 0 8px"><b>怎么用：</b>打开「最近操作」→ 找到那张卡 →「查看明细」逐行看改了什么（点编号直达原记录）→ 要反悔就「回退此批次的修改」（先预览，逐项确认；他人后续的修改不会被误伤）。保存时断网了？在「按请求 ID 确认」里输入请求 ID，系统告诉你那次写入到底成没成，避免重复提交。</p>
    </div>

    <!-- ========== 数据体检 ========== -->
    <div class="about-card" id="about-checkup">
      <h3>数据体检</h3>
      <p class="hint" style="margin:0 0 6px"><b>这是什么：</b>全库数据的「年度体检报告+门诊」。24 条规则自动扫描：重复编号、危险日期、失配引用、缺失坐标……按严重程度分级列出，能自动修的预览后一键修，拿不准的转成工单人工处理。</p>
      <p class="hint" style="margin:0 0 6px"><b>什么时候用：</b>大批量导入之后（先体检再放心用）；季度性数据整理（把缺失资料队列清一清）；发现某些采集地信息格式五花八门（地名标准化、坐标转小数度一键整理）。</p>
      <p class="hint" style="margin:0 0 8px"><b>怎么用：</b>「数据体检」页 →「扫描」看总览 → 点进各类问题逐条处理；工具菜单里还有专项工具：合并重复采集事件（预览影响后合并）、坐标转小数度（约 1.1 万处可自动转、歧义的列清单人工核对）、地名标准化、WSC 校准（异名→接受名）。所有修复都走统一写入：可预览、可撤销、留审计。</p>
      <table class="about-table" style="width:100%"><thead><tr><th>原则</th><th>说明</th></tr></thead><tbody>
        <tr><td>绝不猜测</td><td>歧义坐标、缺失地点、危险日期不自动改——解释权在研究者，系统只列证据</td></tr>
      </tbody></table>
    </div>

    <!-- ========== 版本管理与回退 ========== -->
    <div class="about-card" id="about-version">
      <h3>版本管理与回退</h3>
      <p class="hint" style="margin:0 0 6px"><b>这是什么：</b>整表的「存档点」。就像游戏里打 Boss 前先存档——大改动之前存一份，改砸了随时读档回来。</p>
      <p class="hint" style="margin:0 0 6px"><b>什么时候用：</b>论文投稿前存一份「投稿版」；要跑一个大胆的批量修改之前先存档；有人手滑删了一批数据需要整表回到上周（回收站只管单条，这时用整表版本）。</p>
      <p class="hint" style="margin:0 0 8px"><b>怎么用：</b>管理菜单「版本管理」→「保存快照」起个名。之后随时「对比」（和当前逐条比差异）或「恢复」（恢复前系统自动再存一份保护快照，所以恢复本身也可撤销）。只想撤销某一次批量操作？去「最近操作」按批次回退更精准。</p>
      <table class="about-table" style="width:100%"><thead><tr><th>细节</th><th>说明</th></tr></thead><tbody>
        <tr><td>删除保护</td><td>标记「受保护/发布」的版本不会被自动清理；管理员强删需 force 留痕</td></tr>
        <tr><td>范围口径</td><td>单表快照；关联列（省份等实时取自事件表）随来源表现状显示，不在单表恢复范围内</td></tr>
      </tbody></table>
    </div>

    <!-- ========== 变更历史 ========== -->
    <div class="about-card" id="about-history">
      <h3>变更历史</h3>
      <p class="hint" style="margin:0 0 6px"><b>这是什么：</b>全库每一条修改的流水账，且防赖账。谁、什么时候、把哪个字段从什么改成什么，永久留痕；记录以哈希链环环相扣，任何人事后改动或删除都会被发现。</p>
      <p class="hint" style="margin:0 0 6px"><b>什么时候用：</b>发现某个字段值不对，想查「是谁、哪天改的」；两人对某次修改有分歧，需要对质原始记录；论文数据被质疑，要出示完整的修改轨迹。</p>
      <p class="hint" style="margin:0 0 8px"><b>怎么用：</b>「变更历史」页按表/操作人/日期/关键字筛选；「校验审计链」一键验证全部记录未被篡改。采集事件行的历史受触发器强制保护——不可修改、不可删除。</p>
    </div>

    <!-- ========== 统计 ========== -->
    <div class="about-card" id="about-stats">
      <h3>统计</h3>
      <p class="hint" style="margin:0 0 6px"><b>这是什么：</b>库里家底的一页速览。按科/省份/年份/模式标本类型的分布图，配上说得清道得明的计数口径。</p>
      <p class="hint" style="margin:0 0 6px"><b>什么时候用：</b>写基金申请要一句「本馆收藏跳蛛 X 科 Y 属 Z 号」；年度汇报要采集量趋势图；论文方法部分要交代标本数量与统计口径。</p>
      <p class="hint" style="margin:0 0 8px"><b>怎么用：</b>侧边栏「统计」直接看。每个数字都带定义、分母与排除条件（如「独立采集事件数：无事件号的记录单独统计，不当新事件」），统计范围只按你的权限算；「下载数据」导出 CSV 附口径说明，可直接进论文。</p>
    </div>

    <!-- ========== 权限与安全 ========== -->
    <div class="about-card" id="about-security">
      <h3>权限与安全</h3>
      <table class="about-table" style="width:100%"><thead><tr><th>功能</th><th>操作方法</th></tr></thead><tbody>
        <tr><td>三级角色</td><td>管理员（全部）/ 类群管理员（指定类群成员管理与邀请）/ 学生（逐表三态：可编辑/只读/不可见）</td></tr>
        <tr><td>邀请码</td><td>管理员或类群管理员生成 → 协作者注册页填入即获得对应权限，无需逐个建号</td></tr>
        <tr><td>审计链</td><td>每次写入留审计事件（操作者/时间/字段/旧值→新值/来源/权限快照）；哈希链防篡改；「变更历史→校验审计链」一键验证</td></tr>
        <tr><td>敏感信息</td><td>令牌/口令/会话信息不入审计、不入附件、不入导出文件</td></tr>
      </tbody></table>
    </div>

    <!-- ========== 部署与备份 ========== -->
    <div class="about-card" id="about-deploy">
      <h3>部署与备份</h3>
      <p class="hint" style="margin:0 0 8px">以下为生产环境的真实部署与备份事实（管理员可在「备份与恢复」页核对当前备份包列表）。</p>
      <table class="about-table" style="width:100%"><thead><tr><th style="width:130px">项目</th><th>说明</th></tr></thead><tbody>
        <tr><td>部署位置</td><td>实验室服务器（局域网 10.51.42.96:8000）的 <code>/opt/specimen-manager</code> 目录。数据库 <code>data.db</code>（SQLite，WAL 模式）、附件 <code>uploads/</code>、备份 <code>backups/</code> 均在该目录下；systemd 服务 <code>specimen-manager</code> 托管（开机自启、崩溃自动重启，waitress 生产模式）。</td></tr>
        <tr><td>备份方式</td><td>每次生成<b>完整恢复包</b>：<code>backups/pkg-时间戳-类型/</code> 目录，内含 data.db 一致性快照（SQLite backup API，WAL 安全）+ 附件全量副本 + manifest.json（各表行数、SHA-256 校验和、应用/结构版本）+ 配置说明（不含任何口令/令牌）。备份窗口内短暂暂停附件写入并做二次快照，保证数据库与附件引用一致；integrity_check 通过后原子发布，失败即整体作废、绝不留半成品。</td></tr>
        <tr><td>备份频率</td><td>三种触发，均为全量：① <b>每天 03:00</b> 自动备份（systemd 定时器，错过自动补跑）；② <b>每次服务启动</b>时自动备份；③ 管理员在「备份与恢复」页<b>随时手动</b>备份。</td></tr>
        <tr><td>留存策略</td><td>自动轮换：最多保留最近 <b>30 个包</b>、总量上限 <b>20 GB</b>，超出自动清理最旧的包。恢复点目标 24 小时内，恢复时间目标 30 分钟内。</td></tr>
        <tr><td>异地备份</td><td><b>当前未配置异地副本</b>：备份与生产数据同在一台服务器，若磁盘/整机损坏，本地备份可能同时受损。建议管理员定期（如每周）从「备份与恢复」页下载最新恢复包，保存到其他电脑或移动硬盘。</td></tr>
        <tr><td>恢复途径</td><td>管理员「备份与恢复」页可查看、下载、校验并恢复任意恢复包（下载包自动剥离会话与 API 令牌）；单条/单批错误用「变更历史」回退或「版本管理与回退」恢复快照即可，无需整库恢复。</td></tr>
      </tbody></table>
    </div>

    <!-- ========== 常见问题 ========== -->
    <div class="about-card" id="about-faq">
      <h3>常见问题（FAQ）</h3>
      <table class="about-table" style="width:100%"><thead><tr><th>问题</th><th>解答</th></tr></thead><tbody>
        <tr><td>保存失败怎么办？</td><td>查看错误提示（黄色/红色），常见原因：另一人先改了同一格（选保留草稿→稍后重试）、编号重复（改编号）、必填字段缺失（补填）</td></tr>
        <tr><td>误关了弹窗/页面？</td><td>长表单内容自动存为本机草稿（7 天），重新打开会询问恢复；表格编辑的「结果待确认」可通过请求 ID 查询服务器真实结果</td></tr>
        <tr><td>「—」和 0 有什么区别？</td><td>「—」表示未记录/缺失，0 表示明确为零。系统绝不把缺失补造为 0 或任何猜测值</td></tr>
        <tr><td>不想让别人看到我的草稿？</td><td>草稿按 账号/表/记录/字段 隔离存在本机浏览器（不上传服务器），登出自动清除本账号全部草稿</td></tr>
        <tr><td>怎么回退错误的批量操作？</td><td>「最近操作」找到该批次 →「查看明细」→「回退此批次的修改」→ 预览确认（只回退该批次改动，他人后续修改保留）</td></tr>
        <tr><td>编号忘了怎么找？</td><td>搜索框输入属名/地名/采集人等任意关键字；或用全局搜索（Ctrl+K）跨所有有权限的表查找</td></tr>
        <tr><td>两个人同时改怎么办？</td><td>后保存者会收到冲突提示：选「保存修改」覆盖 / 「保留草稿」稍后再试 / 「放弃修改」</td></tr>
        <tr><td>怎么把数据给合作者？</td><td>勾选行 → 创建协作包 → 下载工作簿发给协作者 → 协作者改好后上传回传 → 服务器三方合并</td></tr>
        <tr><td>怎么导出论文用的表格？</td><td>筛选标本 → 导出菜单「导出向导」→ 选模板（不选则默认全部列）→ 预览确认 → 生成 XLSX（含说明文件）；或模板导出页用内置 Material examined 模板</td></tr>
        <tr><td>数据会丢吗？</td><td>系统每天 03:00 自动全量备份（另有启动/手动备份），保留最近 30 个包；单条/单批错误优先用「变更历史」回退。部署、备份与恢复细节见「部署与备份」章节</td></tr>
        <tr><td>DetStatus 是什么？</td><td>鉴定状态：待鉴定（还没看）/暂定（初步鉴定）/已复核（专家确认过）/有争议（多人意见不一致）；改「已复核」自动登记确认人</td></tr>
      </tbody></table>
    </div>

    <!-- ========== 版本信息（放最后） ========== -->
    <div class="about-card" style="border-top:2px solid var(--border)" id="about-changelog">
      <h3 style="font-size:14px;margin:0 0 8px">系统版本
        <span class="st st-saved" style="margin-left:8px">${esc(appVer)}</span>
        <span class="hint">更新于 ${esc(schemaDate)}</span></h3>
      <table class="about-table" style="width:100%">
        <thead><tr><th>项目</th><th>值</th></tr></thead><tbody>
          <tr><td>应用版本</td><td><b>${esc(appVer)}</b></td></tr>
          <tr><td>数据库结构版本</td><td><b>${esc(schemaVer)}</b></td></tr>
          <tr><td>结构更新时间</td><td>${esc(schemaDate)}</td></tr>
          ${wscSync ? `<tr><td>WSC 名录版本</td><td>${esc(wscSync)}</td></tr>` : ""}
          <tr><td>服务地址</td><td>${esc(location.host)}</td></tr>
          <tr><td>部署方式</td><td>systemd + waitress（生产模式）</td></tr>
          <tr><td>数据库</td><td>SQLite（WAL 模式）</td></tr>
        </tbody>
      </table>
      <p class="hint" style="margin-top:8px">当前结构版本 ${esc(schemaVer)}；自 3.17 起 edit_packages 表含 pending_review 列（协作冲突审阅功能）。每次结构变更版本号 +1 并记录在 SCHEMA_CHANGELOG 中。</p>
      ${changelog.length ? `
      <details class="cl-wrap" style="margin-top:10px">
        <summary style="cursor:pointer;font-weight:600;user-select:none">版本更新历史（共 ${changelog.length} 个版本，点击展开）</summary>
        <div style="max-height:60vh;overflow-y:auto;border:1px solid var(--border);border-radius:8px;padding:4px 0;margin-top:8px">
          ${changelog.map((c) => `
            <details style="border-bottom:1px dashed var(--border)">
              <summary style="cursor:pointer;padding:7px 12px;user-select:none;display:flex;gap:8px;align-items:baseline">
                <span class="st st-saved" style="flex:0 0 auto">v${esc(c.ver)}</span>
                <span class="hint" style="flex:0 0 auto">${esc(c.date)}</span>
                <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text);font-size:13px">${esc(c.note.split(/[；。]/)[0] || "")}</span>
              </summary>
              <div style="padding:2px 14px 10px 56px;font-size:12.5px;line-height:1.7;color:var(--text);white-space:pre-wrap;word-break:break-word">${esc(c.note)}</div>
            </details>`).join("")}
        </div>
      </details>` : ""}
    </div>

    <div class="about-footer">标本信息管理系统 · 数据每 10 秒自动同步 · 全程可追溯</div>
  </div>`;

  /* ---- 目录点击滚动 ---- */
  document.querySelectorAll(".about-toc-link").forEach((a) => (a.onclick = () => {
    const el = document.getElementById("about-" + a.dataset.toc);
    if (el) { el.scrollIntoView({ behavior: "smooth", block: "start" }); el.style.borderLeft = "3px solid var(--primary)"; setTimeout(() => { el.style.borderLeft = ""; }, 1500); }
  }));
}


/* ---------------- 模板导出（v3.66/v3.69）：通用起始模板 + 自定义/期刊模板 ---------------- */
const TPL_RS = {
  personal: { label: "未核实期刊规范（通用 / 作者自定义）", cls: "st st-pending" },
  lab: { label: "实验室规范", cls: "st st-linked" },
  official: { label: "已核实期刊规范", cls: "st st-saved" },
};
const TPL_KIND = { phylo: "系统发育样本信息表", evolution: "演化分析元数据表", material: "Material examined（分类学）", custom: "自定义" };
const TPL_SOURCES = ["plain", "key", "sci", "date", "number", "composite"];

async function renderTplExp() {
  const host = $("view-tplexp");
  host.innerHTML = `<div class="toolbar">
      <button class="btn" id="tp-new">${icon("plus")} 新建模板</button>
      <span class="stat" id="tp-stat"></span>
    </div><div id="tp-body"><p class="hint">加载中…</p></div>`;
  $("tp-new").onclick = () => tplEditor(null, null);
  let d;
  try { d = await api("/api/export-templates"); } catch (e) {
    $("tp-body").innerHTML = `<p class="hint warn">${esc(e.message)}</p>`; return;
  }
  $("tp-stat").textContent = `已保存模板 ${d.templates.length} 套 · 内置起始模板 ${d.defaults.length} 套`;
  const rs = (r) => { const x = TPL_RS[r.rules_source] || TPL_RS.personal;
    return `<span class="${x.cls}" title="rules_source=${esc(r.rules_source || "personal")}">${x.label}</span>`; };
  const defaults = (d.defaults || []).map((t, i) => `
    <div class="dash-card" style="margin-bottom:10px">
      <b>${esc(t.name)}</b> <span class="hint">（${esc(TPL_KIND[t.template_kind] || t.template_kind)} · 内置 · ${TPL_RS.personal.label}）</span>
      <div class="hint" style="margin:4px 0">${t.columns_def.length} 列：${esc(t.columns_def.map((c) => c.out || c.field).join("，"))}</div>
      <div class="form-actions" style="margin:0">
        <button class="btn" data-dprev="${i}">预览效果</button>
        <button class="btn primary" data-dnew="${i}">以此新建模板</button>
      </div>
    </div>`).join("");
  const rows = d.templates.map((t) => `
    <tr>
      <td><b>${esc(t.name)}</b>${t.favorite ? " ★" : ""}<div class="hint">${esc(TPL_KIND[t.template_kind] || t.template_kind)} · ${t.columns_def.length} 列</div></td>
      <td>${t.scope === "team" ? "团队" : "个人"}${t.journal ? `<div class="hint">${esc(t.journal)}</div>` : ""}</td>
      <td>${rs(t)}</td>
      <td style="white-space:nowrap">
        <button class="btn primary" data-act="gen" data-id="${t.id}" ${canWriteTable(t.tname) ? "" : "disabled"}>生成文件</button>
        <button class="btn" data-act="edit" data-id="${t.id}">编辑</button>
        ${rowMenu([
          { act: "prev", label: "预览" },
          { act: "fav", label: t.favorite ? "取消收藏" : "收藏" },
          { act: "copy", label: "复制模板" },
          { act: "del", label: "删除模板", danger: true,
            disabled: !canWriteTable(t.tname) && t.scope === "personal" ? false : !canWriteTable(t.tname),
            title: canWriteTable(t.tname) ? "" : "您对本表只读" },
        ])}
      </td>
    </tr>`).join("");
  $("tp-body").innerHTML = `
    <div class="about-card"><h3>通用起始模板（内置）</h3>
      <p class="hint">三类起始模板按常见论文配套数据表组织；<b>期刊规范未经核实，明确标记为「通用 / 作者自定义」（rules_source=personal）</b>——投稿前请按目标期刊作者须知核对列与格式，核实后可在编辑中把规则来源改为「已核实期刊规范」并登记期刊与核实日期。</p>
      ${defaults}</div>
    <div class="about-card"><h3>我的 / 团队模板</h3>
      ${d.templates.length ? `<table class="about-table" style="width:100%"><thead><tr><th>模板</th><th>范围 / 期刊</th><th>规范核实状态</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table>`
        : `<p class="hint">还没有保存的模板。从上方内置起始模板「以此新建」最快。</p>`}</div>`;
  wireRowMenus(host);
  host.querySelectorAll("[data-dnew]").forEach((b) => (b.onclick = () =>
    tplEditor(null, d.defaults[+b.dataset.dnew])));
  host.querySelectorAll("[data-dprev]").forEach((b) => (b.onclick = () => {
    const t = d.defaults[+b.dataset.dprev];
    tplPreview({ tname: "specimens", columns_def: t.columns_def }, t.name);
  }));
  const byId = (id) => d.templates.find((t) => String(t.id) === id);
  host.querySelectorAll("[data-act]").forEach((b) => (b.onclick = async () => {
    const t = byId(b.dataset.id);
    if (!t) return;
    wireRowMenus(host);
    try {
      if (b.dataset.act === "prev") tplPreview({ tname: t.tname, columns_def: t.columns_def, template_id: t.id }, t.name);
      else if (b.dataset.act === "gen") openExportWizard({ context: "tplexp", tname: t.tname, templateId: t.id });
      else if (b.dataset.act === "edit") tplEditor(t, null);
      else if (b.dataset.act === "fav") { await api(`/api/export-templates/${t.id}`, { method: "PUT", body: JSON.stringify({ favorite: !t.favorite }) }); renderTplExp(); }
      else if (b.dataset.act === "copy") { await api(`/api/export-templates/${t.id}/copy`, { method: "POST", body: JSON.stringify({}) }); renderTplExp(); toast("已复制为个人模板", "ok"); }
      else if (b.dataset.act === "del") {
        if (await confirmBox(`删除模板「${t.name}」？`)) { await api(`/api/export-templates/${t.id}`, { method: "DELETE" }); renderTplExp(); }
      }
    } catch (e) { toast(e.message, "err"); }
  }));
}

function tplEditor(t, base) {
  const src = t || base || { tname: "specimens", template_kind: "custom", row_identity: "record", one_to_many: "expand", scope: t ? undefined : "personal", rules_source: "personal", columns_def: [] };
  const draftTarget = "tpl:" + (t ? t.id : "new:" + src.tname);
  const draftRestored = formDraftLoad("tpl", draftTarget);
  const tnames = Object.keys(myTables());
  const fieldsFor = (tn) => (((S.meta.tables[tn] || {}).columns) || []).map((c) => c.name).join("|");
  openModal(t ? `编辑模板 · ${t.name}` : "新建导出模板", `
    <div class="tpl-form" style="display:grid;grid-template-columns:1fr 1fr;gap:8px 14px">
      <label>模板名称 *<input id="te-name" value="${esc(t ? t.name : base ? base.name.replace(/（通用[^）]*）/, "") : "")}" class="input"></label>
      <label>目标表<select id="te-tname" class="input">${tnames.map((n) => `<option ${((t || base) || {}).tname === n ? "selected" : ""}>${esc(n)}</option>`).join("")}</select></label>
      <label>模板类型<select id="te-kind" class="input">${Object.entries(TPL_KIND).map(([k, v]) => `<option value="${k}" ${(t || base || {}).template_kind === k ? "selected" : ""}>${v}</option>`).join("")}</select></label>
      <label>保存范围<select id="te-scope" class="input"><option value="personal" ${(t || {}).scope !== "team" ? "selected" : ""}>个人</option>${S.me.admin ? `<option value="team" ${(t || {}).scope === "team" ? "selected" : ""}>团队（修订留痕）</option>` : ""}</select></label>
      <label style="grid-column:1/-1">规范核实状态（未核实 = 通用 / 作者自定义）
        <select id="te-rs" class="input">${Object.entries(TPL_RS).map(([k, v]) => `<option value="${k}" ${(t || src).rules_source === k ? "selected" : ""}>${v.label}</option>`).join("")}</select></label>
      <label>期刊<input id="te-journal" class="input" value="${esc((t || {}).journal || "")}" placeholder="如 Systematic Entomology"></label>
      <label>文章类型<input id="te-atype" class="input" value="${esc((t || {}).article_type || "")}" placeholder="如 原始研究/修订"></label>
      <label>作者须知链接<input id="te-guide" class="input" value="${esc((t || {}).author_guide_url || "")}"></label>
      <label>核实日期<input id="te-checked" class="input" type="date" value="${esc((t || {}).rules_checked_at || "")}"></label>
    </div>
    <h3 style="margin:14px 0 6px">列定义</h3>
    <p class="hint">类型：原文=照抄字段；标识=编号列；学名=属斜体拼接；日期=三分量合成（选项 JSON 可声明 missing: dash|unknown|custom 与自定义缺省文本）；数值=小数位；复合=白名单函数 join/locality/material_desc/first_non_empty（选项 JSON 填 {"func":...,"parts":[...]}）。</p>
    <div id="te-cols"></div>
    <button class="btn" id="te-addcol">${icon("plus")} 加一列</button>
    <div class="form-actions">
      <button class="btn" id="te-cancel">取消</button>
      <button class="btn primary" id="te-save">保存模板</button>
    </div>`);
  $("modal").classList.add("modal-wide");
  const dl = document.createElement("datalist");
  dl.id = "te-fields";
  const fillDl = (tn) => {
    dl.innerHTML = fieldsFor(tn).split("|").map((f) => `<option value="${esc(f)}">`).join("");
  };
  fillDl(src.tname || "specimens");
  $("modal-body").appendChild(dl);
  $("te-tname").onchange = () => fillDl($("te-tname").value);
  let teCols = JSON.parse(JSON.stringify(src.columns_def || []));
  if (draftRestored) teCols = JSON.parse(JSON.stringify(draftRestored.data.columns || teCols));
  const ce = renderColumnEditor($("te-cols"), { tname: src.tname || "specimens", columns: teCols });
  $("te-addcol").style.display = "none";
  if (draftRestored) {
    const mins = Math.max(1, Math.round((Date.now() - draftRestored.at) / 60000));
    $("te-name").value = draftRestored.data.name || "";
    if (draftRestored.data.journal !== undefined) $("te-journal").value = draftRestored.data.journal || "";
    $("modal-body").insertAdjacentHTML("afterbegin",
      `<div class="dash-card" style="margin-bottom:8px;display:flex;gap:10px;align-items:center">
        <span class="hint">已恢复 ${mins < 60 ? mins + " 分钟前" : Math.round(mins / 60) + " 小时前"}保存的本机草稿（仅存本机，按账号隔离）</span>
        <button class="btn danger" id="te-dropdraft" type="button">丢弃草稿</button></div>`);
    $("te-dropdraft").onclick = () => { formDraftDrop("tpl", draftTarget); closeModal(); tplEditor(t, base); };
  }
  const tplCollect = () => ({
    name: $("te-name") ? $("te-name").value.trim() : "",
    journal: $("te-journal") ? $("te-journal").value.trim() : "",
    columns: ce.get(),
  });
  const tplGuardId = "guard:" + draftTarget;
  let tplDirty = false;
  $("modal-body").addEventListener("input", () => { tplDirty = true; }, true);
  registerFormGuard(tplGuardId, {
    dirty: () => tplDirty,
    canSaveLeave: () => {
      const c = tplCollect();
      return !!c.name && c.columns.length > 0;
    },
    saveLeave: async () => { $("te-save").click(); return !modalLayers.length || true; },
    keepDraft: () => formDraftSave("tpl", draftTarget, tplCollect()),
    discard: () => { formDraftDrop("tpl", draftTarget); tplDirty = false; },
  });
  setTopModalGuard({ id: tplGuardId, dirty: () => formGuards.get(tplGuardId).dirty(),
    canSaveLeave: () => formGuards.get(tplGuardId).canSaveLeave ? formGuards.get(tplGuardId).canSaveLeave() : false,
    saveLeave: formGuards.get(tplGuardId).saveLeave, keepDraft: () => formGuards.get(tplGuardId).keepDraft(), discard: () => formGuards.get(tplGuardId).discard() });
  $("te-save").addEventListener("click", () => {
    formDraftDrop("tpl", draftTarget); unregisterFormGuard(tplGuardId); tplDirty = false;
  });
  $("te-cancel").onclick = closeModal;

  $("te-save").onclick = async () => {
    const name = $("te-name").value.trim();
    if (!name) { toast("模板名称必填", "err"); return; }
    const columns_def = JSON.parse(JSON.stringify(ce.get()));  // 可视化编辑器当前状态（含增删/重排序）
    const body = {
      name, tname: $("te-tname").value, template_kind: $("te-kind").value,
      scope: $("te-scope").value, rules_source: $("te-rs").value,
      journal: $("te-journal").value.trim() || null, article_type: $("te-atype").value.trim() || null,
      author_guide_url: $("te-guide").value.trim() || null,
      rules_checked_at: $("te-checked").value || null,
      row_identity: "record", one_to_many: "expand", columns_def,
    };
    try {
      if (t) await api(`/api/export-templates/${t.id}`, { method: "PUT", body: JSON.stringify(body) });
      else await api("/api/export-templates", { method: "POST", body: JSON.stringify(body) });
      closeModal(); toast("模板已保存", "ok"); renderTplExp();
    } catch (e) { toast(e.message, "err"); }
  };
}

async function tplPreview(body, name) {
  let r;
  try { r = await api("/api/export-templates/preview", { method: "POST", body: JSON.stringify(body) }); }
  catch (e) { toast(e.message, "err"); return; }
  const heads = r.preview.length ? Object.keys(r.preview[0].cells) : [];
  openModal(`预览 · ${name}（前 20 行 / 共 ${r.total} 行）`, `
    ${r.problem_count ? `<p class="hint warn">问题 ${r.problem_count} 处：${esc(r.problems.slice(0, 8).map((p) => p.kind + "：" + (p.detail || "")).join("；"))}${r.problem_count > 8 ? "…" : ""}</p>` : `<p class="hint">全量检查通过，无阻断问题。</p>`}
    <div style="overflow:auto;max-height:50vh"><table class="about-table" style="width:100%">
      <thead><tr>${heads.map((h) => `<th>${esc(h)}</th>`).join("")}</tr></thead>
      <tbody>${r.preview.map((row) => `<tr>${heads.map((h) => `<td>${esc(row.cells[h] ?? "")}</td>`).join("")}</tr>`).join("")}</tbody>
    </table></div>`);
  $("modal").classList.add("modal-wide");
}

async function postDownload(url, body, fallbackName) {
  const r = await fetch(url, { method: "POST", headers: { "X-Auth": token(), "Content-Type": "application/json" },
    body: JSON.stringify(body) });
  if (!r.ok) { let m = `生成失败 (${r.status})`; try { m = (await r.json()).error || m; } catch { } throw new Error(m); }
  const blob = await r.blob();
  const cd = r.headers.get("Content-Disposition") || "";
  let name = fallbackName;
  const m = /filename\*=(?:UTF-8'')?([^;]+)/i.exec(cd) || /filename="?([^";]+)"?/i.exec(cd);
  if (m) { try { name = decodeURIComponent(m[1].trim().replace(/^"|"$/g, "")); } catch { name = m[1]; } }
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 8000);
  return name;
}

/* ---------------- 导出向导（v3.73）：统一表页/模板库/项目的模板导出入口 ----------------
   范围：勾选记录 / 当前筛选全部（resolve=ids 稳定计划固化）/ 全表（必须主动选择，绝不因输入为空悄悄退回）
        / 项目确认成员（当前数据或指定冻结版本）。
   顶部固定范围摘要；模板优先收藏与适用模板（可搜索），字段编辑收进高级配置；
   预览/检查/生成使用同一份范围+模板+格式配置（任一变化即要求重新检查）；返回不丢输入，生成可重试、进行中幂等锁定。 */
async function openExportWizard(cfg) {
  const W = {
    tname: cfg.tname || "specimens",
    context: cfg.context || "table",
    project: cfg.project || null,
    grid: cfg.grid || null,
    scopeType: null, ids: null, origCount: null, binding: false,
    version: "current", projVersions: [],
    templates: [], tplSearch: "", templateId: cfg.templateId || null,
    tplName: "", tplRev: null, columnsDef: null, advOpen: false,
    fmt: "xlsx",
    preview: null, previewKey: "", previewBusy: false,
    blockAck: false, genBusy: false, result: null,
    step: 1,
  };
  openModal("导出向导", `
    <div id="wz-sum" style="border:1px solid var(--border);border-radius:8px;padding:8px 12px;margin-bottom:10px"></div>
    <div id="wz-body"></div>
    <div class="form-actions" style="margin-top:10px">
      <button class="btn" id="wz-back" style="display:none">上一步</button>
      <span style="flex:1"></span>
      <button class="btn" id="wz-cancel">取消</button>
      <button class="btn primary" id="wz-next">下一步</button>
      <button class="btn primary" id="wz-gen" style="display:none">${icon("download")} 生成并下载</button>
    </div>`);
  $("modal").classList.add("modal-wide");
  const host = $("wz-body");
  const sumEl = $("wz-sum"), nextBtn = $("wz-next"), genBtn = $("wz-gen"), backBtn = $("wz-back");

  const tplRow = () => W.templates.find((t) => t.id === W.templateId);
  const normCols = (c) => (Array.isArray(c) ? c : JSON.parse(c || "[]"));
  // 未选模板时的默认列集：按表定义顺序导出全部列（显示名作列头）
  const defaultAllCols = () => (((S.meta.tables[W.tname] || {}).columns) || [])
    .map((c) => ({ field: c.name, out: c.label || c.name, source: "plain" }));
  const effCols = () => W.columnsDef || (tplRow() ? normCols(tplRow().columns_def) : defaultAllCols());
  const scopeReady = () => W.scopeType && !W.binding && (W.scopeType === "all" || W.scopeType === "project" || (W.ids && W.ids.length >= 0));
  const previewKeyOf = () => JSON.stringify([W.tname, W.ids, W.version, W.templateId, W.columnsDef]);
  const stale = () => !W.preview || W.previewKey !== previewKeyOf();

  function renderSum() {
    const src = W.context === "project" ? `项目「${W.project.name}」确认成员`
      : ({ checked: "表格勾选记录", filtered: "当前筛选全部（稳定计划固化）", all: "全表（主动选择）", picker: "手动选择记录" }[W.scopeType] || "未选择");
    const ver = W.version === "current" ? "当前数据"
      : (() => { const v = W.projVersions.find((x) => String(x.id) === String(W.version)); return v ? `冻结版本 #${v.id} ${v.name}` : "冻结版本"; })();
    const t = tplRow();
    sumEl.innerHTML = `<div style="display:flex;flex-wrap:wrap;gap:4px 18px;font-size:12.8px">
      <span>所属：<b>${esc(W.context === "project" ? "项目 " + W.project.name : tblName(S.meta.tables[W.tname]) || W.tname)}</b></span>
      <span>范围来源：<b>${esc(src)}</b></span>
      <span>原记录数：<b>${W.origCount === null ? "—" : W.origCount.toLocaleString()}</b></span>
      <span>输出行数：<b>${W.preview && !stale() ? W.preview.total.toLocaleString() : "待检查"}</b></span>
      <span>模板：<b>${esc(W.tplName || (tplRow() ? "未选择" : "默认全部列"))}</b>${W.tplRev !== null && W.tplRev !== undefined ? `（修订 v${W.tplRev}）` : ""}</span>
      <span>数据状态：<b>${esc(ver)}</b></span>
      <span>格式：<b>${W.fmt.toUpperCase()}</b></span>
    </div>
    ${stale() && W.step >= 3 ? `<div class="hint warn" style="margin-top:4px">范围/模板设置已变化——请重新执行「检查与预览」；生成使用与预览完全相同的配置。</div>` : ""}`;
  }

  async function bindScope() {
    W.binding = true; W.preview = null; W.bindError = null; W.bindTried = true; renderSum(); render();
    try {
      if (W.context === "project") {
        const d = await api(`/api/projects/${W.project.pid}`);
        const t = tplRow();
        const members = d.members.filter((m) => !m.exclude_reason && (!t || m.tname === t.tname));
        W.ids = members.map((m) => m.rid);
        W.origCount = W.ids.length;
      } else if (W.scopeType === "checked") {
        W.ids = W.grid.selIds.slice();
        W.origCount = W.ids.length;
      } else if (W.scopeType === "filtered") {
        // cap=200000：导出不设行数上限（默认 5000 仅用于批量操作防失控）
        const p = new URLSearchParams({ q: W.grid.q, sort: "id", dir: "asc", resolve: "ids", cap: "200000" });
        if (W.grid.deleted) p.set("deleted", "1");
        if (W.grid.preset) p.set("preset", W.grid.preset);
        if (Object.keys(W.grid.filters).length) p.set("filters", JSON.stringify(W.grid.filters));
        const pl = await api(`/api/rows/${W.tname}?` + p);
        if (pl.truncated) throw new Error(`筛选命中 ${pl.count} 行，超出计划上限 ${pl.cap}——请缩小范围（全表导出请显式选择「全表」）`);
        if (!pl.ids.length) throw new Error("当前筛选没有命中行——请调整筛选，或显式选择「全表」");
        W.ids = pl.ids;
        W.origCount = pl.ids.length;
      } else if (W.scopeType === "all") {
        W.ids = null;
        const tot = await api(`/api/rows/${W.tname}?per=1`);
        W.origCount = tot.total;
      }
    } catch (e) {
      // 失败不自动重试（bindTried 防止「失败→重渲染→再绑定」死循环刷屏）；
      // 错误内联显示在范围步骤（不再只靠 toast，避免遮挡向导内容）。
      W.bindError = e.message;
      W.scopeType = null; W.ids = null; W.origCount = null;
      toast(e.message, "err");
    }
    W.binding = false;
    renderSum(); render();
  }

  function renderStep1() {
    if (W.context === "project") {
      host.innerHTML = `
        <h3>① 范围</h3>
        <p class="hint">来源固定为项目「${esc(W.project.name)}」的确认纳入成员（已移除/已排除的行不导出）；所选模板确定导出哪张表的成员。</p>
        <label>数据状态<select id="wz-ver" class="input" style="max-width:520px">
          <option value="current" ${W.version === "current" ? "selected" : ""}>当前数据</option>
          ${W.projVersions.map((v) => `<option value="${v.id}" ${String(W.version) === String(v.id) ? "selected" : ""}>冻结版本 #${v.id} ${esc(v.name)}（${v.total} 行 · ${esc(v.created_at || "")}）</option>`).join("")}
        </select></label>
        <p class="hint" id="wz-bind">${W.binding ? "正在绑定范围…" : (W.version === "current" ? "以成员的当前记录导出。" : "以冻结版本快照导出——之后修改当前数据不影响该版本。")}</p>`;
      $("wz-ver").onchange = () => { W.version = $("wz-ver").value; bindScope(); };
      if (!W.projVersions.length) {
        api(`/api/projects/${W.project.pid}/versions`).then((d) => { W.projVersions = d.versions; render(); }).catch(() => { });
      }
      return;
    }
    const g = W.grid;
    const opts = [];
    if (W.context === "table" && g && g.selIds.length) opts.push(["checked", `已勾选记录（${g.selIds.length} 行）`]);
    if (W.context === "table" && g) opts.push(["filtered", `当前筛选全部${g && g.q ? `（搜索「${esc(g.q.slice(0, 20))}」${Object.keys(g.filters || {}).length ? "＋列筛选" : ""}）` : "（无搜索词）"}`]);
    if (W.context === "table" && g) opts.push(["all", `全表${g.total ? `（${g.total.toLocaleString()} 行）` : ""}——主动选择`]);
    if (W.context === "tplexp") opts.push(["all", "全表（主动选择）"], ["picker", "选择记录（粘贴编号/名称批量选择，无需内部 ID）"]);
    if (!opts.length) { host.innerHTML = `<p class="hint">当前上下文没有可用范围。</p>`; return; }
    if (!W.scopeType) W.scopeType = opts[0][0];
    const bindHint = W.binding ? "正在按稳定查询计划绑定范围…"
      : W.bindError ? `绑定失败：${esc(W.bindError)}（可另选范围后重试）`
      : (W.ids && W.scopeType !== "all" ? `已固化 ${W.ids.length} 行（之后筛选变化或新增记录不会扩大本次范围）` : "");
    host.innerHTML = `
      <h3>① 范围（必须显式选择；不会因输入为空而悄悄退回全表）</h3>
      ${opts.map(([v, label]) => `<label style="display:block;margin:4px 0"><input type="radio" name="wz-scope" value="${v}" ${W.scopeType === v ? "checked" : ""}> ${esc(label)}</label>`).join("")}
      ${W.scopeType === "picker" ? `<div id="wz-picker"></div>` : ""}
      <p class="hint ${W.bindError ? "warn" : ""}" id="wz-bind">${bindHint}</p>`;
    host.querySelectorAll('input[name="wz-scope"]').forEach((rad) => { rad.onclick = () => {
      W.scopeType = rad.value; W.ids = null; W.origCount = null; W.preview = null;
      W.bindTried = false; W.bindError = null;   // 换范围：允许重新绑定一次
      renderSum(); render();
      if (W.scopeType === "filtered" || W.scopeType === "checked" || W.scopeType === "all") bindScope();
    }; });
    if (W.scopeType === "picker" && !host.querySelector(".ep-box")) {
      renderEntityPicker(host.querySelector("#wz-picker"), { tables: [W.tname], multi: true,
        onChange: (st) => {
          W.ids = st.selected.map((m) => m.rid);
          W.origCount = W.ids.length;
          W.preview = null;
          renderSum(); syncButtons();
        } });
    }
    if ((W.scopeType === "filtered" || W.scopeType === "checked") && W.ids === null
        && !W.binding && !W.bindTried) bindScope();
  }

  function renderStep2() {
    const list = W.templates
      .filter((t) => t.tname === W.tname)
      .filter((t) => !W.tplSearch || (t.name || "").toLowerCase().includes(W.tplSearch.toLowerCase()))
      .sort((a, b) => (b.favorite - a.favorite) || (b.id - a.id));
    host.innerHTML = `
      <h3>② 模板（收藏与适用模板优先；不选模板 = 按表定义全部列默认导出）</h3>
      <input id="wz-tsearch" class="input" placeholder="搜索模板名称…" value="${esc(W.tplSearch)}" style="max-width:320px">
      <div style="max-height:32vh;overflow:auto;margin-top:6px">
      ${list.length ? list.map((t) => `
        <label style="display:block;margin:4px 0;padding:6px 8px;border:1px solid ${W.templateId === t.id ? "var(--primary)" : "var(--border)"};border-radius:8px;cursor:pointer">
          <input type="radio" name="wz-tpl" value="${t.id}" ${W.templateId === t.id ? "checked" : ""}>
          <b>${t.favorite ? "★ " : ""}${esc(t.name)}</b>
          <span class="hint">${t.scope === "team" ? "团队" : "个人"} · 修订 v${t.revision ?? 1} · ${t.columns_def.length} 列${t.rules_source === "personal" ? " · 规范未核实（通用/作者自定义）" : t.rules_source === "official" ? " · 已核实期刊规范" : ""}</span>
        </label>`).join("") : `<p class="hint">没有适用「${esc(tblName(S.meta.tables[W.tname]) || W.tname)}」的已存模板——<b>不选择模板将按表定义的全部列默认导出</b>；也可在下方高级配置自定义列（仅本次生成使用）。</p>`}
      </div>
      <details ${W.advOpen ? "open" : ""} id="wz-adv" style="margin-top:8px">
        <summary style="cursor:pointer">高级配置（字段/列编辑——常规用户无需展开）</summary>
        <p class="hint">${tplRow() ? "在所选模板基础上调整列" : "当前为默认全部列；可在此增删/调整列"}；修改仅用于本次生成（不改已保存模板）。</p>
        <div id="wz-cols"></div>
        <button class="btn" id="wz-addcol" type="button">${icon("plus")} 加一列</button>
      </details>`;
    $("wz-tsearch").oninput = () => { W.tplSearch = $("wz-tsearch").value; renderStep2(); };
    host.querySelectorAll('input[name="wz-tpl"]').forEach((rad) => (rad.onclick = () => {
      W.templateId = +rad.value;
      const t = tplRow();
      W.tplName = t.name; W.tplRev = t.revision ?? 1;
      W.columnsDef = null; W.preview = null;
      renderSum(); renderStep2(); syncButtons();
    }));
    const adv = $("wz-adv");
    adv.ontoggle = () => { W.advOpen = adv.open; };
    if (W.advOpen) {
      if (!W.columnsDef) W.columnsDef = tplRow() ? normCols(tplRow().columns_def) : defaultAllCols();
      renderColumnEditor($("wz-cols"), { tname: W.tname, columns: W.columnsDef || [],
        onChange: (cols) => {
          W.columnsDef = cols; W.preview = null;
          if (W.templateId && tplRow()) { W.tplName = tplRow().name + "（已调整，仅本次）"; W.tplRev = tplRow().revision ?? 1; }
          renderSum(); syncButtons();
        } });
      const addbtn = $("wz-addcol");
      if (addbtn) addbtn.style.display = "none";
    }
  }

  function runPreview() {
    if (!scopeReady()) { toast("请先完成范围选择", "err"); return; }
    W.previewBusy = true; renderSum(); render();
    const myKey = previewKeyOf();
    const body = { tname: W.tname, columns_def: effCols(), version: W.version };
    if (W.ids) body.rids = W.ids;
    api("/api/export-templates/preview", { method: "POST", body: JSON.stringify(body) })
      .then((pv) => {
        if (previewKeyOf() !== myKey) return;   // 设置已变：丢弃过期预览
        W.preview = pv; W.previewKey = myKey; W.blockAck = false;
      })
      .catch((e) => toast(e.message, "err"))
      .finally(() => { W.previewBusy = false; renderSum(); render(); });
  }

  function renderStep3() {
    if (W.previewBusy) {
      host.innerHTML = `<h3>③ 检查与预览</h3><p class="hint">正在按与生成完全相同的范围/模板/版本执行全量检查…</p>`;
      return;
    }
    const pv = W.preview;
    if (!pv || stale()) {
      host.innerHTML = `<h3>③ 检查与预览</h3>
        <p class="hint warn">尚未检查或设置已变化——「生成」使用与预览完全相同的范围/模板/格式，请先执行检查。</p>
        <button class="btn primary" id="wz-pv">${icon("view")} 检查与预览</button>`;
      $("wz-pv").onclick = runPreview;
      return;
    }
    const groups = { blocking: [], confirm: [], info: [] };
    for (const p of pv.problems) (groups[p.severity || "info"] || groups.info).push(p);
    const probRow = (p, ic) => `<div class="hint" style="margin:2px 0">${ic} ${p.rid ? `记录 #${p.rid}` : "全表"}${p.col ? ` · 列 ${esc(p.col)}` : ""}：${esc(p.detail || p.kind)}</div>`;
    host.innerHTML = `
      <h3>③ 检查与预览（与生成同一份范围/模板/格式）</h3>
      <label>文件格式<select id="wz-fmt" class="input" style="width:auto">
        ${["xlsx", "csv", "tsv"].map((f) => `<option value="${f}" ${W.fmt === f ? "selected" : ""}>${f.toUpperCase()}</option>`).join("")}</select>
        <span class="hint">XLSX 附独立说明文件（README）；CSV/TSV 不保留富文本（学名斜体等）。</span></label>
      ${groups.blocking.length ? `<div class="dash-card" style="border-color:var(--c-risk)"><b style="color:var(--c-risk)">⛔ 阻断 ${groups.blocking.length}</b>${groups.blocking.slice(0, 8).map((p) => probRow(p, "⛔")).join("")}
        <label style="display:block;margin-top:4px"><input type="checkbox" id="wz-ack"> 我已知悉上述阻断问题，仍要生成</label></div>` : ""}
      ${groups.confirm.length ? `<div class="dash-card"><b style="color:var(--c-pending)">？需确认 ${groups.confirm.length}</b>${groups.confirm.slice(0, 8).map((p) => probRow(p, "？")).join("")}${groups.confirm.length > 8 ? `<span class="hint">…其余 ${groups.confirm.length - 8} 条（生成文件的说明文件同样记录）</span>` : ""}</div>` : ""}
      ${groups.info.length ? `<div class="dash-card"><b>ℹ 信息 ${groups.info.length}</b>${groups.info.slice(0, 5).map((p) => probRow(p, "ℹ")).join("")}</div>` : ""}
      ${pv.missing.length ? `<div class="dash-card"><b>缺失与输出策略（全量统计，非笼统合格）</b>
        ${pv.missing.map((m) => `<div class="hint" style="margin:2px 0">列「${esc(m.out)}」：<b>${m.count}</b> 行缺失 → ${esc(m.strategy)}</div>`).join("")}</div>`
        : `<div class="dash-card"><b>缺失与输出策略</b><div class="hint">本次范围内所有输出列均无缺失值。</div></div>`}
      <div style="overflow:auto;max-height:30vh;margin-top:6px"><table class="about-table" style="width:100%">
        <thead><tr>${pv.preview.length ? Object.keys(pv.preview[0].cells).map((h) => `<th>${esc(h)}</th>`).join("") : ""}</tr></thead>
        <tbody>${pv.preview.map((row) => `<tr>${Object.keys(pv.preview[0].cells).map((h) => `<td>${esc(row.cells[h] ?? "")}</td>`).join("")}</tr>`).join("")}</tbody>
      </table>
      <p class="hint">预览前 20 行 / 共输出 ${pv.total.toLocaleString()} 行（原记录 ${pv.input_rows.toLocaleString()} 行）。</p></div>`;
    const fmtSel = $("wz-fmt");
    if (fmtSel) fmtSel.onchange = () => { W.fmt = fmtSel.value; renderSum(); };
    const ack = $("wz-ack");
    if (ack) ack.onchange = () => { W.blockAck = ack.checked; syncButtons(); };
  }

  function syncButtons() {
    backBtn.style.display = W.step > 1 ? "" : "none";
    nextBtn.style.display = W.step < 3 ? "" : "none";
    genBtn.style.display = W.step === 3 ? "" : "none";
    nextBtn.disabled = false;
    if (W.step === 1) nextBtn.disabled = !scopeReady();
    if (W.step === 3) {
      const pv = W.preview;
      const blocking = pv && !stale() && (pv.problems || []).some((p) => p.severity === "blocking");
      genBtn.disabled = W.previewBusy || stale() || !pv || (blocking && !W.blockAck) || W.genBusy;
      genBtn.innerHTML = W.genBusy ? "生成中…" : `${icon("download")} 生成并下载`;
    }
    renderSum();
  }

  function render() {
    if (W.step === 1) renderStep1();
    else if (W.step === 2) renderStep2();
    else renderStep3();
    syncButtons();
  }

  backBtn.onclick = () => { if (W.step > 1) { W.step--; render(); } };
  nextBtn.onclick = () => {
    if (W.step === 1 && !scopeReady()) { toast("请先完成范围选择", "err"); return; }
    if (W.step < 3) { W.step++; render(); if (W.step === 3) runPreview(); }
  };
  $("wz-cancel").onclick = closeModal;

  genBtn.onclick = async () => {
    if (W.genBusy) return;               // 幂等锁：生成进行中重复点击不再发起
    const pv = W.preview;
    if (!pv || stale()) { toast("设置已变化——请重新执行检查与预览", "err"); return; }
    const t = tplRow();
    const body = { tname: W.tname, columns_def: effCols(), version: W.version, fmt: W.fmt,
      template_name: (W.tplName || "全部列导出").replace(/（已调整，仅本次）/, ""),
      sheet_name: t ? t.sheet_name : null, title: t ? t.title : null, footer: t ? t.footer : null,
      journal: t ? t.journal : null, article_type: t ? t.article_type : null,
      rules_checked_at: t ? t.rules_checked_at : null, rules_source: t ? t.rules_source : null };
    if (W.ids) body.rids = W.ids;
    W.genBusy = true; syncButtons();
    try {
      const fname = await postDownload("/api/export-templates/generate", body, `${W.tplName || "全部列导出"}.${W.fmt}`);
      if (W.templateId) pushRecent("tplexp", W.templateId, { name: W.tplName });
      W.result = { name: fname, rows: pv.total, input: pv.input_rows, time: new Date().toLocaleString(), body };
      host.innerHTML = `<h3>✔ 已生成</h3><div class="dash-card">
        <div>文件名：<b>${esc(W.result.name)}</b></div>
        <div>输出行数：<b>${W.result.rows.toLocaleString()}</b>（原记录 ${(W.result.input || 0).toLocaleString()} 行）</div>
        <div>生成时间：${esc(W.result.time)}</div>
        <div>格式：${W.fmt.toUpperCase()} · 范围/列顺序与上方预览完全一致</div></div>
        <div class="form-actions"><button class="btn" id="wz-again">${icon("download")} 再次下载（同一份配置）</button></div>`;
      $("wz-again").onclick = async () => {
        try { await postDownload("/api/export-templates/generate", W.result.body, W.result.name); toast("已重新下载", "ok"); }
        catch (e) { toast(e.message, "err"); }
      };
      genBtn.style.display = "none"; nextBtn.style.display = "none";
      backBtn.style.display = ""; backBtn.onclick = () => { W.step = 3; render(); };
      toast(`已生成 ${W.result.name}`, "ok");
    } catch (e) {
      toast(`生成失败：${e.message}（可重试）`, "err");
    }
    W.genBusy = false; syncButtons();
  };

  try {
    W.templates = (await api("/api/export-templates")).templates;
  } catch (e) { toast(e.message, "err"); }
  if (W.context === "project") {
    try { W.projVersions = (await api(`/api/projects/${W.project.pid}/versions`)).versions; } catch { }
    W.scopeType = "project";
    if (!W.templateId) {
      const first = W.templates.find((t) => t.tname === "specimens");
      if (first) { W.templateId = first.id; W.tplName = first.name; W.tplRev = first.revision ?? 1; }
    }
    await bindScope();
  }
  render();
}

/* ---------------- 协作编辑包（v3.62/v3.69）：离线编辑 → 三方合并 ---------------- */
const PKG_STATUS = { active: ["进行中", "st st-saved"], merged: ["已合并", "st st-linked"], revoked: ["已收回", "st st-readonly"] };

async function renderCollab() {
  const host = $("view-collab");
  host.innerHTML = `<div class="toolbar">
      <button class="btn" id="cb-new">${icon("plus")} 新建协作包</button>
      <button class="btn" id="cb-refresh">${icon("restore")} 刷新</button>
      <span class="stat" id="cb-stat"></span>
    </div>
    <p class="hint" style="margin:6px 2px">流程：创建包（选行 + 允许编辑的列）→ 下载工作簿发给协作者 → 协作者离线改（Excel 只带包 ID+数据，不含账号/令牌）→ 这里上传回传，服务器按「基线 vs 当前 vs 回传」三方合并：非冲突自动应用；同字段双方修改需审阅（可 force 以回传为准，逐处留审计）；删行不代表删除记录。</p>
    <div id="cb-body"><p class="hint">加载中…</p></div>`;
  $("cb-new").onclick = pkgCreate;
  $("cb-refresh").onclick = renderCollab;
  let d;
  try { d = await api("/api/collab/packages"); } catch (e) {
    $("cb-body").innerHTML = `<p class="hint warn">${esc(e.message)}</p>`; return;
  }
  $("cb-stat").textContent = `共 ${d.packages.length} 个包`;
  $("cb-body").innerHTML = d.packages.length ? `<table class="about-table" style="width:100%">
    <thead><tr><th>包 ID</th><th>创建者</th><th>可编辑列</th><th>行数</th><th>状态</th><th>创建时间</th><th>操作</th></tr></thead>
    <tbody>${d.packages.map((p) => {
    const st = PKG_STATUS[p.status] || [p.status, ""];
    return `<tr><td><code>${esc(p.pkg_id)}</code></td><td>${esc(p.created_by)}</td>
      <td>${esc(p.allowed_cols.join("、"))}</td><td>${p.rows_n}</td>
      <td><span class="${st[1]}">${st[0]}</span></td><td>${esc(p.created_at || "")}</td>
      <td style="white-space:nowrap">
        <button class="btn primary" data-a="rcv" data-id="${p.pkg_id}" ${p.status === "active" && canWriteTable(p.tname) ? "" : "disabled"}>回传合并</button>
        <button class="btn" data-a="wb" data-id="${p.pkg_id}" ${p.status === "active" ? "" : "disabled"}>下载工作簿</button>
        ${rowMenu([
          { act: "det", label: "查看详情" },
          ...(S.me.admin && p.status === "active"
            ? [{ act: "rvk", label: "收回此包", danger: true, title: "收回后回传将被明确阻止" }] : []),
        ])}
      </td></tr>`;
  }).join("")}</tbody></table>` : `<p class="hint">还没有协作包。点「新建协作包」开始。</p>`;
  wireRowMenus(host);
  host.querySelectorAll("[data-a]").forEach((b) => (b.onclick = async () => {
    const p = d.packages.find((x) => x.pkg_id === b.dataset.id);
    if (!p) return;
    if (b.dataset.a === "wb") { try { await downloadFile(`/api/collab/packages/${p.pkg_id}/workbook`); } catch (e) { toast(e.message, "err"); } }
    else if (b.dataset.a === "rcv") pkgReceive(p);
    else if (b.dataset.a === "det") pkgDetail(p);
    else if (b.dataset.a === "rvk") {
      if (await confirmBox(`收回包 ${p.pkg_id}？收回后回传将被明确阻止（不可合并）。`)) {
        try { await api(`/api/collab/packages/${p.pkg_id}/revoke`, { method: "POST", body: "{}" }); renderCollab(); } catch (e) { toast(e.message, "err"); }
      }
    }
  }));
}

async function pkgCreate() {
  openModal("新建协作包（实体选择器：粘贴编号即可，无需内部 ID）", `
    <div id="pc-picker"></div>
    <label>允许编辑的列（勾选即可；编号列与关联只读字段自动不可选）</label>
    <div id="pc-cols"></div>
    <div class="form-actions"><button class="btn" id="pc-cancel">取消</button>
      <button class="btn primary" id="pc-go" disabled>创建包</button></div>
    <div id="pc-done"></div>
    <div id="pc-err"></div>`);
  $("pc-cancel").onclick = closeModal;
  const goBtn = $("pc-go");
  let picker, pcCols;
  const pcTarget = "collab-new";
  const pcRestored = formDraftLoad("collab", pcTarget);
  if (pcRestored) {
    $("pc-done").insertAdjacentHTML("beforebegin",
      `<div class="dash-card" style="margin-bottom:8px;display:flex;gap:10px;align-items:center">
        <span class="hint">已恢复 ${Math.max(1, Math.round((Date.now() - pcRestored.at) / 60000))} 分钟前保存的本机草稿</span>
        <button class="btn danger" id="pc-dropdraft" type="button">丢弃草稿</button></div>`);
    $("pc-dropdraft").onclick = () => { formDraftDrop("collab", pcTarget); closeModal(); pkgCreate(); };
  }
  picker = renderEntityPicker($("pc-picker"), { tables: ["specimens"], multi: true,
    placeholder: "粘贴标本编号（每行/Tab/逗号分隔）；重复、失配、多候选可在此修正" });
  pcCols = renderEditableCols($("pc-cols"), { tname: "specimens",
    value: pcRestored ? (pcRestored.data.cols || ["Notes", "DetBy", "DetDate"]) : ["Notes", "DetBy", "DetDate"],
    onChange: () => { goBtn.disabled = !picker.ready() || !picker.selected().length || !pcCols.get().length; } });
  if (pcRestored && (pcRestored.data.rids || []).length) picker.preselect(pcRestored.data.rids);
  goBtn.disabled = true;
  let pcDirty = !!pcRestored;
  const pcGuardId = "guard:" + pcTarget;
  $("modal-body").addEventListener("input", () => { pcDirty = true; }, true);
  registerFormGuard(pcGuardId, {
    dirty: () => pcDirty,
    canSaveLeave: () => picker.ready() && picker.selected().length > 0 && pcCols.get().length > 0,
    saveLeave: async () => { const okBefore = !goBtn.disabled; if (okBefore) goBtn.click(); return okBefore && !document.getElementById("pc-go"); },
    keepDraft: () => formDraftSave("collab", pcTarget, {
      rids: picker.selected().map((m) => ({ tname: m.tname, rid: m.rid, key: m.key })),
      cols: pcCols.get() }),
    discard: () => { formDraftDrop("collab", pcTarget); pcDirty = false; },
  });
  setTopModalGuard({ id: pcGuardId, dirty: () => formGuards.get(pcGuardId).dirty(),
    canSaveLeave: () => formGuards.get(pcGuardId).canSaveLeave ? formGuards.get(pcGuardId).canSaveLeave() : false,
    saveLeave: formGuards.get(pcGuardId).saveLeave, keepDraft: () => formGuards.get(pcGuardId).keepDraft(), discard: () => formGuards.get(pcGuardId).discard() });
  goBtn.onclick = async () => {
    const refs = picker.selected();
    if (!refs.length) { toast("请先解析并选择标本", "err"); return; }
    const allowed_cols = pcCols.get();
    if (!allowed_cols.length) { toast("请至少勾选一列", "err"); return; }
    goBtn.disabled = true;
    try {
      const r = await api("/api/collab/packages", { method: "POST",
        body: JSON.stringify({ rids: refs.map((m) => m.rid), allowed_cols }) });
      $("pc-done").innerHTML = `<p class="hint" style="color:var(--ok)">已创建 ${esc(r.pkg_id)}（${r.rows} 行）。</p>
        <div class="form-actions"><button class="btn primary" id="pc-dl">${icon("download")} 下载工作簿（发给协作者）</button></div>`;
      $("pc-dl").onclick = async () => { try { await downloadFile(`/api/collab/packages/${r.pkg_id}/workbook`); } catch (e) { toast(e.message, "err"); } };
    } catch (e) {
      const errEl = $("pc-err");
      if (errEl) errEl.innerHTML = `<p class="hint warn">创建失败：${esc(e.message)}（已保留所选内容，可修正后重试）</p>`;
      toast(e.message, "err"); goBtn.disabled = false;
    }
  };
}

function pkgDetail(p) {
  api(`/api/collab/packages/${p.pkg_id}`).then((d) => {
    const cols = d.rows.length ? Object.keys(d.rows[0].base_values) : [];
    openModal(`包详情 · ${p.pkg_id}`, `
      <p class="hint">状态 ${d.package.status} · 创建者 ${esc(d.package.created_by)} · 行数 ${d.rows.length}。基线值是创建包时的快照，回传时与服务器当前值三方比较。</p>
      <div style="overflow:auto;max-height:55vh"><table class="about-table" style="width:100%">
      <thead><tr><th>记录 ID</th><th>行版本</th>${cols.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead>
      <tbody>${d.rows.map((r) => `<tr><td>${r.rid}</td><td>${r.base_row_version}</td>${cols.map((c) => `<td>${esc(r.base_values[c] ?? "")}</td>`).join("")}</tr>`).join("")}</tbody>
      </table></div>`);
    $("modal").classList.add("modal-wide");
  }).catch((e) => toast(e.message, "err"));
}

/* 冲突字段级审阅（v3.80）：三值并列/筛选/逐项决策/批量（先展示范围再确认）；提交重检后部分应用 */
function renderPendingReview(host, p, onDone) {
  let pending = null, kw = "", onlyUndecided = true;
  const decisions = new Map();   // "rid|col" → "accept"|"keep"
  host.innerHTML = '<p class="hint">读取待处理审阅…</p>';
  async function load() {
    try {
      const resp = await api(`/api/collab/packages/${p.pkg_id}/pending`);
      pending = resp && resp.pending !== undefined ? resp : null;
    }
    catch (e) { host.innerHTML = `<p class="hint warn">${esc(e.message)}</p>`; return; }
    if (!pending || !(pending.field_items || []).length) {
      host.innerHTML = '<p class="hint">该包没有待处理审阅项。</p>';
      return;
    }
    paint();
  }
  function visibleItems() {
    return (pending.field_items || []).filter((f) => {
      if (onlyUndecided && (pending.statuses || {})[f.rid + "|" + f.col] !== undefined) return false;
      if (!kw) return true;
      return (f.key || "").toLowerCase().includes(kw) || (f.col || "").toLowerCase().includes(kw)
        || (f.col_label || "").toLowerCase().includes(kw);
    });
  }
  function valCell(v, disp) {
    const text = disp ?? (v === null ? "（空）" : String(v));
    const long = text.length > 24;
    return `<span class="pv" title="${esc(text)}" style="display:inline-block;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;vertical-align:bottom">${esc(text)}${long ? " …" : ""}</span>`;
  }
  function paint() {
    const items = visibleItems();
    const undec = (pending.field_items || []).filter((f) => (pending.statuses || {})[f.rid + "|" + f.col] === "undecided");
    const rows = items.map((f) => {
      const sk = f.rid + "|" + f.col;
      const dec = decisions.get(sk);
      const st = (pending.statuses || {})[sk] || "undecided";
      const stBadge = { applied: '<span class="st st-saved">已应用</span>',
                        kept: '<span class="st st-readonly">已保留当前</span>' }[st]
                      || (st === "undecided" ? '<span class="st st-pending">未决</span>' : esc(st));
      return `<tr data-sk="${esc(sk)}">
        <td><b>${esc(f.key)}</b></td><td>${esc(f.col_label)}<div class="hint">${esc(f.col || "")}</div></td>
        <td>${valCell(f.base, (f.base_disp || {}).disp)}</td>
        <td>${valCell(f.server, (f.server_disp || {}).disp)}</td>
        <td>${valCell(f.returned, (f.returned_disp || {}).disp)}</td>
        <td>${stBadge}</td>
        <td style="white-space:nowrap">
          <button class="btn ${dec === "keep" ? "primary" : ""}" data-dec="keep" data-sk="${esc(sk)}"
            ${st !== "undecided" ? "disabled" : ""} title="保留服务器当前值，忽略回传">保留当前</button>
          <button class="btn ${dec === "accept" ? "primary" : ""}" data-dec="accept" data-sk="${esc(sk)}"
            ${st !== "undecided" ? "disabled" : ""} title="以回传值覆盖服务器当前值（提交时重新核验）">接受回传</button>
        </td></tr>`;
    }).join("");
    host.innerHTML = `
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:6px">
        <input id="pv-kw" class="input" style="max-width:220px" placeholder="筛选：编号 / 字段名…" value="${esc(kw)}">
        <label><input type="checkbox" id="pv-undec" ${onlyUndecided ? "checked" : ""}> 只看未决</label>
        <span class="hint">未决 ${undec.length} 项 · 已选决策 ${decisions.size} 项${pending.pending_updates ? ` · 另有 ${pending.pending_updates} 行非冲突修改待应用` : ""}</span>
      </div>
      <div style="overflow:auto;max-height:40vh"><table class="about-table" style="width:100%">
        <thead><tr><th>业务编号</th><th>字段</th><th>基线值（建包时）</th><th>服务器当前值</th><th>回传值</th><th>状态</th><th>决策</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="7" class="hint">没有符合筛选的项。</td></tr>'}</tbody></table></div>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:8px">
        <span class="hint">批量（对当前可见 ${items.length} 项）：</span>
        <button class="btn" id="pv-bkeep" ${items.length ? "" : "disabled"}>全部保留当前</button>
        <button class="btn" id="pv-bacc" ${items.length ? "" : "disabled"}>全部接受回传</button>
        <span class="hint" id="pv-scope"></span>
      </div>
      <div class="form-actions"><button class="btn primary" id="pv-submit" ${decisions.size ? "" : "disabled"}>
        提交 ${decisions.size} 项决策（提交时逐项重新核验）</button></div>
      <div id="pv-out"></div>`;
    $("pv-kw").oninput = (e) => { kw = e.target.value; paint(); };
    $("pv-undec").onchange = (e) => { onlyUndecided = e.target.checked; paint(); };
    host.querySelectorAll("[data-dec]").forEach((b) => (b.onclick = () => {
      const sk = b.dataset.sk, dec = b.dataset.dec;
      if (decisions.get(sk) === dec) decisions.delete(sk); else decisions.set(sk, dec);
      paint();
    }));
    const batch = (dec2) => {
      const scope = `将对当前可见的 ${items.length} 项（未决）执行「${dec2 === "keep" ? "保留当前值" : "接受回传值"}」`;
      const scopeEl = $("pv-scope");
      scopeEl.innerHTML = `<b>${esc(scope)}</b> <button class="btn danger" id="pv-bok">确认执行</button>`;
      $("pv-bok").onclick = () => {
        scopeEl.innerHTML = "";
        for (const f of items) decisions.set(f.rid + "|" + f.col, dec2);
        paint();
      };
    };
    $("pv-bkeep").onclick = () => batch("keep");
    $("pv-bacc").onclick = () => batch("accept");
    const sub = $("pv-submit");
    if (sub) sub.onclick = async () => {
      sub.disabled = true;
      try {
        const decs = [...decisions.entries()].map(([sk2, decision]) => {
          const [rid, col] = sk2.split("|");
          return { rid: +rid, col, decision };
        });
        const r = await api(`/api/collab/packages/${p.pkg_id}/resolve`, { method: "POST",
          body: JSON.stringify({ decisions: decs }) });
        $("pv-out").innerHTML = `<div class="dash-card">
          <b>本次结果</b>：应用 ${r.applied.length} · 保留 ${r.kept.length} · 回冲突 ${r.reconflicts.length}
          ${r.merged ? ' · <span class="st st-saved">包已全部合并</span>' : ` · 剩余待处理 ${r.remaining}`}</div>
          ${r.reconflicts.length ? `<div class="hint warn">${esc(r.reconflicts.map((x) => `#${x.rid} ${x.col}：${x.reason}`).join("；"))}</div>` : ""}`;
        if (r.merged) { toast("协作包已全部合并", "ok"); setTimeout(() => { closeModal(); renderCollab(); }, 1200); }
        else {
          decisions.clear();
          await load(); paint();
        }
      } catch (e) { $("pv-out").innerHTML = `<p class="hint warn">${esc(e.message)}</p>`; }
      if (sub) sub.disabled = false;
    };
  }
  load();
}

function pkgReceive(p) {
  openModal(`回传合并 · ${p.pkg_id}`, `
    <p class="hint">上传协作者改好的工作簿（xlsx / csv / tsv）。三方合并：非冲突自动应用；冲突逐条审阅。</p>
    <input type="file" id="pr-file" accept=".xlsx,.xlsm,.csv,.tsv" class="input">
    <div class="form-actions"><button class="btn" id="pr-cancel">关闭</button>
      <button class="btn primary" id="pr-go">${icon("upload")} 上传并合并</button></div>
    <div id="pr-out"></div>`);
  $("pr-cancel").onclick = closeModal;
  const run = async (force) => {
    const f = $("pr-file").files[0];
    if (!f) { toast("请选择回传文件", "err"); return; }
    const fd = new FormData();
    fd.append("file", f);
    if (force) fd.append("force", "1");
    $("pr-go").disabled = true;
    let r;
    try {
      r = await fetch(`/api/collab/packages/${p.pkg_id}/receive-file`, { method: "POST", headers: { "X-Auth": token() }, body: fd });
    } catch (e) { toast(e.message, "err"); $("pr-go").disabled = false; return; }
    const j = await r.json().catch(() => ({}));
    $("pr-go").disabled = false;
    if (r.ok) {
      $("pr-out").innerHTML = `<p class="hint" style="color:var(--ok)">合并完成：应用 ${j.applied} 行${j.forced && j.forced.length ? `，force 压过 ${j.forced.length} 处同字段冲突（已留审计）` : ""}，无变化 ${j.noop} 行${j.skipped_n ? `；文件中 ${j.skipped_n} 个编号未在库中（见下）` : ""}。包已置为「已合并」。</p>
        ${j.skipped_n ? `<p class="hint warn">未解析编号：${esc((j.skipped_numbers || []).map((x) => x.number).join("、"))}</p>` : ""}
        <div class="form-actions"><button class="btn primary" id="pr-done">完成并刷新列表</button></div>`;
      $("pr-done").onclick = () => { closeModal(); renderCollab(); };
      $("pr-go").disabled = true;
      toast("协作包已合并", "ok");
      return;
    }
    // 冲突：切换到字段级审阅界面（业务编号/中文列名/基线/当前/回传三值；逐项与批量决策；提交重检）
    $("pr-out").innerHTML = `<p class="hint warn">检出冲突——切换到字段级审阅…</p>`;
    renderPendingReview($("pr-out"), p, () => renderCollab());
  };
  $("pr-go").onclick = () => run(false);
}

/* ---------------- 流转与借出（v3.55/v3.70） ---------------- */
const MOV_KINDS = { accession: "入库", move: "移盒", tissue_sample: "组织取样",
  loan: "借出", return: "归还", transfer: "转赠", loss: "损耗" };
const MOV_MOVABLE = ["specimens", "dali", "swu"];

async function renderMovements() {
  const host = $("view-movements");
  host.innerHTML = `<div class="toolbar">
      <button class="btn" id="mv-new">${icon("plus")} 登记流转</button>
      <button class="btn" id="mv-refresh">${icon("restore")} 刷新</button>
      <span class="stat" id="mv-stat"></span>
    </div><div id="mv-body"><p class="hint">加载中…</p></div>`;
  $("mv-new").onclick = () => movDialog();
  $("mv-refresh").onclick = renderMovements;
  let loans, recent;
  try {
    [loans, recent] = await Promise.all([api("/api/loans"), api("/api/movements?limit=100")]);
  } catch (e) { $("mv-body").innerHTML = `<p class="hint warn">${esc(e.message)}</p>`; return; }
  const outQty = (l) => l.qty_unknown
    ? `<span class="hint" title="借出/归还存在未记数量的流转，保持未知">未知</span>`
    : esc(l.still_out ?? 0);
  $("mv-stat").textContent = `借出在外 ${loans.loans.length} 条 · 近期流转 ${recent.items.length} 条`;
  $("mv-body").innerHTML = `
    <div class="about-card"><h3>借出台账（仍在外）</h3>
      ${loans.loans.length ? `<table class="about-table" style="width:100%"><thead><tr>
        <th>标本</th><th>表</th><th>借给</th><th>期限</th><th>借出数量</th><th>归还次数</th><th>仍在外</th><th>最近事件</th><th>操作</th>
      </tr></thead><tbody>${loans.loans.map((l) => `
        <tr><td><b>${esc(l.key ?? l.rid)}</b></td><td>${esc(l.tname)}</td><td>${esc(l.counterparty || "")}</td>
        <td>${esc(l.due_date || "")}</td><td>${l.qty_unknown ? "未知" : esc(l.qty ?? "")}</td>
        <td>${l.returns}</td><td><b>${outQty(l)}</b></td><td>${esc(l.last_event || "")}</td>
        <td><button class="btn" data-ret="${l.tname}|${l.rid}" ${canWriteTable(l.tname) ? "" : "disabled"}>归还</button></td>
      </tr>`).join("")}</tbody></table>` : `<p class="hint">暂无借出记录。</p>`}
      <p class="hint">仍在外数量按「借出合计 − 归还合计」计算；数量未知时保持未知，不当 0。归还允许部分数量。</p></div>
    <div class="about-card"><h3>近期流转</h3>
      ${recent.items.length ? `<table class="about-table" style="width:100%"><thead><tr>
        <th>时间</th><th>类型</th><th>标本</th><th>数量</th><th>位置变化</th><th>对方 / 说明</th><th>操作人</th><th>操作</th>
      </tr></thead><tbody>${recent.items.map((m) => `
        <tr><td>${esc(m.created_at || "")}</td><td><span class="st ${m.kind === "loan" ? "st-pending" : m.kind === "return" ? "st-saved" : "st-readonly"}">${MOV_KINDS[m.kind] || m.kind}</span></td>
        <td><b>${esc(m.key ?? m.rid)}</b></td><td>${m.qty ?? "—"}</td>
        <td>${esc(m.from_pos || "")}${m.to_pos ? " → " + esc(m.to_pos) : ""}</td>
        <td>${esc([m.counterparty, m.note, m.ref].filter(Boolean).join(" · ") || "")}</td>
        <td>${esc(m.op_user || "")}</td>
        <td>${m.undo_of ? `<span class="hint">已回退（原事件 #${m.undo_of}）</span>` : `<button class="btn danger" data-undo="${m.id}" title="回退此条流转的改动；有后续修改时会被阻止" ${canWriteTable(m.tname) ? "" : "disabled"}>回退</button>`}</td>
      </tr>`).join("")}</tbody></table>` : `<p class="hint">还没有流转记录。历史存量不伪造移动——从「登记流转」开始记录。</p>`}
      <p class="hint">撤销有后续依赖流转时会被阻止（应生成纠正事件，不回写覆盖历史）；借出未归还时移盒需显式确认。</p></div>`;
  host.querySelectorAll("[data-ret]").forEach((b) => (b.onclick = () => {
    const [tname, rid] = b.dataset.ret.split("|");
    const l = loans.loans.find((x) => x.tname === tname && String(x.rid) === rid);
    movDialog({ tname, rid: +rid, kind: "return", counterparty: l ? l.counterparty : "" });
  }));
  host.querySelectorAll("[data-undo]").forEach((b) => (b.onclick = async () => {
    if (!(await confirmBox("回退这条流转的改动？仅撤销该事件本身的影响；有后续修改时会被阻止（此时应登记一条纠正事件）。", { danger: false }))) return;
    try { const r = await api(`/api/movements/${b.dataset.undo}/undo`, { method: "POST", body: "{}" });
      toast(`已撤销（批次 ${r.batch || ""}）`, "ok"); renderMovements(); } catch (e) { toast(e.message, "err"); }
  }));
}

function movDialog(pref = {}) {
  const movable = MOV_MOVABLE.filter((t) => S.meta.tables[t]);
  const movTarget = "mov:" + (pref.rid || "new");
  openModal("登记流转事件", `
    <label>流转类型<select id="md-kind" class="input">${Object.entries(MOV_KINDS).map(([k, v]) => `<option value="${k}" ${pref.kind === k ? "selected" : ""}>${v}</option>`).join("")}</select></label>
    <label>标本（实体选择器覆盖标本/借入台账：粘贴编号即可；同号跨表会要求人工确认所属表）</label>
    <div id="md-picker"></div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px 14px">
      <label>数量（未知留空，不当 0）<input id="md-qty" class="input" inputmode="numeric"></label>
      <label>对方单位/人（借出/归还/转赠）<input id="md-cp" class="input" value="${esc(pref.counterparty || "")}"></label>
      <label id="md-move1" style="display:none">目标盒号（移盒）<input id="md-box" class="input" placeholder="如 MHBU-BOX-017"></label>
      <label id="md-move2" style="display:none">目标孔位（移盒）<input id="md-hole" class="input" placeholder="如 B3"></label>
      <label>归还期限（借出，YYYY-MM-DD）<input id="md-due" class="input" type="date"></label>
      <label>凭据/依据<input id="md-ref" class="input" placeholder="送检单/邮件/台账页码"></label>
      <label style="grid-column:1/-1">说明<input id="md-note" class="input"></label>
      <label id="md-forcewrap" style="display:none;grid-column:1/-1"><input type="checkbox" id="md-force"> 确认实物在库，强制移盒（借出未归还时需说明原因）</label>
    </div>
    <p class="hint">移盒会同步更新盒/孔字段（行版本条件更新，并发同孔 409）；借出未归还时不能重复借出；历史不回写，错误用纠正事件。</p>
    <div class="form-actions"><button class="btn" id="md-cancel">取消</button>
      <button class="btn primary" id="md-go">登记</button></div>`);
  $("md-cancel").onclick = closeModal;
  const goBtn = $("md-go");
  const mvRestored = !pref.rid ? formDraftLoad("mov", "new") : null;
  let picker;
  picker = renderEntityPicker($("md-picker"), { tables: movable, multi: false, compact: true,
    placeholder: "标本编号（精确优先，前缀/名称辅助；跨表同号需人工确认所属表）",
    preselected: pref.rid ? [{ tname: pref.tname || "specimens", rid: pref.rid, key: pref.key || ("#" + pref.rid) }]
      : (mvRestored && mvRestored.data.entity ? [mvRestored.data.entity] : []),
    onChange: (st) => { goBtn.disabled = !st.ready || st.selected.length !== 1; } });
  const kindSel = $("md-kind");
  if (mvRestored) {
    for (const [id, v] of [["md-kind", mvRestored.data.kind], ["md-qty", mvRestored.data.qty],
        ["md-cp", mvRestored.data.cp], ["md-box", mvRestored.data.to_box], ["md-hole", mvRestored.data.to_hole],
        ["md-due", mvRestored.data.due_date], ["md-ref", mvRestored.data.ref], ["md-note", mvRestored.data.note]]) {
      const el = $(id);
      if (el && v) el.value = v;
    }
    $("modal-body").insertAdjacentHTML("afterbegin",
      `<div class="dash-card" id="mv-restore-note" style="margin-bottom:8px;display:flex;gap:10px;align-items:center">
        <span class="hint">已恢复 ${Math.max(1, Math.round((Date.now() - mvRestored.at) / 60000))} 分钟前保存的本机草稿——正重新核验所选标本的当前状态…</span>
        <button class="btn danger" id="mv-dropdraft" type="button">丢弃草稿</button></div>`);
    $("mv-dropdraft").onclick = () => { formDraftDrop("mov", movTarget); closeModal(); movDialog(pref); };
  }
  let mvDirty = false;
  $("modal-body").addEventListener("input", () => { mvDirty = true; }, true);
  const mvGuardId = "guard:" + movTarget;
  const mvCollect = () => ({
    kind: kindSel.value, qty: $("md-qty").value.trim(), cp: $("md-cp").value.trim(),
    to_box: $("md-box") ? $("md-box").value.trim() : "", to_hole: $("md-hole") ? $("md-hole").value.trim() : "",
    due_date: $("md-due").value || "", ref: $("md-ref").value.trim(), note: $("md-note").value.trim(),
    entity: picker.selected()[0] ? { tname: picker.selected()[0].tname, rid: picker.selected()[0].rid, key: picker.selected()[0].key } : null,
  });
  registerFormGuard(mvGuardId, {
    dirty: () => mvDirty,
    canSaveLeave: () => false,      // 流转登记没有「保存后离开」——只有「登记」一个提交动作
    keepDraft: () => formDraftSave("mov", movTarget, mvCollect()),
    discard: () => { formDraftDrop("mov", movTarget); mvDirty = false; },
  });
  setTopModalGuard({ id: mvGuardId, dirty: () => formGuards.get(mvGuardId).dirty(),
    canSaveLeave: () => formGuards.get(mvGuardId).canSaveLeave ? formGuards.get(mvGuardId).canSaveLeave() : false,
    saveLeave: formGuards.get(mvGuardId).saveLeave, keepDraft: () => formGuards.get(mvGuardId).keepDraft(), discard: () => formGuards.get(mvGuardId).discard() });
  if (mvRestored && mvRestored.data.entity) {
    // 恢复草稿时重新核验所选标本（身份/权限/当前状态）：已删除或无权限 → 明确提示并移除
    api(`/api/rows/${mvRestored.data.entity.tname}/${mvRestored.data.entity.rid}`)
      .then((d) => {
        if (d.row.deleted) { toast("草稿中的标本已在回收站——请重新选择", "warn"); picker.invalidate(); }
        else if (!picker.selected().length) picker.preselect([{ ...mvRestored.data.entity }]);
      })
      .catch(() => { toast("草稿中的标本已不可用（不存在或无权限）——请重新选择", "warn"); });
  }
  const syncKind = () => {
    const k = kindSel.value;
    $("md-move1").style.display = $("md-move2").style.display = k === "move" ? "" : "none";
    $("md-forcewrap").style.display = k === "move" ? "" : "none";
    $("md-cp").parentElement.style.opacity = ["loan", "return", "transfer"].includes(k) ? 1 : 0.55;
  };
  kindSel.onchange = syncKind; syncKind();
  $("md-go").onclick = async () => {
    const m = picker.selected()[0];
    if (!m) { toast("请先通过选择器确定标本（多候选需人工点选）", "err"); return; }
    const body = { kind: kindSel.value, tname: m.tname, rid: m.rid,
      qty: $("md-qty").value.trim() || null, counterparty: $("md-cp").value.trim() || null,
      due_date: $("md-due").value || null, ref: $("md-ref").value.trim() || null,
      note: $("md-note").value.trim() || null };
    if (kindSel.value === "move") {
      body.to_box = $("md-box").value.trim(); body.to_hole = $("md-hole").value.trim();
      if ($("md-force").checked) { body.force = 1; body.note = body.note || "强制移盒：确认实物在库"; }
    }
    try {
      const r = await api("/api/movements", { method: "POST", body: JSON.stringify(body) });
      formDraftDrop("mov", movTarget);
      closeModal(); toast(`已登记 ${MOV_KINDS[r.kind]}：${m.key}（批次 ${r.batch}）`, "ok");
      if (S.t === "movements") renderMovements();
      if (!$("drawer").hidden) { const b = $("det-movlist"); if (b) loadDrawerMovements(S.t, S.drawerRid || m.rid); }
    } catch (e) { toast(e.message, "err"); }
  };
}

async function loadDrawerMovements(tname, rid) {
  const host = $("det-movlist");
  if (!host) return;
  try {
    const d = await api(`/api/rows/${tname}/${rid}/movements`);
    if (!d.items.length) { host.innerHTML = ""; return; }
    const ls = d.loan_state;
    host.innerHTML = `<div class="ck-card"><h3>流转记录 ${ls ? (ls.unknown ? "· 借出状态：数量未知" : `· 在外 ${ls.still_out}（借 ${ls.sent} / 还 ${ls.returned}）`) : ""}</h3>
      ${d.items.map((m) => `<div class="det-row" data-col="movements" style="padding:3px 0;border-bottom:1px dashed var(--border)">
        <b>${MOV_KINDS[m.kind] || m.kind}</b> ${m.qty ?? ""} ${esc(m.to_pos ? "→ " + m.to_pos : "")}
        ${esc(m.counterparty ? "· " + m.counterparty : "")} ${esc(m.note ? "· " + m.note : "")}
        <span class="hint">${esc(m.created_at || "")} · ${esc(m.op_user || "")}${m.undo_of ? " · 已撤销 #" + m.undo_of : ""}</span></div>`).join("")}</div>`;
  } catch { host.innerHTML = ""; }
}

async function loadDrawerCitations(tname, rid) {
  const host = $("det-citelist");
  if (!host) return;
  try {
    const d = await api(`/api/rows/${tname}/${rid}/citations`);
    if (!d.citations.length) { host.innerHTML = ""; return; }
    host.innerHTML = `<div class="ck-card"><h3>被引文献（${d.citations.length}）</h3>
      ${d.citations.map((c) => `<div class="det-row" data-col="citations" style="padding:3px 0;border-bottom:1px dashed var(--border)">
        <b>${esc(c.title)}</b> ${esc(c.authors || "")} ${esc(c.year ? "· " + c.year : "")}
        ${c.doi ? `<span class="hint">DOI:${esc(c.doi)}</span>` : ""}
        <span class="hint">原文编号：${esc(c.orig_number || "—")}${c.orig_text ? " · 原文引用：" + esc(c.orig_text.slice(0, 40)) : ""} · 引证于 ${esc(c.created_at || "")}</span></div>`).join("")}
      <p class="hint">原文引用与编号保持已发表原文；当前接受名以「历次鉴定/主库」为准并列呈现。</p></div>`;
  } catch { host.innerHTML = ""; }
}

/* ---------------- 实验板（v3.58/v3.70）：批次 / 板与孔位 / 混池 / 条码 / 定量 QC ---------------- */
const WELL_KIND_STYLE = { sample: ["●", "var(--c-saved)", "var(--c-saved-soft)"],
  control: ["◆", "var(--c-pending)", "var(--c-pending-soft)"],
  blank: ["○", "var(--c-readonly)", "var(--c-readonly-soft)"] };

async function renderLab() {
  const host = $("view-labpage");
  host.innerHTML = `<div class="toolbar">
      <button class="btn" id="lb-newbatch">${icon("plus")} 新建批次</button>
      <button class="btn" id="lb-newplate">${icon("plus")} 新建板</button>
      <button class="btn" id="lb-refresh">${icon("restore")} 刷新</button>
      <span class="stat" id="lb-stat"></span>
    </div><div id="lb-body"><p class="hint">加载中…</p></div>`;
  $("lb-refresh").onclick = renderLab;
  $("lb-newbatch").onclick = () => labSimpleInput("新建实验批次", "批次编号（唯一）", "lab-batch", (code, note) =>
    api("/api/lab/batches", { method: "POST", body: JSON.stringify({ code, note }) }).then(renderLab));
  $("lb-newplate").onclick = () => labNewPlate();
  let bt, pl, po, bp, qc;
  try {
    [bt, pl, po, bp, qc] = await Promise.all([
      api("/api/lab/batches"), api("/api/lab/plates"), api("/api/lab/pools"),
      api("/api/lab/barcode-pairs"), api("/api/lab/qc")]);
  } catch (e) { $("lb-body").innerHTML = `<p class="hint warn">${esc(e.message)}</p>`; return; }
  $("lb-stat").textContent = `批次 ${bt.batches.length} · 板 ${pl.plates.length} · 混池 ${po.pools.length} · 条码对 ${bp.pairs.length} · QC ${qc.records.length}`;
  $("lb-body").innerHTML = `
    <div class="about-card"><h3>实验批次</h3>
      ${bt.batches.length ? `<table class="about-table" style="width:100%"><thead><tr><th>批次</th><th>说明</th><th>送测</th><th>操作</th></tr></thead><tbody>
      ${bt.batches.map((b) => `<tr><td><b>${esc(b.code)}</b></td><td>${esc(b.note || "")}</td>
        <td>${b.sent_at ? `已送测 v${b.sent_version}（${esc(b.sent_at)}）` : `<span class="hint">未送测</span>`}</td>
        <td>${b.sent_at ? "" : `<button class="btn" data-send="${b.id}" ${canWriteTable("uce") ? "" : "disabled"}>标记送测</button>`}</td></tr>`).join("")}
      </tbody></table>` : `<p class="hint">还没有批次。</p>`}
      <p class="hint">送测绑定批次版本：发送后更改保留修订（新版本行），不覆盖已送出的内容。</p></div>
    <div class="about-card"><h3>板与孔位</h3>
      ${pl.plates.length ? `<table class="about-table" style="width:100%"><thead><tr><th>板</th><th>规格</th><th>批次</th><th>说明</th><th>操作</th></tr></thead><tbody>
      ${pl.plates.map((p) => `<tr><td><b>${esc(p.code)}</b></td><td>${p.spec} 孔</td>
        <td>${esc((bt.batches.find((x) => x.id === p.batch_id) || {}).code || "")}</td><td>${esc(p.note || "")}</td>
        <td><button class="btn" data-plate="${p.id}">孔位图 / 占孔</button></td></tr>`).join("")}
      </tbody></table>` : `<p class="hint">还没有板。</p>`}</div>
    <div class="about-card"><h3>混池</h3>
      ${po.pools.length ? po.pools.map((p) => `<div style="margin-bottom:8px"><b>${esc(p.code)}</b> <span class="hint">${esc(p.note || "")} · ${p.members.length} 成员</span>
        <button class="btn" data-pool="${p.id}">加成员</button>
        <div class="hint">${esc(p.members.map((m) => `${m.udi5 || "?"}+${m.udi7 || "?"}`).join("，"))}</div></div>`).join("")
        : `<p class="hint">还没有混池。</p>`}
      <div class="form-actions" style="margin:0"><button class="btn" id="lb-newpool">${icon("plus")} 新建混池</button></div>
      <p class="hint">同一有效混池内 i5/i7 组合不得重复（8bp ACGTN，来自实测规则；其他策略需实验室确认后修改）。</p></div>
    <div class="about-card"><h3>条码对（源序列照录，不覆盖）</h3>
      ${bp.pairs.length ? `<table class="about-table" style="width:100%"><thead><tr><th>UDI5</th><th>UDI7</th><th>i5 原序列</th><th>i7 原序列</th><th>方向</th><th>来源</th></tr></thead><tbody>
      ${bp.pairs.map((x) => `<tr><td>${esc(x.udi5 || "")}</td><td>${esc(x.udi7 || "")}</td><td><code>${esc(x.i5_seq_raw)}</code></td><td><code>${esc(x.i7_seq_raw)}</code></td><td class="hint">i5:${esc(x.i5_dir)} / i7:${esc(x.i7_dir)}</td><td>${esc(x.source || "")}</td></tr>`).join("")}
      </tbody></table>` : `<p class="hint">还没有条码对。</p>`}
      <div class="form-actions" style="margin:0"><button class="btn" id="lb-newpair">${icon("plus")} 登记条码对</button></div>
      <p class="hint">导出转换按仪器/软件/试剂配置绑定（config.LAB_EXPORT_CONVERT），数据库永远保留源序列。</p></div>
    <div class="about-card"><h3>定量测量（QC）</h3>
      ${qc.records.length ? `<table class="about-table" style="width:100%"><thead><tr><th>对象</th><th>值</th><th>单位</th><th>方法</th><th>仪器</th><th>采用</th><th>时间</th></tr></thead><tbody>
      ${qc.records.map((x) => `<tr><td>${esc(x.obj_table)}#${x.obj_rid}</td><td><b>${esc(x.value)}</b></td><td>${esc(x.unit || "")}</td><td>${esc(x.method || "")}</td><td>${esc(x.instrument || "")}</td>
        <td>${x.adopted ? `<span class="st st-saved">采用</span><span class="hint">${esc(x.adopt_reason || "")}</span>` : `<span class="hint">未采用</span>`}</td><td class="hint">${esc(x.created_at || "")}</td></tr>`).join("")}
      </tbody></table>` : `<p class="hint">还没有测量记录。</p>`}
      <div class="form-actions" style="margin:0"><button class="btn" id="lb-newqc">${icon("plus")} 登记测量</button></div>
      <p class="hint">单位不明不自动换算；多次测量都可保存，采用哪次需给理由。</p></div>`;
  host.querySelectorAll("[data-send]").forEach((b) => (b.onclick = async () => {
    try { const r = await api(`/api/lab/batches/${b.dataset.send}/send`, { method: "POST", body: "{}" });
      toast(`已标记送测（v${r.sent_version}）`, "ok"); renderLab(); } catch (e) { toast(e.message, "err"); }
  }));
  host.querySelectorAll("[data-plate]").forEach((b) => (b.onclick = () =>
    labPlateWells(+b.dataset.plate, pl.plates.find((x) => x.id === +b.dataset.plate))));
  host.querySelectorAll("[data-pool]").forEach((b) => (b.onclick = () =>
    labPoolMember(+b.dataset.pool)));
  $("lb-newpool").onclick = () => labSimpleInput("新建混池", "混池编号（唯一）", "lab-pool", (code, note) =>
    api("/api/lab/pools", { method: "POST", body: JSON.stringify({ code, note }) }).then(renderLab));
  $("lb-newpair").onclick = () => labNewPair();
  $("lb-newqc").onclick = () => labNewQc();
}

function labSimpleInput(title, label, idPrefix, submit) {
  openModal(title, `
    <label>${label}<input id="${idPrefix}-code" class="input"></label>
    <label>说明<input id="${idPrefix}-note" class="input"></label>
    <div class="form-actions"><button class="btn" id="${idPrefix}-cancel">取消</button>
      <button class="btn primary" id="${idPrefix}-go">创建</button></div>`);
  $(`${idPrefix}-cancel`).onclick = closeModal;
  $(`${idPrefix}-go`).onclick = async () => {
    const code = $(`${idPrefix}-code`).value.trim();
    if (!code) { toast("编号必填", "err"); return; }
    try { await submit(code, $(`${idPrefix}-note`).value.trim() || null); closeModal(); }
    catch (e) { toast(e.message, "err"); }
  };
}

function labNewPlate() {
  labSimpleInput("新建板", "板编号（唯一）", "lab-plate", async (code, note) => {
    const spec = $("lab-plate-spec").value;
    await api("/api/lab/plates", { method: "POST", body: JSON.stringify({ code, spec, note }) });
    renderLab();
  });
  const sel = document.createElement("label");
  sel.innerHTML = `规格<select id="lab-plate-spec" class="input"><option value="96">96 孔（8×12）</option><option value="384">384 孔（16×24）</option></select>`;
  $("lab-plate-code").parentElement.after(sel);
}

async function labPlateWells(pid, plate) {
  let d;
  try { d = await api(`/api/lab/plates/${pid}/wells`); }
  catch (e) { toast(e.message, "err"); return; }
  const spec = plate.spec === "384"
    ? { rows: [...Array(16)].map((_, i) => String.fromCharCode(65 + i)), cols: [...Array(24)].map((_, i) => i + 1) }
    : { rows: [...Array(8)].map((_, i) => String.fromCharCode(65 + i)), cols: [...Array(12)].map((_, i) => i + 1) };
  const cell = (g) => {
    const [sym, col, bg] = WELL_KIND_STYLE[g.well ? g.well.kind : ""] || ["", "", ""];
    const abnormal = g.well && g.well.status && g.well.status !== "ok";
    return `<div data-well="${g.row}${g.col}" title="${g.row}${g.col} ${esc(g.state)}${g.well && g.well.label ? " " + esc(g.well.label) : ""}"
      style="height:26px;border:1px solid var(--border);border-radius:4px;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:11px;
      ${g.well ? `background:${bg};color:${col};` : ""}${abnormal ? "outline:2px solid var(--c-risk);" : ""}">${g.well ? sym : ""}</div>`;
  };
  const grid = d.grid;
  openModal(`孔位图 · ${plate.code}（${plate.spec} 孔；点击孔位在下方内联占孔/编辑）`, `
    <div style="overflow:auto;max-height:50vh">
      <div style="display:inline-grid;grid-template-columns:34px repeat(${spec.cols.length},30px);gap:2px">
        <div></div>${spec.cols.map((c) => `<div class="hint" style="text-align:center">${c}</div>`).join("")}
        ${spec.rows.map((r) => `<div class="hint" style="display:flex;align-items:center">${r}</div>` +
          spec.cols.map((c) => cell(grid.find((g) => g.row === r && g.col === c))).join("")).join("")}
      </div></div>
    <div id="pw-form" style="margin-top:10px"><p class="hint">点击任一孔位开始占孔/编辑（本弹窗不会跳走）。</p></div>
    <p class="hint">●样本 ◆对照 ○空白；红框=异常状态（failed/qc_hold）。占孔对象可关联标本/UCE 台账行。</p>
    <div class="form-actions"><button class="btn" id="pw-close">关闭</button></div>`);
  $("modal").classList.add("modal-wide");
  $("pw-close").onclick = closeModal;
  const wellEl = (pos) => $("modal-body").querySelector(`[data-well="${pos}"]`);
  const paintCell = (g) => {
    const el = wellEl(g.row + g.col);
    if (!el) return;
    const [sym, col, bg] = WELL_KIND_STYLE[g.well ? g.well.kind : ""] || ["", "", ""];
    const abnormal = g.well && g.well.status && g.well.status !== "ok";
    el.style.cssText = "height:26px;border:1px solid var(--border);border-radius:4px;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:11px;"
      + (g.well ? `background:${bg};color:${col};` : "") + (abnormal ? "outline:2px solid var(--c-risk);" : "");
    el.innerHTML = g.well ? sym : "";
    el.title = `${g.row}${g.col} ${g.state}${g.well && g.well.label ? " " + g.well.label : ""}`;
  };
  const renderForm = (pos) => {
    const m = pos.match(/^([A-Z]+)(\d+)$/);
    if (!m) return;
    const g = grid.find((x) => x.row === m[1] && x.col === +m[2]);
    const w = g.well || {};
    $("pw-form").innerHTML = `
      <div class="dash-card"><b>孔位 ${pos}</b>${g.well ? ` <span class="hint warn">已占用（${esc(w.kind)}${w.obj_rid ? `，对象 ${esc(w.obj_table)}#${w.obj_rid}` : ""}）——保存将显式覆盖</span>` : ""}
      <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:6px 12px;margin-top:6px">
        <label>孔类型<select id="wf-kind" class="input">
          ${["sample", "control", "blank"].map((k) => `<option value="${k}" ${(w.kind || "sample") === k ? "selected" : ""}>${{ sample: "样本", control: "对照", blank: "空白" }[k]}</option>`).join("")}</select></label>
        <label>状态<input id="wf-status" class="input" value="${esc(w.status || "")}" placeholder="ok / failed / qc_hold"></label>
        <label>备注<input id="wf-note" class="input" value="${esc(w.note || "")}"></label>
      </div>
      <label style="display:block;margin-top:6px">关联标本（样本类型可用；实体选择器：粘贴编号/名称，无需内部 ID）</label>
      <div id="wf-picker"></div>
      <div class="form-actions" style="margin:6px 0 0"><button class="btn primary" id="wf-go">${g.well ? "覆盖写入" : "占孔"}</button>
        <span class="hint" id="wf-res"></span></div></div>`;
    let wpicker = null;
    wpicker = renderEntityPicker($("wf-picker"), { tables: ["specimens"], multi: false, compact: true, rows: 2,
      preselected: w.obj_rid && w.label ? [{ tname: w.obj_table || "specimens", rid: w.obj_rid, key: w.label }] : [],
      placeholder: "标本编号或名称（留空=不关联实体，仅作标签）" });
    $("wf-go").onclick = async () => {
      const kind = $("wf-kind").value;
      const picked = wpicker.selected()[0] || null;
      const well = { row: m[1], col: +m[2], kind,
        label: picked ? picked.key : null,
        status: $("wf-status").value.trim() || null, note: $("wf-note").value.trim() || null };
      if (picked) { well.obj_table = picked.tname; well.obj_rid = picked.rid; }
      $("wf-go").disabled = true;
      try {
        await api(`/api/lab/plates/${pid}/wells/preview`, { method: "POST", body: JSON.stringify({ wells: [well] }) });
        await api(`/api/lab/plates/${pid}/wells/commit`, { method: "POST",
          body: JSON.stringify({ wells: [well], allow_overwrite: true }) });
        g.well = { ...w, ...well };
        g.state = "occupied:" + kind;
        paintCell(g);
        $("wf-res").textContent = `✔ 孔 ${pos} 已写入${picked ? `（关联 ${picked.key}）` : ""}（可继续点其他孔位）`;
      } catch (e) { $("wf-res").textContent = "✕ " + e.message; }
      $("wf-go").disabled = false;
    };
  };
  $("modal-body").querySelectorAll("[data-well]").forEach((el) => (el.onclick = () => renderForm(el.dataset.well)));
}

function labPoolMember(pid) {
  openModal("混池加成员", `
    <label>UDI5（8bp ACGTN，可空）<input id="pm-u5" class="input" style="text-transform:uppercase"></label>
    <label>UDI7（8bp ACGTN，可空）<input id="pm-u7" class="input" style="text-transform:uppercase"></label>
    <label>板（可选）<select id="pm-plate" class="input"><option value="">不关联</option></select></label>
    <label>孔位（可选，如 A1）<input id="pm-well" class="input"></label>
    <div class="form-actions"><button class="btn" id="pm-cancel">取消</button>
      <button class="btn primary" id="pm-go">加入</button></div>`);
  api("/api/lab/plates").then((d) => {
    $("pm-plate").innerHTML += d.plates.map((p) => `<option value="${p.id}">${esc(p.code)}</option>`).join("");
  }).catch(() => { });
  $("pm-cancel").onclick = closeModal;
  $("pm-go").onclick = async () => {
    try {
      await api(`/api/lab/pools/${pid}/members`, { method: "POST", body: JSON.stringify({
        udi5: $("pm-u5").value.trim(), udi7: $("pm-u7").value.trim(),
        plate_id: +$("pm-plate").value || null, well_id: $("pm-well").value.trim() || null }) });
      closeModal(); toast("已加入混池", "ok"); renderLab();
    } catch (e) { toast(e.message, "err"); }
  };
}

function labNewPair() {
  openModal("登记条码对（i5/i7 源序列照录）", `
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px 14px">
      <label>UDI5 名称<input id="bp-u5" class="input"></label>
      <label>UDI7 名称<input id="bp-u7" class="input"></label>
      <label>i5 原序列（8bp ACGTN）<input id="bp-i5" class="input" style="text-transform:uppercase"></label>
      <label>i7 原序列（8bp ACGTN）<input id="bp-i7" class="input" style="text-transform:uppercase"></label>
      <label>i5 方向<select id="bp-d5" class="input"><option>read1</option><option>read2</option></select></label>
      <label>i7 方向<select id="bp-d7" class="input"><option>read2</option><option>read1</option></select></label>
      <label style="grid-column:1/-1">来源（试剂表/台账）<input id="bp-src" class="input"></label>
    </div>
    <div class="form-actions"><button class="btn" id="bp-cancel">取消</button>
      <button class="btn primary" id="bp-go">登记</button></div>`);
  $("bp-cancel").onclick = closeModal;
  $("bp-go").onclick = async () => {
    try {
      await api("/api/lab/barcode-pairs", { method: "POST", body: JSON.stringify({
        udi5: $("bp-u5").value.trim() || null, udi7: $("bp-u7").value.trim() || null,
        i5_seq_raw: $("bp-i5").value.trim(), i7_seq_raw: $("bp-i7").value.trim(),
        i5_dir: $("bp-d5").value, i7_dir: $("bp-d7").value, source: $("bp-src").value.trim() || null }) });
      closeModal(); toast("条码对已登记", "ok"); renderLab();
    } catch (e) { toast(e.message, "err"); }
  };
}

function labNewQc() {
  openModal("登记定量测量", `
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px 14px">
      <label>对象表<select id="qc-t" class="input">${["uce", "dna_extract", "specimens", "mygalomorph", "dali", "swu"].filter((t) => S.meta.tables[t]).map((t) => `<option>${t}</option>`).join("")}</select></label>
      <label>记录 ID<input id="qc-r" class="input" inputmode="numeric"></label>
      <label>测量值 *<input id="qc-v" class="input"></label>
      <label>单位<input id="qc-u" class="input" placeholder="ng/µL 等（不明留空）"></label>
      <label>方法<input id="qc-m" class="input"></label>
      <label>仪器<input id="qc-i" class="input"></label>
      <label style="grid-column:1/-1"><input type="checkbox" id="qc-adopted"> 采用本次测量（需给理由）</label>
      <label style="grid-column:1/-1">采用理由<input id="qc-ar" class="input"></label>
    </div>
    <div class="form-actions"><button class="btn" id="qc-cancel">取消</button>
      <button class="btn primary" id="qc-go">登记</button></div>`);
  $("qc-cancel").onclick = closeModal;
  $("qc-go").onclick = async () => {
    try {
      await api("/api/lab/qc", { method: "POST", body: JSON.stringify({
        obj_table: $("qc-t").value, obj_rid: +$("qc-r").value || null, value: $("qc-v").value.trim(),
        unit: $("qc-u").value.trim() || null, method: $("qc-m").value.trim() || null,
        instrument: $("qc-i").value.trim() || null,
        adopted: $("qc-adopted").checked, adopt_reason: $("qc-ar").value.trim() || null }) });
      closeModal(); toast("测量已登记", "ok"); renderLab();
    } catch (e) { toast(e.message, "err"); }
  };
}

/* ---------------- 文献与引证（v3.59/v3.70） ---------------- */
async function renderBiblio() {
  const host = $("view-biblio");
  host.innerHTML = `<div class="toolbar">
      <button class="btn" id="bb-new">${icon("plus")} 新增文献</button>
      <button class="btn" id="bb-mat">${icon("check")} 生成 Material examined 文本</button>
      <button class="btn" id="bb-refresh">${icon("restore")} 刷新</button>
      <span class="stat" id="bb-stat"></span>
    </div><div id="bb-body"><p class="hint">加载中…</p></div>`;
  $("bb-refresh").onclick = renderBiblio;
  $("bb-new").onclick = () => bibRefForm();
  $("bb-mat").onclick = bibMaterialText;
  let d;
  try { d = await api("/api/refs-bib"); }
  catch (e) { $("bb-body").innerHTML = `<p class="hint warn">${esc(e.message)}</p>`; return; }
  $("bb-stat").textContent = `文献 ${d.references.length} 篇`;
  $("bb-body").innerHTML = d.references.length ? `<table class="about-table" style="width:100%">
    <thead><tr><th>文献</th><th>标识 / DOI</th><th>期刊 / 年份</th><th>引证</th><th>操作</th></tr></thead>
    <tbody>${d.references.map((r) => `
      <tr><td><b>${esc(r.title)}</b><div class="hint">${esc(r.authors || "")}</div></td>
      <td>${esc(r.stable_id || "")}${r.doi ? `<div class="hint">DOI:${esc(r.doi)}</div>` : ""}</td>
      <td>${esc(r.journal || "")}<div class="hint">${esc(r.year || "")}${r.pages ? " · " + esc(r.pages) : ""}${r.plates ? " · 图版 " + esc(r.plates) : ""}</div></td>
      <td>${r.citations}</td>
      <td style="white-space:nowrap">
        <button class="btn" data-mat="${r.id}">材料清单</button>
        <button class="btn" data-cite="${r.id}" ${canWriteTable("specimens") ? "" : "disabled"}>登记引证</button>
      </td></tr>`).join("")}</tbody></table>
    <p class="hint">无 DOI 老文献保留完整书目、不虚构标识；引证时冻结标本版本，原文引用与当前接受名并列保留。</p>`
    : `<p class="hint">还没有文献。点「新增文献」登记（题名必填；DOI 可空）。</p>`;
  host.querySelectorAll("[data-mat]").forEach((b) => (b.onclick = () => bibMaterials(+b.dataset.mat)));
  host.querySelectorAll("[data-cite]").forEach((b) => (b.onclick = () =>
    bibCiteForm(+b.dataset.cite, d.references.find((x) => x.id === +b.dataset.cite))));
}

function bibRefForm() {
  openModal("新增文献", `
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px 14px">
      <label style="grid-column:1/-1">题名 *<input id="bf-title" class="input"></label>
      <label style="grid-column:1/-1">作者<input id="bf-authors" class="input" placeholder="如 Wang, C. & Zhang, Z."></label>
      <label>年份<input id="bf-year" class="input"></label>
      <label>期刊<input id="bf-journal" class="input"></label>
      <label>页码<input id="bf-pages" class="input" placeholder="如 1-25"></label>
      <label>图版<input id="bf-plates" class="input"></label>
      <label>DOI（可空）<input id="bf-doi" class="input"></label>
      <label>稳定标识（可空）<input id="bf-sid" class="input"></label>
      <label style="grid-column:1/-1">备注<input id="bf-note" class="input"></label>
    </div>
    <div class="form-actions"><button class="btn" id="bf-cancel">取消</button>
      <button class="btn primary" id="bf-go">保存</button></div>`);
  $("bf-cancel").onclick = closeModal;
  $("bf-go").onclick = async () => {
    const title = $("bf-title").value.trim();
    if (!title) { toast("题名必填", "err"); return; }
    try {
      await api("/api/refs-bib", { method: "POST", body: JSON.stringify({
        title, authors: $("bf-authors").value.trim(), year: $("bf-year").value.trim(),
        journal: $("bf-journal").value.trim(), pages: $("bf-pages").value.trim(),
        plates: $("bf-plates").value.trim(), doi: $("bf-doi").value.trim() || null,
        stable_id: $("bf-sid").value.trim() || null, note: $("bf-note").value.trim() }) });
      closeModal(); toast("文献已登记", "ok"); renderBiblio();
    } catch (e) { toast(e.message, "err"); }
  };
}

async function bibMaterials(bid) {
  let d;
  try { d = await api(`/api/refs-bib/${bid}/materials`); }
  catch (e) { toast(e.message, "err"); return; }
  openModal(`材料清单 · ${d.reference.title}（${d.materials.length} 条引证）`, `
    ${d.materials.length ? `<table class="about-table" style="width:100%"><thead><tr>
      <th>当前编号</th><th>当前接受名</th><th>原文材料编号</th><th>原文引用</th><th>引证时点</th></tr></thead><tbody>
      ${d.materials.map((m) => `<tr><td><b>${esc(m.current_key ?? "")}</b></td>
        <td><i>${esc(m.current_accepted || "")}</i></td><td>${esc(m.orig_number || "")}</td>
        <td class="hint">${esc((m.orig_text || "").slice(0, 80))}</td><td class="hint">${esc(m.created_at || "")}</td></tr>`).join("")}
      </tbody></table>` : `<p class="hint">该文献还没有材料引证。</p>`}
    <p class="hint">${esc(d.note || "")}</p>
    <div class="form-actions"><button class="btn" id="bm-close">关闭</button></div>`);
  $("modal").classList.add("modal-wide");
  $("bm-close").onclick = closeModal;
}

function bibCiteForm(bid, ref, pre) {
  pre = pre || {};
  const citeTables = [pre.tname || "specimens"];
  openModal(`登记材料引证${ref && ref.title ? " · " + ref.title : ""}`, `
    ${bid ? "" : `<label>文献 *<select id="bc-bid" class="input"><option value="">加载文献列表…</option></select></label>`}
    <label>标本（实体选择器：编号/名称均可检索；预填直接带出）</label>
    <div id="bc-picker"></div>
    <label>原文材料引用（论文里该标本那段原文）<textarea id="bc-text" class="input" rows="3"></textarea></label>
    <label>原文材料编号（含历史编号）<input id="bc-onum" class="input"></label>
    <div class="form-actions"><button class="btn" id="bc-cancel">取消</button>
      <button class="btn primary" id="bc-go">登记引证</button></div>
    <p class="hint">引证会冻结标本当前版本；重复引证会被唯一约束拦截；原文与当前接受名并列保留。</p>`);
  $("bc-cancel").onclick = closeModal;
  const goBtn = $("bc-go");
  let picker;
  picker = renderEntityPicker($("bc-picker"), { tables: citeTables, multi: false, compact: true,
    placeholder: "标本编号（精确优先，前缀/名称辅助检索）",
    preselected: pre.rid ? [{ tname: pre.tname || "specimens", rid: pre.rid, key: pre.num || ("#" + pre.rid) }] : [],
    onChange: (st) => { goBtn.disabled = !st.ready || st.selected.length !== 1; } });
  if (!bid) {
    api("/api/refs-bib").then((d) => {
      $("bc-bid").innerHTML = `<option value="">— 选择文献 —</option>` + d.references
        .map((r) => `<option value="${r.id}">${esc((r.title || "").slice(0, 60))}${r.year ? "（" + esc(r.year) + "）" : ""}</option>`).join("");
    }).catch((e) => { $("bc-bid").innerHTML = `<option value="">${esc(e.message)}</option>`; });
  }
  goBtn.onclick = async () => {
    const target = bid || +$("bc-bid").value;
    if (!target) { toast("请选择文献", "err"); return; }
    const m = picker.selected()[0];
    if (!m) { toast("请先选择标本", "err"); return; }
    try {
      await api(`/api/refs-bib/${target}/cite`, { method: "POST", body: JSON.stringify({
        rid: m.rid, tname: m.tname, orig_text: $("bc-text").value, orig_number: $("bc-onum").value.trim() }) });
      closeModal(); toast(`引证已登记（${m.key}，标本版本已冻结）`, "ok");
      if (S.t === "biblio") renderBiblio();
      if (!$("drawer").hidden && ["specimens", "dali", "swu"].includes(S.t)) loadDrawerCitations(S.t, S.drawerRid);
    } catch (e) { toast(e.message, "err"); }
  };
}

async function bibMaterialText() {
  openModal("生成 Material examined 文本（按分类学惯例聚合）", `
    <label>标本（实体选择器：粘贴编号批量选择；留空则用下方属筛选）</label>
    <div id="mt-picker"></div>
    <label>或按属筛选<input id="mt-genus" class="input" placeholder="如 Antirrhea（留空=全部）"></label>
    <div class="form-actions"><button class="btn" id="mt-cancel">关闭</button>
      <button class="btn primary" id="mt-go">生成</button></div>
    <div id="mt-out"></div>`);
  $("mt-cancel").onclick = closeModal;
  const mtp = renderEntityPicker($("mt-picker"), { tables: ["specimens"], multi: true,
    placeholder: "粘贴标本编号（每行/Tab/逗号分隔）；重复、失配、多候选可在此修正" });
  $("mt-go").onclick = async () => {
    const refs = mtp.selected();
    const params = new URLSearchParams();
    if (refs.length) {
      params.set("rids", refs.map((m) => m.rid).join(","));
    } else if ($("mt-genus").value.trim()) {
      params.set("filters", JSON.stringify({ Genus: { v: $("mt-genus").value.trim() } }));
    }
    try {
      const d = await api(`/api/material-examined?${params}`);
      $("mt-out").innerHTML = `<p class="hint">共 ${d.count} 号 / ${d.species} 种。点击文本框全选复制：</p>
        <textarea class="input" rows="12" style="width:100%;font-family:monospace" onclick="this.select()">${esc(d.text)}</textarea>`;
    } catch (e) { toast(e.message, "err"); }
  };
}

/* ---------------- 项目工作集（v3.61/v3.70） ---------------- */
const PROJ_STAGES = ["筹备", "进行中", "分析中", "撰写中", "完成"];

async function renderProjects() {
  const host = $("view-projects");
  host.innerHTML = `<div class="toolbar">
      <button class="btn" id="pj-new">${icon("plus")} 新建项目</button>
      <button class="btn" id="pj-refresh">${icon("restore")} 刷新</button>
      <span class="stat" id="pj-stat"></span>
    </div><div id="pj-body"><p class="hint">加载中…</p></div>`;
  $("pj-refresh").onclick = renderProjects;
  $("pj-new").onclick = () => projForm();
  let d;
  try { d = await api("/api/projects"); }
  catch (e) { $("pj-body").innerHTML = `<p class="hint warn">${esc(e.message)}</p>`; return; }
  $("pj-stat").textContent = `共 ${d.projects.length} 个项目`;
  $("pj-body").innerHTML = d.projects.length ? `<table class="about-table" style="width:100%">
    <thead><tr><th>项目</th><th>类型</th><th>阶段</th><th>成员</th><th>冻结</th><th>创建者</th><th>操作</th></tr></thead>
    <tbody>${d.projects.map((p) => `
      <tr><td><b>${esc(p.name)}</b><div class="hint">${esc(p.purpose || "")}</div></td>
      <td>${p.kind === "dynamic" ? "动态（筛选定义）" : "固定（明确成员）"}</td>
      <td>${esc(p.stage || "")}</td><td>${p.member_count}</td>
      <td>${p.frozen_name ? `<span class="st st-saved">${esc(p.frozen_name)}</span>` : `<span class="hint">未冻结</span>`}</td>
      <td>${esc(p.created_by || "")}</td>
      <td style="white-space:nowrap">
        <button class="btn primary" data-pj="${p.id}">成员与详情</button>
        ${rowMenu([
          { act: "pexp", label: "导出项目", disabled: !canWriteTable("specimens") },
          { act: "ppin", label: (S.prefs.pinned || []).includes(p.id) ? "取消首页固定" : "固定到首页" },
        ])}
      </td></tr>`).join("")}
    </tbody></table>
    <p class="hint">成员以稳定记录标识引用主库（不复制事实）；项目内备注/任务状态与主库字段分开；冻结只含确认纳入的成员，并把采集地/分类依赖一并冻结供导出。</p>`
    : `<p class="hint">还没有项目。固定项目逐号纳入成员；动态项目用筛选定义（可一键固化成员版本）。</p>`;
  host.querySelectorAll("[data-pj]").forEach((b) => (b.onclick = () => projDetail(+b.dataset.pj)));
  host.querySelectorAll("[data-pexp]").forEach((b) => (b.onclick = () =>
    openExportWizard({ context: "project", tname: "specimens",
      project: { pid: +b.dataset.pexp, name: b.dataset.pname } })));
  host.querySelectorAll("[data-ppin]").forEach((b) => (b.onclick = () => {
    const now = pinProject(+b.dataset.ppin);
    toast(now ? "已固定到首页" : "已从首页取消固定", "ok");
    renderProjects();
  }));
  wireRowMenus(host);
}

function projForm(p) {
  openModal(p ? `编辑项目 · ${p.name}` : "新建项目", `
    <label>项目名称 *<input id="pf-name" class="input" value="${esc(p ? p.name : "")}"></label>
    <label>研究目的<input id="pf-purpose" class="input" value="${esc(p ? p.purpose || "" : "")}" placeholder="如 系统发育分析 / 形态修订"></label>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px 14px">
      <label>类型<select id="pf-kind" class="input" ${p ? "disabled" : ""}>
        <option value="fixed" ${p && p.kind === "fixed" ? "selected" : ""}>固定（逐号纳入成员）</option>
        <option value="dynamic" ${p && p.kind === "dynamic" ? "selected" : ""}>动态（筛选定义）</option></select></label>
      <label>阶段<select id="pf-stage" class="input">${PROJ_STAGES.map((s) => `<option ${p && p.stage === s ? "selected" : ""}>${s}</option>`).join("")}</select></label>
    </div>
    <div id="pf-dyn" style="display:${p && p.kind === "dynamic" ? "" : "none"}">
      <label>关键词（整行内容包含匹配；留空=不限）<input id="pf-q" class="input" value="${esc((p && p.filters && p.filters.q) || "")}"></label>
      <div id="pf-fb"></div>
      <p class="hint">动态项目的成员随筛选实时解析（上方实时显示匹配数量）；「固化成员版本」把当时结果固化为明确成员（之后改筛选不影响已固化成员）。</p></div>
    <div class="form-actions"><button class="btn" id="pf-cancel">取消</button>
      <button class="btn primary" id="pf-go">保存</button></div>`);
  let fb = null;
  const pfTarget = "proj:" + (p ? p.id : "new");
  const pfRestored = !p ? formDraftLoad("proj", pfTarget) : null;
  const mountFb = () => {
    if ($("pf-kind").value !== "dynamic" || fb) return;
    fb = renderFilterBuilder($("pf-fb"), {
      tname: "specimens",
      value: { q: (p && p.filters && p.filters.q) || (pfRestored && pfRestored.data.q) || "",
               filters: (p && p.filters && p.filters.filters) || (pfRestored && pfRestored.data.filters) || {} },
    });
  };
  $("pf-kind").onchange = () => {
    $("pf-dyn").style.display = $("pf-kind").value === "dynamic" ? "" : "none";
    if ($("pf-kind").value === "dynamic") mountFb();
  };
  if (!p || p.kind === "dynamic") mountFb();
  $("pf-cancel").onclick = closeModal;
  if (pfRestored) {
    if (pfRestored.data.name) $("pf-name").value = pfRestored.data.name;
    if (pfRestored.data.purpose) $("pf-purpose").value = pfRestored.data.purpose;
    if (pfRestored.data.stage) $("pf-stage").value = pfRestored.data.stage;
    if (pfRestored.data.kind) { $("pf-kind").value = pfRestored.data.kind; $("pf-dyn").style.display = pfRestored.data.kind === "dynamic" ? "" : "none"; }
    $("modal-body").insertAdjacentHTML("afterbegin",
      `<div class="dash-card" style="margin-bottom:8px;display:flex;gap:10px;align-items:center">
        <span class="hint">已恢复 ${Math.max(1, Math.round((Date.now() - pfRestored.at) / 60000))} 分钟前保存的本机草稿</span>
        <button class="btn danger" id="pf-dropdraft" type="button">丢弃草稿</button></div>`);
    $("pf-dropdraft").onclick = () => { formDraftDrop("proj", pfTarget); closeModal(); projForm(null); };
  }
  let pfDirty = false;
  $("modal-body").addEventListener("input", () => { pfDirty = true; }, true);
  const pfGuardId = "guard:" + pfTarget;
  registerFormGuard(pfGuardId, {
    dirty: () => pfDirty,
    canSaveLeave: () => !!$("pf-name").value.trim() && $("pf-kind").value === "dynamic" ? !!fb : !!$("pf-name").value.trim(),
    saveLeave: async () => { const okBefore = !$("pf-go").disabled; $("pf-go").click(); return okBefore; },
    keepDraft: () => formDraftSave("proj", pfTarget, {
      name: $("pf-name").value.trim(), purpose: $("pf-purpose").value.trim(),
      kind: $("pf-kind").value, stage: $("pf-stage").value,
      q: $("pf-q") ? $("pf-q").value.trim() : "",
      filters: fb ? fb.get().filters : {} }),
    discard: () => { formDraftDrop("proj", pfTarget); pfDirty = false; },
  });
  setTopModalGuard({ id: pfGuardId, dirty: () => formGuards.get(pfGuardId).dirty(),
    canSaveLeave: () => formGuards.get(pfGuardId).canSaveLeave ? formGuards.get(pfGuardId).canSaveLeave() : false,
    saveLeave: formGuards.get(pfGuardId).saveLeave, keepDraft: () => formGuards.get(pfGuardId).keepDraft(), discard: () => formGuards.get(pfGuardId).discard() });
  $("pf-go").onclick = async () => {
    const name = $("pf-name").value.trim();
    if (!name) { toast("项目名称必填", "err"); return; }
    const kind = $("pf-kind").value;
    const body = { name, purpose: $("pf-purpose").value.trim(), kind, stage: $("pf-stage").value };
    if (kind === "dynamic") {
      if (!fb) { toast("筛选器未就绪", "err"); return; }
      body.filters = fb.get();
    }
    try {
      if (p) { await api(`/api/projects/${p.id}`, { method: "PUT", body: JSON.stringify(body) }).catch(async () => {
        // 无 PUT 端点时退化：阶段/目的经 member-note 同款接口不可用，提示手动
        toast("该项目基本信息创建后不可改（当前版本未提供编辑）；可新建项目替代", "warn"); });
      } else {
        await api("/api/projects", { method: "POST", body: JSON.stringify(body) });
      }
      closeModal(); toast("项目已保存", "ok"); renderProjects();
    } catch (e) { toast(e.message, "err"); }
  };
}

async function projDetail(pid) {
  let d;
  try { d = await api(`/api/projects/${pid}`); }
  catch (e) { toast(e.message, "err"); return; }
  const p = d.project;
  pushRecent("projects", pid, { name: p.name });
  openModal(`项目 · ${p.name}（${p.kind === "dynamic" ? "动态" : "固定"} · ${p.stage || ""}）`, `
    <p class="hint">${esc(p.purpose || "")}${d.restricted_member_count ? ` · 有 ${d.restricted_member_count} 个成员因表权限未显示` : ""}${p.frozen_name ? ` · 已冻结：${esc(p.frozen_name)}` : ""}</p>
    <div class="form-actions" style="justify-content:flex-start;flex-wrap:wrap">
      <button class="btn" id="pd-add">${icon("plus")} 纳入成员</button>
      <button class="btn" id="pd-csv">${icon("download")} 成员清单 CSV</button>
      <button class="btn" id="pd-collab">${icon("user")} 创建协作包（当前成员）</button>
      ${p.kind === "dynamic" ? `<button class="btn" id="pd-freeze-dyn">固化成员版本</button>` : ""}
      <button class="btn" id="pd-freeze">${icon("download")} 冻结（生成受保护版本）</button>
      <button class="btn" id="pd-close">关闭</button>
    </div>
    <div style="overflow:auto;max-height:50vh"><table class="about-table" style="width:100%">
      <thead><tr><th>编号</th><th>表</th><th>纳入原因</th><th>项目内备注 / 任务状态</th><th>操作</th></tr></thead>
      <tbody>${d.members.map((m) => `
        <tr><td><b>${esc(m.current_key ?? m.rid)}</b></td><td>${esc(m.tname)}</td><td class="hint">${esc(m.join_reason || "")}</td>
        <td>${esc(m.project_note || "")}${m.task_status ? `<span class="st st-pending">${esc(m.task_status)}</span>` : ""}</td>
        <td style="white-space:nowrap">
          <button class="btn" data-note="${m.id}" title="项目内备注/任务状态（不写主库）">备注</button>
          <button class="btn danger" data-rm="${m.id}">移出</button></td></tr>`).join("")}
      </tbody></table></div>
    <p class="hint">移出需给原因（留痕，不删记录）；冻结只含确认纳入且未排除的成员。</p>`);
  $("modal").classList.add("modal-wide");
  $("pd-close").onclick = closeModal;
  $("pd-csv").onclick = () => {
    const lines = ["编号,表,纳入原因,纳入时间,项目内备注,任务状态",
      ...d.members.map((m) => [m.current_key ?? m.rid, m.tname, m.join_reason || "", m.added_at || "", m.project_note || "", m.task_status || ""]
        .map((x) => '"' + String(x).replace(/"/g, '""') + '"').join(","))];
    const blob = new Blob(["\ufeff" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
    const a2 = document.createElement("a");
    a2.href = URL.createObjectURL(blob);
    a2.download = `${p.name}_成员清单.csv`;
    document.body.appendChild(a2); a2.click(); a2.remove();
  };
  $("pd-collab").onclick = () => {
    const refs = d.members.filter((m) => !m.exclude_reason).map((m) => ({ tname: m.tname, rid: m.rid, key: m.current_key ?? m.rid }));
    if (!refs.length) { toast("该项目还没有确认纳入的成员", "err"); return; }
    window.closeModal();
    openCollabForIds(refs, `项目「${p.name}」当前成员`);
  };
  $("pd-add").onclick = () => projAddMember(pid);
  api(`/api/projects/${pid}/versions`).then((v) => {
    if (v.versions.length) {
      $("pd-add").parentElement.insertAdjacentHTML("beforebegin",
        `<p class="hint">冻结版本 ${v.versions.length} 个：${esc(v.versions.map((x) => "#" + x.id + " " + x.name).join("；"))}——导出向导中可指定冻结版本。</p>`);
    }
  }).catch(() => { });
  const fd = $("pd-freeze-dyn");
  if (fd) fd.onclick = async () => {
    try { const r = await api(`/api/projects/${pid}/from-dynamic`, { method: "POST", body: "{}" });
      toast(`已固化 ${r.frozen_members} 个成员`, "ok"); projDetail(pid); } catch (e) { toast(e.message, "err"); }
  };
  $("pd-freeze").onclick = () => {
    openModal("冻结项目（生成受保护版本）", `
      <p class="hint">只冻结确认纳入且未排除的成员（每表独立受保护版本），并把采集地/分类依赖内容一并冻结；冻结后修改当前数据不影响该版本。</p>
      <label>冻结版本名称 *<input id="fz-name" class="input" value="项目冻结 ${pid}（${new Date().toISOString().slice(0, 10)}）"></label>
      <div class="form-actions"><button class="btn" id="fz-cancel">取消</button>
        <button class="btn primary" id="fz-go">冻结</button></div>`);
    $("fz-cancel").onclick = closeModal;
    $("fz-go").onclick = async () => {
      const name = $("fz-name").value.trim();
      if (!name) { toast("请填写冻结版本名称", "err"); return; }
      $("fz-go").disabled = true;
      try {
        const r = await api(`/api/projects/${pid}/freeze`, { method: "POST", body: JSON.stringify({ name }) });
        closeModal();
        toast(`已冻结：${(r.versions || []).map((v) => `${v.tname}#${v.version_id}`).join("、") || JSON.stringify(r).slice(0, 80)}`, "ok");
        projDetail(pid); renderProjects();
      } catch (e) { toast(e.message, "err"); $("fz-go").disabled = false; }
    };
  };
  $("modal-body").querySelectorAll("[data-note]").forEach((b) => (b.onclick = () => {
    const m = d.members.find((x) => x.id === +b.dataset.note);
    openModal("项目内备注（不写主库）", `
      <p class="hint">成员：${esc(m.current_key ?? "#" + m.rid)}。项目内备注/任务状态与主库字段分开保存。</p>
      <label>备注<input id="mn-text" class="input" value="${esc(m.project_note || "")}" placeholder="如：DNA 待补 / 已寄送测序"></label>
      <label>任务状态<select id="mn-status" class="input">
        ${["", "待处理", "进行中", "已完成", "暂缓"].map((s) => `<option value="${s}" ${(m.task_status || "") === s ? "selected" : ""}>${s || "（无）"}</option>`).join("")}</select></label>
      <div class="form-actions"><button class="btn" id="mn-cancel">取消</button>
        <button class="btn primary" id="mn-go">保存</button></div>`);
    $("mn-cancel").onclick = closeModal;
    $("mn-go").onclick = async () => {
      try {
        await api(`/api/projects/${pid}/member-note`, { method: "POST", body: JSON.stringify({
          member_id: m.id, project_note: $("mn-text").value, task_status: $("mn-status").value }) });
        closeModal(); toast("已保存（仅项目内，主库未改动）", "ok"); projDetail(pid);
      } catch (e) { toast(e.message, "err"); }
    };
  }));
  $("modal-body").querySelectorAll("[data-rm]").forEach((b) => (b.onclick = async () => {
    const reason = prompt("移出原因（留痕）：", "不再纳入本次研究");
    if (reason === null) return;
    try { await api(`/api/projects/${pid}/members/${b.dataset.rm}`, { method: "DELETE", body: JSON.stringify({ remove_reason: reason }) });
      toast("已移出（留痕，记录未删除）", "ok"); projDetail(pid); } catch (e) { toast(e.message, "err"); }
  }));
}

function projAddMember(pid) {
  openModal("纳入成员（实体选择器：粘贴编号即可，无需内部 ID）", `
    <div id="pa-picker"></div>
    <label>纳入原因<input id="pa-reason" class="input" value="纳入本次研究"></label>
    <div class="form-actions"><button class="btn" id="pa-cancel">取消</button>
      <button class="btn primary" id="pa-go" disabled>解析并纳入</button></div>`);
  $("pa-cancel").onclick = closeModal;
  const goBtn = $("pa-go");
  let picker;
  picker = renderEntityPicker($("pa-picker"), { tables: ["specimens"], multi: true,
    placeholder: "粘贴标本编号（每行/Tab/逗号分隔）；重复、失配、多候选可在此修正",
    sources: { projects: { excludePid: pid } },
    onChange: (st) => { goBtn.disabled = !st.ready || !st.selected.length; } });
  goBtn.onclick = async () => {
    const refs = picker.selected();
    if (!refs.length) { toast("请先解析并选择标本", "err"); return; }
    const reason = $("pa-reason").value.trim() || "纳入本次研究";
    goBtn.disabled = true;
    let ok = 0;
    const errs = [];
    for (const m of refs) {
      try { await api(`/api/projects/${pid}/members`, { method: "POST",
        body: JSON.stringify({ rid: m.rid, tname: m.tname, join_reason: reason }) });
        ok++;
      } catch (e) { errs.push(`${m.key}: ${e.message}`); }
    }
    closeModal();
    toast(`已纳入 ${ok} 个成员${errs.length ? `；失败 ${errs.length}（${errs[0]}）` : ""}`, ok ? "ok" : "err");
    projDetail(pid);
  };
}

/* ---------------- 最近操作（v3.76）：统一任务/批次结果入口 ----------------
   复用既有事实存储：tasks 注册表（名录更新等后台任务，含取消四态）、edits 批次审计
   （批量/导入/稳定引用迁移）、collab 包（待继续处理的协作合并）、导出审计。
   小提示（toast）负责即时反馈；本页结果卡负责后续动作（定位记录/复制编号/导出清单/
   继续处理/安全重试）；结果未知写入用 GET /api/idempotency/<req_id> 确认，不盲目重试。 */
const ACT_STATUS = {
  running: ["进行中", "st st-pending"], done: ["已完成", "st st-saved"],
  failed: ["失败", "st st-risk"], cancelled: ["已取消", "st st-readonly"],
  stopping: ["正在停止", "st st-pending"], merged: ["已合并", "st st-saved"],
  active: ["进行中", "st st-saved"], revoked: ["已收回", "st st-readonly"],
};
function actBadge(st) {
  const [label, cls] = ACT_STATUS[st] || [st, "st st-readonly"];
  return `<span class="${cls}">${esc(label)}</span>`;
}
function actLocate(tname, rid, key) {
  if (!canWriteAnyTable && !S.meta.tables[tname]) return;
  switchView(tname);
  setTimeout(() => openDrawerRecord(tname, rid), 600);
}
function copyText(text) {
  try { navigator.clipboard.writeText(text); toast("已复制", "ok"); }
  catch { toast("复制失败——请手动选择复制", "err"); }
}
/* 明细弹窗：可点击定位原记录、复制编号、导出问题清单（CSV 下载，不走控制台） */
function showOpDetailModal(title, rows, csvName) {
  const heads = ["时间", "操作", "表", "编号", "说明"];
  const esc1 = (r) => esc(r.ts || "");
  openModal(title + `（${rows.length} 条）`, `
    <div style="overflow:auto;max-height:52vh"><table class="about-table" style="width:100%">
      <thead><tr>${heads.map((h) => `<th>${h}</th>`).join("")}</tr></thead>
      <tbody>${rows.map((r, i) => `
        <tr><td class="hint">${esc1(r)}</td><td>${esc(r.action || "")}</td><td>${esc(r.tname || "")}</td>
        <td>${r.rid && r.tname
          ? `<a href="javascript:void 0" class="act-loc" data-t="${esc(r.tname)}" data-r="${r.rid}" data-k="${esc(r.rkey || "")}" style="text-decoration:underline"><b>${esc(r.rkey || "#" + r.rid)}</b></a>`
          : esc(r.rkey || "")}</td>
        <td class="hint" style="max-width:340px">${esc(r.summary || r.note || "")}</td></tr>`).join("")}
      </tbody></table></div>
    <div class="form-actions" style="flex-wrap:wrap">
      <button class="btn" id="od-copy">${icon("copy")} 复制编号清单</button>
      <button class="btn" id="od-csv">${icon("download")} 导出问题/明细清单（CSV）</button>
      <button class="btn" id="od-close">关闭</button>
    </div>`);
  $("modal").classList.add("modal-wide");
  $("od-close").onclick = closeModal;
  $("od-copy").onclick = () => copyText(rows.map((r) => r.rkey || (r.rid ? "#" + r.rid : "")).filter(Boolean).join("\n"));
  $("od-csv").onclick = () => {
    const lines = ["时间,操作,表,编号,说明",
      ...rows.map((r) => [r.ts || "", r.action || "", r.tname || "", r.rkey || (r.rid ? "#" + r.rid : ""), (r.summary || r.note || "").replace(/[\r\n,]+/g, " ")].map((x) => `"${String(x).replace(/"/g, '""')}"`).join(","))];
    const blob = new Blob(["\ufeff" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
    const a2 = document.createElement("a");
    a2.href = URL.createObjectURL(blob);
    a2.download = csvName;
    document.body.appendChild(a2); a2.click(); a2.remove();
  };
  document.querySelectorAll(".act-loc").forEach((a) => (a.onclick = () => {
    closeModal();
    actLocate(a.dataset.t, +a.dataset.r, a.dataset.k);
  }));
}

async function renderRecent() {
  const host = $("view-recent");
  host.innerHTML = '<div class="toolbar">'
    + '<button class="btn" id="rc-refresh">' + icon("restore") + ' 刷新（读取服务器真实状态）</button>'
    + '<button class="btn" id="rc-idem">' + icon("view") + ' 按请求 ID 确认写入结果</button>'
    + '<span class="stat" id="rc-stat"></span></div>'
    + '<p class="hint" style="margin:6px 2px">导入、协作合并、批量操作、稳定引用迁移、名录更新与导出的统一结果入口（复用审计批次/任务注册表/协作包记录；按您的权限过滤）。错过页面提示或刷新后，从这里继续处理未完成项。</p>'
    + '<div id="rc-list"><p class="hint">加载中…</p></div>';
  $("rc-refresh").onclick = renderRecent;
  $("rc-idem").onclick = () => {
    openModal("按请求 ID 确认写入结果",
      '<p class="hint">写入请求发出后若网络中断（提示「结果待确认」），可凭确定性请求 ID 查询服务器真实结果——已成功的操作不会被重复提交。</p>'
      + '<label>请求 ID（req_id）<input id="id-rid" class="input" placeholder="粘贴请求 ID"></label>'
      + '<div class="form-actions"><button class="btn" id="id-go">查询</button>'
      + '<button class="btn" id="id-close">关闭</button></div>'
      + '<div id="id-out"></div>');
    $("id-close").onclick = closeModal;
    $("id-go").onclick = async () => {
      const rid = $("id-rid").value.trim();
      if (!rid) { toast("请粘贴请求 ID", "err"); return; }
      try {
        const d = await api("/api/idempotency/" + encodeURIComponent(rid));
        $("id-out").innerHTML = d.found
          ? '<div class="dash-card"><b>状态：</b>'
            + (d.status === 200 && d.response && d.response.ok
              ? '<span class="st st-saved">已成功保存（可安全关闭，无需重试）</span>'
              : "服务器当时返回：" + esc(String((d.response && d.response.error) || d.status)))
            + '<div class="hint">查询时间 ' + esc(new Date().toLocaleString()) + '</div></div>'
          : '<div class="dash-card"><span class="hint">服务器没有该请求的记录——该写入未被执行，可安全重试。</span></div>';
      } catch (e) { $("id-out").innerHTML = '<p class="hint warn">' + esc(e.message) + '</p>'; }
    };
  };
  let act, pkgs, tasks;
  try {
    const r2 = await Promise.all([
      api("/api/activity/recent"),
      api("/api/collab/packages").catch(() => ({ packages: [] })),
      api("/api/tasks").catch(() => ({ tasks: [] })),
    ]);
    act = r2[0]; pkgs = r2[1]; tasks = r2[2];
  } catch (e) { $("rc-list").innerHTML = '<p class="hint warn">' + esc(e.message) + '</p>'; return; }
  const activePkgs = (pkgs.packages || []).filter((p) => p.status === "active");
  const runningTasks = (tasks.tasks || []).filter((t) => ["running", "pending"].includes(t.status));
  $("rc-stat").textContent = "批次 " + act.batches.length + " · 导入 " + act.imports.length
    + " · 任务 " + (tasks.tasks || []).length + " · 待处理协作包 " + activePkgs.length;
  const h = [];
  if (runningTasks.length) {
    h.push('<div class="dash-card" style="margin-bottom:10px"><h3>后台任务（进行中 ' + runningTasks.length + '）——离开页面仍可在此查看，刷新读取服务器真实状态</h3>');
    for (const t of runningTasks) {
      h.push('<div style="border:1px dashed var(--border);border-radius:8px;padding:6px 8px;margin-bottom:6px">'
        + '<b>' + esc(t.kind) + '</b> ' + actBadge(t.status)
        + ' <span class="hint">阶段 ' + (t.stage_index ?? 0) + '/' + ((t.stages || []).length)
        + (t.detail ? " · " + esc(t.detail) : "") + '</span>'
        + ' <button class="btn danger" data-tcancel="' + t.id + '" style="margin-left:8px">请求取消</button>'
        + ' <span class="hint" data-tstate="' + t.id + '"></span></div>');
    }
    h.push('</div>');
  }
  if (activePkgs.length) {
    h.push('<div class="dash-card" style="margin-bottom:10px"><h3>待处理：协作包（进行中 ' + activePkgs.length + '）——继续回传或下载工作簿</h3>');
    for (const p of activePkgs) {
      h.push('<div style="margin-bottom:6px"><b>' + esc(p.pkg_id) + '</b> <span class="hint">'
        + esc(p.created_by) + ' · 可编辑列 ' + esc((p.allowed_cols || []).join("、")) + '</span>'
        + ' <button class="btn" data-pgo="' + p.pkg_id + '">继续处理</button></div>');
    }
    h.push('</div>');
  }
  const batchRows = [
    ...act.imports.map((x) => ({ ...x, kind: "导入" })),
    ...act.batches.filter((b) => !act.imports.some((x) => x.batch_id === b.batch_id)).map((x) => ({ ...x, kind: "批量" })),
  ].slice(0, 20);
  h.push('<div class="dash-card" style="margin-bottom:10px"><h3>导入与批量批次（最近）</h3>');
  if (batchRows.length) {
    h.push('<table class="about-table" style="width:100%"><thead><tr><th>开始</th><th>结束</th><th>类型</th><th>表</th><th>行数</th><th>操作人</th><th>操作</th></tr></thead><tbody>');
    for (const b of batchRows) {
      h.push('<tr><td class="hint">' + esc(b.started || "") + '</td><td class="hint">' + esc(b.ended || "")
        + '</td><td>' + esc(b.kind) + '</td><td>' + esc(b.tname || b.tnames || "")
        + '</td><td>' + b.rows_n + '</td><td>' + esc(b.user || "")
        + '</td><td><button class="btn" data-batch="' + esc(b.batch_id) + '">查看明细</button></td></tr>');
    }
    h.push('</tbody></table>');
  } else h.push('<p class="hint">还没有批次记录。</p>');
  h.push('</div>');
  h.push('<div class="dash-card" style="margin-bottom:10px"><h3>稳定引用迁移（最近）</h3>');
  if (act.refs.length) {
    h.push('<table class="about-table" style="width:100%"><thead><tr><th>开始</th><th>表</th><th>操作人</th><th>明细</th></tr></thead><tbody>');
    for (const x of act.refs) {
      h.push('<tr><td class="hint">' + esc(x.started || "") + '</td><td>' + esc(x.tname || "")
        + '</td><td>' + esc(x.user || "") + '</td><td><button class="btn" data-batch="' + esc(x.batch_id) + '">查看明细</button></td></tr>');
    }
    h.push('</tbody></table>');
  } else h.push('<p class="hint">暂无迁移记录。</p>');
  h.push('</div>');
  h.push('<div class="dash-card" style="margin-bottom:10px"><h3>名录更新（最近）</h3>');
  if (act.catalogs.length) {
    h.push('<table class="about-table" style="width:100%"><thead><tr><th>时间</th><th>操作人</th><th>说明</th></tr></thead><tbody>');
    for (const c of act.catalogs) h.push('<tr><td class="hint">' + esc(c.ts || "") + '</td><td>' + esc(c.user || "") + '</td><td>' + esc(c.summary || "") + '</td></tr>');
    h.push('</tbody></table>');
  } else h.push('<p class="hint">暂无名录更新记录（管理员可在数据体检 → 刷新 WSC 名录发起）。</p>');
  h.push('</div>');
  h.push('<div class="dash-card" style="margin-bottom:10px"><h3>导出记录（最近）——文件内容以导出时点为准</h3>');
  if (act.exports.length) {
    h.push('<table class="about-table" style="width:100%"><thead><tr><th>时间</th><th>操作人</th><th>说明</th></tr></thead><tbody>');
    for (const x of act.exports) h.push('<tr><td class="hint">' + esc(x.ts || "") + '</td><td>' + esc(x.user || "") + '</td><td>' + esc(x.summary || "") + '</td></tr>');
    h.push('</tbody></table>');
  } else h.push('<p class="hint">暂无导出记录。</p>');
  h.push('</div>');
  $("rc-list").innerHTML = h.join("");
  host.querySelectorAll("[data-tcancel]").forEach((b) => (b.onclick = async () => {
    const tid = b.dataset.tcancel;
    const stateEl = host.querySelector('[data-tstate="' + tid + '"]');
    b.disabled = true;
    if (stateEl) stateEl.textContent = "取消请求已发送…";
    try {
      await api("/api/tasks/" + tid + "/cancel", { method: "POST", body: "{}" });
      if (stateEl) stateEl.textContent = "正在停止（在阶段边界生效）…";
      const poll = setInterval(async () => {
        let t;
        try { t = await api("/api/tasks/" + tid); } catch { clearInterval(poll); return; }
        if (["done", "failed", "cancelled"].includes(t.status)) {
          clearInterval(poll);
          if (stateEl) stateEl.textContent = t.status === "cancelled" ? "已取消" : t.status === "done" ? "已完成" : "失败：" + (t.error || "");
          b.disabled = true;
        }
      }, 2500);
    } catch (e) { toast(e.message, "err"); b.disabled = false; }
  }));
  host.querySelectorAll("[data-pgo]").forEach((b) => (b.onclick = () => switchView("collab")));
  host.querySelectorAll("[data-batch]").forEach((b) => (b.onclick = async () => {
    try {
      const d = await api("/api/activity/batch/" + encodeURIComponent(b.dataset.batch));
      const rows = d.rows.map((x) => ({ ts: x.ts, action: x.action, tname: x.tname, rid: x.rid, rkey: x.rkey, summary: x.summary }));
      showOpDetailModal("批次明细 · " + b.dataset.batch, rows, "批次_" + b.dataset.batch + ".csv");
      const allUndoable = d.rows.some((x) => ["create", "update", "delete", "import"].includes(x.action));
      const foot = document.querySelector("#modal .form-actions");
      if (foot && allUndoable && canWriteAnyTable()) {
        foot.insertAdjacentHTML("beforeend", '<button class="btn danger" id="od-undo">' + icon("restore") + ' 回退此批次的修改…</button>');
        document.getElementById("od-undo").onclick = async () => {
          try {
            const pv = await api("/api/undo/" + b.dataset.batch + "/preview");
            openModal("回退预览 · " + b.dataset.batch + "（仅回退该批次的改动，非整表恢复）",
              '<p class="hint">' + esc(pv.summary || "确认后将生成一条反向批次，只抵消该批次做过的修改；其他人之后做的修改会全部保留。") + '</p>'
              + '<div class="form-actions"><button class="btn" id="ud-cancel">取消</button>'
              + '<button class="btn danger" id="ud-go">确认回退</button></div>');
            $("ud-cancel").onclick = closeModal;
            $("ud-go").onclick = async () => {
              try {
                const r3 = await api("/api/undo/" + b.dataset.batch, { method: "POST", body: "{}" });
                closeModal();
                toast("已撤销：" + (r3.summary || r3.batch || ""), "ok");
                renderRecent();
              } catch (e) { toast(e.message, "err"); }
            };
          } catch (e) { toast(e.message, "err"); }
        };
      }
    } catch (e) { toast(e.message, "err"); }
  }));
}


function fieldEntries(tname) {
  const tcfg = S.meta.tables[tname] || {};
  const cols = tcfg.columns || [];
  const key = tcfg.key;
  return cols.map((c) => {
    const doc = S.docs[c.name] || "";
    const linked = !!c.linked;
    const identity = c.name === key;
    return { name: c.name, label: c.label || c.name, doc, linked, identity,
             hint: [c.label && c.label !== c.name ? c.label : "", doc].filter(Boolean).join(" · ") };
  });
}

/* ---- ① 动态项目筛选器：字段—条件—值（查询模型 in/contains/empty/notempty + q 关键词） ---- */
const FB_MODES = [
  ["in", "属于以下值（每行一个；「（空）」代表空值）"],
  ["contains", "包含文本"],
  ["empty", "为空"],
  ["notempty", "不为空"],
];
function renderFilterBuilder(host, opts = {}) {
  const tname = opts.tname || "specimens";
  const entries = fieldEntries(tname);
  const supportedModes = FB_MODES.map((m) => m[0]);
  let q = (opts.value && opts.value.q) || "";
  let rows = [];
  let rawUnsupported = [];
  let errors = {};
  const init = (opts.value && opts.value.filters) || {};
  for (const [col, f] of Object.entries(init)) {
    const okShape = f && typeof f === "object" && supportedModes.includes(f.mode)
      && (f.mode !== "in" || Array.isArray(f.values))
      && Object.keys(f).every((k) => ["mode", "values", "text"].includes(k));
    if (okShape) rows.push({ col, mode: f.mode, values: f.values || [], text: f.text || "" });
    else rawUnsupported.push({ col, f });
  }
  host.innerHTML = `
    <div class="fb" style="border:1px solid var(--border);border-radius:8px;padding:8px 10px">
      <label style="display:block">关键词（整行内容包含匹配；留空=不限）<input class="input fb-q" style="width:100%" value="${esc(q)}" placeholder="如 Brettus / 云南省"></label>
      <div class="fb-rows" style="margin-top:6px"></div>
      <div class="form-actions" style="margin:6px 0 0;justify-content:flex-start">
        <button class="btn fb-add" type="button">${icon("plus")} 加条件</button>
        <span class="hint">动态成员会随数据变化：符合筛选的行实时计入，之后新增/修改也会改变成员（可随时固化成员版本）。</span>
      </div>
      ${rawUnsupported.length ? `<div class="hint warn" style="margin-top:6px">以下 ${rawUnsupported.length} 条条件为本构建器不支持的写法，将<b>原样保留</b>（不修改、不丢失）：<code>${esc(JSON.stringify(rawUnsupported.map((x) => ({ [x.col]: x.f }))))}</code></div>` : ""}
      <div class="fb-count hint" style="margin-top:6px">匹配数量：未计算</div>
    </div>`;
  const rowsEl = host.querySelector(".fb-rows");
  const countEl = host.querySelector(".fb-count");
  function valueEditor(r) {
    if (r.mode === "in")
      return `<textarea class="input fb-text" rows="2" style="width:100%" placeholder="每行一个值；「（空）」代表空值">${esc((r.values || []).join("\n"))}</textarea>`;
    if (r.mode === "contains")
      return `<input class="input fb-text" style="width:100%" value="${esc(r.text || "")}" placeholder="包含的文本">`;
    return `<span class="hint">该条件不需要值</span>`;
  }
  function paintRows() {
    rowsEl.innerHTML = rows.map((r, i) => {
      const entry = entries.find((e) => e.name === r.col);
      return `<div class="fb-row" data-i="${i}" style="border:1px dashed var(--border);border-radius:8px;padding:6px 8px;margin-bottom:6px">
      <div style="display:grid;grid-template-columns:minmax(140px,2fr) minmax(170px,2fr) 3fr 34px;gap:6px;align-items:start">
        <select class="input fb-col">
          <option value="">— 选择字段 —</option>
          ${entries.map((e2) => `<option value="${esc(e2.name)}" ${r.col === e2.name ? "selected" : ""}>${esc(e2.label !== e2.name ? e2.label + "（" + e2.name + "）" : e2.name)}</option>`).join("")}
        </select>
        <select class="input fb-mode">${FB_MODES.map(([mv, ml]) => `<option value="${mv}" ${r.mode === mv ? "selected" : ""}>${ml}</option>`).join("")}</select>
        <div class="fb-val"></div>
        <button class="btn danger fb-del" type="button" title="删除此条件">✕</button>
      </div>
      <div class="hint">${entry ? esc(entry.hint || entry.name) : "选择字段后显示说明"}</div>
      <div class="fb-err" style="color:var(--c-risk);font-size:12.5px"></div>
    </div>`;
    }).join("") || `<p class="hint">还没有条件——只按上方关键词筛选；点「加条件」按字段精确筛选。</p>`;
    rowsEl.querySelectorAll(".fb-row").forEach((rowEl) => {
      const i = +rowEl.dataset.i;
      const r = rows[i];
      const valEl = rowEl.querySelector(".fb-val");
      valEl.innerHTML = valueEditor(r);
      const textEl = valEl.querySelector(".fb-text");
      if (textEl) textEl.oninput = () => {
        if (r.mode === "in") r.values = textEl.value.split(/\r?\n/).map((x) => x.trim()).filter(Boolean);
        else r.text = textEl.value;
        scheduleCount(); emit();
      };
      rowEl.querySelector(".fb-col").onchange = (e) => { r.col = e.target.value; paintRows(); scheduleCount(); emit(); };
      rowEl.querySelector(".fb-mode").onchange = (e) => { r.mode = e.target.value; paintRows(); scheduleCount(); emit(); };
      rowEl.querySelector(".fb-del").onclick = () => { rows.splice(i, 1); paintRows(); scheduleCount(); emit(); };
      const err = errors[r.col];
      const errEl = rowEl.querySelector(".fb-err");
      if (err) errEl.textContent = err;
    });
  }
  function currentValue() {
    const filters = {};
    for (const r of rows) {
      if (!r.col || !r.mode) continue;
      filters[r.col] = r.mode === "in" ? { mode: "in", values: r.values }
        : r.mode === "contains" ? { mode: "contains", text: r.text }
        : { mode: r.mode };
    }
    for (const u of rawUnsupported) filters[u.col] = u.f;
    return { q: host.querySelector(".fb-q").value.trim(), filters };
  }
  let countTimer = null;
  function scheduleCount() { clearTimeout(countTimer); countTimer = setTimeout(runCount, 600); }
  async function runCount() {
    const { q, filters } = currentValue();
    countEl.textContent = "匹配数量：计算中…";
    try {
      const p = new URLSearchParams({ q });
      if (Object.keys(filters).length) p.set("filters", JSON.stringify(filters));
      const d = await api(`/api/projects/dynamic-count?` + p);
      errors = {};
      countEl.innerHTML = `当前匹配 <b>${d.count.toLocaleString()}</b> 行 —— <span class="hint">${esc(d.note || "")}</span>`;
    } catch (e) {
      countEl.innerHTML = `<span style="color:var(--c-risk)">${esc(e.message)}</span>`;
      const m = /列「(.+?)」/.exec(e.message || "");
      if (m) { errors[m[1]] = e.message; paintRows(); }
    }
    emit();
  }
  function state() { return { value: currentValue(), ready: true }; }
  function emit() { if (opts.onChange) opts.onChange(state()); }
  host.querySelector(".fb-add").onclick = () => { rows.push({ col: "", mode: "contains", values: [], text: "" }); paintRows(); };
  host.querySelector(".fb-q").addEventListener("input", () => { scheduleCount(); emit(); });
  paintRows();
  runCount();
  return { state, get: currentValue, refreshCount: runCount };
}

/* ---- ② 协作可编辑列勾选列表（身份字段/关联只读字段显示不可选原因；结果可读） ---- */
function renderEditableCols(host, opts = {}) {
  const tname = opts.tname || "specimens";
  const entries = fieldEntries(tname);
  const chosen = new Set((opts.value || ["Notes", "DetBy", "DetDate"]).slice());
  let search = "";
  host.innerHTML = `
    <div class="ec" style="border:1px solid var(--border);border-radius:8px;padding:8px 10px">
      <input class="input ec-search" style="width:100%" placeholder="搜索字段（中文名 / 原字段名 / 说明）…">
      <div class="ec-chips" style="margin:6px 0"></div>
      <div class="ec-list" style="max-height:26vh;overflow:auto"></div>
      <p class="hint">身份字段用于回传核对，不允许编辑；采集事件带出的字段是关联只读值（修改应到采集地信息表）。</p>
    </div>`;
  const listEl = host.querySelector(".ec-list");
  const chipsEl = host.querySelector(".ec-chips");
  function reason(e) {
    if (e.identity) return "行身份字段（回传核对用），不可编辑";
    if (e.linked) return "采集事件带出的关联字段（只读），如需修改请到采集地信息表";
    return null;
  }
  function paintChips() {
    const items = [...chosen].map((name) => {
      const e = entries.find((x) => x.name === name);
      return `<span style="border:1px solid var(--border);border-radius:14px;padding:2px 9px;font-size:12.5px;display:inline-flex;gap:6px;align-items:center">
        <b>${esc(e ? (e.label !== name ? e.label + "（" + name + "）" : name) : name)}</b>
        ${e ? "" : `<span class="hint">外部列（原样保留）</span>`}
        <a href="javascript:void 0" class="ec-rm" data-n="${esc(name)}">✕</a></span>`;
    });
    chipsEl.innerHTML = `<div style="display:flex;flex-wrap:wrap;gap:5px;align-items:center">
      <span class="hint">已选 ${chosen.size} 列：</span>${items.join("") || "<span class='hint'>尚未选择</span>"}</div>`;
    chipsEl.querySelectorAll(".ec-rm").forEach((a) => (a.onclick = () => { chosen.delete(a.dataset.n); paintChips(); paintList(); emit(); }));
  }
  function paintList() {
    const kw = search.toLowerCase();
    const rows = entries.filter((e) => !kw || e.name.toLowerCase().includes(kw) || (e.label || "").toLowerCase().includes(kw) || (e.doc || "").toLowerCase().includes(kw));
    const extras = [...chosen].filter((n) => !entries.some((e) => e.name === n));
    listEl.innerHTML = rows.map((e) => {
      const rs = reason(e);
      const checked = chosen.has(e.name);
      return `<label style="display:flex;gap:8px;align-items:flex-start;margin:3px 0;${rs ? "opacity:.62" : ""}">
        <input type="checkbox" data-n="${esc(e.name)}" ${checked ? "checked" : ""} ${rs ? "disabled" : ""} style="margin-top:3px">
        <span><b>${esc(e.label !== e.name ? e.label + "（" + e.name + "）" : e.name)}</b>
        ${rs ? `<span class="hint">— ${rs}</span>` : ""}
        ${e.doc ? `<span class="hint" style="display:block">${esc(e.doc.slice(0, 90))}</span>` : ""}</span></label>`;
    }).join("") + extras.map((n) => `<label style="display:flex;gap:8px;margin:3px 0;opacity:.62">
        <input type="checkbox" checked disabled><span><b>${esc(n)}</b> <span class="hint">— 不在本表字段清单（原样保留）</span></span></label>`).join("");
    listEl.querySelectorAll('input[type=checkbox]:not([disabled])').forEach((cb) => (cb.onchange = () => {
      cb.checked ? chosen.add(cb.dataset.n) : chosen.delete(cb.dataset.n);
      paintChips(); emit();
    }));
  }
  function emit() { if (opts.onChange) opts.onChange([...chosen]); }
  host.querySelector(".ec-search").oninput = (e) => { search = e.target.value; paintList(); };
  paintChips(); paintList();
  return { get: () => [...chosen] };
}

/* ---- ③ 模板列编辑器（中文名/定义/来源搜索；↑↓+拖拽排序；类型专用控件；未支持规则原样保留） ---- */
const COL_TYPE_CN = { plain: "原文", key: "标识编号", sci: "学名（属斜体）", date: "日期三分量", number: "数值", composite: "复合字段" };
const COL_FUNCS_CN = { locality: "地点组合", material_desc: "材料描述", join: "文本连接", first_non_empty: "取第一个非空" };
const MISSING_CN = { dash: "—（短横）", unknown: "未知", empty: "留空", custom: "自定义文本…" };
function renderColumnEditor(host, opts = {}) {
  const tname = opts.tname || "specimens";
  const entries = fieldEntries(tname);
  const KNOWN_BASE = ["field", "out", "source"];
  const KNOWN_OPTS = {
    plain: ["missing", "missing_text"], key: [], sci: [],
    date: ["y", "m", "d", "missing", "missing_text"],
    number: ["decimals", "missing", "missing_text"],
  };
  const isKnown = (c) => {
    const src = c.source || "plain";
    if (!(src in COL_TYPE_CN)) return false;
    const keysOk = Object.keys(c).every((k) => KNOWN_BASE.includes(k) || k === "opts" || k === "func" || k === "parts");
    const optsOk = !c.opts || src === "composite" || Object.keys(c.opts).every((k) => (KNOWN_OPTS[src] || []).includes(k));
    const funcOk = src !== "composite" || !!COL_FUNCS_CN[c.func];
    return keysOk && optsOk && funcOk;
  };
  const items = (opts.columns || []).map((c) => ({ kind: isKnown(c) ? "ui" : "raw", col: c }));
  let advanced = false, dragIdx = null;
  host.innerHTML = `
    <div class="ce" style="border:1px solid var(--border);border-radius:8px;padding:8px 10px">
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <button class="btn ce-add" type="button">${icon("plus")} 添加列</button>
        <button class="btn ce-toggle" type="button">高级（JSON）</button>
        <span class="hint">拖动 ☰ 或用 ↑↓ 调整列顺序；错误显示在对应控件旁。</span>
      </div>
      <div class="ce-list" style="margin-top:8px"></div>
      <div class="ce-json" style="display:none;margin-top:8px"></div>
    </div>`;
  const listEl = host.querySelector(".ce-list");
  const jsonEl = host.querySelector(".ce-json");
  function classify(col) {
    const src = col.source || "plain";
    if (!(src in COL_TYPE_CN)) return "未知列类型「" + src + "」（将以原样保留）";
    if (src === "composite" && !COL_FUNCS_CN[col.func]) return "复合函数需选择：地点组合／材料描述／文本连接／取第一个非空";
    if (src === "composite" && !(col.parts || []).length) return "复合字段至少添加一个组成字段";
    if (src !== "composite" && !col.field) return "请选择字段";
    return null;
  }
  function missingSel(cur) {
    return `<select class="input ce-missing" style="width:auto">
      ${Object.entries(MISSING_CN).map(([k, v]) => `<option value="${k}" ${(cur || "dash") === k ? "selected" : ""}>${v}</option>`).join("")}</select>`;
  }
  function rowHtml(it, i) {
    if (it.kind === "raw") {
      return `<div class="ce-row-raw" style="border:1px dashed var(--c-pending);border-radius:8px;padding:6px 8px;margin-bottom:6px">
        <b>未支持配置（原样保留，不修改不丢失）</b>
        <textarea class="input" rows="2" readonly style="width:100%;font-family:monospace;margin-top:4px">${esc(JSON.stringify(it.col))}</textarea>
        <div class="form-actions" style="margin:4px 0 0"><button class="btn danger ce-rawdel" data-i="${i}" type="button">删除此列</button></div></div>`;
    }
    const c = it.col;
    const src = c.source || "plain";
    const entry = entries.find((e) => e.name === c.field);
    const err = classify(c);
    return `<div class="ce-row" draggable="true" data-i="${i}" style="border:1px solid var(--border);border-radius:8px;padding:6px 8px;margin-bottom:6px">
      <div style="display:flex;gap:6px;align-items:center">
        <span class="ce-drag" title="拖动排序" style="cursor:grab">☰</span>
        <button class="btn ce-up" type="button" title="上移">↑</button>
        <button class="btn ce-down" type="button" title="下移">↓</button>
        <b>${esc(c.out || c.field || "（未命名列）")}</b>
        <span class="hint">${COL_TYPE_CN[src] || src}${entry && entry.hint ? " · " + esc(entry.hint).slice(0, 60) : ""}</span>
        <span style="flex:1"></span>
        <button class="btn danger ce-del" type="button">删除</button>
      </div>
      <div style="display:grid;grid-template-columns:2fr 1.4fr 130px;gap:6px;margin-top:5px">
        <input class="input ce-field" list="ce-fields" value="${esc(c.field || "")}" placeholder="字段（可按中文名/原字段名/定义搜索）">
        <input class="input ce-out" placeholder="导出列名" value="${esc(c.out || "")}">
        <select class="input ce-src">${Object.entries(COL_TYPE_CN).map(([k, v]) => `<option value="${k}" ${src === k ? "selected" : ""}>${v}</option>`).join("")}</select>
      </div>
      <div class="ce-extra" style="margin-top:5px"></div>
      <div class="ce-err" style="color:var(--c-risk);font-size:12.5px">${err ? esc(err) : ""}</div>
    </div>`;
  }
  function extraControls(it) {
    const c = it.col;
    const src = c.source || "plain";
    const o = c.opts || {};
    const opt = (name, cur) => `<option value="${esc(name)}" ${cur === name ? "selected" : ""}>${esc(name)}</option>`;
    if (src === "composite") {
      const parts = c.parts || [];
      return `<div style="display:grid;grid-template-columns:auto 1fr;gap:6px;align-items:start">
        <label>复合函数<select class="input ce-func" style="width:auto">
          ${Object.entries(COL_FUNCS_CN).map(([k, v]) => `<option value="${k}" ${c.func === k ? "selected" : ""}>${v}</option>`).join("")}</select></label>
        <div>组成字段：<span class="ce-parts" style="display:inline-flex;flex-wrap:wrap;gap:5px;vertical-align:middle">
          ${parts.map((p, pi) => `<span style="border:1px solid var(--border);border-radius:14px;padding:2px 9px;font-size:12.5px">${esc(p.field || "")}
            <a href="javascript:void 0" class="ce-prm" data-pi="${pi}">✕</a></span>`).join("") || "<span class='hint'>尚未添加</span>"}</span>
          <select class="input ce-partadd" style="width:auto;max-width:230px"><option value="">添加组成字段…</option>
            ${entries.map((e2) => opt(e2.name, "")).join("")}</select>
        </div></div>`;
    }
    if (src === "date") {
      const fy = o.y || "YYYY1", fm = o.m || "MM1", fd = o.d || "DD1";
      return `<div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">
        <label>年字段<select class="input ce-dy">${entries.map((e2) => opt(e2.name, fy)).join("")}</select></label>
        <label>月字段<select class="input ce-dm">${entries.map((e2) => opt(e2.name, fm)).join("")}</select></label>
        <label>日字段<select class="input ce-dd">${entries.map((e2) => opt(e2.name, fd)).join("")}</select></label>
        <label>缺失表示 ${missingSel(o.missing || "dash")}${(o.missing || "dash") === "custom" ? `<input class="input ce-mtext" style="width:130px" value="${esc(o.missing_text || "")}" placeholder="自定义文本">` : ""}</label>
      </div>`;
    }
    if (src === "number") {
      return `<div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">
        <label>小数位<input class="input ce-dec" type="number" min="0" max="6" style="width:76px" value="${o.decimals ?? ""}" placeholder="不填=不处理"></label>
        <label>缺失表示 ${missingSel(o.missing || "dash")}${(o.missing || "dash") === "custom" ? `<input class="input ce-mtext" style="width:130px" value="${esc(o.missing_text || "")}" placeholder="自定义文本">` : ""}</label>
      </div>`;
    }
    if (src === "plain") {
      return `<label>缺失表示 ${missingSel(o.missing || "dash")}${(o.missing || "dash") === "custom" ? `<input class="input ce-mtext" style="width:130px" value="${esc(o.missing_text || "")}" placeholder="自定义文本">` : ""}</label>`;
    }
    if (src === "sci") return `<span class="hint">学名按属斜体规则输出；缺失输出「—」。</span>`;
    return `<span class="hint">标识编号按文本原样输出（保护前导零）。</span>`;
  }
  function readRow(it, rowEl) {
    const c = it.col;
    if (rowEl.querySelector(".ce-field")) c.field = rowEl.querySelector(".ce-field").value.trim();
    if (rowEl.querySelector(".ce-out")) c.out = rowEl.querySelector(".ce-out").value.trim();
    if (rowEl.querySelector(".ce-src")) c.source = rowEl.querySelector(".ce-src").value;
    const src = c.source || "plain";
    if (src === "composite") {
      const fn = rowEl.querySelector(".ce-func");
      if (fn) c.func = fn.value;
      delete c.opts;
    } else {
      delete c.func; delete c.parts;
      const o = {};
      if (src === "date") {
        const dy = rowEl.querySelector(".ce-dy"), dm = rowEl.querySelector(".ce-dm"), dd = rowEl.querySelector(".ce-dd");
        if (dy) o.y = dy.value;
        if (dm) o.m = dm.value;
        if (dd) o.d = dd.value;
      }
      if (src === "number") {
        const dec = rowEl.querySelector(".ce-dec");
        if (dec && dec.value !== "") o.decimals = Math.max(0, Math.min(6, parseInt(dec.value, 10) || 0));
      }
      const ms = rowEl.querySelector(".ce-missing");
      if (ms) {
        o.missing = ms.value;
        const mt = rowEl.querySelector(".ce-mtext");
        if (ms.value === "custom") o.missing_text = mt ? mt.value : "";
      }
      if (Object.keys(o).length) c.opts = o; else delete c.opts;
    }
    if (!c.out) c.out = c.field || c.source;
  }
  function paint() {
    if (advanced) {
      listEl.style.display = "none";
      jsonEl.style.display = "";
      jsonEl.innerHTML = `<p class="hint">高级模式：直接编辑列定义 JSON（结构与服务器 columns_def 契约一致）。返回基础模式时校验转换；不支持的结构将<b>原样保留</b>并标记为未支持。</p>
        <textarea class="input ce-jsontext" rows="12" style="width:100%;font-family:monospace">${esc(JSON.stringify(items.map((x) => x.col), null, 1))}</textarea>
        <div class="ce-jsonerr" style="color:var(--c-risk);font-size:12.5px"></div>`;
      return;
    }
    listEl.style.display = "";
    jsonEl.style.display = "none";
    if (!document.getElementById("ce-fields")) {
      const dl = document.createElement("datalist");
      dl.id = "ce-fields";
      dl.innerHTML = entries.map((e) => `<option value="${esc(e.name)}">${esc((e.label !== e.name ? e.label + " · " : "") + (e.doc || "").slice(0, 50))}</option>`).join("");
      host.querySelector(".ce").appendChild(dl);
    }
    listEl.innerHTML = items.map((it, i) => rowHtml(it, i)).join("")
      || `<p class="hint">还没有列——点「添加列」开始。</p>`;
    listEl.querySelectorAll(".ce-row").forEach((rowEl) => {
      const i = +rowEl.dataset.i;
      const it = items[i];
      const exEl = rowEl.querySelector(".ce-extra");
      exEl.innerHTML = extraControls(it);
      const syncFromDom = () => { readRow(it, rowEl); const err = classify(it.col); rowEl.querySelector(".ce-err").textContent = err ? err : ""; emit(); };
      const fld = rowEl.querySelector(".ce-field");
      if (fld) { fld.onchange = syncFromDom; fld.onblur = syncFromDom; }
      const outI = rowEl.querySelector(".ce-out");
      if (outI) outI.oninput = syncFromDom;
      const srcSel = rowEl.querySelector(".ce-src");
      if (srcSel) srcSel.onchange = () => { syncFromDom(); paint(); };
      const fn = rowEl.querySelector(".ce-func");
      if (fn) fn.onchange = () => { syncFromDom(); paint(); };
      const pa = rowEl.querySelector(".ce-partadd");
      if (pa) pa.onchange = () => { if (pa.value) { (it.col.parts = it.col.parts || []).push({ field: pa.value }); paint(); emit(); } };
      rowEl.querySelectorAll(".ce-prm").forEach((a) => (a.onclick = () => { it.col.parts.splice(+a.dataset.pi, 1); paint(); emit(); }));
      for (const cls of [".ce-dy", ".ce-dm", ".ce-dd", ".ce-missing", ".ce-dec"]) {
        const el = rowEl.querySelector(cls);
        if (el) el.onchange = () => { syncFromDom(); paint(); };
      }
      const mt = rowEl.querySelector(".ce-mtext");
      if (mt) mt.oninput = () => { readRow(it, rowEl); };
      rowEl.querySelector(".ce-del").onclick = () => { items.splice(i, 1); paint(); emit(); };
      rowEl.querySelector(".ce-up").onclick = () => { if (i > 0) { [items[i - 1], items[i]] = [items[i], items[i - 1]]; paint(); emit(); } };
      rowEl.querySelector(".ce-down").onclick = () => { if (i < items.length - 1) { [items[i + 1], items[i]] = [items[i], items[i + 1]]; paint(); emit(); } };
      rowEl.addEventListener("dragstart", (e) => { dragIdx = i; e.dataTransfer.effectAllowed = "move"; });
      rowEl.addEventListener("dragover", (e) => e.preventDefault());
      rowEl.addEventListener("drop", (e) => {
        e.preventDefault();
        if (dragIdx === null || dragIdx === i) return;
        const [m] = items.splice(dragIdx, 1);
        items.splice(i, 0, m);
        dragIdx = null;
        paint(); emit();
      });
    });
    listEl.querySelectorAll(".ce-rawdel").forEach((b) => (b.onclick = () => { items.splice(+b.dataset.i, 1); paint(); emit(); }));
  }
  function emit() { if (opts.onChange) opts.onChange(get()); }
  function get() { return items.map((x) => x.col); }
  host.querySelector(".ce-add").onclick = () => { items.push({ kind: "ui", col: { field: "", out: "", source: "plain" } }); paint(); emit(); };
  host.querySelector(".ce-toggle").onclick = () => {
    if (!advanced) {
      advanced = true;
      host.querySelector(".ce-toggle").textContent = "返回基础模式";
      paint();
      emit();
      return;
    }
    const ta = jsonEl.querySelector(".ce-jsontext");
    let parsed;
    try { parsed = JSON.parse(ta ? ta.value : "null"); }
    catch (e) {
      jsonEl.querySelector(".ce-jsonerr").textContent = "JSON 解析失败：" + e.message + "——请修正后切换（输入已保留）";
      return;
    }
    if (!Array.isArray(parsed)) {
      jsonEl.querySelector(".ce-jsonerr").textContent = "列定义需为数组（[ {...}, ... ]）——输入已保留";
      return;
    }
    items.length = 0;
    for (const c of parsed) {
      if (!c || typeof c !== "object" || Array.isArray(c)) { items.push({ kind: "raw", col: c }); continue; }
      items.push({ kind: isKnown(c) ? "ui" : "raw", col: c });
    }
    advanced = false;
    host.querySelector(".ce-toggle").textContent = "高级（JSON）";
    paint();
    emit();
  };
  paint();
  return { get };
}

/* 从明确记录集合创建协作包（v3.78）：表格勾选 / 项目成员共用；不需要再次粘贴编号 */
function openCollabForIds(refs, srcLabel) {
  const out = { closed: false };
  openModal(`创建协作包 · ${srcLabel || "明确记录集合"} ${refs.length} 行`, `
    <p class="hint">记录集合已显式传入（来源：${esc(srcLabel || "选中记录")}）；不含令牌，权威基线存服务器。</p>
    <label>允许编辑的列（勾选即可；身份列与关联只读列自动禁选）</label>
    <div id="cf2-cols"></div>
    <div class="form-actions"><button class="btn" id="cf2-cancel">取消</button>
      <button class="btn primary" id="cf2-go" disabled>创建包</button></div>
    <div id="cf2-done"></div>`);
  $("cf2-cancel").onclick = closeModal;
  let colsSel = [];
  const goBtn = $("cf2-go");
  renderEditableCols($("cf2-cols"), { tname: "specimens", value: ["Notes", "DetBy", "DetDate"],
    onChange: (names) => { colsSel = names; goBtn.disabled = !names.length; } });
  goBtn.onclick = async () => {
    if (!colsSel.length) { toast("请至少勾选一列", "err"); return; }
    goBtn.disabled = true;
    try {
      const r = await api("/api/collab/packages", { method: "POST",
        body: JSON.stringify({ rids: refs.map((m) => m.rid), allowed_cols: colsSel }) });
      $("cf2-done").innerHTML = `<p class="hint" style="color:var(--ok)">已创建 ${esc(r.pkg_id)}（${r.rows} 行）。</p>
        <div class="form-actions"><button class="btn primary" id="cf2-dl">${icon("download")} 下载工作簿（发给协作者）</button>
        <button class="btn" id="cf2-goto">前往协作编辑包页</button></div>`;
      $("cf2-dl").onclick = () => downloadFile(`/api/collab/packages/${r.pkg_id}/workbook`).catch((e) => toast(e.message, "err"));
      $("cf2-goto").onclick = () => { closeModal(); switchView("collab"); };
    } catch (e) { toast(e.message, "err"); goBtn.disabled = false; }
  };
  return out;
}

/* ---- 行内更多菜单（v3.80）：主要动作留在行内，复制/删除/维护收进菜单；键盘与读屏可达 ---- */
function rowMenu(items) {
  /* items: [{label, act, danger, disabled, title}] → html；动作经既有 [data-act]/[data-a] 委托执行 */
  const btns = items.map((it) => `<button class="btn ${it.danger ? "danger" : ""}" type="button" data-rmi="${it.act}"
      ${it.disabled ? "disabled" : ""} ${it.title ? `title="${esc(it.title)}"` : ""}>${esc(it.label)}</button>`).join("");
  return `<span class="rowmenu-wrap" style="position:relative;display:inline-block">
    <button class="btn" type="button" data-rmb aria-haspopup="true" aria-expanded="false" title="更多操作">更多 ▾</button>
    <div class="rowmenu" hidden role="menu"
      style="position:absolute;right:0;top:100%;z-index:40;min-width:150px;background:var(--bg,#fff);border:1px solid var(--border);border-radius:8px;box-shadow:0 6px 18px rgba(0,0,0,.16);padding:4px">
      ${btns}</div></span>`;
}
function wireRowMenus(scope) {
  (scope || document).querySelectorAll("[data-rmb]").forEach((b) => {
    b.onclick = (e) => {
      e.stopPropagation();
      const menu = b.parentElement.querySelector(".rowmenu");
      const show = menu.hidden;
      document.querySelectorAll(".rowmenu").forEach((m) => { if (m !== menu) m.hidden = true; });
      document.querySelectorAll("[data-rmb]").forEach((x) => { if (x !== b) x.setAttribute("aria-expanded", "false"); });
      menu.hidden = !show;
      b.setAttribute("aria-expanded", String(show));
    };
  });
  (scope || document).querySelectorAll(".rowmenu").forEach((m) => {
    m.addEventListener("click", (e) => {
      if (e.target.closest("button")) { m.hidden = true; const b = m.parentElement.querySelector("[data-rmb]"); if (b) b.setAttribute("aria-expanded", "false"); }
    });
  });
}
document.addEventListener("click", (e) => {
  if (!e.target.closest(".rowmenu-wrap")) {
    document.querySelectorAll(".rowmenu").forEach((m) => { m.hidden = true; });
    document.querySelectorAll("[data-rmb]").forEach((x) => x.setAttribute("aria-expanded", "false"));
  }
});

/* ---------------- 可视化配置编辑器（v3.74）
/* ---------------- 可复用实体选择器（v3.72） ----------------
   renderEntityPicker(host, opts) → { selected, ready, setTables, invalidate }
   opts.tables 允许表范围；opts.multi 单/多选；opts.compact 单行输入；opts.preselected 预置稳定引用；
   opts.sources = { checkbox: [{tname,rid,key,label}]（接收表格勾选）, projects: {excludePid}（接收项目成员） }
   opts.onChange(state) → {selected:[...], ready, parsing, stale, note}
   约束：内部只存稳定引用（tname+rid）；用户只见业务编号/中文表名/学名/凭证，全程不要求记忆 rid；
   多候选（跨表/同表重复/前缀多条/名称检索）必须人工点选，绝不默认取第一条；
   输入内容或表范围一变立即失效旧解析（stale），解析未完成时 ready=false 禁止宿主提交；
   服务端业务端点提交时仍核对身份/权限/行版本。 */
let _epSeq = 0;
function renderEntityPicker(host, opts = {}) {
  const pid = "ep" + (++_epSeq);
  const multi = opts.multi !== false;
  let tables = (opts.tables || ["specimens"]).slice();
  const sel = new Map();                    // "tname:rid" → {tname,rid,key,sci,voucher,label}
  let session = 0, parsing = false, stale = false, lastInputs = "";
  host.innerHTML = `
    <div class="ep-box" data-pid="${pid}" style="border:1px solid var(--border);border-radius:8px;padding:8px 10px">
      ${opts.compact
        ? `<input class="input ep-in" style="width:100%" placeholder="${esc(opts.placeholder || "编号或名称（批量用逗号分隔）")}">`
        : `<textarea class="input ep-in" rows="${opts.rows || 4}" style="width:100%" placeholder="${esc(opts.placeholder || "粘贴编号或名称；支持换行 / Tab / 中英文逗号 / 分号批量分隔")}"></textarea>`}
      <div class="form-actions" style="margin:5px 0 0;justify-content:flex-start;flex-wrap:wrap;gap:6px">
        <button class="btn ep-go" type="button">${icon("view")} 解析</button>
        ${(opts.sources && opts.sources.checkbox || []).length ? `<button class="btn ep-ck" type="button">接收表格勾选（${opts.sources.checkbox.length} 行）</button>` : ""}
        ${(opts.sources && opts.sources.projects) ? `<select class="input ep-proj" style="width:auto;max-width:240px"><option value="">从项目导入成员…</option></select>` : ""}
        <span class="hint ep-state"></span>
      </div>
      <div class="ep-sel" style="margin-top:6px"></div>
      <div class="ep-idv" style="margin-top:4px"></div>
      <div class="ep-res" style="margin-top:6px"></div>
    </div>`;
  const $c = (cls) => host.querySelector("." + cls);
  const state = () => {
    const sizeOk = multi || sel.size <= 1;
    let note = "";
    if (parsing) note = "解析中…";
    else if (stale) note = "输入或范围已变化，请重新解析（旧结果已失效）";
    else if (!multi && sel.size > 1) note = `单选模式：已选 ${sel.size} 个，请移除多余项`;
    return { selected: [...sel.values()], ready: !parsing && !stale && sizeOk,
             parsing, stale, note };
  };
  const emit = () => { const s = state();
    $c("ep-state").textContent = `已选 ${sel.size}${s.note ? " · " + s.note : ""}`;
    if (opts.onChange) opts.onChange(s); };
  const escKey = (m) => m.tname + ":" + m.rid;
  function renderSel() {
    const box = $c("ep-sel");
    if (!sel.size) { box.innerHTML = ""; return; }
    box.innerHTML = `<div style="display:flex;flex-wrap:wrap;gap:5px;align-items:center">
      <span class="hint">已选 ${sel.size}：</span>
      ${[...sel.values()].map((m) => `<span style="border:1px solid var(--border);border-radius:14px;padding:2px 9px;font-size:12.5px;display:inline-flex;gap:6px;align-items:center">
          <b>${esc(m.key)}</b><span class="hint">${esc(m.label || m.tname)}</span>
          <a href="javascript:void 0" class="ep-view" data-k="${escKey(m)}" title="查看身份（只读，不改变选择）">身份</a>
          <a href="javascript:void 0" class="ep-rm" data-k="${escKey(m)}" title="移除">✕</a></span>`).join("")}
      <button class="btn ep-clear" type="button" style="padding:1px 8px">清空</button></div>
    ${[...sel.values()].map((m) => `<div class="ep-idinfo" data-ik="${escKey(m)}" style="display:none" >
        <span class="hint">身份：${esc(m.label || m.tname)} · 记录 ID ${m.rid} · 编号 ${esc(m.key)}${m.sci ? " · " + esc(m.sci) : ""}${m.voucher ? " · 凭证 " + esc(m.voucher) : ""}（只读查看，不改变选择）</span></div>`).join("")}`;
    box.querySelectorAll(".ep-rm").forEach((a) => (a.onclick = () => { sel.delete(a.dataset.k); renderSel(); emit(); }));
    box.querySelector(".ep-clear").onclick = () => { sel.clear(); renderSel(); emit(); };
    box.querySelectorAll(".ep-view").forEach((a) => (a.onclick = () => {
      const d = host.querySelector(`.ep-idinfo[data-ik="${CSS.escape(a.dataset.k)}"]`);
      if (d) d.style.display = d.style.display === "none" ? "" : "none";
    }));
  }
  function invalidate(markStale = true) {
    session++;
    parsing = false;
    stale = markStale;
    if (markStale) $c("ep-res").innerHTML = `<p class="hint">输入或范围已变化——旧解析结果已失效，请重新解析。</p>`;
    emit();
  }
  function applyResults(rj) {
    const groups = { matched: [], multi: [], not_found: [], duplicate_input: [], out_of_scope: [] };
    for (const r of rj.results) (groups[r.status] || groups.not_found).push(r);
    for (const r of groups.matched) for (const m of r.matches) sel.set(escKey(m), m);
    const row = (m, chosen) => `<button class="btn ep-cand" type="button" data-m="${esc(JSON.stringify(m))}"
        style="${chosen ? "border-color:var(--c-saved);color:var(--c-saved)" : ""}">${esc(m.key)}（${esc(m.label || m.tname)}${m.sci ? " · " + esc(m.sci) : ""}${m.voucher ? " · 凭证 " + esc(m.voucher) : ""}）${chosen ? " ✓" : ""}</button>`;
    const box = $c("ep-res");
    box.innerHTML = `
      ${groups.matched.length ? `<div><b>✔ 已匹配 ${groups.matched.length}</b>${groups.matched.map((r) =>
        `<div class="hint" style="margin:2px 0">${esc(r.input)} → ${r.matches.map((m) => row(m, sel.has(escKey(m)))).join(" ")}${r.via === "prefix" ? "（前缀唯一命中）" : ""}</div>`).join("")}</div>` : ""}
      ${groups.multi.length ? `<div style="margin-top:6px"><b style="color:var(--c-pending)">？多候选 ${groups.multi.length}（需人工确认，未默认选择）</b>${groups.multi.map((r) =>
        `<div style="margin:3px 0"><span class="hint">${esc(r.input)}：</span>${esc(r.reason || "")}<div style="display:flex;flex-wrap:wrap;gap:5px;margin-top:3px">${r.matches.map((m) => row(m, sel.has(escKey(m)))).join("")}</div></div>`).join("")}</div>` : ""}
      ${groups.out_of_scope.length ? `<div style="margin-top:6px"><b>⛔ 不在范围 ${groups.out_of_scope.length}</b>${groups.out_of_scope.map((r) =>
        `<div class="hint" style="margin:2px 0">${esc(r.input)} → 存在于：${esc((r.where || []).join("、"))}（不在本选择器范围）</div>`).join("")}</div>` : ""}
      ${groups.not_found.length ? `<div style="margin-top:6px"><b style="color:var(--c-risk)">✖ 未找到 ${groups.not_found.length}</b>
        <div class="hint" style="margin:2px 0">保留原输入便于修正：${groups.not_found.map((r) => `<code>${esc(r.input)}</code>`).join("、")}——可在上方修改后重新解析</div></div>` : ""}
      ${groups.duplicate_input.length ? `<div class="hint" style="margin-top:6px">重复输入 ${groups.duplicate_input.length} 条（与前面重复，已跳过）</div>` : ""}`;
    box.querySelectorAll(".ep-cand").forEach((b) => (b.onclick = () => {
      const m = JSON.parse(b.dataset.m);
      if (!multi && sel.size >= 1 && !sel.has(escKey(m))) sel.clear();
      sel.set(escKey(m), m);
      renderSel(); applyResults(rj);   // 重渲染以更新高亮与计数
    }));
    renderSel();
    emit();
  }
  async function parse() {
    const text = $c("ep-in").value;
    const inputs = text.split(/[\r\n\t,，;；]+/).map((x) => x.trim()).filter(Boolean);
    if (!inputs.length) { invalidate(false); $c("ep-res").innerHTML = `<p class="hint">输入为空——多选模式可直接使用下方来源按钮，或粘贴编号。</p>`; emit(); return; }
    const my = ++session;
    parsing = true; stale = false;
    $c("ep-res").innerHTML = `<p class="hint">解析中…</p>`;
    emit();
    let rj;
    try { rj = await api("/api/resolve/entities", { method: "POST", body: JSON.stringify({ inputs, tables }) }); }
    catch (e) { if (my !== session) return; parsing = false; stale = true; $c("ep-res").innerHTML = `<p class="hint warn">${esc(e.message)}</p>`; emit(); return; }
    if (my !== session) return;        // 会话已失效：丢弃旧响应（绝不应用过期解析）
    parsing = false;
    lastInputs = text;
    applyResults(rj);
  }
  $c("ep-go").onclick = parse;
  $c("ep-in").addEventListener("input", () => invalidate(true));
  if (opts.sources && opts.sources.checkbox && opts.sources.checkbox.length) {
    $c("ep-ck").onclick = () => {
      for (const r of opts.sources.checkbox) if (multi || !sel.size) sel.set(r.tname + ":" + r.rid, r);
      renderSel(); emit();
    };
  }
  if (opts.sources && opts.sources.projects) {
    const drop = $c("ep-proj");
    api("/api/projects").then((d) => {
      drop.innerHTML = `<option value="">从项目导入成员…</option>` + d.projects
        .filter((p) => !opts.sources.projects.excludePid || p.id !== opts.sources.projects.excludePid)
        .map((p) => `<option value="${p.id}">${esc(p.name)}（${p.member_count}）</option>`).join("");
    }).catch(() => { });
    drop.onchange = async () => {
      const pid2 = +drop.value;
      if (!pid2) return;
      try {
        const d = await api(`/api/projects/${pid2}`);
        let n = 0;
        for (const m of d.members) {
          if (!m.tname || !tables.includes(m.tname)) continue;
          sel.set(m.tname + ":" + m.rid, { tname: m.tname, rid: m.rid, key: m.current_key || ("#" + m.rid),
            sci: "", voucher: "", label: tblName(S.meta.tables[m.tname]) || m.tname });
          n++;
        }
        renderSel(); emit();
        toast(`已接收项目成员 ${n} 个（稳定引用，不复制数据）`, "ok");
      } catch (e) { toast(e.message, "err"); }
      drop.value = "";
    };
  }
  for (const m of opts.preselected || []) sel.set(m.tname + ":" + m.rid, m);
  renderSel();
  emit();
  return {
    selected: () => [...sel.values()],
    ready: () => state().ready,
    state,
    setTables(list) { tables = (list || []).slice(); invalidate(true); },
    preselect(list) { for (const m of list || []) sel.set(m.tname + ":" + m.rid, m); renderSel(); emit(); },
    invalidate,
  };
}

/* ---- 主导航：按任务组织（数据录入 / 质量与追溯 / 更多数据 / 管理），折叠由按钮与窄屏自动触发 ---- */
const NAV_GROUPS = [
  { key: "enter", title: "数据录入", match: (t, c) => c.nav !== "more", icon: "table" },
  { key: "quality", title: "质量与追溯", items: [
    ["workbench", "鉴定工作台", "check"], ["checkup", "数据体检", "check"],
    ["history", "变更历史", "clock"], ["stats", "统计", "chart"],
    ["movements", "流转与借出", "linked"],
    ["recent", "最近操作", "clock"]] },
  { key: "more", title: "更多数据", match: (t, c) => c.nav === "more", icon: "table" },
  { key: "lab", title: "实验与项目", items: [
    ["labpage", "实验板", "box"], ["projects", "项目工作集", "tag"]] },
  { key: "output", title: "成果输出", items: [
    ["tplexp", "模板导出", "download"], ["biblio", "文献与引证", "copy"],
    ["collab", "协作编辑包", "user"]] },
  { key: "system", title: "系统", items: [["about", "关于本系统", "check"]] },
];
const VIEW_TITLES = { home: "标本库总览", workbench: "鉴定工作台", checkup: "数据体检",
  history: "变更历史", stats: "统计", about: "关于本系统",
  tplexp: "模板导出", collab: "协作编辑包",
  movements: "流转与借出", labpage: "实验板", biblio: "文献与引证", projects: "项目工作集",
  recent: "最近操作" };

function buildTabs() {
  const nav = $("table-tabs");
  nav.innerHTML = "";
  // 任务分组：数据录入=主表（逐表可见性决定是否出现）；类群作为分组副标题
  for (const g of NAV_GROUPS) {
    let entries = g.items ? g.items.map(([t, label, icn]) => [t, { label, _icon: icn }])
      : Object.entries(myTables()).filter(([t, c]) => g.match(t, c)).map(([t, c]) => [t, { ...c, _icon: "table" }]);
    if (!entries.length) continue;
    // 分组视觉框：每组一张卡片，模块边界更清晰
    const box = document.createElement("div");
    box.className = "nav-group";
    nav.appendChild(box);
    const title = document.createElement("div");
    title.className = "nav-group-title";
    title.textContent = g.title;
    box.appendChild(title);
    for (const [t, c] of entries) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "nav-btn";
      b.dataset.t = t;
      b.innerHTML = icon(c._icon || "table")
        + `<span class="nav-label">${esc(tblName(c))}</span>`;
      // 图标按钮在折叠态也要有可访问名称
      b.setAttribute("aria-label", tblName(c));
      b.title = tblName(c);
      b.onclick = () => { $("more-menu").hidden = true; switchView(t); };
      box.appendChild(b);
    }
  }
  // 组合表（v4.07）：已发布/授权的组合表按导航分组展示，附构建器与管理入口
  renderCompositeNav();
  $("more-wrap").hidden = true;  // 旧"更多"入口由侧边栏任务分组取代
}

/* ---- 面包屑：任务组 / 当前表 ---- */
function updateCrumb(t) {
  const crumb = $("crumb");
  if (String(t).startsWith("ct:")) {
    const d0 = ctDefById(t.slice(3));
    crumb.innerHTML = `<span class="crumb-root" role="link" tabindex="0">组合表</span><span class="crumb-sep">/</span>
      <span class="crumb-here">${esc(d0 ? d0.name : t)}</span>`;
    const r0 = crumb.querySelector(".crumb-root");
    if (r0) r0.onclick = () => switchView("home");
    document.title = (d0 ? d0.name + " · " : "") + (S.meta.system_name || "标本信息管理系统");
    return;
  }
  const cfg = S.meta.tables[t];
  const group = t === "home" ? "" : (NAV_GROUPS.find((g) =>
    g.items ? g.items.some(([x]) => x === t) : (cfg && g.match && g.match(t, cfg))) || {}).title || "";
  const here = cfg ? tblName(cfg) : (VIEW_TITLES[t] || "");
  crumb.innerHTML =
    (group ? `<span class="crumb-root" role="link" tabindex="0">${esc(group)}</span><span class="crumb-sep">/</span>` : "")
    + `<span class="crumb-here">${esc(here || "标本信息管理系统")}</span>`;
  const root = crumb.querySelector(".crumb-root");
  if (root) root.onclick = () => switchView("home");
  document.title = (here ? here + " · " : "") + (S.meta.system_name || "标本信息管理系统");
}

/* 折叠导航：按钮切换 + localStorage 记忆；1366×768 或 200% 缩放（窄视口）自动收起 */
$("nav-collapse").onclick = () => {
  document.body.classList.toggle("nav-collapsed");
  try { localStorage.setItem("sm_nav_collapsed", document.body.classList.contains("nav-collapsed") ? "1" : ""); } catch { }
};
try { if (localStorage.getItem("sm_nav_collapsed")) document.body.classList.add("nav-collapsed"); } catch { }
try { if (localStorage.getItem("sm_density") === "compact") document.body.classList.add("density-compact"); } catch { }

/* ---- 工具栏收纳菜单（导入/导出/管理）开合；管理菜单支持检索发现 ---- */
function wireTmenu(btnId, menuId, opts = {}) {
  const btn = $(btnId), menu = $(menuId);
  btn.onclick = (e) => {
    e.stopPropagation();
    const show = menu.hidden;
    document.querySelectorAll(".tmenu").forEach((m) => { if (m !== menu) m.hidden = true; });
    menu.hidden = !show;
    if (show && opts.onopen) opts.onopen();
    if (show) {
      const s = menu.querySelector(".tmenu-search input");
      if (s) s.focus();
    }
  };
  // 点菜单项即执行动作并收起菜单（避免菜单残留在弹窗/页面之后）
  menu.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (b && !b.disabled) menu.hidden = true;
  });
}
document.addEventListener("click", (e) => {
  if (!e.target.closest(".tmenu-wrap")) document.querySelectorAll(".tmenu").forEach((m) => m.hidden = true);
});
$("task-view").onchange = () => applyTaskView($("task-view").value);

/* ---- 任务视图：预设（tables_def VIEWS）+ 应用/恢复 ---- */
function taskViews() { return (curCfg() && curCfg().views) || []; }
async function applyTaskView(viewId) {
  const v = taskViews().find((x) => x.id === viewId);
  const allNames = S.columns.map((c) => c.name);
  const keyName = curCfg().key;
  if (!v) {
    // 恢复整表默认：清空该表的隐藏/列序/固定。
    // 表级浅合并协议下，本地 delete 不会传给服务器——必须显式 __delete__（v3.91 修复：
    // 此前只删本地，服务器保留旧值，刷新后被 prefsReload 拉回，恢复默认时灵时不灵）
    const dels = [`hidden.${S.t}`, `colorder.${S.t}`, `freeze.${S.t}`];
    for (const k of ["hidden", "colorder", "freeze"]) {
      if (S.prefs[k]) delete S.prefs[k][S.t];
    }
    S.prefs.view_sel = S.prefs.view_sel || {};
    delete S.prefs.view_sel[S.t];
    S.sort = ""; S.dir = "asc"; S.page = 1;
    await savePrefs([], dels);
    if (S.prefsReload) await S.prefsReload();
    loadTable();
    toast("已恢复本表默认列显示", "ok");
    return;
  }
  const keep = new Set(v.columns);
  if (keyName) keep.add(keyName);           // 主键始终保留（身份列）
  S.prefs.hidden = S.prefs.hidden || {};
  S.prefs.hidden[S.t] = allNames.filter((n) => !keep.has(n));
  S.prefs.colorder = S.prefs.colorder || {};
  S.prefs.colorder[S.t] = v.columns.filter((n) => allNames.includes(n));
  S.prefs.freeze = S.prefs.freeze || {};
  S.prefs.freeze[S.t] = (v.freeze || []).filter((n) => allNames.includes(n));
  S.prefs.view_sel = S.prefs.view_sel || {};
  S.prefs.view_sel[S.t] = viewId;
  if (v.sort && S.columns.some((c) => c.name === v.sort)) { S.sort = v.sort; S.dir = v.dir || "asc"; }
  S.page = 1;
  await savePrefs();
  if (S.prefsReload) await S.prefsReload();
  loadTable();
  toast(`已应用任务视图「${v.label}」：${v.columns.length} 列、固定 ${ (v.freeze || []).length } 列`, "ok");
}
wireTmenu("btn-import-menu", "menu-import");
wireTmenu("btn-export-menu", "menu-export");
wireTmenu("btn-manage-menu", "menu-manage", {
  onopen: () => {
    const q = $("manage-search");
    q.value = "";
    const apply = () => {
      const kw = q.value.trim().toLowerCase();
      $("menu-manage").querySelectorAll("button").forEach((b) => {
        if (b.id === "manage-search") return;
        b.style.display = !kw || b.textContent.toLowerCase().includes(kw) ? "" : "none";
      });
    };
    q.oninput = apply;
    apply();
  },
});

/* 恢复全部偏好（范围明确：清空 hidden/colw/views/colorder/freeze/view_sel/density；重置前自动留存快照） */
$("mi-reset-prefs").onclick = async () => {
  if (!(await confirmBox("恢复全部偏好为默认值？\n范围：列显示/列宽/我的视图/列序/固定列/视图选择/显示密度，全部恢复默认。\n重置前自动留存快照——可在「偏好历史」中审阅找回。", { okText: "恢复默认" }))) return;
  try {
    await api("/api/prefs/reset", { method: "POST", body: JSON.stringify({ confirm: "RESET" }) });
    S.prefs = {};
    try { localStorage.removeItem("sm_density"); document.body.classList.remove("density-compact"); } catch { }
    toast("全部偏好已恢复默认（历史快照保留，可审阅找回）", "ok");
    if (S.meta && S.meta.tables[S.t]) loadTable();
  } catch (e) { toast(e.message, "err"); }
};
/* 偏好历史（可审阅找回）：列出最近快照，一键恢复 */
$("mi-prefs-history").onclick = async () => {
  try {
    const d = await api("/api/prefs/history");
    const rows = (d.history || []).map((h, i) => `
      <tr><td>${i}</td><td>${esc(h.at || "")}</td><td>${esc(h.tag || "")}</td>
      <td>${esc((h.keys || []).join("、") || "（空）")}</td>
      <td>${h.views_count || 0} 个视图</td>
      <td><button class="btn" data-rh="${i}">恢复此快照</button></td></tr>`).join("");
    openModal("偏好历史（可审阅找回）", `
      <p class="hint">每次保存/重置前自动留存最近 ${d.max} 份快照。历史已丢失且不在快照中的视图无法凭空恢复。</p>
      <div style="max-height:50vh;overflow:auto"><table class="tbl" style="width:100%;font-size:12px">
      <thead><tr><th>#</th><th>时间</th><th>来源</th><th>包含键</th><th>视图</th><th></th></tr></thead>
      <tbody>${rows || '<tr><td colspan="6" class="hint">暂无历史快照</td></tr>'}</tbody></table></div>
      <div class="form-actions"><button class="btn" id="ph-close">关闭</button></div>`);
    $("ph-close").onclick = closeModal;
    document.querySelectorAll("#modal-body [data-rh]").forEach((b) => {
      b.onclick = async () => {
        try {
          await api("/api/prefs/restore-history", { method: "POST", body: JSON.stringify({ index: +b.dataset.rh }) });
          const pr = await api("/api/prefs");
          S.prefs = pr.prefs || {};
          closeModal();
          toast("已从历史快照恢复", "ok");
          if (S.meta && S.meta.tables[S.t]) loadTable();
        } catch (e) { toast(e.message, "err"); }
      };
    });
  } catch (e) { toast(e.message, "err"); }
};

/* 显示密度切换：紧凑/舒适（localStorage 记忆；仅影响排版，不改数据） */
$("mi-density").onclick = () => {
  const compact = document.body.classList.toggle("density-compact");
  try { localStorage.setItem("sm_density", compact ? "compact" : "cozy"); } catch { }
  toast(compact ? "已切换为紧凑密度" : "已切换为舒适密度", "ok");
};

/* 品牌（左上角标题）即首页入口 */
$("brand-home").onclick = () => switchView("home");
$("global-search-btn").onclick = openGlobalSearch;
$("brand-home").onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") switchView("home"); };

window.addEventListener("beforeunload", (e) => {
  if (dirtyEditing()) { e.preventDefault(); e.returnValue = "有未提交的修改，刷新将丢失（草稿仅保留已输入内容）"; return e.returnValue; }
  try {
    if (S.me && S.meta && S.meta.tables && S.meta.tables[S.t]) {
      const gw = document.querySelector(".grid-wrap");
      sessionStorage.setItem("sm_lastview", JSON.stringify({
        t: S.t, page: S.page,
        scroll: gw ? { left: gw.scrollLeft, top: gw.scrollTop } : null,
        sel: [...S.sel],
        cur: S.cur ? { rid: S.cur.rid, col: S.cur.col } : null,
      }));
    }
  } catch { }
});

async function switchView(t) {
  if (!(await closeGate("切换模块"))) return;   // 统一关闭门（含表格编辑/详情字段/表单草稿）
  if (S.editing) cancelEdit();
  S.t = t;
  updateCrumb(t);
  document.querySelectorAll("#table-tabs button").forEach((b) =>
    b.classList.toggle("active", b.dataset.t === t));
  $("brand-home").classList.toggle("active", t === "home");
  const moreActive = !!(S.meta.tables[t] && S.meta.tables[t].nav === "more");
  $("more-btn").classList.toggle("active", moreActive);
  document.querySelectorAll("#more-menu button").forEach((b) =>
    b.classList.toggle("active", b.dataset.t === t));
  for (const id of ["view-home", "view-table", "view-history", "view-checkup", "view-stats", "view-about", "view-workbench", "view-tplexp", "view-collab", "view-movements", "view-labpage", "view-biblio", "view-projects", "view-recent", "view-composite"])
    $(id).hidden = true;
  if (String(t).startsWith("ct:")) {
    $("view-composite").hidden = false;
    ctOpen(t.slice(3));
    return;
  }
  if (t === "home") {
    $("view-home").hidden = false;
    loadDashboard();
    loadMetrics();
  } else if (t === "history") {
    $("view-history").hidden = false;
    S.histPage = 1;
    loadHistory();
    loadActivity();
  } else if (t === "checkup") {
    $("view-checkup").hidden = false;
    loadCheckup();
  } else if (t === "workbench") {
    $("view-workbench").hidden = false;
    loadWorkbench();
  } else if (t === "about") {
    $("view-about").hidden = false;
    renderAbout();
  } else if (t === "stats") {
    $("view-stats").hidden = false;
    loadStats();
  } else if (t === "tplexp") {
    $("view-tplexp").hidden = false;
    renderTplExp();
  } else if (t === "collab") {
    $("view-collab").hidden = false;
    renderCollab();
  } else if (t === "movements") {
    $("view-movements").hidden = false;
    renderMovements();
  } else if (t === "labpage") {
    $("view-labpage").hidden = false;
    renderLab();
  } else if (t === "biblio") {
    $("view-biblio").hidden = false;
    renderBiblio();
  } else if (t === "projects") {
    $("view-projects").hidden = false;
    renderProjects();
  } else if (t === "recent") {
    $("view-recent").hidden = false;
    renderRecent();
  } else {
    $("view-table").hidden = false;
    S.showDeleted = false;
    S.preset = "";
    S.filters = {};
    S.q = ""; $("search").value = "";
    S.page = 1; S.sort = "";
    S.sel.clear();
    $("btn-sra").hidden = S.t !== "uce";
    // 任务视图下拉：预设 + 恢复默认；记忆上次选择
    {
      const tv = $("task-view");
      const views = taskViews();
      tv.innerHTML = `<option value="">任务视图：整表默认</option>`
        + views.map((v) => `<option value="${esc(v.id)}" title="${esc(v.desc || "")}">${esc(v.label)}</option>`).join("");
      S.prefs.view_sel = S.prefs.view_sel || {};
      tv.value = S.prefs.view_sel[S.t] || "";
    }
    $("btn-bin-tools").hidden = !(S.showDeleted && S.me.admin);
    const ckb = $("ck-back");
    if (ckb) {
      if (S._ckBack) {
        ckb.hidden = false;
        ckb.innerHTML = `<button class="btn" id="ck-back-btn">← 返回数据体检</button>
          <span class="hint">体检清单还有约 ${S._ckBack?.remaining ?? "?"} 项未处理（已处理项已本地记住）</span>`;
        document.getElementById("ck-back-btn")?.addEventListener("click", () => {
          S._ckBack = null;
          switchView("checkup");
        });
      } else ckb.hidden = true;
    }
    const _tabName = tblName(curCfg());
    document.title = (_tabName ? _tabName + " · " : "") + S.meta.system_name;
    const w = canWriteTable(t);
    const chip = $("perm-chip");
    chip.innerHTML = w
      ? `<span class="st st-saved">${icon("check")} 可编辑</span>`
      : `<span class="st st-readonly">${icon("view")} 只读</span>`;
    // 只读：不可用动作给出可见原因（不依赖悬停提示）
    $("btn-add").disabled = !w;
    $("btn-add").title = w ? "新增一行" : "您对本表只读，不能新增";
    for (const id of ["btn-import", "btn-paste"]) {
      const b = $(id);
      b.setAttribute("aria-disabled", String(!w));
      b.disabled = !w;
      b.title = w ? "" : "您对本表只读，不能导入数据";
    }
    $("why-ro").hidden = w;
    $("btn-loaned").hidden = !curCfg().loan_view;
    $("btn-loaned").classList.toggle("primary", false);
    // 鉴定联动（v4.31）：非权威侧（UCE 文库 ← DNA 提取台账）显示一键同步按钮
    {
      const ident = curCfg().ident_sync;
      const btn = $("btn-ident-sync");
      btn.hidden = !(ident && !ident.authority);
      if (!btn.hidden) {
        $("ident-sync-label").textContent = `以「${ident.peer_label}」为准同步`;
        btn.title = `把本表与「${ident.peer_label}」不一致的属/暂定名/最终名按权威值同步（差异徽标所在的行）`;
      }
    }
    $("btn-deleted").classList.toggle("primary", false);
    $("btn-collab-filtered").hidden = t !== "specimens";
    $("btn-mateex-filtered").hidden = t !== "specimens";
    loadTable();
  }
}

/* 勾选动作条（v3.78）：把明确记录集合交给目标模块（保留来源=勾选，不当作筛选全部） */
function ensureSelBar() {
  let bar = document.getElementById("sel-bar");
  if (!bar) {
    bar = document.createElement("div");
    bar.id = "sel-bar";
    bar.style.cssText = "display:none;align-items:center;gap:8px;flex-wrap:wrap;margin:0 0 8px;padding:6px 10px;border:1px solid var(--border);border-radius:8px;font-size:13px";
    const grid = document.getElementById("grid");
    grid.parentNode.insertBefore(bar, grid);
  }
  return bar;
}
function renderSelBar() {
  if (S.t === "home" || S.t === "history" || S.t === "checkup" || S.t === "stats" || S.t === "about"
      || S.t === "tplexp" || S.t === "collab" || S.t === "movements" || S.t === "labpage"
      || S.t === "biblio" || S.t === "projects" || String(S.t).startsWith("ct:")) { const b0 = document.getElementById("sel-bar"); if (b0) b0.style.display = "none"; return; }
  const bar = ensureSelBar();
  const n = S.sel.size;
  if (!n) { bar.style.display = "none"; bar.innerHTML = ""; return; }
  bar.style.display = "flex";
  bar.innerHTML = `<span>已勾选 <b>${n}</b> 行（明确记录集合，来源：表格勾选）：</span>
    <button class="btn" id="sb-project">加入项目</button>
    <button class="btn" id="sb-collab">创建协作包</button>
    <button class="btn" id="sb-tplexp">模板导出（勾选）</button>
    <button class="btn ghost" id="sb-clear">清除选择</button>`;
  document.getElementById("sb-clear").onclick = () => { S.sel.clear(); renderSelBar(); };
  document.getElementById("sb-project").onclick = async () => {
    try {
      const d = await api("/api/projects");
      const w = d.projects.filter((p) => p.kind === "fixed" || p.kind === "dynamic");
      openModal("加入项目（勾选的 " + n + " 行）",
        '<label>目标项目<select id="sp-pid" class="input">'
        + w.map((p) => `<option value="${p.id}">${esc(p.name)}（${esc(p.kind === "dynamic" ? "动态" : "固定")}，${p.member_count} 成员）</option>`).join("")
        + '</select></label><label>纳入原因<input id="sp-reason" class="input" value="纳入本次研究"></label>'
        + '<div class="form-actions"><button class="btn" id="sp-cancel">取消</button>'
        + '<button class="btn primary" id="sp-go">加入</button></div>'
        + '<p class="hint">逐条写入成员（稳定记录标识），失败行会单独提示，不影响其余。</p>');
      $("sp-cancel").onclick = closeModal;
      $("sp-go").onclick = async () => {
        const pid = +$("sp-pid").value;
        const reason = $("sp-reason").value.trim() || "纳入本次研究";
        $("sp-go").disabled = true;
        let ok = 0; const errs = [];
        for (const rid of S.sel) {
          try { await api(`/api/projects/${pid}/members`, { method: "POST",
            body: JSON.stringify({ rid, tname: S.t, join_reason: reason }) }); ok++; }
          catch (e) { errs.push("#" + rid + ": " + e.message); }
        }
        closeModal();
        toast(`已加入 ${ok} 行${errs.length ? `；失败 ${errs.length}（${errs[0]}）` : ""}`, ok ? "ok" : "err");
      };
    } catch (e) { toast(e.message, "err"); }
  };
  document.getElementById("sb-collab").onclick = () => openCollabForIds([...S.sel].map((rid) => {
    const row = S.rows.find((r) => r.id === rid);
    return { tname: S.t, rid, key: row ? String(row.data[curCfg().key] || "#" + rid) : "#" + rid };
  }));
  document.getElementById("sb-tplexp").onclick = () => openExportWizard({
    context: "table", tname: S.t,
    grid: { q: "", filters: {}, preset: "", deleted: S.showDeleted, selIds: [...S.sel], total: n },
  });
}

/* ---------------- 首页仪表盘 ---------------- */
const DONUT_COLORS = ["#3f6b99", "#6d94b8", "#92aec7", "#4e8a63", "#a8823c", "#a8575f", "#776b9e", "#98a0ab"];

function relTime(ts) {
  if (!ts) return "";
  const t = new Date(ts.replace(" ", "T"));
  const s = (Date.now() - t.getTime()) / 1000;
  if (isNaN(s)) return ts;
  if (s < 60) return "刚刚";
  if (s < 3600) return Math.floor(s / 60) + " 分钟前";
  if (s < 86400) return Math.floor(s / 3600) + " 小时前";
  if (s < 172800) return "昨天";
  return ts.slice(5, 10);
}

function animateNumber(el, to, suffix = "") {
  if (!el) return;
  const final = to.toLocaleString() + suffix;
  if (document.hidden) { el.textContent = final; return; }
  const from = parseInt((el.textContent || "0").replace(/[^\d]/g, "")) || 0;
  const t0 = performance.now(), dur = 800;
  let done = false;
  const finish = () => { if (!done) { done = true; el.textContent = final; } };
  setTimeout(finish, dur + 200);
  function step(t) {
    const p = Math.min(1, (t - t0) / dur);
    const e = 1 - Math.pow(1 - p, 3);
    el.textContent = Math.round(from + (to - from) * e).toLocaleString() + suffix;
    if (p < 1) requestAnimationFrame(step);
    else finish();
  }
  requestAnimationFrame(step);
}

function fmtNum(n) {
  if (n >= 10000) return (n / 10000).toFixed(1).replace(/\.0$/, "") + "w";
  if (n >= 1000) return (n / 1000).toFixed(1).replace(/\.0$/, "") + "k";
  return String(n);
}

function donutHTML(items, centerLabel) {
  const total = items.reduce((s, it) => s + it.value, 0);
  const R = 50, C = 2 * Math.PI * R;
  const GAP = items.length > 1 ? 1.6 : 0;  // 分段间隙（像素），视觉切分
  let start = 0;
  const segs = items.map((it, i) => {
    const frac = total ? it.value / total : 0;
    const len = Math.max(0.5, frac * C - GAP);
    const seg = `<circle cx="60" cy="60" r="${R}" fill="none" stroke="${DONUT_COLORS[i % DONUT_COLORS.length]}"
      stroke-width="17" stroke-dasharray="0 ${C.toFixed(1)}" stroke-dashoffset="${(-start).toFixed(1)}"
      data-len="${len.toFixed(1)}" data-c="${C.toFixed(1)}" data-full="${(it.value / total * 100).toFixed(1)}"><title>${esc(it.name)}：${it.value.toLocaleString()}（${(it.value / total * 100).toFixed(1)}%）</title></circle>`;
    start += frac * C;
    return seg;
  }).join("");
  const legend = items.map((it, i) => `
    <div class="lg-row"><span class="lg-dot" style="background:${DONUT_COLORS[i % DONUT_COLORS.length]}"></span>
      <span class="lg-name" title="${esc(it.name)}">${esc(it.name)}</span>
      <span class="lg-pct">${(it.value / total * 100).toFixed(1)}%</span>
      <span class="lg-val">${it.value.toLocaleString()}</span></div>`).join("");

  return `<div class="donut-wrap">
    <svg width="128" height="128" viewBox="0 0 120 120" class="donut-svg"><g transform="rotate(-90 60 60)">
      <circle cx="60" cy="60" r="${R}" fill="none" stroke="#eef1f5" stroke-width="17"></circle>${segs}</g>
      <text x="60" y="58" text-anchor="middle" font-size="18" font-weight="700" fill="#212836">${fmtNum(total)}</text>
      <text x="60" y="74" text-anchor="middle" font-size="9" fill="#6b7590">${esc(centerLabel)}</text></svg>
    <div class="donut-legend">${legend}</div></div>`;
}

function animateDonuts(root) {
  const circles = root.querySelectorAll("circle[data-len]");
  const finish = () => circles.forEach((c) => {
    c.setAttribute("stroke-dasharray", `${c.dataset.len} ${(+c.dataset.c - +c.dataset.len).toFixed(1)}`);
  });
  if (document.hidden) return finish();
  const t0 = performance.now(), dur = 900;
  setTimeout(finish, dur + 200);
  function step(t) {
    const p = Math.min(1, (t - t0) / dur);
    const e = 1 - Math.pow(1 - p, 3);
    circles.forEach((c) => {
      const len = +c.dataset.len, C = +c.dataset.c;
      c.setAttribute("stroke-dasharray", `${(len * e).toFixed(1)} ${(C - len * e).toFixed(1)}`);
    });
    if (p < 1) requestAnimationFrame(step);
    else finish();
  }
  requestAnimationFrame(step);
}

function growBars(root) {
  const apply = () => {
    root.querySelectorAll("[data-w]").forEach((el) => { el.style.width = el.dataset.w; });
    root.querySelectorAll("[data-h]").forEach((el) => { el.style.height = el.dataset.h; });
  };
  if (document.hidden) return apply();
  requestAnimationFrame(apply);
  setTimeout(apply, 120);  // 双保险：确保离开后台再进入时也能到位
}

function dashBarChart(items, unitLabel) {
  const max = Math.max(1, ...items.map((i) => i.value));
  return items.map((it, i) => `
    <div class="bar-row${i === 0 ? " top" : ""}">
      <span class="bar-label" title="${esc(it.name)}">${esc(it.name)}</span>
      <span class="bar-track"><span class="bar-fill" data-w="${(it.value / max * 100).toFixed(1)}%" style="width:0" title="${esc(it.name)}：${it.value.toLocaleString()}${unitLabel}"></span></span>
      <span class="bar-val">${it.value.toLocaleString()}</span>
    </div>`).join("");
}

function yearColumns(items) {
  if (!items.length) return `<p class="hint">暂无带年份的采集记录</p>`;
  const max = Math.max(1, ...items.map((i) => i.value));
  const peak = items.reduce((best, it, i) => it.value > items[best].value ? i : best, 0);
  const labelEvery = Math.max(1, Math.ceil(items.length / 14));
  return `<div class="col-chart">${items.map((it, i) => `
    <div class="colwrap${i === peak ? " peak" : ""}" data-v="${it.value}" title="${it.year} 年：${it.value} 号">
      <span class="colv">${it.value}</span>
      <span class="col" data-h="${(it.value / max * 100).toFixed(1)}%" style="height:0"></span>
      <span class="yr">${i % labelEvery === 0 ? it.year : ""}</span>
    </div>`).join("")}</div>`;
}

let _dashSig = null;
const SEX_LBL = { male: "♂ 雄性", female: "♀ 雌性", subadult: "亚成体", juvenile: "幼体" };
function mayBoxesHome() {
  return !!(S.me && (S.me.admin || (S.me.can_boxes && (!S.me.group || S.me.group === "spider"))));
}

let _wbCache = null, _wbCacheTs = 0;
async function renderDashActions() {
  const box = $("dash-actions");
  if (!box) return;
  const now = Date.now();
  if (!_wbCache || now - _wbCacheTs > 60000) {
    try { _wbCache = await api("/api/workbench"); _wbCacheTs = now; } catch { _wbCache = null; }
  }
  const w = _wbCache || { identify: { n: "?" }, no_event: { n: "?" }, dna_gap: { n: "?" }, cf_review: { n: "?" } };
  const loaned = (S.meta.tables.specimens || {}).loaned || 0;
  const item = (n, label, tip, go) =>
    `<button class="dash-action" data-go="${go}" title="${esc(tip)}">
      <b>${typeof n === "number" ? n.toLocaleString() : n}</b><span>${esc(label)}</span></button>`;
  box.innerHTML = [
    item(w.identify.n, "条待鉴定", "有暂定名、无最终名——去工作台逐条定名", "wb:identify"),
    item(w.cf_review.n, "条 cf. 待复查", "鉴定信心不足标记，复查后移出", "wb:cf_review"),
    item(w.no_event.n, "条缺采集信息", "无采集事件号——去工作台查看清单", "wb:no_event"),
    item(w.dna_gap.n, "条 DNA 凭证断链", "UCE 已建库但 DNA 提取台账无对应记录", "wb:dna_gap"),
    item(loaned, "笔借出中", "借出未归还清单（跳蛛表）", "loaned"),
  ].join("");
  box.querySelectorAll(".dash-action").forEach((b) => b.onclick = () => {
    const go = b.dataset.go;
    if (go === "loaned") {
      switchView("specimens");
      setTimeout(() => { $("btn-loaned").click(); }, 250);
    } else {
      _wbTab = go.split(":")[1];
      switchView("workbench");
    }
  });
  // 管理员健康卡
  // ---- 最近使用 / 继续处理（v3.78：按账号偏好，简洁列表；权限失效条目隐藏并静默修剪） ----
  renderHomeRecents();
  if (S.me.admin) {
    let health = "";
    try {
      const bk = await api("/api/backups");
      const latest = bk.backups[0];
      const si = S.meta.schema || {};
      health = `<div class="dash-card admin-health" style="margin:0 0 14px">
        <h3>系统健康（仅管理员可见）</h3>
        <div style="display:flex;gap:22px;flex-wrap:wrap;font-size:13px;align-items:center">
          <span>成员 <b>${"-"}</b></span>
          <span>最近备份 <b>${latest ? esc(latest.mtime) : "无"}</b>（保留 ${bk.backups.length}/${bk.keep} 份）</span>
          <span>库结构版本 <b>v${esc(si.version || "?")}</b></span>
          <button class="btn" id="btn-drill">运行恢复演练</button>
          <button class="btn" id="btn-bknow">立即备份</button>
          <span id="drill-out" class="hint"></span>
        </div>
      </div>`;
    } catch { }
    if (health) {
      const body = $("dash-body");
      if (body) {
        body.insertAdjacentHTML("afterbegin", health);
        const dr = document.getElementById("btn-drill");
        if (dr) dr.onclick = async () => {
          dr.disabled = true;
          const out = document.getElementById("drill-out");
          if (out) out.textContent = "演练中（恢复到临时副本并校验）…";
          try {
            const r = await api("/api/backups/drill", { method: "POST" });
            if (out) out.textContent = `✔ 演练通过：备份 ${r.backup} 完整性 ${r.integrity}，各表行数已核对`;
          } catch (e) { if (out) out.textContent = "✕ " + e.message; }
          dr.disabled = false;
        };
        const bk2 = document.getElementById("btn-bknow");
        if (bk2) bk2.onclick = async () => {
          bk2.disabled = true;
          const out = document.getElementById("drill-out");
          if (out) out.textContent = "备份中…";
          try {
            const r = await api("/api/backups/create", { method: "POST", body: JSON.stringify({ kind: "manual" }) });
            if (out) out.textContent = `✔ 备份完成：${r.name || r.backup || ""}`;
          } catch (e) { if (out) out.textContent = "✕ " + e.message; }
          bk2.disabled = false;
        };
      }
    }
  }
}

/* ---- 首页：最近使用 / 继续处理（v3.78）----
   数据=账号偏好（recents/pinned）+ 收藏模板 + 我的保存视图；渲染时按当前权限隐藏失效条目并静默修剪偏好。 */
async function renderHomeRecents() {
  const host = document.createElement("div");
  host.id = "home-recents";
  const body = $("dash-body");
  const healthEl = document.querySelector(".admin-health");
  if (healthEl) healthEl.after(host); else body.prepend(host);
  host.innerHTML = '<p class="hint">读取最近使用…</p>';
  let projects = [], tpls = [], pinnedIds = S.prefs.pinned || [];
  try { projects = (await api("/api/projects")).projects; } catch { }
  try { tpls = (await api("/api/export-templates")).templates; } catch { }
  const rec = S.prefs.recents || {};
  // 权限失效修剪：项目不存在/不可操作 → 从偏好移除（静默）
  const pIds = new Set(projects.map((x) => x.id));
  const stale = (rec.projects || []).filter((x) => !pIds.has(x.id));
  if (stale.length) {
    S.prefs.recents.projects = (rec.projects || []).filter((x) => pIds.has(x.id));
    savePrefs(["recents"]);
  }
  const pinned = (pinnedIds || []).map((id) => projects.find((x) => x.id === id)).filter(Boolean);
  const recentProjects = (rec.projects || []).map((id) => projects.find((x) => x.id === id)).filter(Boolean).slice(0, 3);
  const favTpls = tpls.filter((t) => t.favorite).slice(0, 3);
  const recentTpls = recentList("tplexp")
    .map((x) => tpls.find((t) => t.id === x.id)).filter(Boolean).slice(0, 3);
  const myViews = [];
  for (const [t, views] of Object.entries(S.prefs.views || {})) {
    if (!S.meta.tables[t]) continue;
    for (const v of views || []) myViews.push({ t, id: v.id, name: v.name || v.id });
  }
  const projBtns = (p, extra) => `
    <span style="display:inline-flex;gap:6px;align-items:center;border:1px solid var(--border);border-radius:8px;padding:4px 10px">
      <a href="javascript:void 0" class="hr-proj" data-id="${p.id}" style="text-decoration:underline"><b>${esc(p.name)}</b></a>
      ${extra || ""}
    </span>`;
  host.innerHTML = `
    <div class="dash-card" style="margin-bottom:14px"><h3>继续处理</h3>
      ${pinned.length || recentProjects.length ? `
        <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center">
          ${pinned.map((p) => projBtns(p, '<span class="st st-saved">已固定</span>')).join("")}
          ${recentProjects.filter((p) => !pinned.includes(p.id)).map((p) => projBtns(p, '<span class="hint">最近</span>')).join("")}
        </div>
        <p class="hint" style="margin-top:4px">点击项目名打开成员与详情；导出/协作在该弹窗内一键继续。</p>`
      : `<p class="hint">还没有最近使用的项目——在「项目工作集」打开一次后会出现在这里（可固定最多 5 个）。</p>`}
      ${favTpls.length ? `<div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:8px">
        <span class="hint">收藏模板：</span>
        ${favTpls.map((t) => `<span style="display:inline-flex;gap:6px;align-items:center;border:1px solid var(--border);border-radius:8px;padding:4px 10px">
          <a href="javascript:void 0" class="hr-tpl" data-id="${t.id}" style="text-decoration:underline"><b>${esc(t.name)}</b></a>
          <span class="hint">规范${t.rules_source === "official" ? "已核实" : "未核实"}</span></span>`).join("")}
      </div>` : ""}
      ${myViews.length ? `<div style="margin-top:8px"><span class="hint">我的保存视图：</span>
        ${myViews.slice(0, 6).map((v) => `<a href="javascript:void 0" class="hr-view" data-t="${esc(v.t)}" data-id="${esc(v.id)}" style="margin-right:10px;text-decoration:underline">${esc(tblName(S.meta.tables[v.t]) || v.t)} · ${esc(v.name)}</a>`).join("")}
      </div>` : ""}
    </div>`;
  const goProject = async (pid) => {
    switchView("projects");
    await new Promise((r) => setTimeout(r, 800));
    const b = [...document.querySelectorAll("[data-pj]")].find((x) => +x.dataset.pj === pid);
    if (b) b.click(); else toast("该项目不在当前列表", "err");
  };
  host.querySelectorAll(".hr-proj").forEach((a) => (a.onclick = () => goProject(+a.dataset.id)));
  host.querySelectorAll(".hr-tpl").forEach((a) => (a.onclick = () => {
    const t = tpls.find((x) => x.id === +a.dataset.id);
    if (t) openExportWizard({ context: "tplexp", tname: t.tname, templateId: t.id });
  }));
  host.querySelectorAll(".hr-view").forEach((a) => (a.onclick = async () => {
    const t = a.dataset.t;
    await switchView(t);
    await new Promise((r) => setTimeout(r, 600));
    const sel = document.getElementById("task-view");
    if (sel) { sel.value = a.dataset.id; sel.dispatchEvent(new Event("change", { bubbles: true })); }
  }));
}

/* 核心计量卡片：分别展示各指标并标注分母与排除条件（不做综合得分） */
async function loadMetrics() {
  try {
    const m = await api("/api/metrics");
    const host = document.getElementById("dash-metrics");
    if (!host) return;
    host.innerHTML = `<div class="ck-card" style="padding:12px 14px;margin-top:14px">
      <h3 style="font-size:13px">核心计量（分母与排除条件随项标注）</h3>
      <table class="about-table" style="width:100%;margin-top:6px;font-size:12.5px">
        <thead><tr><th>指标</th><th>值</th><th>分母 / 口径</th><th>排除条件</th></tr></thead>
        <tbody>${m.metrics.map((x) => `
          <tr><td>${esc(x.label)}</td><td><b>${x.value.toLocaleString()}</b></td>
          <td class="hint">${esc(x.denominator)}</td><td class="hint">${esc(x.exclusions)}</td></tr>`).join("")}
        </tbody></table>
      <p class="hint" style="margin:6px 0 0">统计时点 ${esc(m.generated_at)}；只统计您有权限的表。</p></div>`;
  } catch { /* 首页其他部分不受影响 */ }
}

async function loadDashboard(silent = false) {
  let d;
  try {
    d = await api("/api/dashboard");
  } catch (e) {
    if (!silent) toast(e.message, "err");
    return;
  }
  const changed = _dashSig !== null && _dashSig !== d.sig;
  _dashSig = d.sig;
  renderDashActions();
  // 无任何可见主表：空状态（不回退默认主表，不显示任何标本统计）
  if (d.primary === null || d.empty) {
    $("dash-total").textContent = "—";
    $("dash-sub-text").textContent = "当前账号没有可见的标本数据表";
    $("dash-chips").innerHTML = "";
    $("dash-body").innerHTML = `
      <div class="ck-card" style="text-align:center;padding:38px 20px">
        <p style="font-size:15px;margin-bottom:6px">暂无可展示的数据</p>
        <p class="hint" style="margin:0">您的账号当前没有任何可见的标本主表（可见表：${(d.tables || []).map((t) => esc(t.label)).join("、") || "无"}）。
        如需访问标本数据，请联系管理员在「成员与邀请码」中调整逐表权限。</p>
      </div>`;
    return;
  }
  animateNumber($("dash-total"), d.total);
  const le = d.last_edit;
  $("dash-sub-text").textContent =
    `每 10 秒自动同步 · 最近修改：${le ? `${le.user}（${relTime(le.ts)}）` : "暂无"}`;
  const ptCfg = S.meta.tables[userPrimaryT()] || {};
  const chip = (go, label, title) =>
    `<span class="dt-chip${go ? " clickable" : ""}"${go ? ` data-go="${go}" style="cursor:pointer" title="${esc(title)}"` : ""}>${label}</span>`;
  $("dash-chips").innerHTML =
    chip("events", `采集事件 ${d.events_total.toLocaleString()}`, "查看采集地信息") +
    chip(S.meta.tables.taxonomy ? "taxonomy" : "", `${d.families} 科 · ${d.genera} 属`, "查看分类信息") +
    chip(ptCfg.type_field ? "types" : "", `模式标本 ${d.type_total.toLocaleString()}`, "查看模式标本（Holotype/Paratype 等）") +
    chip(ptCfg.loan_view ? "loaned" : "", `借出中 ${d.loaned}`, "查看借出中的标本");
  const palette = [["#5f7fe8", "#33587e"], ["#4e8a63", "#33684a"], ["#a8823c", "#7a6330"],
                   ["#776b9e", "#5a4f85"], ["#a8575f", "#8f444c"], ["#38b6c9", "#2a8a99"]];
  const kpi = (d.tables || []).map((t, i) => ({
    n: t.total, l: t.label, go: t.t, c: palette[i % palette.length],
    f: i === 0 ? `回收站 ${d.deleted} · 版本快照 ${d.versions}` : "",
  }));
  const h = d.health;
  kpi.push({ n: h.total, l: "待修复数据问题",
    f: `重复编号 ${h.dup_specimens + h.dup_events} · 悬空引用 ${h.missing_events}`,
    go: "checkup", c: ["#a8575f", "#8f444c"], warn: h.total > 0 });
  $("dash-body").innerHTML = `
    <div class="kpi-grid">${kpi.map((k) => `
      <div class="kpi-card ${k.warn ? "warn" : ""}" data-go="${k.go}" style="--k1:${k.c[0]};--k2:${k.c[1]}">
        <div class="kpi-num" data-to="${k.n}">0</div>
        <div class="kpi-label">${esc(k.l)}</div>
        <div class="kpi-foot">${esc(k.f)}</div>
      </div>`).join("")}
    </div>
    <div class="dash-grid">
      <div class="dash-card"><h3>标本数 · 按科 Top10</h3>${dashBarChart(d.top_families, " 号")}</div>
      <div class="dash-card" id="map-card">
        <h3>标本分布地图 <span class="hint">颜色越深采集越多 · 点击省份查看标本</span></h3>
        <div class="map-flex">
          <div class="map-box" id="cn-map"></div>
          <div class="map-side">
            <div class="map-legend"><span class="lg-min">少</span><span class="lg-bar"></span><span class="lg-max">多</span></div>
            ${dashBarChart(d.top_provinces, " 号")}
          </div>
        </div>
      </div>
      <div class="dash-card" style="grid-column:1/-1"><h3>采集年份趋势 <span class="hint">按采集事件的 YYYY1 关联统计</span></h3>${yearColumns(d.years)}</div>
      <div class="dash-card"><h3>模式标本类型</h3>${donutHTML(d.types, "模式标本")}</div>
      <div class="dash-card"><h3>保存液浓度分布</h3>${donutHTML(d.preserve.map((p) => ({ ...p, name: /^\d/.test(String(p.name)) ? p.name + "%" : p.name })), "号标本")}</div>
      ${d.coverage ? `<div class="dash-card" style="grid-column:1/-1"><h3>🌐 物种覆盖度（数据源 WSC）<span class="hint">馆藏跳蛛物种 = 已鉴定种 + 暂定名种（去重，排除仅到属/唯 sp. 的记录）</span></h3>
        <div class="cov-grid" style="grid-template-columns:repeat(4,1fr)">
          <div class="cov-item">
            <div class="cov-num">${d.coverage.our_spc_total}</div>
            <div class="cov-lbl">馆藏跳蛛物种</div>
            <div class="cov-sub">已鉴定 ${d.coverage.our_spc_final} · 暂定 ${d.coverage.our_spc_tmp}</div>
          </div>
          <div class="cov-item">
            <div class="cov-num">${d.coverage.our_spc_wsc}<span class="cov-unit">/${d.coverage.wsc_salt_spc}</span></div>
            <div class="cov-lbl">WSC 已知种覆盖</div>
            <div class="cov-bar"><div class="cov-fill" style="width:${d.coverage.our_spc_pct}%"></div></div>
            <div class="cov-pct">${d.coverage.our_spc_pct}%</div>
          </div>
          <div class="cov-item">
            <div class="cov-num">${d.coverage.our_spc_new}</div>
            <div class="cov-lbl">新种 / WSC 未收录</div>
            <div class="cov-sub">含待发表新种，不计入覆盖度分母</div>
          </div>
          <div class="cov-item">
            <div class="cov-num">${d.coverage.uce_salt_spc}<span class="cov-unit">（${d.coverage.uce_salt_spc_wsc} 已收录）</span></div>
            <div class="cov-lbl">UCE 建库跳蛛物种</div>
            <div class="cov-bar"><div class="cov-fill" style="width:${d.coverage.uce_salt_spc_pct}%"></div></div>
            <div class="cov-pct">${d.coverage.uce_salt_spc_pct}%</div>
          </div>
        </div>
        <p class="hint" style="margin:8px 0 0">另有跳蛛属覆盖 ${d.coverage.our_salt_gen}/${d.coverage.wsc_salt_gen}（${d.coverage.our_salt_gen_pct}%）。暂定名与已鉴定名同为一种时只计一次；纯 sp./cf. 记录不计入物种。</p>
      </div>` : ""}
      <div class="dash-card"><h3>性别与虫态</h3>
        <div class="sex-grid">
          ${(d.sex_stats || []).map((s) => `
          <div class="sex-item sx-${s.key}"><span class="sx-chip">${{ male: "♂", female: "♀", subadult: "▲", juvenile: "•" }[s.key] || "·"}</span>
            <div class="sx-main"><span class="sex-num">${(s.value ?? 0).toLocaleString()}</span><span class="sex-lbl">${SEX_LBL[s.key] || s.key}</span></div></div>`).join("")}
        </div>
      </div>
      ${d.box_stats?.length ? (() => {
        const mayBoxes = S.me.admin || (S.me.can_boxes && (!S.me.group || S.me.group === "spider"));
        return `<div class="dash-card" style="grid-column:1/-1"><h3>标本盒占用 <span class="hint">共 ${d.box_stats.length} 盒 · 淡黄=已用满 · 浅绿=未满</span></h3>
        <div class="box-grid">${d.box_stats.map((b) => `
          <div class="box-item ${b.used >= b.total ? "box-full" : "box-free"}" title="${esc(b.name)}：已用 ${b.used}/${b.total}">
            <span class="box-name">${esc(b.name.replace(/\s*\(.*\)\s*/, ""))}</span>
            <span class="box-range">${(b.start ?? "").toLocaleString()} – ${(b.end ?? "").toLocaleString()}</span>
            <div class="bar-track" style="height:5px"><div class="bar-fill" style="width:${(b.used / b.total * 100).toFixed(0)}%"></div></div>
            <span class="box-cnt">${b.used}/${b.total}</span>
          </div>`).join("")}
        ${mayBoxes ? `<div class="box-item box-add" id="box-add-btn" title="添加标本盒"><span class="box-plus">＋</span><span class="box-addlbl">添加</span></div>` : ""}
        </div></div>`;
      })() : mayBoxesHome() ? `<div class="dash-card" style="grid-column:1/-1"><h3>标本盒占用</h3><p class="hint">尚未登记标本盒。<button class="btn" id="box-add-btn0">前往添加</button></p></div>` : ""}
      ${d.proj_dist?.length ? `<div class="dash-card"><h3>项目分布 Top8</h3>${dashBarChart(d.proj_dist, " 号")}</div>` : ""}
      ${d.uce_stats ? `<div class="dash-card" style="grid-column:1/-1"><h3>UCE 文库统计</h3>
        <div class="uce-body">
          <div class="uce-left">
            <div class="uce-kpis">
              <div class="uce-kpi"><span class="kpi-num" style="font-size:22px">${d.uce_stats.total.toLocaleString()}</span><span class="kpi-label">条建库记录</span></div>
              <div class="uce-kpi"><span class="kpi-num" style="font-size:22px">${d.uce_stats.species.toLocaleString()}</span><span class="kpi-label">物种数</span></div>
              <div class="uce-kpi"><span class="kpi-num" style="font-size:22px">${d.uce_stats.families?.length ?? 0}</span><span class="kpi-label">涉及科数</span></div>
            </div>
            ${d.uce_stats.years?.length ? `
            <div class="uce-sub">按送出年份</div>
            <div class="col-chart uce-years">${d.uce_stats.years.map((it, i) => `
              <div class="colwrap" data-v="${it.value}" title="${it.year} 年：${it.value} 条">
                <span class="colv">${it.value}</span>
                <span class="col" data-h="${(it.value / Math.max(1, ...d.uce_stats.years.map(x => x.value)) * 100).toFixed(1)}%" style="height:0"></span>
                <span class="yr">${it.year}</span>
              </div>`).join("")}</div>` : `<p class="hint">暂无带年份的送测记录</p>`}
          </div>
          <div class="uce-main">
            <div class="uce-sub">按科 Top8</div>
            ${dashBarChart(d.uce_stats.families, " 条")}
          </div>
        </div>
      </div>` : ""}
      <div class="dash-card" style="grid-column:1/-1">
        <h3><span class="live-dot"></span> 实时动态 <span class="hint">who · what · when（每 10 秒刷新）</span></h3>
        ${d.feed.map((f, i) => `
          <div class="feed-item ${changed && i === 0 ? "flash" : ""}">
            <span class="badge ${esc(f.action)}">${ACTION_TXT[f.action] || esc(f.action)}</span>
            <span class="f-text"><b>${esc(f.user)}</b>${f.rkey ? ` <span class="h-key">#${esc(f.rkey)}</span>` : ""}${f.text ? ` · ${esc(f.text)}` : ""}</span>
            <span class="f-time">${relTime(f.ts)}</span>
          </div>`).join("") || `<p class="hint">还没有操作记录。</p>`}
      </div>
    </div>`;
  $("dash-body").querySelectorAll(".kpi-card").forEach((card) => {
    card.onclick = () => switchView(card.dataset.go);
  });
  // 仅动画带 data-to 的 KPI（UCE 卡等静态 kpi-num 无 data-to，不可覆盖）
  $("dash-body").querySelectorAll(".kpi-num[data-to]").forEach((el) => animateNumber(el, +el.dataset.to));
  growBars($("dash-body"));
  animateDonuts($("dash-body"));
  renderChinaMap(d.provinces_all || {});
}

/* ---- 中国地图（SVG 填色图，数据源 static/china.json 省级边界） ---- */
let _chinaGeo = null;
function chinaColor(v, max) {
  if (!v) return "#f0f2f5";
  const t = Math.sqrt(v / (max || 1));
  const steps = ["#dbe6f1", "#b9cfe4", "#8fb3d6", "#5f8cc0", "#3f6b99"];
  return steps[Math.min(steps.length - 1, Math.floor(t * steps.length))];
}

async function renderChinaMap(provCount) {
  const box = document.getElementById("cn-map");
  if (!box) return;
  try {
    if (!_chinaGeo) {
      const r = await fetch("/static/china.json");
      _chinaGeo = await r.json();
    }
  } catch { box.innerHTML = '<p class="hint">地图数据加载失败</p>'; return; }
  const K = 100, midCos = Math.cos(36 * Math.PI / 180);
  const proj = (lng, lat) => [lng * K * midCos, -lat * K];
  const named = _chinaGeo.features.filter(f => f.properties.name);
  const inset = _chinaGeo.features.filter(f => !f.properties.name);
  let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
  const eachPoint = (geom, cb) => {
    const polys = geom.type === "Polygon" ? [geom.coordinates] : (geom.coordinates || []);
    for (const poly of polys) for (const ring of poly) for (const pt of ring) cb(pt);
  };
  for (const f of named) eachPoint(f.geometry, ([lng, lat]) => {
    const [x, y] = proj(lng, lat);
    if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y;
  });
  const W = 520, H = 400, pad = 12;
  const s = Math.min((W - pad * 2) / (x1 - x0), (H - pad * 2) / (y1 - y0));
  const ox = (W - (x1 - x0) * s) / 2, oy = (H - (y1 - y0) * s) / 2;
  const px = (lng, lat) => { const [x, y] = proj(lng, lat); return [(x - x0) * s + ox, (y - y0) * s + oy]; };
  const pathOf = (geom) => {
    const polys = geom.type === "Polygon" ? [geom.coordinates] : (geom.coordinates || []);
    return polys.map(poly => poly.map(ring =>
      "M" + ring.map(pt => px(pt[0], pt[1]).map(n => n.toFixed(1)).join(",")).join("L") + "Z").join("")).join("");
  };
  const max = Math.max(1, ...Object.values(provCount));
  const paths = named.map((f) => {
    const name = f.properties.name;
    const v = provCount[name] || 0;
    return `<path d="${pathOf(f.geometry)}" fill="${chinaColor(v, max)}" stroke="#fff" stroke-width="0.8"
      class="cn-prov" data-prov="${esc(name)}" tabindex="0"><title>${esc(name)}：${v.toLocaleString()} 号标本</title></path>`;
  }).join("");
  let insetSvg = "";
  if (inset.length) {
    let ix0 = 1e9, iy0 = 1e9, ix1 = -1e9, iy1 = -1e9;
    for (const f of inset) eachPoint(f.geometry, ([lng, lat]) => {
      const [x, y] = proj(lng, lat);
      if (x < ix0) ix0 = x; if (x > ix1) ix1 = x; if (y < iy0) iy0 = y; if (y > iy1) iy1 = y;
    });
    const bw = 92, bh = 118, bx = W - bw - 8, by = H - bh - 8;
    const isc = Math.min((bw - 12) / (ix1 - ix0), (bh - 12) / (iy1 - iy0));
    const ipath = inset.map((f) => {
      const polys = f.geometry.type === "Polygon" ? [f.geometry.coordinates] : (f.geometry.coordinates || []);
      return polys.map(poly => poly.map(ring =>
        "M" + ring.map(pt => {
          const [x, y] = proj(pt[0], pt[1]);
          return [bx + 6 + (x - ix0) * isc, by + 6 + (y - iy0) * isc].map(n => n.toFixed(1)).join(",");
        }).join("L") + "Z").join("")).join("");
    }).join("");
    insetSvg = `<g><rect x="${bx}" y="${by}" width="${bw}" height="${bh}" fill="#fff" stroke="#cfd5dd"/>${ipath
      ? `<path d="${ipath}" fill="none" stroke="#3f6b99" stroke-width="1"/>` : ""}</g>`;
  }
  box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;display:block">${paths}${insetSvg}</svg>`;
  box.querySelectorAll(".cn-prov").forEach((p) => {
    p.addEventListener("click", async () => {
      // 先切视图并等 closeGate/渲染完成，再设置筛选（switchView 内部会清空 filters）
      await switchView("specimens");
      S.filters = { Province: { mode: "in", values: [p.dataset.prov] } };
      S.q = ""; S.page = 1;
      document.getElementById("search").value = "";
      await loadTable();
      toast(`已筛选省份：${p.dataset.prov}`, "ok");
    });
  });
}

let _dashTimer = null;
function startDashTicker() {
  if (_dashTimer) return;
  _dashTimer = setInterval(() => {
    if (S.t === "home" && !$("view-home").hidden && document.visibilityState === "visible")
      loadDashboard(true);
  loadMetrics();
  }, 10000);
}

/* ---------------- 数据表 ---------------- */
function curCfg() { return S.meta.tables[S.t]; }

/* 本表的属名/暂定名/最终名列名：标本表存 FinalSpeciesName/TemporarySpeciesName，
   DNA 提取台账与 UCE 文库存 Final Name/Temporary Name/Species name。属名与种名联想、
   cf. 按钮、名录核对一律按本表列名挂载（服务端同一份 taxon_cols 随 /api/meta 下发）。 */
const taxCols = (cfg) => {
  const c = cfg || curCfg() || {};
  return c.taxon_cols || { genus: "Genus", temp: "TemporarySpeciesName", final: "FinalSpeciesName" };
};
const isFinalNameCol = (name, cfg) => name === taxCols(cfg).final;
const isTempNameCol = (name, cfg) => name === taxCols(cfg).temp;

let _loadSeq = 0;  // 请求序号：防止旧请求晚到覆盖新结果（竞态）
let _loadbarEl = null;
function showLoadbar() {
  if (_loadbarEl) return;
  _loadbarEl = document.createElement("div");
  _loadbarEl.className = "loadbar";
  document.body.appendChild(_loadbarEl);
}
function hideLoadbar() {
  if (_loadbarEl) { _loadbarEl.remove(); _loadbarEl = null; }
}

let _loadAbort = null;
async function loadTable() {
  showLoadbar();
  setTimeout(hideLoadbar, 8000);  // 兜底：异常时进度条不残留
  const seq = ++_loadSeq;
  if (_loadAbort) { try { _loadAbort.abort(); } catch { } }   // 取消上一笔未完成查询（慢请求不覆盖新响应）
  _loadAbort = typeof AbortController !== "undefined" ? new AbortController() : null;
  const signal = _loadAbort ? _loadAbort.signal : undefined;
  $("table-stat").textContent = "加载中…";
  const p = new URLSearchParams({ page: S.page, per: S.per, q: S.q, sort: S.sort, dir: S.dir, natural: "1" });
  if (S.showDeleted) p.set("deleted", "1");
  if (S.preset) p.set("preset", S.preset);
  if (Object.keys(S.filters).length) p.set("filters", JSON.stringify(S.filters));
  let r;
  try {
    r = await api(`/api/rows/${S.t}?` + p, { signal });
  } catch (err) {
    if (seq !== _loadSeq || (err && (err.name === "AbortError" || String(err.message).includes("abort")))) return;
    hideLoadbar();
    // 查询失败 ≠ 空表：独立呈现并可重试
    if (!(String(err.message).includes("403") || String(err.message).includes("权限"))) {
      const ge = $("grid-empty");
      if (ge) {
        ge.hidden = false;
        ge.innerHTML = `<b>查询失败</b>（${esc(err.message || "网络/服务器异常")}）。数据没有变化，不是空表。
          <button class="btn primary" id="ge-retry" style="margin-left:8px">重试</button>`;
        const gr = document.getElementById("ge-retry");
        if (gr) gr.onclick = () => loadTable();
      }
      $("table-stat").textContent = "查询失败";
      return;
    }
    if (String(err.message).includes("403") || String(err.message).includes("权限")) {
      // 无权限：与「无数据/无匹配」分开呈现
      $("grid-body").innerHTML = "";
      const np = $("grid-noperm");
      if (np) np.hidden = false;
      const ge = $("grid-empty");
      if (ge) ge.hidden = true;
      $("table-stat").textContent = "无权限";
      return;
    }
    throw err;
  }
  const np0 = $("grid-noperm");
  if (np0) np0.hidden = true;
  if (seq !== _loadSeq) return;  // 已发起更新的查询，丢弃过期响应
  hideLoadbar();
  S.rows = r.rows; S.total = r.total; S.columns = r.columns;
  const dcnt = $("dna-diff-count");
  if (dcnt) dcnt.textContent = r.dna_diff_total || "";
  S.queryDigest = r.query_digest || null;      // 统一查询状态模型：查询指纹
  if (r.scope) { const sb = $("search"); if (sb) sb.title = "搜索范围：" + r.scope; }
  renderGrid();
  renderPager();
  if (typeof renderSelBar === "function") renderSelBar();
  renderFilterChips();
  const cfg = curCfg();
  $("ver-count").textContent = cfg.versions || "";
  $("del-count").textContent = cfg.deleted || "";
  $("hide-count").textContent = getHidden().size || "";
  let stat = `共 ${r.total.toLocaleString()} 行（${tblName(cfg)}${S.showDeleted ? " · 回收站" : S.preset === "loaned" ? " · 借出中" : ""}）`;
  if (Object.keys(S.filters).length) stat = `筛出 ${r.total.toLocaleString()} 行 / 全表 ${cfg.total.toLocaleString()} 行`;
  $("table-stat").textContent = stat;
  if (S.columns.some((c) => c.cascade) && !S.cascade[S.t]) {
    api(`/api/cascade/${S.t}`).then((c) => { S.cascade[S.t] = c; }).catch(() => { });
  }
  refreshUndo();
}

/* 首页标本盒"添加"块 → 打开标本盒管理（含添加表单） */
document.addEventListener("click", (e) => {
  if (e.target.closest("#box-add-btn") || e.target.closest("#box-add-btn0")) {
    if (mayBoxesHome()) openBoxes();
  }
});

/* 首页"借出中超期"chip → 跳标本表借出视图 */
$("dash-chips").addEventListener("click", (e) => {
  const chipEl = e.target.closest("[data-go]");
  if (!chipEl) return;
  const go = chipEl.dataset.go;
  if (go === "loaned") {
    switchView("specimens");
    setTimeout(() => { $("btn-loaned").click(); }, 120);
  } else if (go === "types") {
    switchView("specimens");
    setTimeout(() => { S.preset = "types"; S.page = 1; loadTable(); renderFilterChips(); }, 120);
  } else {
    switchView(go);
  }
});

function visibleCols() {
  const hid = getHidden();
  const order = (S.prefs.colorder || {})[S.t];
  let cols = S.columns.filter((c) => !hid.has(c.name));
  if (order && order.length) {
    const pos = new Map(order.map((n, i) => [n, i]));
    cols.sort((a, b) => (pos.has(a.name) ? pos.get(a.name) : 9999)
      - (pos.has(b.name) ? pos.get(b.name) : 9999));
  }
  return cols;
}
function frozenCols() {
  // 固定列（横向滚动仍可见的身份列）：主键默认固定，可用视图/列显示调整
  const fz = (S.prefs.freeze || {})[S.t];
  if (fz && fz.length) return fz;
  const key = curCfg().key;
  return key ? [key] : [];
}
/* 偏好保存队列：防抖 + 失败重试 + 乱序丢弃（基于服务器回执的 prefs_version）；
   局部合并协议：只传本次变化的顶层键（未提供字段服务器保持原值，不再丢 views/colorder 等） */
let _prefsSaveTimer = null, _prefsInFlight = null, _prefsDirty = {};
function prefsMergeAndSend(keys, deletes = []) {
  const body = {};
  for (const k of keys) body[k] = S.prefs[k];
  if (deletes.length) body.__delete__ = deletes;   // 表级浅合并协议：删除必须显式表达
  return api("/api/prefs", { method: "PUT", body: JSON.stringify(body) }).then((r) => {
    // 回执基于服务器实际保存内容；同步版本号防乱序
    if (r && r.prefs) S.prefs.prefs_version = r.prefs.prefs_version;
  });
}
let _prefsDeletesPending = [];   // 待发送的显式删除（失败重试时不丢失）
async function savePrefs(extraKeys = [], deletes = []) {
  if (deletes.length) _prefsDeletesPending = [...new Set([..._prefsDeletesPending, ...deletes])];
  for (const k of extraKeys) _prefsDirty[k] = true;
  ["hidden", "colw", "views", "colorder", "freeze", "view_sel", "density", "recents", "pinned"].forEach(
    (k) => { if (S.prefs[k] !== undefined || extraKeys.includes(k)) _prefsDirty[k] = true; });
  clearTimeout(_prefsSaveTimer);
  return new Promise((resolve) => {
    _prefsSaveTimer = setTimeout(async () => {
      const keys = Object.keys(_prefsDirty);
      _prefsDirty = {};
      const dels = _prefsDeletesPending;
      _prefsDeletesPending = [];
      try {
        if (_prefsInFlight) await _prefsInFlight;   // 乱序保护：等待上一笔
        _prefsInFlight = prefsMergeAndSend(keys, dels).catch(() => { });
        await _prefsInFlight;
        _prefsInFlight = null;
        resolve(true);
      } catch {
        for (const k of keys) _prefsDirty[k] = true;  // 失败保留本地待保存状态
        _prefsDeletesPending = [...new Set([..._prefsDeletesPending, ...dels])];
        toast("偏好保存失败（本地已保留，稍后自动重试）", "warn");
        _prefsSaveTimer = setTimeout(() => savePrefs(), 5000);  // 轻量自动重试
        resolve(false);
      }
    }, 500);
  });
}

/* ---- 标本盒：可用盒下拉 ---- */
async function loadBoxes(force) {
  if (S.boxes && !force) return S.boxes;
  S.boxes = await api("/api/boxes").catch(() => ({ boxes: [] }));
  return S.boxes;
}
function availableBoxOptions(currentVal) {
  const boxes = (S.boxes?.boxes) || [];
  const opts = [`<option value="">（空）</option>`];
  for (const b of boxes) {
    if (b.full && b.name !== currentVal) continue;  // 用满的盒不可选
    const label = `${b.name}（${b.used}/${b.total}${b.full ? " 已满" : "，余 " + b.free}）`;
    opts.push(`<option value="${esc(b.name)}" ${b.name === currentVal ? "selected" : ""}>${esc(label)}</option>`);
  }
  if (currentVal && !boxes.some((b) => b.name === currentVal))
    opts.push(`<option value="${esc(currentVal)}" selected>${esc(currentVal)}（未登记）</option>`);
  return opts.join("");
}

/* 标本编号：固定前缀 + 8 位数字。用户只填数字部分，系统自动补前缀和前导零。
   已带前缀的输入原样规范化（大小写、空格）。 */
const KEY_PREFIX = "MHBU-ARA-";
const KYUARA_PREFIX = "KYUARA#";

/* 原蛛编号归一：用户只填数字，自动补 KYUARA# 前缀并补齐 4 位 */
function normalizeKyuara(input) {
  let s = String(input ?? "").trim();
  if (!s) return s;
  const up = s.toUpperCase().replace(/\s+/g, "");
  const m = up.match(/^KYUARA[#\-]?(.+)$/);
  if (m) {
    const rest = m.group(1);
    return "KYUARA#" + (rest.length < 4 ? rest.padStart(4, "0") : rest);
  }
  if (/^\d+$/.test(up)) return "KYUARA#" + up.padStart(4, "0");
  return s;
}

function normalizeSpecKey(input) {
  let s = String(input ?? "").trim();
  if (!s) return s;
  // 统一大写、去内部空格
  const up = s.toUpperCase().replace(/\s+/g, "");
  // 已带前缀（任意大小写/空格变体）
  const m = up.match(/^MHBU[- ]?ARA[- ]?(.+)$/);
  if (m) {
    const rest = m[1];
    if (/^\d+$/.test(rest)) return KEY_PREFIX + rest.padStart(8, "0");
    return KEY_PREFIX + rest;  // 非纯数字尾巴保留
  }
  if (/^\d+$/.test(up)) return KEY_PREFIX + up.padStart(8, "0");
  return s;  // 其他格式（如 HBUARA#2024-32）原样
}

function displayVal(colName, v) {
  if (v === null || v === undefined || v === "") return v;
  const isAlcohol = colName.startsWith("PreserveCondition") || colName === "保存条件";
  if (isAlcohol) {
    const n = Number(v);
    if (!isNaN(n)) {
      const pct = n > 0 && n <= 1.0001 ? Math.round(n * 100) : n;  // 小数比例（0.95）→ 百分数
      return pct + "%";
    }
    return v;  // 非数字（如"95%（死后泡）"）原样保留
  }
  return v;
}

function renderGrid() {
  const cols = visibleCols();
  const key = curCfg().key;
  const FUNNEL = '<svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor"><path d="M3 5h18l-7 8.5V19l-4 2v-7.5L3 5z"/></svg>';
  const colw = S.prefs.colw && S.prefs.colw[S.t] ? S.prefs.colw[S.t] : {};
  const frozen = frozenCols();
  const fzW = (name) => colw[name] ? Math.max(84, Math.min(420, colw[name])) : 150;
  const fzLeft = [];
  let fzAcc = 68;  // 勾选列 34 + 行号列 34
  for (const fname of frozen) {
    fzLeft.push(fzAcc);
    fzAcc += fzW(fname);
  }
  const pageSel = S.rows.filter((r) => S.sel.has(r.id)).length;
  const head = [`<th class="rowck" title="勾选多行后可批量修改"><input type="checkbox" id="ck-all" ${S.rows.length && pageSel === S.rows.length ? "checked" : ""}></th>`,
    `<th class="rownum">#</th>`];
  for (const c of cols) {
    const arrow = S.sort === c.name ? `<span class="arrow">${S.dir === "asc" ? "▲" : "▼"}</span>` : "";
    const fActive = S.filters[c.name] ? " active" : "";
    // 存量自定义列宽不小于 84px：避免表头/编号被裁切（如 Specimen # 显示成 pecimen #）
    const fzIdx = frozen.indexOf(c.name);
    const wVal = fzIdx >= 0 ? fzW(c.name)
      : (colw[c.name] ? Math.max(84, colw[c.name]) : null);
    let wst = wVal ? `width:${wVal}px;min-width:${wVal}px;max-width:${wVal}px;` : "";
    if (fzIdx >= 0) wst += `position:sticky;left:${fzLeft[fzIdx]}px;z-index:12;`;
    const srcIco = c.linked ? `<span class="th-src" title="关联列：实时取自${esc(tblName(S.meta.tables[curCfg().linked_source]) || "来源表")}">${icon("linked")}</span>` : "";
    const draggable = ' draggable="true"';
    head.push(`<th data-col="${esc(c.name)}" class="${c.wide ? "wide" : ""}${c.numeric ? " num-h" : ""}${fzIdx >= 0 ? " frozen" : ""}"${wst ? ` style="${wst}"` : ""} title="${esc(detTitle(c.name))}"${draggable}><span class="th-label">${esc(c.label || c.name)}</span>${srcIco}${arrow}<span class="th-btns"><span class="th-filter${fActive}" data-fcol="${esc(c.name)}" title="筛选此列">${FUNNEL}</span><span class="th-hide" data-hidecol="${esc(c.name)}" title="隐藏此列（可在“列显示”里找回）">×</span></span><span class="th-grip" data-grip="${esc(c.name)}" title="拖拽右缘调列宽；拖拽表头调列序，双击列宽恢复默认"></span></th>`);
  }
  head.push(`<th class="rowact">操作</th>`);
  $("grid-head").innerHTML = "<tr>" + head.join("") + "</tr>";
  wireColResize();
  // 列序拖拽（表头拖到另一列上完成重排；键列与固定列也可移动，但保持在前排会自动纠正）
  const ths = $("grid-head").querySelectorAll("th[data-col]");
  ths.forEach((th) => {
    th.addEventListener("dragstart", (e) => { e.dataTransfer.setData("text/plain", th.dataset.col); });
    th.addEventListener("dragover", (e) => { e.preventDefault(); });
    th.addEventListener("drop", async (e) => {
      e.preventDefault();
      const from = e.dataTransfer.getData("text/plain");
      const to = th.dataset.col;
      if (!from || from === to) return;
      const names = visibleCols().map((c) => c.name);
      const fi = names.indexOf(from), ti = names.indexOf(to);
      if (fi < 0 || ti < 0) return;
      names.splice(ti, 0, names.splice(fi, 1)[0]);
      S.prefs.colorder = S.prefs.colorder || {};
      S.prefs.colorder[S.t] = names;
      S.prefs.view_sel = S.prefs.view_sel || {};
      delete S.prefs.view_sel[S.t];   // 自定义列序后不再标记为预设视图
      await savePrefs();
      renderGrid();
    });
  });
  // 身份列隐藏警告：主键列/固定列隐藏前必须确认
  $("grid-head").querySelectorAll(".th-hide").forEach((x) => {
    x.addEventListener("click", async (e) => {
      const col = x.dataset.hidecol;
      const isIdentity = col === curCfg().key || frozenCols().includes(col);
      if (!isIdentity) return;  // 普通列交给既有委托处理
      {
        const ok = await confirmBox(
          `「${esc(col)}」是行的身份列（固定列）。隐藏后横向滚动时将难以辨认每一行是哪条标本。\n确定隐藏吗？可随时在「管理 → 列显示」或任务视图里恢复。`,
          { okText: "仍要隐藏" });
        e.stopPropagation(); e.preventDefault();
        if (!ok) return;
        hideColumn(col);
      }
    }, true);
  });

  const body = [];
  const start = (S.page - 1) * S.per;
    S.rows.forEach((row, i) => {
    const typeStar = curCfg().type_field && row._type
      ? `<span class="type-star" title="模式标本：${esc(row._type)}">★</span>` : "";
    const tds = [`<td class="rowck"><input type="checkbox" data-ckrid="${row.id}" ${S.sel.has(row.id) ? "checked" : ""}></td>`,
      `<td class="rownum">${start + i + 1}${typeStar}</td>`];
    let fzAcc2 = 68;
    const fzLeftMap = {};
    for (const fname of frozen) { fzLeftMap[fname] = fzAcc2; fzAcc2 += fzW(fname); }
    for (const c of cols) {
      const v = displayVal(c.name, row.data[c.name]);
      const cls = ["cell"];
      if (c.linked) cls.push("linked");
      if (frozen.includes(c.name)) { cls.push("frozen"); cls.push("fz"); }
      if (c.numeric) cls.push("num");
      const lkey = curCfg().linked_key;
      if (c.name === key) cls.push("key-cell");
      if (row.miss && (c.name === key || (lkey && c.name === lkey))) cls.push("miss");
      if (isTempNameCol(c.name) && isCfTempName(v)) cls.push("cf-cell");
      if (row.dna_diff && row.dna_diff[c.name] !== undefined) cls.push("dna-diff-cell");
      if (S.cur && S.cur.rid === row.id && S.cur.col === c.name) cls.push("cur");
      if (S.preset === "loaned" && c.name === "LoanSpecimenTo" && (v === null || v === undefined || v === "")) {
        tds.push(`<td class="cell" data-rid="${row.id}" data-col="${esc(c.name)}"><span class="val"><span class="loan-open">未归还</span></span></td>`);
        continue;
      }
      // 历史缓存回退：来源记录缺失但本行留有导入时的缓存值 → 明确标注，不标成当前来源值
      const missCache = row.miss && (c.linked || c.name === key || c.name === (curCfg().linked_key || ""))
        && v !== null && v !== undefined && v !== "";
      const tip = missCache ? ` title="历史缓存，关联未匹配：来源记录缺失，显示的是本行导入时的缓存值（不是当前来源值）"`
        : c.linked ? ` title="来自“采集地信息”，请到该表修改"`
        : row.miss && (c.name === key || (curCfg().linked_key && c.name === curCfg().linked_key)) ? ` title="该行的事件编号在采集地信息中未找到，关联列为空"`
        : v !== null && v !== undefined && v !== "" ? ` title="${esc(v)}"` : "";
      const fzst = frozen.includes(c.name)
        ? ` style="position:sticky;left:${fzLeftMap[c.name]}px;z-index:7;background:#fff;width:${fzW(c.name)}px;min-width:${fzW(c.name)}px;max-width:${fzW(c.name)}px;"`
        : "";
      tds.push(`<td class="${cls.join(" ")}" data-rid="${row.id}" data-col="${esc(c.name)}"${tip}${fzst}><span class="val">${cellHtml(c.name, v, row)}${missCache ? ' <span class="st st-pending" title="来源记录缺失，此为导入时缓存值">历史缓存</span>' : ""}</span></td>`);
    }
    let acts;
    if (S.showDeleted) {
      acts = `<button class="btn" data-act="undel" data-rid="${row.id}" title="还原此行">${icon("restore")} 还原</button>`
        + (S.me.admin ? ` <button class="btn danger" data-act="purge" data-rid="${row.id}" title="彻底删除（不可恢复）">彻底删除</button>` : "");
    } else {
      const lkey = curCfg().linked_key;
      const needGeo = lkey && (!row.data[lkey] || row.miss);
      const geoBtn = needGeo && canWriteTable("events")
        ? ` <button class="btn" data-act="supgeo" data-rid="${row.id}" title="采集编号缺失：手动补充采集信息并自动关联">${icon("geo")}</button>` : "";
      acts = `<button class="btn" data-act="view" data-rid="${row.id}" title="查看此行全部字段">${icon("view")}</button>`
        + geoBtn +
        ` <button class="btn" data-act="hist" data-rid="${row.id}" title="此行的修改历史">${icon("clock")}</button>`
        + ` <button class="btn" data-act="files" data-rid="${row.id}" title="附件">${icon("paperclip")}</button>`
        + ` <button class="btn" data-act="copyrow" data-rid="${row.id}" title="复制整行（含表头，可粘贴到 Excel 或“粘贴导入”）">${icon("copy")}</button>`
        + (canWriteTable(S.t) ? ` <button class="btn danger" data-act="del" data-rid="${row.id}" title="删除此行">${icon("trash")}</button>` : "");
    }
    tds.push(`<td class="rowact">${acts}</td>`);
    body.push(`<tr>${tds.join("")}</tr>`);
  });
  $("grid-body").innerHTML = body.join("");
  const ge = $("grid-empty");
  ge.hidden = S.rows.length > 0;
  if (!ge.hidden) {
    const nFilter = Object.keys(S.filters).length;
    if (S.showDeleted) ge.textContent = "回收站是空的。";
    else if (S.q || nFilter) {
      // 无匹配（数据存在，条件太严）：给出一键清除 + 相近属名建议
      ge.innerHTML = `没有匹配的行（${S.q ? `搜索词「${esc(S.q)}」` : ""}${S.q && nFilter ? "＋" : ""}${nFilter ? nFilter + " 个列筛选" : ""}太严了，数据都还在）。 <button class="btn" id="ge-clear">清除搜索与筛选，显示全部</button><span id="ge-suggest" style="margin-left:10px"></span>`;
      const gec = document.getElementById("ge-clear");
      if (gec) gec.onclick = () => {
        S.q = ""; $("search").value = "";
        S.filters = {}; S.preset = ""; S.page = 1;
        loadTable(); renderFilterChips();
      };
      if (S.q && !nFilter) {
        api(`/api/search-suggest?q=${encodeURIComponent(S.q)}`).then((r) => {
          const box = document.getElementById("ge-suggest");
          if (!box || !r.items || !r.items.length) return;
          box.innerHTML = `您是不是要找：` + r.items.map((it) =>
            `<button class="btn" data-sug="${esc(it.text)}" style="margin:0 4px 0 0;padding:2px 10px">${esc(it.text)}</button>`).join("");
          box.querySelectorAll("button[data-sug]").forEach((b) => b.onclick = () => {
            $("search").value = b.dataset.sug;
            S.q = b.dataset.sug; S.page = 1;
            loadTable();
          });
        }).catch(() => { });
      }
    } else {
      // 无数据（全表为空，与无匹配区分）
      ge.innerHTML = `该表还没有数据。点击 <button class="btn primary" id="ge-add" style="margin:0 4px">新增行</button> 开始录入，或用「导入 ▾ → 导入 Excel」批量导入。`;
      const ga = document.getElementById("ge-add");
      if (ga) ga.onclick = () => $("btn-add").click();
    }
  }
  syncCurColTh();  // 表头重建后恢复当前列高亮
  const applyRange = (fromIdx, toIdx, state) => {
    const [a, b] = fromIdx <= toIdx ? [fromIdx, toIdx] : [toIdx, fromIdx];
    for (let i = a; i <= b; i++) {
      const r = S.rows[i];
      if (!r) continue;
      state ? S.sel.add(r.id) : S.sel.delete(r.id);
    }
  };
  // Shift 状态在 mousedown 阶段捕获（change 事件的 shiftKey 在真实点击中不可靠）
  $("grid-body").addEventListener("mousedown", (e) => {
    const ck = e.target.closest("input[data-ckrid]");
    if (ck) {
      S._ckDown = {
        idx: S.rows.findIndex((r) => r.id === +ck.dataset.ckrid),
        shift: e.shiftKey,
      };
    }
  });
  $("grid-body").querySelectorAll("input[data-ckrid]").forEach((ck) =>
    ck.onchange = () => {
      const rid = +ck.dataset.ckrid;
      const idx = S.rows.findIndex((r) => r.id === rid);
      const useShift = S._ckDown && S._ckDown.shift && S._lastCkIdx !== null && S._lastCkIdx !== undefined;
      if (useShift) {
        const [a, b] = S._lastCkIdx <= idx ? [S._lastCkIdx, idx] : [idx, S._lastCkIdx];
        for (let i = a; i <= b; i++) {
          const r = S.rows[i];
          if (!r) continue;
          ck.checked ? S.sel.add(r.id) : S.sel.delete(r.id);
          if (typeof renderSelBar === "function") renderSelBar();
        }
      } else {
        ck.checked ? S.sel.add(rid) : S.sel.delete(rid);
        if (typeof renderSelBar === "function") renderSelBar();
      }
      S._lastCkIdx = idx; S._lastCkChecked = ck.checked;
      renderGrid();  // 重绘使范围内所有勾选框同步显示
      renderSelbar();
    });
  const ckAll = $("ck-all");
  if (ckAll) ckAll.onchange = () => {
    if (ckAll.checked) S.rows.forEach((r) => S.sel.add(r.id));
    if (typeof renderSelBar === "function") renderSelBar();
    else S.rows.forEach((r) => S.sel.delete(r.id));
    renderGrid();
  };
  renderSelbar();
}

/* ---- 列宽拖拽 ---- */
function wireColResize() {
  $("grid-head").querySelectorAll(".th-grip").forEach((grip) => {
    grip.onmousedown = (e) => {
      e.preventDefault();
      e.stopPropagation();
      const th = grip.closest("th");
      const col = grip.dataset.grip;
      const startX = e.clientX, startW = th.offsetWidth;
      const move = (ev) => {
        const w = Math.max(56, Math.min(900, startW + ev.clientX - startX));
        th.style.width = w + "px";
        th.style.minWidth = w + "px";
        th.style.maxWidth = w + "px";
        grip._w = w;
      };
      const up = () => {
        document.removeEventListener("mousemove", move);
        document.removeEventListener("mouseup", up);
        if (grip._w) setColw(col, grip._w);
      };
      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up);
    };
    grip.ondblclick = (e) => {
      e.preventDefault();
      const th = grip.closest("th");
      th.style.width = "";
      th.style.maxWidth = "";
      setColw(grip.dataset.grip, null);
    };
  });
}

/* ---- 批量编辑 ---- */
function renderSelbar() {
  const bar = $("selbar");
  if (!bar) return;
  const n = S.sel.size;
  if (S.showDeleted) {
    const mayW = canWriteTable(S.t);
    const mayP = S.me.admin;
    bar.hidden = !(n > 0 && (mayW || mayP));
    if (bar.hidden) return;
    bar.innerHTML = `
      <span class="sel-cnt">回收站已选 <b>${n}</b> 行</span>
      ${mayW ? `<button class="btn primary" id="rb-restore">批量还原</button>` : ""}
      ${mayP ? `<button class="btn danger" id="rb-purge">批量彻底删除</button>` : ""}
      <span class="hint">还原 = 恢复到可见列表；彻底删除不可恢复（仅管理员）。</span>`;
    $("rb-restore")?.addEventListener("click", async () => {
      try {
        const r = await api(`/api/rows/${S.t}/bulk-restore`, { method: "POST", body: JSON.stringify({ rids: [...S.sel] }) });
        toast(`已还原 ${r.restored} 行`, "ok");
        S.sel.clear();
        loadTable(); refreshMetaCounts();
      } catch (e) { toast(e.message, "err"); }
    });
    $("rb-purge")?.addEventListener("click", async () => {
      if (!(await confirmBox(`彻底删除选中的 ${n} 行？不可恢复（含附件）。`, { okText: "彻底删除" }))) return;
      try {
        const r = await api(`/api/rows/${S.t}/bulk-purge`, { method: "POST", body: JSON.stringify({ rids: [...S.sel] }) });
        toast(`已彻底删除 ${r.purged} 行`, "ok");
        S.sel.clear();
        loadTable(); refreshMetaCounts();
      } catch (e) { toast(e.message, "err"); }
    });
    return;
  }
  const mayW = canWriteTable(S.t);
  const show = n > 0;
  bar.hidden = !show;
  if (!show) return;
  bar.innerHTML = `
    <span class="sel-cnt">已勾选 <b>${n}</b> 行</span>
    <button class="btn primary" id="btn-sel-export">导出选中</button>
    ${mayW ? `<button class="btn primary" id="btn-batch-edit">批量修改共享字段</button>` : ""}
    <button class="btn" id="btn-sel-plan" title="把当前筛选解析为带版本的固定行集合：之后筛选变化不会扩大操作范围">按筛选全选（固化计划）</button>
    <button class="btn" id="btn-sel-clear">取消勾选</button>
    <span class="hint">${mayW ? "勾选多行 → 一次性改同一字段（如统一保存浓度、项目、盒号）；整批操作记为一个批次，可整体撤销。" : "勾选行后可导出所选条目（只读权限）。"}</span>`;
  $("btn-sel-clear").onclick = () => { S.sel.clear(); S.planDigest = null; renderGrid(); };
  $("btn-sel-export").onclick = () => $("btn-export").click();
  // 稳定计划：当前筛选 → 显式行集合（上限 5000，超出提示分段）；勾选集合并入后固化，
  // 之后筛选变化/新增记录都不会扩大批量操作范围（操作只针对计划内的显式行 ID）。
  $("btn-sel-plan").onclick = async () => {
    try {
      const p = new URLSearchParams({ q: S.q, sort: S.sort, dir: S.dir, resolve: "ids" });
      if (S.showDeleted) p.set("deleted", "1");
      if (S.preset) p.set("preset", S.preset);
      if (Object.keys(S.filters).length) p.set("filters", JSON.stringify(S.filters));
      const pl = await api(`/api/rows/${S.t}?` + p);
      const nCheckedHidden = [...S.sel].filter((id) => !pl.ids.includes(id)).length;
      const ok = await confirmBox(
        `把当前筛选解析为固定行集合：\n· 计划操作 <b>${pl.count}</b> 行${pl.truncated ? `（超出上限 ${pl.cap}，已截断——请先用筛选缩小范围或分段处理）` : ""}\n` +
        `· 当前已勾选 ${S.sel.size} 行（其中 ${nCheckedHidden} 行在筛选结果之外，将被移出集合）\n` +
        `· 查询指纹 ${esc(pl.digest)}\n\n样例：${esc((pl.sample || []).map((x) => x.key || ("#" + x.id)).join("、"))}\n\n` +
        `固化后筛选变化或新增记录不会扩大本次批量操作。`, { okText: `固化 ${pl.count} 行`, danger: false });
      if (!ok) return;
      S.sel = new Set(pl.ids);
      S.planDigest = pl.digest;
      renderGrid(); renderSelbar();
      toast(`已按筛选固化 ${pl.ids.length} 行（计划指纹 ${pl.digest}）`, "ok");
    } catch (e) { toast(e.message, "err"); }
  };
  if (mayW) $("btn-batch-edit").onclick = openBatchEdit;
}

function openBatchEdit() {
  const rids = [...S.sel];
  if (!rids.length) return;
  if (S.planDigest && S.planDigest !== S.queryDigest) {
    toast("筛选条件在固化计划后已变化：操作仍限于计划内的 " + rids.length + " 行（不会扩大）；如需纳入新匹配行请重新固化", "warn");
  }
  const keyName = curCfg().key;
  const own = S.columns.filter((c) => !c.linked && c.name !== keyName);
  const opts = own.map((c) => `<option value="${esc(c.name)}">${esc(c.name)}${S.docs[c.name] ? " — " + esc(S.docs[c.name]).slice(0, 26) + "…" : ""}</option>`).join("");
  openModal(`批量修改 · ${curCfg().label}（${rids.length} 行）`, `
    <p class="hint">选择要修改的字段并填入新值，将对勾选的 <b>${rids.length}</b> 行统一生效。
    留空的行内其他字段不受影响；编号列不可批量改（避免冲突）。</p>
    <div class="grid-form">
      <label>字段 <input id="batch-filter" type="search" placeholder="输入筛选字段名…" style="margin-bottom:4px">
        <select id="batch-col" size="8" style="width:100%">${opts}</select>
      </label>
      <label>新值<span class="hint" id="batch-doc"></span>
        <span id="batch-valwrap"><input id="batch-val"></span>
      </label>
    </div>
    <p class="hint" id="batch-tip"></p>
    <div class="form-actions">
      <button class="btn" id="batch-cancel">取消</button>
      <button class="btn primary" id="batch-ok">应用到 ${rids.length} 行</button>
    </div>`);
  const colSel = $("batch-col"), valWrap = $("batch-valwrap"), tip = $("batch-tip");
  const renderVal = () => {
    const c = own.find((x) => x.name === colSel.value);
    if (!c) return;
    $("batch-doc").textContent = S.docs[c.name] ? "　" + S.docs[c.name] : "";
    if (c.enum_locked && c.options) {
      valWrap.innerHTML = `<select id="batch-val" style="padding:7px 9px;border:1px solid var(--border);border-radius:6px">
        <option value="">（清空为空值）</option>${c.options.map((o) => `<option value="${esc(String(o))}">${esc(o)}</option>`).join("")}</select>`;
    } else if (S.t === "specimens" && c.name === "Box # (Start#-End#)") {
      valWrap.innerHTML = `<select id="batch-val" style="padding:7px 9px;border:1px solid var(--border);border-radius:6px"><option value="">（清空为空值）</option></select>`;
      loadBoxes().then(() => {
        const sel = $("batch-val");
        sel.innerHTML = `<option value="">（清空为空值）</option>` + availableBoxOptions("");
      });
      tip.textContent = "盒号会按编号区间校验占用情况；满盒不可选。";
    } else {
      valWrap.innerHTML = `<input id="batch-val" placeholder="留空 = 清空该字段">`;
    }
  };
  const bFilter = $("batch-filter");
  if (bFilter) {
    bFilter.oninput = () => {
      const q = bFilter.value.trim().toLowerCase();
      colSel.querySelectorAll("option").forEach((o) =>
        o.hidden = q && !o.textContent.toLowerCase().includes(q));
      const firstVisible = [...colSel.options].find((o) => !o.hidden);
      if (firstVisible && (!colSel.selectedOptions.length || colSel.selectedOptions[0].hidden)) {
        colSel.value = firstVisible.value;
        colSel.dispatchEvent(new Event("change"));
      }
    };
  }
  colSel.onchange = renderVal;
  renderVal();
  $("batch-cancel").onclick = closeModal;
  $("batch-ok").onclick = async () => {
    const colName = colSel.value;
    const raw = ($("batch-val").value || "").trim();
    if (isTempNameCol(colName) && raw) {
      const chk = await confirmTempName(raw, $("batch-val"));
      if (chk === false) return;
    }
    try {
      const baseVersions = {};
      rids.forEach((rid) => { const row = rowById(rid); if (row) baseVersions[rid] = row.row_version; });
      const r = await api(`/api/rows/${S.t}/batch_update`, {
        method: "POST",
        body: JSON.stringify({ rids, changes: { [colName]: raw }, baseVersions }),
      });
      closeModal();
      let msg = `批量修改完成：更新 ${r.updated} 行` + (r.noop ? `、${r.noop} 行无变化` : "");
      if (r.failed?.length) msg += `；${r.failed.length} 行失败`;
      if (r.not_executed?.length) msg += `；${r.not_executed.length} 行未执行`;
      if (r.ident_sync?.rows) msg += `；已联动同步「${r.ident_sync.peer_label || "关联表"}」${r.ident_sync.rows} 行`;
      if (r.ident_sync?.skipped_perm) msg += `；「${r.ident_sync.peer_label || "关联表"}」${r.ident_sync.skipped_perm} 行因无编辑权限未联动`;
      toast(msg, (r.failed?.length || r.not_executed?.length) ? "err" : "ok");
      if (r.warn) toast(r.warn, "err");
      if (r.failed?.length) console.warn("批量修改失败明细", r.failed);
      if (r.not_executed?.length) console.warn("批量修改未执行明细", r.not_executed);
      S.sel.clear();
      await loadTable();
      refreshUndo();
    } catch (e) { toast(e.message, "err"); }
  };
}

function renderPager() {
  const pages = Math.max(1, Math.ceil(S.total / S.per));
  S.page = Math.min(S.page, pages);
  const nums = [];
  for (let p = 1; p <= pages; p++) {
    if (p === 1 || p === pages || Math.abs(p - S.page) <= 2) nums.push(p);
    else if (nums[nums.length - 1] !== "…") nums.push("…");
  }
  $("pager").innerHTML = `
    <span class="stat">每页
      <select id="per-sel">${[50, 100, 200, 500].map((n) => `<option ${n === S.per ? "selected" : ""}>${n}</option>`).join("")}</select> 行
    </span>
    <button class="btn" id="pg-first" ${S.page <= 1 ? "disabled" : ""}>«</button>
    <button class="btn" id="pg-prev" ${S.page <= 1 ? "disabled" : ""}>‹</button>
    ${nums.map((n) => n === "…" ? `<span class="stat">…</span>`
      : `<button class="btn pg ${n === S.page ? "cur" : ""}" data-p="${n}">${n}</button>`).join("")}
    <button class="btn" id="pg-next" ${S.page >= pages ? "disabled" : ""}>›</button>
    <button class="btn" id="pg-last" ${S.page >= pages ? "disabled" : ""}>»</button>
    <input id="pg-go" type="number" min="1" max="${pages}" value="${S.page}" title="输入页码后回车直达"
      style="width:64px;padding:3px 6px;border:1px solid var(--border);border-radius:6px;font-size:12px">`;
  const sel = $("per-sel");
  sel.onchange = () => { S.per = +sel.value; S.page = 1; loadTable(); };
  const pgGo = $("pg-go");
  if (pgGo) pgGo.onkeydown = (e) => {
    if (e.key !== "Enter") return;
    const p = Math.max(1, Math.min(pages, parseInt(pgGo.value, 10) || 1));
    S.page = p;
    loadTable();
  };
  $("pg-first").onclick = () => { S.page = 1; loadTable(); };
  $("pg-prev").onclick = () => { S.page--; loadTable(); };
  $("pg-next").onclick = () => { S.page++; loadTable(); };
  $("pg-last").onclick = () => { S.page = pages; loadTable(); };
  document.querySelectorAll(".pg").forEach((b) => b.onclick = () => { S.page = +b.dataset.p; loadTable(); });
}

/* ---- 行操作 ---- */
$("grid-body").addEventListener("click", async (e) => {
  const dnaBadge = e.target.closest(".dna-diff-badge");
  if (dnaBadge) {
    e.stopPropagation();
    syncRowFromAuthority(+dnaBadge.dataset.dnasyncRid);
    return;
  }
  const actBtn = e.target.closest("button[data-act]");
  if (actBtn) {
    const rid = +actBtn.dataset.rid;
    const act = actBtn.dataset.act;
    if (act === "hist") openRowHistory(rid);
    else if (act === "files") openFiles(rid);
    else if (act === "view") openRowDetail(rid);
    else if (act === "supgeo") openSupplementGeo(rid);
    else if (act === "copyrow") copyRow(rid);
    else if (act === "del") deleteRow(rid);
    else if (act === "undel") undeleteRow(rid);
    else if (act === "purge") purgeRow(rid);
    return;
  }
  const rownumTd = e.target.closest("td.rownum");
  if (rownumTd && e.shiftKey) {
    // Shift+点行号：从上次基准行到本行连续勾选
    const rid = +e.target.closest("tr").querySelector("input[data-ckrid]").dataset.ckrid;
    const idx = S.rows.findIndex((r) => r.id === rid);
    if (S._lastCkIdx !== null && S._lastCkIdx !== undefined) {
      const [a, b] = S._lastCkIdx <= idx ? [S._lastCkIdx, idx] : [idx, S._lastCkIdx];
      for (let i = a; i <= b; i++) if (S.rows[i]) S.sel.add(S.rows[i].id);
      S._lastCkIdx = idx;
      renderGrid();
      renderSelbar();
      return;
    }
    S._lastCkIdx = idx; S._lastCkChecked = true;
    S.sel.add(rid);
    renderGrid(); renderSelbar();
    return;
  }
  const td = e.target.closest("td.cell");
  if (!td) return;
  // v4.35：正在编辑的单元格内再点击 = 定位光标/选词（原生输入行为），不再重启编辑——
  // 此前这一下会提交并重建输入框再次全选，改一个字符只能全删重打
  if (S.editing && S.editing.td === td) return;
  setCur(td);
  const col = S.columns.find((c) => c.name === td.dataset.col);
  if (!col) return;
  if (S.showDeleted) return toast("回收站中的行不可编辑，请先还原", "err");
  if (!canWriteTable(S.t)) return toast("您的账号无权修改此表", "err");
  if (col.linked && col.name !== curCfg().linked_key) {
    toast("这一列由“采集地信息”自动带出，请到 采集地信息 页修改对应采集事件", "err");
    return;
  }
  beginEdit(td, col);
});

function rowById(rid) { return S.rows.find((r) => r.id === rid); }

/* 以此行为模板新增：复制非主键字段，主键自动 +1 */
function cloneRow(rid) {
  const row = rowById(rid);
  if (!row) return;
  const key = curCfg().key;
  const data = {};
  for (const [k, v] of Object.entries(row.data)) {
    if (k === key || v === null || v === undefined || v === "" || k.startsWith("_")) continue;
    data[k] = v;
  }
  if (key) data[key] = plusOne(row.data[key] || "");
  const keyName = key;
  const isSpec = S.t === "specimens";
  const lkKey = curCfg().linked_key;
  const own = S.columns.filter((c) => !c.linked || c.name === lkKey);
  const priSet = new Set(curCfg().add_first || []);
  const fields = own.map((c) => {
    if (c.enum_locked && c.options) {
      return `<label class="${c.wide ? "span2" : ""}" title="${esc(detTitle(c.name))}"><span class="lb-head">${esc(c.name)}${priSet.has(c.name) ? '<span class="imp">*</span>' : ""}</span>
        <select data-col="${esc(c.name)}" data-locked="1" style="padding:7px 9px;border:1px solid var(--border);border-radius:6px">
          ${c.options.map((o) => `<option value="${esc(String(o))}" ${String(o) === String(data[c.name] ?? c.default ?? "") ? "selected" : ""}>${esc(o)}</option>`).join("")}
        </select></label>`;
    }
    return `<label class="${c.wide ? "span2" : ""}" title="${esc(detTitle(c.name))}"><span class="lb-head">${esc(c.label || c.name)}${priSet.has(c.name) ? '<span class="imp">*</span>' : ""}</span>
      <input data-col="${esc(c.name)}" value="${esc(data[c.name] ?? "")}">
    </label>`;
  }).join("");
  openModal(`以此行新增 · ${curCfg().label}`, `
    <p class="hint">已复制 ${esc(key ? row.data[key] || "该行" : "该行")} 的全部非空字段；
    编号已自动 +1 为 <b>${esc(data[keyName] || "")}</b>。相同采集信息的标本连续录入时，
    采集事件号等保持不变，只需改编号与数量。</p>
    <div class="grid-form">${fields}</div>
    <div class="form-actions">
      <button class="btn" id="clone-cancel">取消</button>
      <button class="btn primary" id="clone-ok">保存</button>
      <button class="btn primary" id="clone-save-next">保存并继续下一行</button>
    </div>`);
  markRequired($("modal-body"));
  // 采集事件号联想（同新增行，铺陈到全部类群）
  if (curCfg().linked_key) {
    const evInp2 = $("modal-body").querySelector(`input[data-col="${CSS.escape(curCfg().linked_key)}"]`);
    if (evInp2) attachEventSuggest(evInp2);
  }
  // 临时鉴定名：联想 + cf. 切换按钮
  const tnC = $("modal-body").querySelector(`input[data-col="${CSS.escape(taxCols().temp)}"]`);
  if (tnC) wireTempNameField(tnC);
  // 属名/最终名联想（按本表列名）：Tribe/Clade 联想 + 向上带出
  if (curCfg().genus_lookup) {
    const gC = $("modal-body").querySelector('input[data-col="Genus"]');
    if (gC) { attachGenusSuggest(gC); attachGenusCheck(gC); wireGenusChainFill(gC, $("modal-body")); }
    const fsC = $("modal-body").querySelector(`input[data-col="${CSS.escape(taxCols().final)}"]`);
    if (fsC) attachSpeciesSuggest(fsC);
    if (S.cascade && S.cascade[S.t]) {
      TAX_SUGGEST_COLS.forEach((n) => {
        const el = $("modal-body").querySelector(`input[data-col="${CSS.escape(n)}"]`);
        if (el) attachTaxValueSuggest(el, n);
      });
    }
  }
  if (S.t === "mygalomorph") {
    const mgi = $("modal-body").querySelector('input[data-col="Genus"]');
    const fsi = $("modal-body").querySelector('input[data-col="FinalSpeciesName"]');
    if (fsi) attachSpeciesSuggest(fsi);
    if (mgi) {
      attachGenusSuggest(mgi);
      mgi.addEventListener("change", async () => {
        const g = mgi.value.trim();
        if (!g) return;
        try {
          const r = await api(`/api/taxonomy-lookup?genus=${encodeURIComponent(g)}`);
          if (!r.found) return;
          let filled = [];
          for (const [k, v] of Object.entries(r.record || {})) {
            if (k === "Genus" || !v) continue;
            const el = $("modal-body").querySelector(`input[data-col="${CSS.escape(k)}"], select[data-col="${CSS.escape(k)}"]`);
            if (el && !el.value.trim()) { el.value = v; filled.push(k); }
          }
          if (filled.length) toast(`已按属名带出：${filled.join("、")}`, "ok");
        } catch { }
      });
    }
  }
  if (S.t === "mygalomorph") wireNumberLink($("modal-body"), S.t);
  if (S.t === "uce") {
    const sci2 = $("modal-body").querySelector('input[data-col="Sample Code"]');
    if (sci2) sci2.addEventListener("change", () => {
      const sp = $("modal-body").querySelector('input[data-col="Species name"]');
      sraTryFill((k) => $("modal-body").querySelector(`input[data-col="${CSS.escape(k)}"]`), sci2.value.trim(), sp ? sp.value : "");
    });
  }
  if (isSpec && keyName) {
    const ki = $("modal-body").querySelector(`input[data-col="${CSS.escape(keyName)}"]`);
    if (ki) ki.addEventListener("input", () => {
      const v = ki.value;
      if (v && !v.toUpperCase().startsWith("MHBU")) {
        const pos = ki.selectionStart;
        ki.value = KEY_PREFIX + v;
        ki.setSelectionRange(pos + KEY_PREFIX.length, pos + KEY_PREFIX.length);
      }
    });
  }
  const collect = () => {
    const out = {};
    $("modal-body").querySelectorAll("input[data-col], select[data-locked]").forEach((i) => {
      if (i.value.trim() !== "") out[i.dataset.col] = i.value;
    });
    return out;
  };
  const save = async (again) => {
    const data2 = collect();
    try {
      const r = await api(`/api/rows/${S.t}`, { method: "POST", body: JSON.stringify({ data: data2, force_name: !!S._fsForceOk }) });
      S._fsForceOk = false;
      toast(`已新增：${r.key || ""}`, "ok");
      if (again) {
        // 编号再 +1，其余保留
        if (keyName && data2[keyName]) {
          const ki = $("modal-body").querySelector(`input[data-col="${CSS.escape(keyName)}"]`);
          if (ki) ki.value = plusOne(data2[keyName]);
        }
        $("modal-body").querySelectorAll("input[data-col]").forEach((i) => {
          if (["AdultMale", "AdultFemale", "Subadult", "Juvenile"].includes(i.dataset.col)) i.value = "";
        });
      } else closeModal();
      await loadTable();
    } catch (e) { toast(e.message, "err"); }
  };
  $("clone-cancel").onclick = closeModal;
  $("clone-ok").onclick = () => save(false);
  $("clone-save-next").onclick = () => save(true);
}

/* 编号 +1：尾部数字进位，保留前缀与位数（MHBU-ARA-00020001 → 00020002，KYUARA#0497 → 0498） */
function plusOne(kv) {
  const m = String(kv).match(/^(.*?)(\d+)(\D*)$/);
  if (!m) return kv;
  const num = String(+m[2] + 1).padStart(m[2].length, "0");
  return m[1] + num + m[3];
}

function copyRow(rid) {
  const row = rowById(rid);
  if (!row) return;
  const lines = [S.columns.map((c) => c.name).join("\t")];
  lines.push(S.columns.map((c) => row.data[c.name] ?? "").join("\t"));
  copyText(lines.join("\n"));
}

async function deleteRow(rid) {
  const row = rowById(rid);
  const key = curCfg().key;
  const label = key ? row?.data[key] || `#${rid}` : `#${rid}`;
  if (!(await confirmBox(`将 ${tblName(S.meta.tables[S.t])} 中的「${esc(label)}」移入回收站？\n移入回收站会记录在变更历史中，可随时还原。`, { okText: "移入回收站" }))) return;
  try {
    await api(`/api/rows/${S.t}/${rid}`, { method: "DELETE" });
    toast("已删除（可在回收站还原）");
    loadTable();
    refreshMetaCounts();
  } catch (err) { toast(err.message, "err"); }
}

async function undeleteRow(rid) {
  try {
    await api(`/api/rows/${S.t}/${rid}/undelete`, { method: "POST" });
    toast("已还原", "ok");
    loadTable();
    refreshMetaCounts();
  } catch (err) { toast(err.message, "err"); }
}

async function purgeRow(rid) {
  const row = rowById(rid);
  const key = curCfg().key;
  if (!(await confirmBox(`彻底删除「${esc(key ? row?.data[key] : "#" + rid)}」？\n此操作不可恢复（含其附件），仅保留一条删除日志。`, { okText: "彻底删除" }))) return;
  try {
    await api(`/api/rows/${S.t}/${rid}/purge`, { method: "DELETE" });
    toast("已彻底删除");
    loadTable();
  } catch (err) { toast(err.message, "err"); }
}

/* ---- 单元格编辑 ---- */
function cascadeOptions(colName, data) {
  const casc = S.cascade[S.t];
  if (!casc) return null;
  const tree = casc.tree;
  const orders = casc.orders;
  const famsOf = (o) => (tree[o] ? Object.keys(tree[o]) : Object.keys(tree).flatMap((k) => Object.keys(tree[k])));
  const subsOf = (o, f) => {
    if (o && tree[o] && tree[o][f]) return Object.keys(tree[o][f]);
    const set = new Set();
    for (const o2 of Object.keys(tree)) for (const f2 of Object.keys(tree[o2]))
      for (const sf of Object.keys(tree[o2][f2])) set.add(sf);
    return [...set];
  };
  const generaOf = (o, f, sf) => {
    const set = new Set();
    const collect = (fammap) => { for (const sf2 of Object.keys(fammap)) (sf ? sf2 === sf : true) && fammap[sf2].forEach((g) => set.add(g)); };
    if (o && tree[o] && tree[o][f]) collect(tree[o][f]);
    else for (const o2 of Object.keys(tree)) if (tree[o2][f]) collect(tree[o2][f]);
    return [...set];
  };
  let opts;
  if (colName === "Order") opts = orders;
  else if (colName === "Family") opts = famsOf(data.Order || "");
  else if (colName === "Subfamily") opts = subsOf(data.Order || "", data.Family || "");
  else if (colName === "Genus") opts = generaOf(data.Order || "", data.Family || "", data.Subfamily || "");
  opts = opts.filter(Boolean);
  return opts.length > 600 ? opts.slice(0, 600) : opts;
}

function showGridGenusWarn(td, text) {
  clearGridGenusWarn();
  const rect = td.getBoundingClientRect();
  const tip = document.createElement("div");
  tip.className = "genus-warn-float";
  tip.style.cssText = `position:fixed;left:${Math.max(8, rect.left)}px;top:${rect.bottom + 5}px;max-width:360px;z-index:300;font-size:12px;line-height:1.5;color:var(--warn);background:#fdf9ee;border:1px solid #e7dcc0;border-radius:6px;padding:5px 9px;box-shadow:0 4px 14px rgba(36,41,47,.14)`;
  tip.textContent = text;
  document.body.appendChild(tip);
}
function clearGridGenusWarn() { document.querySelectorAll(".genus-warn-float").forEach((x) => x.remove()); }

/* ================= 编辑状态机与草稿（v3.48） =================
   状态：draft（草稿）→ submitting（提交中，阻止同一编辑重复提交）
        → success（成功，以服务器值与版本为准，一次应用全部连带字段）
        → failed（失败：保留本人输入与服务器旧值，可 重试/复制草稿/放弃）
        → conflict（冲突：走既有冲突对话框）
        → pending_confirm（响应丢失：结果待确认，用幂等键查询确认，绝不盲目重复提交）
   草稿按 账号|表|记录|字段|基础版本 隔离，仅保存在本机浏览器（localStorage），
   留存 7 天，不上传服务器；恢复草稿前先核对服务器值与版本，绝不自动覆盖。 */
const DRAFT_TTL_MS = 7 * 24 * 3600 * 1000;
function draftAll() { try { return JSON.parse(localStorage.getItem("sm_drafts") || "{}"); } catch { return {}; } }
function draftWrite(d) { try { localStorage.setItem("sm_drafts", JSON.stringify(d)); } catch { } }
function draftKey(t, rid, col, bv) { return (S.me ? S.me.username : "?") + "|" + t + "|" + rid + "|" + col + "|" + bv; }
function draftPut(t, rid, col, bv, value, label) {
  const d = draftAll();
  const now = Date.now();
  for (const k of Object.keys(d)) if (now - (d[k].saved_at || 0) > DRAFT_TTL_MS) delete d[k];
  d[draftKey(t, rid, col, bv)] = { value, label: label || "", saved_at: now };
  draftWrite(d);
}
function draftGet(t, rid, col, bv) {
  const d = draftAll();
  const it = d[draftKey(t, rid, col, bv)];
  if (!it) return null;
  if (Date.now() - (it.saved_at || 0) > DRAFT_TTL_MS) { draftDrop(t, rid, col, bv); return null; }
  return it;
}
function draftDrop(t, rid, col, bv) {
  const d = draftAll();
  delete d[draftKey(t, rid, col, bv)];
  draftWrite(d);
}
function formDraftKeyOf(type, target) { return "form:" + type + ":" + (target || "new"); }
function formDraftSave(type, target, data) {
  const clean = JSON.parse(JSON.stringify(data, (k, v) => (/pass|token|secret|password/i.test(k) ? undefined : v)));
  draftPut("form:" + type, target || "new", "form", "", { data: clean }, new Date().toLocaleString());
}
function formDraftLoad(type, target) {
  const it = draftGet("form:" + type, target || "new", "form", "");
  if (!it || !it.value || !it.value.data) return null;
  return { at: it.saved_at || 0, data: it.value.data };
}
function formDraftDrop(type, target) { draftDrop("form:" + type, target || "new", "form", ""); }

function draftClearMine() {
  const pre = (S.me ? S.me.username : "?") + "|";
  const d = draftAll();
  let n = 0;
  for (const k of Object.keys(d)) if (k.startsWith(pre)) { delete d[k]; n++; }
  draftWrite(d);
  return n;
}
/* 确定性请求 ID：同一编辑（表+行+字段+新值+基础版本）重试时复用同一幂等键 */
function editReqId(t, rid, col, nv, bv) {
  let h = 5381;
  const s = t + "|" + rid + "|" + col + "|" + nv + "|" + bv;
  for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) >>> 0;
  return "E" + h.toString(16) + "-" + String(s.length).padStart(3, "0");
}
const isNetError = (err) => api.lastNetError === true
  || /failed to fetch|networkerror|err_network|timeout|timed out|abort/i.test(String(err && err.message));
/* 编辑单元格内的操作条：重试 / 复制草稿 / 放弃（失败与待确认共用） */
function showEditBar(ed, msg, extraHtml) {
  ed.td.querySelectorAll(".edit-bar").forEach((x) => x.remove());
  const bar = document.createElement("div");
  bar.className = "edit-bar";
  bar.innerHTML = `
    <div class="edit-msg">${esc(msg || "")}</div>
    <div class="edit-btns">
      ${extraHtml || `<button class="btn" data-eb="retry">重试</button>
      <button class="btn" data-eb="copy">复制草稿</button>
      <button class="btn" data-eb="drop">放弃</button>`}
    </div>`;
  ed.td.appendChild(bar);
  const q = (sel) => bar.querySelector(sel);
  if (q('[data-eb="retry"]')) q('[data-eb="retry"]').onclick = () => retryEdit(ed);
  if (q('[data-eb="copy"]')) q('[data-eb="copy"]').onclick = async () => {
    try { await navigator.clipboard.writeText(ed.draft ?? ""); toast("草稿已复制到剪贴板", "ok"); }
    catch { toast("复制失败：请手动选中输入框内容复制", "err"); }
  };
  if (q('[data-eb="drop"]')) q('[data-eb="drop"]').onclick = () => abandonEdit(ed);
  if (q('[data-eb="check"]')) q('[data-eb="check"]').onclick = () => confirmEditResult(ed);
  return bar;
}
function abandonEdit(ed) {
  draftDrop(S.t, ed.row.id, ed.col.name, ed.baseV ?? ed.row.row_version);
  ed.td.innerHTML = `<span class="val">${fmtV(ed.row.data[ed.col.name])}</span>`;
  if (S.editing === ed) S.editing = null;
  toast("已放弃本次修改（草稿已清除）", "ok");
}
async function retryEdit(ed) {
  await submitEdit(ed, ed.draft);
}
/* 响应丢失：凭确定性请求 ID 向服务器查询该次写入是否已落库 */
async function confirmEditResult(ed) {
  try {
    const r = await api(`/api/idempotency/${encodeURIComponent(ed.reqId)}`);
    if (r.found && r.status === 200 && r.response && r.response.ok) {
      toast("已确认：该次修改此前已成功保存（以服务器结果为准）", "ok");
      return applyEditSuccess(ed, r.response);
    }
    if (r.found) {
      ed.state = "failed";
      showEditBar(ed, "服务器已记录该请求，但当时返回失败（" + ((r.response && r.response.error) || r.status) + "）");
      return;
    }
  } catch { /* 查询本身也断网：保持待确认 */ }
  ed.state = "pending_confirm";
  showEditBar(ed, "网络不可用，结果待确认。草稿已保留在本机，恢复联网核对后再提交。",
    `<button class="btn primary" data-eb="check">查询结果</button>
     <button class="btn" data-eb="retry">重新提交</button>
     <button class="btn" data-eb="copy">复制草稿</button>
     <button class="btn" data-eb="drop">放弃</button>`);
}
/* 成功：以服务器值与版本为准，一次应用全部连带字段（级联列原位更新，不整页刷新） */
/* 鉴定联动响应提示：主写入成功后另一表有多少行随之更新/因权限未同步 */
function toastIdentSync(r) {
  const si = r && r.ident_sync;
  if (!si) return;
  const lbl = si.peer_label || si.peer || "关联表";
  if (si.rows) toast(`已联动同步「${lbl}」${si.rows} 行`, "ok");
  if (si.skipped_perm) toast(`「${lbl}」${si.skipped_perm} 行因您在该表无编辑权限未联动同步`, "warn");
}

/* 差异徽标点击 / 单行同步：以权威表（DNA 提取台账）为准更新本行鉴定信息 */
async function syncRowFromAuthority(rid) {
  const ident = curCfg().ident_sync;
  if (!ident) return;
  const row = S.rows.find((r) => r.id === rid);
  const lbl = ident.peer_label || ident.peer;
  const vals = row && row.dna_diff
    ? Object.entries(row.dna_diff).map(([k, v]) => `${k} → ${String(v ?? "").trim() || "（空）"}`).join("；")
    : "";
  const ok = await confirmBox(
    `以「${esc(lbl)}」为准同步本行鉴定信息？` + (vals ? `\n\n将更新：${vals}` : ""),
    { okText: "同步" });
  if (!ok) return;
  try {
    const r = await api(`/api/rows/${S.t}/sync_from_authority`,
                        { method: "POST", body: JSON.stringify({ rids: [rid] }) });
    const failN = (r.failed || []).length;
    if (r.synced) toast(`已按「${lbl}」同步本行`, "ok");
    else if (failN) toast(r.failed[0].error || "同步失败", "err");
    else toast("本行与权威表一致，无需同步", "ok");
  } catch (err) { toast(err.message, "err"); }
  loadTable();
}

function applyEditSuccess(ed, r) {
  const { row, col, td } = ed;
  // 鉴定联动：本次改动若已联动同步，本行差异标记随之消除（值已与权威表一致）
  if (row.dna_diff) {
    for (const c of Object.keys(row.dna_diff)) {
      if ((row.data[c] ?? "").toString().trim() === (row.dna_diff[c] ?? "").toString().trim())
        delete row.dna_diff[c];
    }
    if (!Object.keys(row.dna_diff).length) delete row.dna_diff;
  }
  if (r.noop) {
    td.innerHTML = `<span class="val">${fmtV(row.data[col.name])}</span>`;
  } else {
    for (const [k, pair] of Object.entries(r.changes || {})) {
      if (Array.isArray(pair) && pair.length === 2) row.data[k] = pair[1];
    }
    row.updated_at = r.updated_at;
    row.row_version = r.row_version;
    td.innerHTML = `<span class="val">${cellHtml(col.name, displayVal(col.name, row.data[col.name]), row)}</span>`;
    for (const k of Object.keys(r.changes || {})) {
      if (k === col.name) continue;
      const ctd = document.querySelector(`#grid-body td[data-rid="${row.id}"][data-col="${CSS.escape(k)}"]`);
      const cobj = S.columns.find((c) => c.name === k);
      if (ctd && cobj && ctd !== ed.td) {
        ctd.innerHTML = `<span class="val">${cellHtml(k, displayVal(k, row.data[k]), row)}</span>`;
      }
    }
    if ((r.changes || {})[curCfg().linked_key || ""] !== undefined || col.name === "Genus") {
      // 编号/事件变化可能影响失配标记：该行失配徽章随 cellHtml 重建（无需整表刷新）
    }
  }
  draftDrop(S.t, row.id, col.name, ed.baseV ?? ed.row.row_version);
  if (S.editing === ed) S.editing = null;
  if (r.warn) toast(r.warn, "err");
  toastIdentSync(r);
  if (r.merged_fields) toast(r.warn || "该行在您编辑期间有其他修改，已按字段合并保存", "warn");
  const pm = ed.pendingMove;
  if (pm && (pm[0] || pm[1])) moveCur(pm[0], pm[1]);   // 结果明确后才移动
}
/* 提交（状态机主体）。dRow/dCol 仅在结果明确后执行；提交中重复触发只排队不重发。 */
async function submitEdit(ed, nv) {
  if (ed.state === "submitting") { ed.pendingMove = ed.pendingMove || ed._queued || null; return; }
  ed.state = "submitting";
  ed.reqId = editReqId(S.t, ed.row.id, ed.col.name, nv, ed.row.row_version);
  ed.baseV = ed.row.row_version;
  ed.draft = nv;
  if (ed.input) ed.input.disabled = true;
  ed.td.querySelectorAll(".edit-bar").forEach((x) => x.remove());
  let r;
  try {
    r = await api(`/api/rows/${S.t}/${ed.row.id}`, {
      method: "PUT",
      headers: { "X-Request-Id": ed.reqId },
      body: JSON.stringify({ changes: { [ed.col.name]: nv }, base_version: ed.row.row_version,
                             base_updated_at: ed.row.updated_at, force_name: !!S._fsForceOk }),
    });
    S._fsForceOk = false;
  } catch (err) {
    if (err.conflict) {
      if (S.editing === ed) S.editing = null;
      ed.td.innerHTML = `<span class="val">${fmtV(ed.row.data[ed.col.name])}</span>`;
      draftDrop(S.t, ed.row.id, ed.col.name, ed.row.row_version);
      showConflict(ed.row, ed.col.name, nv, err.conflict);
      return;
    }
    if (isNetError(err)) {
      ed.state = "pending_confirm";
      await confirmEditResult(ed);    // 不标记未保存，不盲目重复提交
      return null;
    }
    ed.state = "failed";
    showEditBar(ed, "保存失败：" + err.message + "（您的输入已保留，服务器值未变）");
    return null;
  }
  applyEditSuccess(ed, r);
  return r;
}
function pendingDraftCount() {
  const pre = (S.me ? S.me.username : "?") + "|";
  const d = draftAll();
  return Object.keys(d).filter((k) => k.startsWith(pre)).length;
}
function dirtyEditing() {
  const ed = S.editing;
  return !!(ed && ed.input && ed.state !== "submitting" && ed.state !== "pending_confirm"
    && String(ed.input.value) !== String(ed.row.data[ed.col.name] ?? ""));
}
/* 离开守卫：切表/关详情/登出/刷新 有未提交值时 询问 保存/保留草稿/放弃 */
function leaveGuardConfirm(action) {
  return new Promise((resolve) => {
    openModal("有未提交的修改", `
      <p style="line-height:1.8">当前单元格的修改尚未提交（${esc(action)}会离开）。</p>
      <p class="hint">「保留草稿」会把修改存放在本机浏览器（按账号/表/记录/版本隔离，保留 7 天，不上传服务器）；回到同一单元格会询问是否恢复。登出会清除本账号的全部本机草稿。</p>
      <div class="form-actions">
        <button class="btn primary" id="lg-save">保存修改</button>
        <button class="btn" id="lg-keep">保留草稿</button>
        <button class="btn danger" id="lg-drop">放弃修改</button>
      </div>`);
    $("lg-save").onclick = async () => { closeModal(); await commitEdit(); resolve("saved"); };
    $("lg-keep").onclick = () => { closeModal(); resolve("kept"); };
    $("lg-drop").onclick = () => { closeModal(); if (S.editing) cancelEdit(true); resolve("dropped"); };
  });
}


function beginEdit(td, col, seed) {
  clearGridGenusWarn();
  if (S.editing) commitEdit();
  const row = rowById(+td.dataset.rid);
  if (!row) return;
  const oldV = row.data[col.name];
  // 锁定枚举：无值自动填默认值；只能选择不可键入
  const useSelect = col.enum_locked && col.options;
  if (col.default && (oldV === null || oldV === undefined || oldV === "")) {
    row.data[col.name] = colDefaultVal(col);   // {{me}} 展开为当前账号（v4.03）
  }
  const useDatalist = useSelect || col.options || (col.cascade && S.cascade);
  // 联想列强制单行 input（textarea 不支持 datalist 联想）
  const forceInput = (curCfg().genus_lookup && (col.name === "Genus" || isFinalNameCol(col.name) || isTempNameCol(col.name)))
    || (curCfg().wac_lookup && (col.name === "属" || col.name === "种"));
  const input = document.createElement(useSelect ? "select" : ((col.wide && !useDatalist && !forceInput) ? "textarea" : "input"));
  if (input.tagName === "INPUT") input.type = "text";
  input.value = seed !== undefined ? seed : (oldV === null || oldV === undefined ? "" : oldV);
  td.innerHTML = "";
  td.appendChild(input);
  if (useDatalist) {
    const dl = document.createElement("datalist");
    dl.id = "dl" + Math.random().toString(36).slice(2);
    let opts = col.options ? col.options.map(String) : cascadeOptions(col.name, row.data) || [];
    for (const o of opts) {
      const op = document.createElement("option");
      op.value = o;
      dl.appendChild(op);
    }
    td.appendChild(dl);
    input.setAttribute("list", dl.id);
    input._dl = dl;
    if (col.name === "Genus" && curCfg().genus_lookup) {
      attachGenusSuggest(input);
      let gTimer = null;
      const gCheck = async () => {
        const g = input.value.trim();
        if (!g) { clearGridGenusWarn(); return; }
        try {
          const r = await api(`/api/genus-check?genus=${encodeURIComponent(g)}`);
          if (r.known) { clearGridGenusWarn(); return; }
          showGridGenusWarn(td, `⚠ 属名「${g}」未在 WSC/分类信息表收录——请核对拼写`
            + (r.suggestion ? `（相近：${r.suggestion}${r.suggestion_family ? "，" + r.suggestion_family : ""}）` : "")
            + "；确为新属可继续保存");
        } catch { }
      };
      input.addEventListener("input", () => { clearTimeout(gTimer); gTimer = setTimeout(gCheck, 600); });
      input.addEventListener("blur", gCheck);
    }
    if (isTempNameCol(col.name)) attachTempNameInput(input);
  }
  if (curCfg().wac_lookup) {
    if (col.name === "属") attachWacGenus(input);
    else if (col.name === "种") attachWacEpithet(input, () => rowById(row.id)?.data["属"] || "");
  }
  if (curCfg().genus_lookup && !useDatalist) {
    if (col.name === "Genus") {
      attachGenusSuggest(input);
      let gTimer = null;
      const gCheck = async () => {
        const g = input.value.trim();
        if (!g) { clearGridGenusWarn(); return; }
        try {
          const r = await api(`/api/genus-check?genus=${encodeURIComponent(g)}`);
          if (r.known) { clearGridGenusWarn(); return; }
          showGridGenusWarn(td, `⚠ 属名「${g}」未在 WSC/分类信息表收录——请核对拼写`
            + (r.suggestion ? `（相近：${r.suggestion}${r.suggestion_family ? "，" + r.suggestion_family : ""}）` : "")
            + "；确为新属可继续保存");
        } catch { }
      };
      input.addEventListener("input", () => { clearTimeout(gTimer); gTimer = setTimeout(gCheck, 600); });
      input.addEventListener("blur", gCheck);
    }
    else if (isFinalNameCol(col.name)) attachSpeciesSuggest(input);
  }
  if (S.cascade && S.cascade[S.t] && TAX_SUGGEST_COLS.includes(col.name)) attachTaxValueSuggest(input, col.name);
  // 采集事件号联想：textarea/input 分支都挂（datalist 分支已挂属名/暂定名）
  if (col.name === "CollectionEventNumber" || col.name === "采集编号") attachEventSuggest(input);
  if (useSelect) {
    // 锁定枚举：select 只含选项（含空），不可键入
    input.innerHTML = `<option value="">（空）</option>` +
      col.options.map((o) => `<option value="${esc(String(o))}" ${String(o) === String(row.data[col.name]) ? "selected" : ""}>${esc(o)}</option>`).join("");
  }
  if (isTempNameCol(col.name)) {
    // 无 datalist 时也要有 cf. 按钮
    const cfBtn = attachTempNameInput(input);
    td.appendChild(cfBtn);
    input.style.width = "calc(100% - 44px)";
    cfBtn.style.width = "40px";
    cfBtn.style.verticalAlign = "top";
  }
  input.focus();
  // v4.35：点击/F2/回车进入编辑时光标落在末尾（不再全选）——退格直接删末字符，
  // 点文字中间可改单字符；整格替换 = 输入框内 Ctrl+A，或方向键导航到格后直接打字。
  // 直接打字（seed）沿用替换语义；锁定枚举列是 select，无光标概念
  if (input.setSelectionRange) input.setSelectionRange(input.value.length, input.value.length);
  // 标本盒列：下拉只列未用满的盒（含当前值）
  if (S.t === "specimens" && col.name === "Box # (Start#-End#)" && S.boxes) {
    const sel = document.createElement("select");
    sel.style.cssText = "width:100%;border:2px solid var(--primary);border-radius:4px;padding:2px 6px;font-size:13px;";
    sel.innerHTML = availableBoxOptions(input.value);
    td.innerHTML = "";
    td.appendChild(sel);
    sel.focus();
    S.editing = { td, row, col, input: sel, oldTd: `<span class="val">${fmtV(oldV)}</span>` };
    sel.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { e.stopPropagation(); cancelEdit(); }
      else if (e.key === "Enter") { e.preventDefault(); commitEdit(true); }
      e.stopPropagation();
    });
    sel.addEventListener("blur", () => setTimeout(() => { if (S.editing && S.editing.input === sel) commitEdit(); }, 120));
    sel.addEventListener("change", () => setTimeout(() => { if (S.editing && S.editing.input === sel) commitEdit(); }, 80));
    return;
  }
  // 标本编号列：实时预览前缀补全效果
  if (S.t === "specimens" && col.name === curCfg().key) {
    const preview = document.createElement("div");
    preview.className = "key-preview";
    const upd = () => {
      const nv = normalizeSpecKey(input.value);
      preview.textContent = nv !== input.value ? "将保存为 " + nv : "";
    };
    input.addEventListener("input", upd);
    upd();
    td.appendChild(preview);
  }
  S.editing = { td, row, col, input, oldTd: `<span class="val">${fmtV(oldV)}</span>`, state: "draft" };
  // 中文输入法：组合输入期间 Enter/Tab 属于候选操作，不触发单元格提交与移动
  input.addEventListener("compositionstart", () => { input._composing = true; });
  input.addEventListener("compositionend", () => { setTimeout(() => { input._composing = false; }, 0); });
  // 草稿：输入即存本机（断网/误关可恢复；按 账号|表|记录|字段|基础版本 隔离，留存 7 天）
  input.addEventListener("input", () => {
    if (String(input.value) !== String(row.data[col.name] ?? ""))
      draftPut(S.t, row.id, col.name, row.row_version, input.value,
               row.data[curCfg().key] || ("#" + row.id));
  });
  // 恢复草稿：同账号同表同记录同字段且基础版本一致才询问；版本已变的陈旧草稿绝不自动套用
  {
    const d = draftGet(S.t, row.id, col.name, row.row_version);
    const ovS = oldV === null || oldV === undefined ? "" : String(oldV);
    if (d && String(d.value) !== String(input.value) && String(d.value) !== ovS) {
      const bar = document.createElement("div");
      bar.className = "edit-bar draft";
      bar.innerHTML = `<div class="edit-msg">发现本机草稿（${esc(d.label || "")}，留存 7 天）</div>
        <div class="edit-btns">
          <button class="btn primary" data-dk="restore">恢复草稿</button>
          <button class="btn" data-dk="discard">忽略</button>
        </div>`;
      td.insertBefore(bar, input);
      bar.querySelector('[data-dk="restore"]').onclick = () => {
        input.value = d.value;
        input.dispatchEvent(new Event("input"));
        bar.remove();
        input.focus();
      };
      bar.querySelector('[data-dk="discard"]').onclick = () => {
        draftDrop(S.t, row.id, col.name, row.row_version);
        bar.remove();
        input.focus();
      };
    }
  }
  input.addEventListener("keydown", (e) => {
    if (input._composing || e.isComposing) return;   // 输入法组合中：交给输入法处理
    const queueOrGo = (r, c) => {
      if (S.editing && S.editing.state === "submitting") { S.editing.pendingMove = [r, c]; return; }  // 提交中只排队
      commitEdit(r, c);
    };
    if (e.key === "Escape") cancelEdit();
    else if (e.key === "Enter" && (input.tagName === "INPUT" || e.ctrlKey)) { e.preventDefault(); queueOrGo(1, 0); }
    else if (e.key === "ArrowDown" && input.tagName === "INPUT") { e.preventDefault(); queueOrGo(1, 0); }
    else if (e.key === "ArrowUp" && input.tagName === "INPUT") { e.preventDefault(); queueOrGo(-1, 0); }
    else if (e.key === "Tab" && input.tagName === "INPUT") { e.preventDefault(); queueOrGo(0, e.shiftKey ? -1 : 1); }
    e.stopPropagation();
  });
  input.addEventListener("blur", () => { if (!(S.editing && S.editing.state === "submitting")) commitEdit(); });
  input.addEventListener("input", () => {
    if (input.tagName === "TEXTAREA") {
      input.style.height = "auto";
      input.style.height = Math.min(300, input.scrollHeight + 4) + "px";
    }
  });
}

function cancelEdit(force) {
  clearGridGenusWarn();
  const ed = S.editing;
  if (!ed) return;
  if (!force && (ed.state === "submitting" || ed.state === "pending_confirm")) return; // 不打断提交中/待确认
  S.editing = null;
  ed.td.innerHTML = ed.oldTd;
}

async function commitEdit(dRow, dCol) {
  clearGridGenusWarn();
  const ed = S.editing;
  if (!ed) return;
  // 提交中：只排队移动，绝不重复提交同一编辑
  if (ed.state === "submitting") { if (dRow || dCol) ed.pendingMove = [dRow, dCol]; return; }
  // 失败/待确认：由操作条（重试/放弃）处理，Enter/Tab 不隐式重发
  if (ed.state === "failed" || ed.state === "pending_confirm") return;
  const { td, row, col, input } = ed;
  const ov = row.data[col.name];
  let nv = input.value;
  if (S.t === "specimens" && col.name === curCfg().key) nv = normalizeSpecKey(nv);
  // 暂定名保存前 WSC 核对提醒（未收录名称弹提醒，可确认保留）
  if (isTempNameCol(col.name) && nv.trim() && nv !== (ov ?? "")) {
    const chk = await confirmTempName(nv, input);
    if (!chk) { td.innerHTML = `<span class="val">${fmtV(ov)}</span>`; if (dRow || dCol) moveCur(dRow, dCol); return; }
    nv = input.value;  // 模糊建议点选后采用替换名
  }
  if ((ov ?? "") === nv.trim() || (ov ?? "") === nv) {
    td.innerHTML = `<span class="val">${fmtV(ov)}</span>`;
    draftDrop(S.t, row.id, col.name, row.row_version);
    if (S.editing === ed) S.editing = null;
    if (dRow || dCol) moveCur(dRow, dCol);
    return;
  }
  draftPut(S.t, row.id, col.name, row.row_version, nv,
           row.data[curCfg().key] || ("#" + row.id));   // 提交前草稿先落本机
  ed.pendingMove = (dRow || dCol) ? [dRow, dCol] : null;
  const myTable = S.t;
  const r = await submitEdit(ed, nv);
  if (!r || S.t !== myTable) return;
  // 成功后的既有联动（级联字段已由 applyEditSuccess 原位应用，无需整表刷新）
  if (col.name === "Genus" && curCfg().genus_lookup && !r.warn) toast("已按分类信息带出上级分类", "ok");
  if (S.t === "uce" && col.name === "Sample Code") {
    sraTryFill(null, nv.trim(), row.data["Species name"], row);
  }
  if (S.t === "mygalomorph" && col.name === "MHBU #") {
    const code = nv.trim();
    if (code.length >= 3) {
      api(`/api/specimen-link?table=${S.t}&code=${encodeURIComponent(code)}`).then((lr) => {
        if (!lr.found) return;
        const changes = {};
        for (const [k, v] of Object.entries(lr.fill || {})) {
          const cur = (row.data[k] ?? "").toString().trim();
          if (!cur) { changes[k] = v; }
        }
        if (!Object.keys(changes).length) return;
        api(`/api/rows/${S.t}/${row.id}`, { method: "PUT", body: JSON.stringify({ changes }) })
          .then((rr) => {
            for (const [k, pair] of Object.entries(rr.changes || {})) {
              if (Array.isArray(pair) && pair.length === 2) row.data[k] = pair[1];
            }
            row.row_version = rr.row_version || row.row_version;
            for (const k of Object.keys(rr.changes || {})) {
              const ctd = document.querySelector(`#grid-body td[data-rid="${row.id}"][data-col="${CSS.escape(k)}"]`);
              if (ctd) ctd.innerHTML = `<span class="val">${cellHtml(k, displayVal(k, row.data[k]), row)}</span>`;
            }
            toast(`已按编号带出物种：${lr.species}（${Object.keys(rr.changes || {}).join("、")}）`, "ok");
          }).catch(() => { });
      }).catch(() => { });
    }
  }
}

function showConflict(row, colName, myVal, conflict, seenVersion) {
  // 字段级三方信息：base=您编辑时看到的值 / current=服务器现值 / proposed=您想改成
  const f = (conflict.fields && conflict.fields[colName]) || {};
  const curV = f.current !== undefined ? f.current : conflict.values[colName];
  const baseV = f.base !== undefined ? f.base : (row.data[colName] ?? "");
  const seen = seenVersion !== undefined ? seenVersion : conflict.row_version;
  const others = conflict.fields ? Object.keys(conflict.fields).filter((k) => k !== colName) : [];
  openModal("编辑冲突", `
    <p class="hint warn">注意：${esc(conflict.by)} 于 ${esc(conflict.at)} 修改了这一行（可能是另一位协作者）。为避免覆盖别人的修改，请选择如何处理：</p>
    <div class="conflict-val">字段「${esc(colName)}」您编辑时的值：<b>${fmtV(baseV)}</b></div>
    <div class="conflict-val">现在服务器上的值：<b>${fmtV(curV)}</b>（最后修改：${esc(f.by || conflict.by)}${f.at ? " · " + esc(f.at) : ""}）</div>
    <div class="conflict-val">您想改成：<b>${fmtV(myVal)}</b></div>
    ${others.length ? `<div class="conflict-val">同批提交中还有 ${others.length} 个字段也存在冲突：${esc(others.join("、"))}</div>` : ""}
    <div class="form-actions">
      <button class="btn" id="cf-drop">放弃我的修改</button>
      <button class="btn" id="cf-refresh">查看当前值</button>
      <button class="btn primary" id="cf-over">仍要覆盖</button>
    </div>`);
  $("cf-drop").onclick = () => { closeModal(); loadTable(); };
  $("cf-refresh").onclick = () => { closeModal(); loadTable(); };
  $("cf-over").onclick = async () => {
    // 覆盖必须携带「本次看到的版本」；确认期间若又被第三人修改（版本变化），服务器再次 409 重新确认
    closeModal();
    try {
      const r = await api(`/api/rows/${S.t}/${row.id}`, {
        method: "PUT",
        body: JSON.stringify({ changes: { [colName]: myVal }, force: true,
                               base_version: seen, overwrite_reason: "用户在冲突提示中选择覆盖" }),
      });
      row.updated_at = r.updated_at;
      row.row_version = r.row_version;
      toast("已覆盖保存（覆盖原因与被覆盖修订已记录）", "ok");
      if (r.changes && r.changes[colName]) row.data[colName] = r.changes[colName][1];
      loadTable();
    } catch (e) {
      if (e.conflict) { showConflict(row, colName, myVal, e.conflict, e.conflict.row_version); return; }
      toast(e.message, "err");
    }
  };
}

/* ---- 键盘导航 ---- */
function visibleCellTds() {
  const trs = [...$("grid-body").querySelectorAll("tr")];
  return trs;
}
function syncCurColTh() {
  document.querySelectorAll("#grid th.cur-col").forEach((x) => x.classList.remove("cur-col"));
  if (!S.cur) return;
  const th = document.querySelector(`#grid-head th[data-col="${CSS.escape(S.cur.col)}"]`);
  if (th) th.classList.add("cur-col");
}
function setCur(td) {
  document.querySelectorAll("#grid td.cell.cur").forEach((x) => x.classList.remove("cur"));
  if (!td) { S.cur = null; syncCurColTh(); return; }
  td.classList.add("cur");
  S.cur = { rid: +td.dataset.rid, col: td.dataset.col };
  syncCurColTh();
  td.scrollIntoView({ block: "nearest", inline: "nearest" });
}
function curTd() {
  if (!S.cur) return null;
  const td = $("grid-body").querySelector(`td.cell[data-rid="${S.cur.rid}"][data-col="${CSS.escape(S.cur.col)}"]`);
  return td;
}
function moveCur(dRow, dCol) {
  if (!S.cur) {
    const first = $("grid-body").querySelector("td.cell");
    if (first) setCur(first);
    return;
  }
  const cols = visibleCols().map((c) => c.name);
  let ri = S.rows.findIndex((r) => r.id === S.cur.rid);
  let ci = cols.indexOf(S.cur.col);
  if (ci < 0) ci = 0;
  ri = Math.max(0, Math.min(S.rows.length - 1, ri + (dRow || 0)));
  ci = Math.max(0, Math.min(cols.length - 1, ci + (dCol || 0)));
  const row = S.rows[ri];
  if (!row) return;
  const td = $("grid-body").querySelector(`td.cell[data-rid="${row.id}"][data-col="${CSS.escape(cols[ci])}"]`);
  if (td) setCur(td);
}
document.addEventListener("keydown", (e) => {
  if (S.t === "home" || S.t === "history" || S.t === "checkup" || S.t === "stats" || S.t === "about" || S.t === "tplexp" || S.t === "collab" || S.t === "movements" || S.t === "labpage" || S.t === "biblio" || S.t === "projects" || S.t === "recent") return;
  if (!$("modal").hidden || !$("drawer").hidden || !$("login-view").hidden) return;
  const tag = (e.target.tagName || "").toLowerCase();
  if (tag === "input" || tag === "textarea" || tag === "select") return;
  if (!S.cur) {
    if (["ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight", "Enter", "F2"].includes(e.key)) {
      moveCur(0, 0);
      e.preventDefault();
    }
    return;
  }
  else if (e.key === "Tab") { e.preventDefault(); moveCur(0, e.shiftKey ? -1 : 1); }
  else if (e.key === "ArrowDown") { e.preventDefault(); moveCur(1, 0); }
  else if (e.key === "ArrowUp") { e.preventDefault(); moveCur(-1, 0); }
  else if (e.key === "ArrowLeft") { e.preventDefault(); moveCur(0, -1); }
  else if (e.key === "ArrowRight") { e.preventDefault(); moveCur(0, 1); }
  else if (e.key === "Enter" || e.key === "F2") {
    e.preventDefault();
    const td = curTd();
    const col = S.columns.find((c) => c.name === S.cur.col);
    if (td && col && (!col.linked || col.name === curCfg().linked_key) && canWriteTable(S.t) && !S.showDeleted) beginEdit(td, col);
  }
  else if (e.key === "Escape") setCur(null);
  else if ((e.ctrlKey || e.metaKey) && !e.shiftKey && e.key.toLowerCase() === "c") {
    const row = rowById(S.cur.rid);
    if (row) copyText(String(row.data[S.cur.col] ?? ""));
  } else if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === "c") {
    copyRow(S.cur.rid);
  } else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
    const td = curTd();
    const col = S.columns.find((c) => c.name === S.cur.col);
    if (td && col && (!col.linked || col.name === curCfg().linked_key) && canWriteTable(S.t) && !S.showDeleted) beginEdit(td, col, e.key);
  }
});

/* ---- 排序 / 筛选入口 / 隐藏列 ---- */
$("grid-head").addEventListener("click", (e) => {
  const fbtn = e.target.closest(".th-filter");
  if (fbtn) { openFilterPop(fbtn.dataset.fcol, fbtn); return; }
  const hbtn = e.target.closest(".th-hide");
  if (hbtn) {
    const set = getHidden();
    set.add(hbtn.dataset.hidecol);
    setHidden(set);
    renderGrid();
    $("hide-count").textContent = set.size || "";
    return;
  }
  const th = e.target.closest("th[data-col]");
  if (!th) return;
  const c = th.dataset.col;
  if (S.sort === c) S.dir = S.dir === "asc" ? "desc" : "asc";
  else { S.sort = c; S.dir = "asc"; }
  loadTable();
});

/* ---- 列筛选 ---- */
function filterSummary(f) {
  if (f.mode === "in") {
    const vs = f.values.filter((v) => v !== "（空）");
    const base = vs.length <= 2 ? (vs.join("、") || "空") : `${f.values.length} 项`;
    return (f.values.includes("（空）") && vs.length ? "+" : "") + (f.values.includes("（空）") && !vs.length ? "（空）" : base);
  }
  if (f.mode === "contains") return `含“${f.text}”`;
  if (f.mode === "empty") return "为空";
  if (f.mode === "notempty") return "非空";
  return "";
}

function renderFilterChips() {
  const box = $("filter-chips");
  const keys = Object.keys(S.filters);
  box.hidden = keys.length === 0;
  const views = ((S.prefs.views || {})[S.t]) || [];
  const viewSel = views.length
    ? `<select id="view-pick" style="padding:3px 6px;font-size:12px"><option value="">我的视图…</option>` +
      views.map((v, i) => `<option value="${i}">${esc(v.name)}（${v.n} 列筛选）</option>`).join("") + `</select>`
    : "";
  const typeChip = S.preset === "types"
    ? `<span class="fchip"><b>模式标本</b>（预置筛选）<span class="x" id="types-x" title="取消模式标本筛选">✕</span></span>` : "";
  box.innerHTML = (keys.length || S.preset === "types"
    ? `<span class="stat">已启用筛选（与搜索叠加）：</span>` + keys.map((c) => `
    <span class="fchip"><b>${esc(colDisp(c))}</b> ${esc(filterSummary(S.filters[c]))}<span class="x" data-c="${esc(c)}" title="移除该列筛选">✕</span></span>`).join("")
    + typeChip + `<button class="btn" id="fclear-all">清除全部</button>`
    : `<span class="stat">无筛选。</span>`)
    + ` <button class="btn" id="view-save" ${keys.length ? "" : "disabled"} title="把当前筛选组合保存为命名视图，之后一键调用">保存为视图</button>
      <button class="btn" id="btn-view-share" title="分享视图定义（只含筛选/列定义，不含数据；对方按其权限重算）">分享/导入视图</button>${viewSel}`;
  box.querySelectorAll(".x[data-c]").forEach((x) => x.onclick = () => {
    delete S.filters[x.dataset.c];
    loadTable();
  });
  const tx = document.getElementById("types-x");
  if (tx) tx.onclick = () => { S.preset = ""; loadTable(); };
  const all = $("fclear-all");
  if (all) all.onclick = () => { S.filters = {}; loadTable(); };
  $("view-save").onclick = () => {
    if (!keys.length) return;
    openModal(`保存视图 · ${curCfg().label}`, `
      <p class="hint">把当前 ${keys.length} 列筛选组合保存为命名视图（如"看某属模式标本"），存在您的账号下，换电脑也生效。</p>
      <div class="grid-form"><label>视图名称<input id="view-name" placeholder="如：云南未鉴定"></label></div>
      <div class="form-actions"><button class="btn" id="view-cancel">取消</button>
        <button class="btn primary" id="view-ok">保存</button></div>`);
    $("view-cancel").onclick = closeModal;
    $("view-ok").onclick = () => {
      const name = ($("view-name").value || "").trim();
      if (!name) return toast("请填写视图名称", "err");
      if (!S.prefs.views) S.prefs.views = {};
      if (!S.prefs.views[S.t]) S.prefs.views[S.t] = [];
      S.prefs.views[S.t].push({ name,
        filters: JSON.parse(JSON.stringify(S.filters)), n: keys.length,
        hidden: [...getHidden()], colorder: (S.prefs.colorder || {})[S.t] || [],
        colw: (S.prefs.colw || {})[S.t] || {}, freeze: (S.prefs.freeze || {})[S.t] || [],
        density: document.body.classList.contains("density-compact") ? "compact" : "cozy" });
      savePrefs().then(() => { closeModal(); toast("视图已保存", "ok"); renderFilterChips(); });
    };
  };
  const shareBtn = $("btn-view-share");
  if (shareBtn) shareBtn.onclick = openViewShare;
  const pick = $("view-pick");
  if (pick) pick.onchange = async () => {
    const v = views[+pick.value];
    if (!v) return;
    S.filters = JSON.parse(JSON.stringify(v.filters || {}));
    // 我的视图：一并还原 列集合/列序/列宽/固定列/密度
    if (v.hidden) { S.prefs.hidden = S.prefs.hidden || {}; S.prefs.hidden[S.t] = v.hidden; }
    if (v.colorder) { S.prefs.colorder = S.prefs.colorder || {}; S.prefs.colorder[S.t] = v.colorder; }
    if (v.colw) { S.prefs.colw = S.prefs.colw || {}; S.prefs.colw[S.t] = v.colw; }
    if (v.freeze) { S.prefs.freeze = S.prefs.freeze || {}; S.prefs.freeze[S.t] = v.freeze; }
    if (v.density) {
      document.body.classList.toggle("density-compact", v.density === "compact");
      try { localStorage.setItem("sm_density", v.density === "compact" ? "compact" : "cozy"); } catch { }
    }
    try { await savePrefs(); } catch { }
    S.page = 1;
    loadTable();
    toast(`已应用视图「${v.name}」`, "ok");
  };
}

function setColFilter(col, f) {
  if (f) S.filters[col] = f;
  else delete S.filters[col];
  $("filter-pop").hidden = true;
  loadTable();
}

async function openFilterPop(col, anchor) {
  const pop = $("filter-pop");
  const f = S.filters[col] || null;
  pop.innerHTML = `
    <div class="fp-head"><b title="${esc(col)}">筛选 · ${esc(col)}</b>
      <button class="btn" id="fp-clear">清除</button></div>
    <input id="fp-search" type="search" placeholder="搜索值…">
    <div class="fp-list" id="fp-list"><p class="hint" style="padding:6px">加载中…</p></div>
    <div class="fp-extra">
      <input id="fp-text" type="text" placeholder="包含文本…"
        value="${f && f.mode === "contains" ? esc(f.text || "") : ""}">
      <button class="btn" id="fp-text-ok">包含</button>
      <button class="btn" id="fp-empty">为空</button>
      <button class="btn" id="fp-notempty">非空</button>
    </div>`;
  pop.hidden = false;
  const rect = anchor.getBoundingClientRect();
  pop.style.left = Math.max(8, Math.min(rect.left - 40, window.innerWidth - 330)) + "px";
  pop.style.top = Math.min(rect.bottom + 6, window.innerHeight - 480) + "px";

  const applyAndRefresh = () => { loadTable(); };
  $("fp-clear").onclick = () => setColFilter(col, null);
  $("fp-empty").onclick = () => setColFilter(col, { mode: "empty" });
  $("fp-notempty").onclick = () => setColFilter(col, { mode: "notempty" });
  const commitText = () => {
    const t = $("fp-text").value.trim();
    t ? setColFilter(col, { mode: "contains", text: t }) : setColFilter(col, null);
  };
  $("fp-text-ok").onclick = commitText;
  $("fp-text").addEventListener("keydown", (e) => { if (e.key === "Enter") commitText(); });

  let vals = [], emptyN = 0, truncated = false;
  try {
    const params = new URLSearchParams({ col });
    if (S.showDeleted) params.set("deleted", "1");
    const r = await api(`/api/rows/${S.t}/values?` + params);
    vals = r.values; emptyN = r.empty; truncated = r.truncated;
  } catch (e) {
    $("fp-list").innerHTML = `<p class="hint" style="padding:6px">${esc(e.message)}</p>`;
    return;
  }
  const checkedSet = f && f.mode === "in" ? new Set(f.values) : null;
  function renderList(kw = "") {
    const k = kw.trim().toLowerCase();
    const rows = [];
    if (emptyN > 0 && (!k || "空".includes(k) || "（空）".includes(k)))
      rows.push(`<label><input type="checkbox" value="（空）" ${checkedSet && checkedSet.has("（空）") ? "checked" : ""}>
        <span class="fp-val">（空）</span><span class="cnt">${emptyN}</span></label>`);
    for (const [v, n] of vals) {
      if (k && !v.toLowerCase().includes(k)) continue;
      rows.push(`<label><input type="checkbox" value="${esc(v)}" ${checkedSet && checkedSet.has(v) ? "checked" : ""}>
        <span class="fp-val" title="${esc(v)}">${esc(v)}</span><span class="cnt">${n}</span></label>`);
    }
    $("fp-list").innerHTML = rows.join("")
      || `<p class="hint" style="padding:6px">没有匹配的值</p>`
      + (truncated && k ? "" : truncated ? `<p class="hint" style="padding:2px 6px">仅显示前 1000 个值，可用上方搜索缩小范围</p>` : "");
  }
  renderList();
  $("fp-search").addEventListener("input", (e) => renderList(e.target.value));
  $("fp-list").addEventListener("change", () => {
    const checked = [...$("fp-list").querySelectorAll("input:checked")].map((i) => i.value);
    if (checked.length) S.filters[col] = { mode: "in", values: checked };
    else delete S.filters[col];
    loadTable();  // 保持浮层打开，便于继续勾选
  });
}
$("more-btn").onclick = (e) => {
  e.stopPropagation();
  $("more-menu").hidden = !$("more-menu").hidden;
};
document.addEventListener("click", (e) => {
  // 逐元素空值防护：任一菜单节点缺失时不得中断后续收起（v3.94 修复 colmenu 缺失
  // 导致 TypeError、用户菜单点外不收起）
  const mm = $("more-menu"); if (mm && !e.target.closest(".more-wrap")) mm.hidden = true;
  const cm = $("colmenu"); if (cm && !e.target.closest(".colmenu-wrap")) cm.hidden = true;
  const ud = $("user-dropdown"); if (ud && !e.target.closest(".user-menu")) ud.hidden = true;
  const fp = $("filter-pop"); if (fp && !e.target.closest("#filter-pop") && !e.target.closest(".th-filter")) fp.hidden = true;
});

/* ---- 搜索 / 刷新 / 回收站 / 借出 ---- */
let searchTimer = null;
$("search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { S.q = e.target.value.trim(); S.page = 1; loadTable(); }, 300);
});
$("search").addEventListener("keydown", (e) => {
  if (e.key === "Escape" && $("search").value) {
    $("search").value = ""; S.q = ""; S.page = 1;
    clearTimeout(searchTimer);
    loadTable();
  }
});
$("btn-refresh").onclick = () => { loadTable(); refreshMetaCounts(); };
$("btn-deleted").onclick = () => {
  S.showDeleted = !S.showDeleted;
  $("btn-deleted").classList.toggle("primary", S.showDeleted);
  $("btn-bin-tools").hidden = !(S.showDeleted && S.me.admin);
  S.page = 1;
  loadTable();
};
$("btn-loaned").onclick = () => {
  S.preset = S.preset === "loaned" ? "" : "loaned";
  $("btn-loaned").classList.toggle("primary", S.preset === "loaned");
  S.page = 1;
  loadTable();
};

/* ---- 鉴定联动：一键以权威表（DNA 提取台账）为准同步全表 ---- */
$("btn-ident-sync").onclick = async () => {
  const ident = curCfg().ident_sync;
  if (!ident) return;
  const lbl = ident.peer_label || ident.peer;
  const n = ($("dna-diff-count") || {}).textContent || "";
  const ok = await confirmBox(
    `以「${esc(lbl)}」为准同步本表全部鉴定差异？\n\n`
    + `将把本表中与「${esc(lbl)}」不一致的属/暂定名/最终名更新为权威值`
    + (n ? `（当前标记 ${n.trim()} 行）` : "")
    + `；未关联到权威表的行不受影响。整批操作可一次性撤销。`,
    { okText: "开始同步" });
  if (!ok) return;
  try {
    const r = await api(`/api/rows/${S.t}/sync_from_authority`,
                        { method: "POST", body: JSON.stringify({}) });
    const failN = (r.failed || []).length;
    toast(`同步完成：更新 ${r.synced} 行`
      + (r.noop ? `、${r.noop} 行本已一致` : "")
      + (r.unlinked ? `、${r.unlinked} 行未关联到「${lbl}」` : "")
      + (failN ? `、${failN} 行失败` : ""), failN ? "warn" : "ok");
  } catch (err) { toast(err.message, "err"); }
  loadTable();
};

/* ---- 列显示 ---- */
$("btn-cols").onclick = (e) => {
  e.stopPropagation();
  $("menu-manage").hidden = true;  // 从管理菜单进入：先收起菜单，列面板锚定同一位置
  const menu = $("colmenu");
  if (!menu.hidden) return (menu.hidden = true);
  const hid = getHidden();
  menu.innerHTML = `
    <div class="colmenu-tools">
      <input id="col-filter" type="search" placeholder="输入筛选字段名…" style="flex:1;min-width:110px;padding:4px 8px;border:1px solid var(--border);border-radius:6px;font-size:12px">
      <button class="btn" id="col-show-all">显示全部</button>
      <span class="stat">已显示 ${S.columns.length - hid.size}/${S.columns.length} 列</span>
    </div>
    ${S.columns.map((c) => `
      <label data-colname="${esc(c.name).toLowerCase()}"><input type="checkbox" data-col="${esc(c.name)}" ${hid.has(c.name) ? "" : "checked"}>
        ${esc(c.name)}${c.linked ? ' <span class="tag">关联</span>' : ""}</label>`).join("")}`;
  menu.hidden = false;
  const colFilter = menu.querySelector("#col-filter");
  if (colFilter) {
    colFilter.focus();
    colFilter.oninput = () => {
      const q = colFilter.value.trim().toLowerCase();
      menu.querySelectorAll("label[data-colname]").forEach((lb) =>
        lb.style.display = !q || lb.dataset.colname.includes(q) ? "" : "none");
    };
  }
  menu.querySelectorAll("input[type=checkbox]").forEach((cb) =>
    cb.onchange = () => {
      const set = getHidden();
      cb.checked ? set.delete(cb.dataset.col) : set.add(cb.dataset.col);
      setHidden(set);
      $("hide-count").textContent = set.size || "";
      renderGrid();
    });
  const showAll = $("col-show-all");
  if (showAll) showAll.onclick = async () => {
    setHidden(new Set());
    // 恢复默认：同时清除列序与固定列设置（显式 __delete__，浅合并协议下本地 delete 传不到服务器）
    if (S.prefs.colorder) delete S.prefs.colorder[S.t];
    if (S.prefs.freeze) delete S.prefs.freeze[S.t];
    S.prefs.view_sel = S.prefs.view_sel || {};
    delete S.prefs.view_sel[S.t];
    try { await savePrefs([], [`colorder.${S.t}`, `freeze.${S.t}`]); } catch { }
    const tv = $("task-view");
    if (tv) tv.value = "";
    $("btn-cols").click(); $("btn-cols").click();
  };
};
$("more-btn").onclick = (e) => {
  e.stopPropagation();
  $("more-menu").hidden = !$("more-menu").hidden;
};
document.addEventListener("click", (e) => {
  const mm = $("more-menu"); if (mm && !e.target.closest(".more-wrap")) mm.hidden = true;
  const cm = $("colmenu"); if (cm && !e.target.closest(".colmenu-wrap")) cm.hidden = true;
  const ud = $("user-dropdown"); if (ud && !e.target.closest(".user-menu")) ud.hidden = true;
});

/* ---- 导出 / 导入 / 粘贴 ---- */
/* 统一导出向导入口（v3.73）：勾选 / 当前筛选 / 全表，范围以稳定计划绑定 */
$("btn-export-wizard").onclick = () => openExportWizard({
  context: "table",
  tname: S.t,
  grid: { q: S.q, filters: JSON.parse(JSON.stringify(S.filters || {})), preset: S.preset,
          deleted: S.showDeleted, selIds: [...S.sel],
          total: S.total },
});
/* 从当前筛选创建动态项目（v3.74）：筛选器预填当前查询，实时匹配计数 */
$("btn-dynproj").onclick = () => {
  const pre = { q: S.q || "", filters: JSON.parse(JSON.stringify(S.filters || {})) };
  const n = Object.keys(pre.filters).length;
  openModal("从当前筛选创建动态项目", `
    <p class="hint">把当前表格筛选（${pre.q ? `搜索「${esc(pre.q)}」` : "无搜索词"}${n ? ` ＋ ${n} 个列条件` : ""}）固化为动态项目的成员规则；成员随数据实时变化，可随时在项目里固化成员版本。</p>
    <label>项目名称 *<input id="dp-name" class="input" placeholder="如 2026 云南省系统发育"></label>
    <label>研究目的<input id="dp-purpose" class="input"></label>
    <div id="dp-fb"></div>
    <div class="form-actions"><button class="btn" id="dp-cancel">取消</button>
      <button class="btn primary" id="dp-go">创建动态项目</button></div>`);
  const fb = renderFilterBuilder($("dp-fb"), { tname: "specimens", value: pre });
  $("dp-cancel").onclick = closeModal;
  $("dp-go").onclick = async () => {
    const name = $("dp-name").value.trim();
    if (!name) { toast("请填写项目名称", "err"); return; }
    $("dp-go").disabled = true;
    try {
      await api("/api/projects", { method: "POST", body: JSON.stringify({
        name, purpose: $("dp-purpose").value.trim(), kind: "dynamic", filters: fb.get() }) });
      closeModal(); toast("动态项目已创建（成员随筛选实时解析）", "ok");
      switchView("projects");
    } catch (e) { toast(e.message, "err"); $("dp-go").disabled = false; }
  };
};

/* 固化当前筛选为稳定计划（与导出/批量同一查询表达），返回 {ids, count, truncated, cap} */
async function resolveCurrentPlan() {
  const p = new URLSearchParams({ q: S.q, sort: S.sort, dir: S.dir, resolve: "ids" });
  if (S.showDeleted) p.set("deleted", "1");
  if (S.preset) p.set("preset", S.preset);
  if (Object.keys(S.filters).length) p.set("filters", JSON.stringify(S.filters));
  const pl = await api(`/api/rows/${S.t}?` + p);
  if (pl.truncated) throw new Error(`筛选命中 ${pl.count} 行，超出单次处理上限 ${pl.cap}——请缩小范围`);
  if (!pl.ids.length) throw new Error("当前筛选没有命中行——请先筛选出要处理的标本");
  return pl;
}

/* 当前筛选 → 协作编辑包：计划固化后建包（权威基线存服务器） */
$("btn-collab-filtered").onclick = async () => {
  let pl;
  try { pl = await resolveCurrentPlan(); } catch (e) { toast(e.message, "err"); return; }
  openModal(`协作编辑包 · 当前筛选 ${pl.ids.length} 行（已固化为稳定计划）`, `
    <p class="hint">行集合已按当前筛选固化：之后筛选变化或新增记录不会扩大本包范围。包创建后请下载工作簿发给协作者，回传在这里三方合并。</p>
    <label>允许编辑的列（勾选即可；编号列与关联只读字段自动不可选）</label>
    <div id="cf-cols"></div>
    <div class="form-actions"><button class="btn" id="cf-cancel">取消</button>
      <button class="btn primary" id="cf-go">创建包（${pl.ids.length} 行）</button></div>
    <div id="cf-done"></div>`);
  $("cf-cancel").onclick = closeModal;
  let cfCols = null;
  cfCols = renderEditableCols($("cf-cols"), { tname: "specimens", value: ["Notes", "DetBy", "DetDate"],
    onChange: (names) => { $("cf-go").disabled = !names.length; } });
  $("cf-go").onclick = async () => {
    const allowed_cols = cfCols.get();
    if (!allowed_cols.length) { toast("请至少勾选一列", "err"); return; }
    $("cf-go").disabled = true;
    try {
      const r = await api("/api/collab/packages", { method: "POST", body: JSON.stringify({ rids: pl.ids, allowed_cols }) });
      $("cf-done").innerHTML = `<p class="hint" style="color:var(--c-saved)">已创建 ${esc(r.pkg_id)}（${r.rows} 行）。</p>
        <div class="form-actions" style="justify-content:flex-start">
          <button class="btn primary" id="cf-dl">${icon("download")} 下载工作簿（发给协作者）</button>
          <button class="btn" id="cf-goto">前往协作编辑包页管理</button></div>`;
      $("cf-dl").onclick = () => downloadFile(`/api/collab/packages/${r.pkg_id}/workbook`).catch((e) => toast(e.message, "err"));
      $("cf-goto").onclick = () => { closeModal(); switchView("collab"); };
    } catch (e) { toast(e.message, "err"); $("cf-go").disabled = false; }
  };
};

/* 当前筛选 → Material examined 文本（按分类学惯例聚合） */
$("btn-mateex-filtered").onclick = async () => {
  let pl;
  try { pl = await resolveCurrentPlan(); } catch (e) { toast(e.message, "err"); return; }
  openModal(`Material examined · 当前筛选 ${pl.ids.length} 行`, `
    <p class="hint">按属种分组聚合采集地/日期/采集人/数量（事件源缺失时用行内缓存，标注规则与导出一致）；空值不补造，不改变任何数据。</p>
    <div class="form-actions"><button class="btn" id="mx-cancel">关闭</button>
      <button class="btn primary" id="mx-go">生成文本</button></div>
    <div id="mx-out"></div>`);
  $("mx-cancel").onclick = closeModal;
  $("mx-go").onclick = async () => {
    $("mx-go").disabled = true;
    try {
      const d = await api(`/api/material-examined?rids=${pl.ids.join(",")}`);
      $("mx-out").innerHTML = `<p class="hint">共 ${d.count} 号 / ${d.species} 种。点击文本框全选复制：</p>
        <textarea class="input" rows="14" style="width:100%;font-family:monospace" onclick="this.select()">${esc(d.text)}</textarea>`;
    } catch (e) { toast(e.message, "err"); }
    $("mx-go").disabled = false;
  };
};
// 视图分享：只分享定义（列/筛选/顺序），接收方应用时按其自身权限与列重新计算
function openViewShare() {
  const views = (S.prefs.views || {})[S.t] || [];
  if (!views.length) return toast("本表还没有可分享的视图", "err");
  openModal("分享视图定义 · " + curCfg().label, `
    <p class="hint">分享的是「视图定义」（筛选条件+列集合+列序+列宽），不含数据；对方导入后按其自身的表权限和可见列重新计算生效。</p>
    <textarea id="vs-text" style="width:100%;height:180px;font-family:Consolas,monospace;font-size:12px">${esc(JSON.stringify({ table: S.t, views }, null, 1))}</textarea>
    <div class="form-actions">
      <button class="btn primary" id="vs-copy">复制定义</button>
      <button class="btn" id="vs-import">从粘贴导入</button>
    </div>`);
  $("vs-copy").onclick = async () => {
    try { await navigator.clipboard.writeText($("vs-text").value); toast("定义已复制，发给同事即可", "ok"); }
    catch { toast("复制失败，请手动选择文本复制", "err"); }
  };
  $("vs-import").onclick = () => {
    try {
      const j = JSON.parse($("vs-text").value);
      if (j.table !== S.t || !Array.isArray(j.views)) throw new Error("定义与本表不匹配");
      S.prefs.views = S.prefs.views || {};
      const mine = (S.prefs.views[S.t] = S.prefs.views[S.t] || []);
      const names = new Set(mine.map((v) => v.name));
      let n = 0;
      for (const v of j.views) {
        let name = v.name || "导入视图";
        while (names.has(name)) name += "·导入";
        names.add(name);
        // 权限在接收方重算：只保留接收方也有的列；筛选原样带入（应用时按本人可见列执行）
        const myCols = new Set(S.columns.map((c) => c.name));
        mine.push({ ...v, name,
          hidden: (v.hidden || []).filter((c) => myCols.has(c)),
          colorder: (v.colorder || []).filter((c) => myCols.has(c)) });
        n++;
      }
      savePrefs().then(() => { closeModal(); toast(`已导入 ${n} 个视图定义（已按您的权限重算列）`, "ok"); renderFilterChips(); });
    } catch (e) { toast("导入失败：" + e.message, "err"); }
  };
}

$("btn-headers").onclick = () => downloadFile(`/api/export/${S.t}?template=1`)
  .then(() => toast("表头模板已下载：在 Excel 按列名填好数据后，用「导入 Excel → 合并」按主键入库", "ok"))
  .catch((e) => toast(e.message, "err"));

/* ---- 导出全库（v4.37 补接线：按钮自 v3.81 起一直未绑定事件，点击无任何反应；
       后端 /api/export/all 一直存在且测试在用）——全库工作簿较大，生成+下载需等待 ---- */
$("btn-export-all").onclick = () => {
  toast("正在生成全库工作簿（全部可见表），数据量大请稍候，浏览器完成下载后会有提示", "ok");
  return downloadFile("/api/export/all")
    .then(() => toast("全库导出已下载（README 含导出人/时点/范围与数量口径）", "ok"))
    .catch((e) => toast(e.message, "err"));
};

/* ---- 打印清单（留痕，v4.06 补接线）：先经 /api/print-log 校验+留痕，再用隐藏 iframe
   打印当前筛选的干净清单；列=当前视图列（隐藏/列序偏好生效），空值不补造 ---- */
const PRINT_CAP = 2000;   // 打印行数上限（留痕仍记录完整命中行数，超出部分明示）
$("btn-print-log").onclick = async () => {
  if (!S.meta.tables[S.t]) { toast("请先在左侧打开一个标本表，再打印清单", "err"); return; }
  const cfg = S.meta.tables[S.t];
  try {
    const withScope = (p) => {
      if (S.showDeleted) p.set("deleted", "1");
      if (S.preset) p.set("preset", S.preset);
      if (Object.keys(S.filters).length) p.set("filters", JSON.stringify(S.filters));
      return p;
    };
    // 第一页：顺带校验可见性，并拿到准确的命中行数
    const p1 = withScope(new URLSearchParams({ page: 1, per: 500, q: S.q, sort: S.sort, dir: S.dir, natural: "1" }));
    const d1 = await api(`/api/rows/${S.t}?` + p1);
    if (!d1.total) { toast("当前筛选没有行，没有可打印的清单", "err"); return; }
    // 留痕（打印前调用：服务端校验该表对当前账号可见，行为进变更历史）
    await api(`/api/print-log/${S.t}`, { method: "POST",
      body: JSON.stringify({ rows: d1.total, q: S.q, filters: S.filters }) });
    // 分页取全量行（上限内）
    const rows = d1.rows.slice();
    while (rows.length < Math.min(d1.total, PRINT_CAP)) {
      const p = withScope(new URLSearchParams({ page: Math.floor(rows.length / 500) + 1, per: 500,
        q: S.q, sort: S.sort, dir: S.dir, natural: "1" }));
      const d = await api(`/api/rows/${S.t}?` + p);
      if (!d.rows.length) break;
      rows.push(...d.rows);
    }
    const cols = visibleCols();
    const scopeBits = [];
    if ((S.q || "").trim()) scopeBits.push("搜索「" + S.q.trim() + "」");
    const nf = Object.keys(S.filters || {}).length;
    if (nf) scopeBits.push(nf + " 个列筛选");
    if (S.showDeleted) scopeBits.push("回收站");
    if (S.preset === "loaned") scopeBits.push("借出中视图");
    const sysName = S.meta.system_name || "标本信息管理系统";
    const now = new Date();
    const pad = (x) => String(x).padStart(2, "0");
    const ts = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}`;
    const html = `<!doctype html><html><head><meta charset="utf-8"><title>${esc(sysName)} · ${esc(cfg.label)} 打印清单</title>
<style>
@page { size: A4 landscape; margin: 9mm; }
body { font: 10.5px/1.45 "Microsoft YaHei","PingFang SC","Segoe UI",sans-serif; color: #111; margin: 0; }
h1 { font-size: 15px; margin: 0 0 2px; }
.meta { font-size: 10px; color: #444; margin-bottom: 8px; }
table { border-collapse: collapse; width: 100%; }
th, td { border: .5px solid #999; padding: 2.5px 5px; text-align: left; vertical-align: top; word-break: break-word; }
thead { display: table-header-group; }
th { background: #eef1f4; font-weight: 600; white-space: nowrap; }
tr { page-break-inside: avoid; }
td.num, th.num { text-align: right; }
.pn { width: 30px; color: #555; }
.foot { margin-top: 8px; font-size: 9.5px; color: #666; }
</style></head><body>
<h1>${esc(sysName)} · ${esc(cfg.label)} 清单</h1>
<div class="meta">打印时间：${ts} · 操作账号：${esc(S.me.username)} · 范围：${esc(scopeBits.length ? scopeBits.join(" · ") : "全表")} · 命中 ${d1.total.toLocaleString()} 行${d1.total > rows.length ? `（仅呈现前 ${PRINT_CAP.toLocaleString()} 行）` : ""} · ${cols.length} 列</div>
<table><thead><tr><th class="pn">#</th>${cols.map((c) => `<th${c.numeric ? ' class="num"' : ""}>${esc(c.label || c.name)}</th>`).join("")}</tr></thead>
<tbody>${rows.map((row, i) => `<tr><td class="pn">${i + 1}</td>${cols.map((c) => {
      const v = displayVal(c.name, row.data[c.name]);
      return `<td${c.numeric ? ' class="num"' : ""}>${esc(v === null || v === undefined ? "" : String(v))}</td>`;
    }).join("")}</tr>`).join("")}</tbody></table>
<div class="foot">本清单由${esc(sysName)}生成（列显示与当前视图一致）；打印行为已留痕，可在「变更历史」核查。</div>
</body></html>`;
    const f = document.createElement("iframe");
    f.style.cssText = "position:fixed;right:0;bottom:0;width:0;height:0;border:0;";
    document.body.appendChild(f);
    const doc = f.contentWindow.document;
    doc.open(); doc.write(html); doc.close();
    const rm = () => setTimeout(() => f.remove(), 60000);   // 兜底：部分浏览器不触发 afterprint
    f.contentWindow.addEventListener("afterprint", () => f.remove(), { once: true });
    rm();
    toast(`打印清单已留痕：${tblName(cfg)} ${d1.total.toLocaleString()} 行（变更历史可查）`, "ok");
    f.contentWindow.focus();
    f.contentWindow.print();
  } catch (e) { toast(e.message || "打印失败", "err"); }
};

$("btn-paste").onclick = () => {
  if (!canWrite()) return toast("只读账号无权导入", "err");
  openModal(`粘贴导入 · ${curCfg().label}`, `
    <p class="hint">在 Excel 里选中<b>含表头行</b>的一块区域复制，然后粘贴到下面（Ctrl+V）。
    系统按表头列名匹配：已有行按「${esc(curCfg().key)}」更新变化字段，新行自动新增。
    导入前会自动保存快照，出错可在版本管理里恢复。</p>
    <textarea id="paste-area" style="width:100%;height:200px;font-family:Consolas,monospace;font-size:12.5px"
      placeholder="MHBU #\tFamily\tDetBy\nMHBU-ARA-00020001\tLiphistiidae\t张三\n……"></textarea>
    <div class="form-actions">
      <button class="btn" id="paste-cancel">取消</button>
      <button class="btn primary" id="paste-ok">解析并导入</button>
    </div>`);
  $("paste-cancel").onclick = closeModal;
  $("paste-area").focus();
  $("paste-ok").onclick = async () => {
    const text = $("paste-area").value;
    if (!text.trim()) return toast("请先粘贴内容", "err");
    $("paste-ok").disabled = true;
    $("paste-ok").textContent = "导入中…";
    try {
      const r = await api(`/api/rows/${S.t}/paste`, { method: "POST", body: JSON.stringify({ text }) });
      closeModal();
      let msg = `粘贴导入完成：共解析 ${r.parsed} 行，新增 ${r.added}、更新 ${r.updated}` + (r.skipped ? `、跳过 ${r.skipped}` : "");
      if (r.unknown_cols?.length) msg += `。未识别列已忽略：${r.unknown_cols.join("、")}`;
      toast(msg, "ok");
      loadTable();
      refreshMetaCounts();
    } catch (e) {
      $("paste-ok").disabled = false;
      $("paste-ok").textContent = "解析并导入";
      toast(e.message, "err");
    }
  };
};

$("btn-import").onclick = () => {
  if (!canWriteTable(S.t)) return toast("您的账号对本表只读，无法导入", "err");
  openModal(`导入 Excel · ${curCfg().label}`, `
    <div class="file-drop" id="file-drop">点击选择或拖入 .xlsx 文件（第一个工作表将被读取，按表头列名匹配）</div>
    <input type="file" id="import-file" accept=".xlsx" hidden>
    <div class="radio-row">
      <label><input type="radio" name="imp-mode" value="merge" checked><span>
        <b>合并（推荐）</b>按「${esc(curCfg().key)}」匹配：已存在的行更新有变化的字段，不存在的行新增。</span></label>
      <label><input type="radio" name="imp-mode" value="replace"><span>
        <b>替换</b>清空本表现有数据后全部重新导入。请谨慎选择。</span></label>
    </div>
    <p class="hint warn">无论哪种模式，导入前都会自动保存一个「导入前快照」，如有问题可随时从版本管理恢复。</p>
    <div class="form-actions">
      <button class="btn" id="imp-template">下载导入模板（仅表头）</button>
      <span style="flex:1"></span>
      <button class="btn" id="imp-cancel">取消</button>
      <button class="btn primary" id="imp-ok" disabled>开始导入</button>
    </div>`);
  const tplBtn = $("imp-template");
  if (tplBtn) tplBtn.onclick = () => downloadFile(`/api/export/${S.t}?template=1`)
    .then(() => toast("模板已下载", "ok")).catch((e) => toast(e.message, "err"));
  const drop = $("file-drop"), fileInput = $("import-file");
  drop.onclick = () => fileInput.click();
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("drag"); };
  drop.ondragleave = () => drop.classList.remove("drag");
  drop.ondrop = (e) => {
    e.preventDefault(); drop.classList.remove("drag");
    if (e.dataTransfer.files[0]) {
      fileInput.files = e.dataTransfer.files;   // v4.33 修：拖入的文件此前从未赋给 input，
      setFile(e.dataTransfer.files[0]);          // 点「开始导入」时 request 里根本没有文件（服务端报「未收到文件」）
    }
  };
  fileInput.onchange = () => fileInput.files[0] && setFile(fileInput.files[0]);
  function setFile(f) {
    if (!/\.xlsx$/i.test(f.name)) return toast("请选择 .xlsx 文件", "err");
    drop.textContent = `已选择：${f.name}`;
    $("imp-ok").disabled = false;
  }
  $("imp-cancel").onclick = closeModal;
  $("imp-ok").onclick = async () => {
    // 注意：预览/确认会重建弹窗内容，先持有文件与按钮引用，
    // 不能再依赖 getElementById（活 DOM 里已找不到）
    const impOk = $("imp-ok");
    const file = fileInput.files[0];
    if (!file) {   // 兜底：拖入链路任何环节丢文件时给出可读提示，不发空请求
      impOk.disabled = true;
      return toast("没有读到文件，请重新点击选择或拖入 .xlsx 文件", "err");
    }
    const mode = document.querySelector("input[name=imp-mode]:checked").value;
    if (mode === "replace" && !(await confirmBox("替换模式会清空本表现有数据！确定继续吗？", { okText: "继续" }))) return;
    impOk.disabled = true;
    impOk.textContent = "解析中…";
    const fd0 = new FormData();
    fd0.append("file", file);
    fd0.append("mode", mode);
    let pv;
    try {
      pv = await api(`/api/import/preview/${S.t}`, { method: "POST", body: fd0 });
    } catch (e) {
      toast(e.message, "err");
      impOk.disabled = false;
      impOk.textContent = "开始导入";
      return;
    }
    // ---- 预览页（计划不可变：确认后按 plan_id 执行同一计划） ----
    const planId = pv.plan_id;
    // unknown/renames 旧契约顶层键（v4.24 补回）；老服务端只在 pv.mapping 里时兜底
    const impUnknown = pv.unknown || (pv.mapping && pv.mapping.unknown) || [];
    const impRenames = pv.renames || (pv.mapping && pv.mapping.renames) || [];
    const tag = (t, cls = "") => `<span class="tag ${cls}">${esc(t)}</span>`;
    const colTags = pv.columns.map((c) => tag(c)).join(" ");
    const renameRows = impRenames.map((x) =>
      `<div>「${esc(x.excel)}」→ 匹配为 <b>${esc(x.db)}</b></div>`).join("");
    const updRows = (pv.sample_upd || []).map((u) => `
      <div class="ck-item"><span class="ck-key">${esc(u.key || "(无主键)")}</span>
      <span class="ids">${u.fields.length ? "变更：" + esc(u.fields.join("、")) : "无字段变化（将跳过）"}</span></div>`).join("");
    openModal(`导入预览 · ${curCfg().label}（${mode === "replace" ? "替换模式" : "合并模式"}）`, `
      <div class="impv-stats">
        <div class="impv-stat ok"><b>${pv.add}</b><span>将新增</span></div>
        <div class="impv-stat"><b>${pv.update}</b><span>将更新</span></div>
        ${pv.skip ? `<div class="impv-stat"><b>${pv.skip}</b><span>完全重复跳过</span></div>` : ""}
        ${mode === "replace" ? `<div class="impv-stat warn"><b>清空</b><span>现有数据移入快照可恢复</span></div>` : ""}
        <div class="impv-stat"><b>${pv.rows}</b><span>文件数据行</span></div>
      </div>
      <p class="hint" style="margin:10px 0 4px">识别到 ${pv.columns.length} 列：${colTags || "（无）"}</p>
      ${(impRenames.length) ? `<div class="hint">列名自动匹配：${renameRows}</div>` : ""}
      ${impUnknown.length ? `<p class="hint warn">⚠ 未识别的列将被忽略（${impUnknown.length}）：${esc(impUnknown.join("、"))}</p>` : ""}
      ${pv.blocking ? `<p class="hint warn">⚠ ${pv.blocking} 行存在阻断性错误（空主键/文件内重复主键），请下载错误清单修正后重新导入</p>` : ""}
      ${pv.warn_total ? `<p class="hint warn">⚠ ${pv.warn_total} 条警告（公式无缓存/疑似日期/单位归一等）</p>` : ""}
      ${(pv.notices || []).map((n) => `<p class="hint">· ${esc(n)}</p>`).join("")}
      ${pv.errors_download ? `<p><a href="${pv.errors_download}&token=${token()}" download>下载错误/警告清单</a></p>` : ""}
      ${pv.sample_add.length ? `<p class="hint" style="margin:8px 0 2px">将新增（示例，主键）：${pv.sample_add.map((k) => tag(k)).join(" ")}</p>` : ""}
      ${updRows ? `<p class="hint" style="margin:8px 0 2px">将更新（示例）：</p><div style="max-height:24vh;overflow-y:auto">${updRows}</div>` : ""}
      <div class="form-actions">
        <button class="btn" id="impv-cancel">取消</button>
        <button class="btn primary" id="impv-go">确认导入</button>
      </div>`);
    $("impv-cancel").onclick = () => closeModal();
    $("impv-go").onclick = async () => {
      const go = $("impv-go");
      go.disabled = true;
      go.textContent = "导入中…";
      const fd = new FormData();
      fd.append("plan_id", planId);
      fd.append("mode", mode);
      try {
        const r = await api(`/api/import/${S.t}`, { method: "POST", body: fd });
        closeModal();
        let msg = `导入完成：新增 ${r.added} 行，更新 ${r.updated} 行` + (r.skipped ? `，跳过 ${r.skipped} 行` : "");
        if (r.unknown_cols?.length) msg += `。未识别的列已忽略：${r.unknown_cols.join("、")}`;
        toast(msg, "ok");
        await loadTable();
        refreshMetaCounts();
      } catch (err) {
        toast(err.message, "err");
        go.disabled = false;
        go.textContent = "确认导入";
      }
    };
  };
};

/* ---- 新增行 ---- */
$("btn-add").onclick = () => {
  if (!canWriteTable(S.t)) return toast("您的账号对本表只读，无法新增", "err");
  const keyName = curCfg().key;
  const isSpec = S.t === "specimens";
  // 关联键列（伪蝎「采集编号」/原蛛「CollectionEventNumber"）虽标记 linked，但它是本行存储的关联字段——
  // 必须出现在新增表单里，否则没有地方填采集编号（v4.19 修复）
  const lkKey = curCfg().linked_key;
  const own = S.columns.filter((c) => !c.linked || c.name === lkKey);
  const evHint = S.t === "events"
    ? `<p class="hint"><b>采集事件号可以留空</b>：适合没有编号的古老采集信息，保存时系统自动生成内部编号（M-0001 格式），之后标本表用它即可关联。</p>`
    : keyName ? `<p class="hint">「${esc(keyName)}」必须唯一：输入已存在的编号将无法保存。</p>` : "";
  const ordered = orderedAddFields(own);
  const priSet = new Set(curCfg().add_first || []);
  const fields = ordered.map((c) => {
    if (c.enum_locked && c.options) {
      // 锁定枚举：select 只能选
      const def = c.default || "";
      return `<label class="${c.wide ? "span2" : ""}" title="${esc(detTitle(c.name))}"><span class="lb-head">${esc(c.label || c.name)}${priSet.has(c.name) ? '<span class="imp">*</span>' : ""}</span>
        <select data-col="${esc(c.name)}" data-locked="1" style="padding:7px 9px;border:1px solid var(--border);border-radius:6px">
          ${c.options.map((o) => `<option value="${esc(String(o))}" ${String(o) === def ? "selected" : ""}>${esc(o)}</option>`).join("")}
        </select></label>`;
    }
    const dv = colDefaultVal(c);
    return `<label class="${c.wide ? "span2" : ""}" title="${esc(detTitle(c.name))}"><span class="lb-head">${esc(c.label || c.name)}${priSet.has(c.name) ? '<span class="imp">*</span>' : ""}</span>
      <input data-col="${esc(c.name)}" value="${esc(dv)}">
    </label>`;
  }).join("");
  // 手动录入采集信息区块：有采集关联列的标本表，未填事件号时可在此逐项手填（保存时自动生成 M-XXXX 编号并关联）
  const lk = curCfg().linked_key;
  const evCfg = S.meta.tables["events"];
  let geoBlock = "";
  if (lk && evCfg && S.t !== "events" && canWriteTable("events")) {
    const geoFields = evCfg.columns
      .filter((c) => !c.linked && c.name !== evCfg.key && c.name !== "采集批次备注")
      .map((c) => `<label class="${c.wide ? "span2" : ""}">${esc(c.name)}
        <input data-evcol="${esc(c.name)}"></label>`).join("");
    geoBlock = `
    <details style="margin-top:10px;border:1px dashed var(--border);border-radius:8px;padding:8px 12px">
      <summary style="cursor:pointer;font-weight:600;user-select:none">没有采集事件号？点此手动录入采集信息</summary>
      <p class="hint">逐项填写地点、采集人、日期等（可只填部分）。保存标本时系统自动生成采集事件号（M-XXXX 内部编号）并关联本标本，
      国家、省份、经纬度等随之自动带出；与已有记录采集要素完全相同时自动复用其编号，不会重复建档。
      已填采集事件号时此处忽略。</p>
      <div class="grid-form">${geoFields}</div>
    </details>`;
  }
  openModal(`新增行 · ${curCfg().label}`, `
    ${evHint}
    <p class="hint">标本表<b>只需填属名（Genus）</b>：科、亚科、族、Clade 等会自动按「分类信息」表匹配带出；
    带“采集地信息”来源的列（国家、省份、经纬度等）也会按「采集事件号」自动带出。
    <span class="imp">*</span> 为重要字段（已排前）。</p>
    <div class="grid-form">${fields}</div>
    ${geoBlock}
    <div class="form-actions">
      <button class="btn" id="add-cancel">取消</button>
      <button class="btn primary" id="add-ok">保存</button>
      <button class="btn primary" id="add-save-next" title="保存本条后表单保留已填内容、编号自动 +1，可连续录入">保存并继续录入</button>
    </div>`);
  wireCascade($("modal-body"));
  markRequired($("modal-body"));
  // 采集事件号联想：关联键列（采集事件号/采集编号）输入时按前缀联想（铺陈到全部类群，v4.20）
  if (curCfg().linked_key) {
    const evInp = $("modal-body").querySelector(`input[data-col="${CSS.escape(curCfg().linked_key)}"]`);
    if (evInp) attachEventSuggest(evInp);
  }
  if (S.t === "pseudoscorpiones") {
    const pgi = $("modal-body").querySelector('input[data-col="属"]');
    if (pgi) pgi.addEventListener("change", async () => {
      const g = pgi.value.trim();
      if (!g) return;
      try {
        const r = await api(`/api/wac-check?genus=${encodeURIComponent(g)}`);
        if (r.family) {
          const fi = $("modal-body").querySelector('input[data-col="科"]');
          if (fi && !fi.value.trim()) { fi.value = r.family; toast(`已按 WAC 带出科：${r.family}`, "ok"); }
        }
      } catch { }
    });
  }
  if (S.t === "mygalomorph") wireNumberLink($("modal-body"), S.t);
  if (S.t === "uce") {
    const sci = $("modal-body").querySelector('input[data-col="Sample Code"]');
    if (sci) sci.addEventListener("change", () => {
      const sp = $("modal-body").querySelector('input[data-col="Species name"]');
      sraTryFill((k) => $("modal-body").querySelector(`input[data-col="${CSS.escape(k)}"]`), sci.value.trim(), sp ? sp.value : "");
    });
  }
  if (curCfg().wac_lookup) {
    const gi = $("modal-body").querySelector('input[data-col="属"]');
    const si = $("modal-body").querySelector('input[data-col="种"]');
    if (gi) attachWacGenus(gi);
    if (si) attachWacEpithet(si, () => gi ? gi.value : "");
  }
  if (S.t === "mygalomorph" && keyName === "KYUARA #") {
    const ki = $("modal-body").querySelector(`input[data-col="${CSS.escape(keyName)}"]`);
    if (ki) {
      ki.value = KYUARA_PREFIX;  // 与标本表一致：预填前缀，用户只填数字（下一可用号由选盒预填）
      ki.addEventListener("input", () => {
        const v = ki.value;
        if (v && !v.toUpperCase().startsWith("KYUARA")) {
          const pos = ki.selectionStart;
          ki.value = KYUARA_PREFIX + v;
          ki.setSelectionRange(pos + KYUARA_PREFIX.length, pos + KYUARA_PREFIX.length);
        }
      });
    }
  }
  if (isSpec) {
    // Position 自动推算：盒号段 + 编号 → [行,列]（手工改过后不再覆盖）
    S._posManual = false;
    const updatePos = () => {
      if (S._posManual) return;
      const b = S._addBox;
      const ki = $("modal-body").querySelector(`input[data-col="${CSS.escape(keyName)}"]`);
      const posEl = $("modal-body").querySelector('input[data-col="Position"]');
      if (!b || !ki || !posEl) return;
      const m = ki.value.trim().match(/^MHBU-ARA-(\d{8})$/);
      if (!m) return;
      const num = +m[1];
      if (num < b.num_start || num > b.num_end) return;
      const off = num - b.num_start;
      const cap = b.num_end - b.num_start + 1;
      let cols = Math.floor(Math.sqrt(cap + 0.5));
      if (cols * cols !== cap) cols = 10;
      posEl.value = `[${Math.floor(off / cols) + 1},${off % cols + 1}]`;
    };
    S._updatePos = updatePos;
    const posInput = $("modal-body").querySelector('input[data-col="Position"]');
    if (posInput) posInput.addEventListener("input", () => { S._posManual = posInput.value.trim() !== ""; });
    const boxInput = $("modal-body").querySelector('input[data-col="Box # (Start#-End#)"]');
    if (boxInput) {
      boxInput.closest("label").style.display = "none";
      const boxLabel = document.createElement("label");
      boxLabel.innerHTML = `Box # (Start#-End#)<span class="perm-grid" id="add-box-sel"><select style="padding:7px 9px;border:1px solid var(--border);border-radius:6px"><option value="">加载中…</option></select></span>`;
      boxInput.closest("label").after(boxLabel);
      loadBoxes().then(() => {
        const sel = boxLabel.querySelector("select");
        sel.innerHTML = availableBoxOptions("");
        sel.id = "add-box-select";
        sel.dataset.col = "Box # (Start#-End#)";
        sel.dataset.box = "1";
        // 默认选中当前有空余的盒（编号顺延的下一盒）
        if (!sel.value && S.boxes?.suggested_box) sel.value = S.boxes.suggested_box;
        // 盒先行：把盒选择移到编号输入之前
        const keyLbl = $("modal-body").querySelector(`label input[data-col="${CSS.escape(keyName)}"]`)?.closest("label");
        if (keyLbl) keyLbl.before(boxLabel);
        const clearKeyMsgs = () => {
          const ki = $("modal-body").querySelector(`input[data-col="${CSS.escape(keyName)}"]`);
          if (ki) ki.parentElement.querySelectorAll(".key-hint, .key-err").forEach((x) => x.remove());
          return ki;
        };
        const applyBox = async () => {
          const name = sel.value.trim();
          S._addBox = null; S._addBoxErr = "";
          const ki = clearKeyMsgs();
          if (!ki) return;
          const b = (S.boxes?.boxes || []).find((x) => x.name === name);
          if (!b) {
            if (name) return;  // 未登记盒：不约束
            const hint = document.createElement("div");
            hint.className = "key-hint";
            hint.style.cssText = "font-size:12px;color:var(--muted);margin-top:3px";
            hint.textContent = "建议先在上方选择标本盒：选盒后编号自动给出该盒下一个可用号，且只能用该盒号段内的编号";
            ki.parentElement.appendChild(hint);
            return;
          }
          S._addBox = b;
          const hint = document.createElement("div");
          hint.className = "key-hint";
          hint.style.cssText = "font-size:12px;color:var(--primary);margin-top:3px";
          hint.textContent = `已选 ${b.name}：编号须在 ${String(b.num_start).padStart(8, "0")} – ${String(b.num_end).padStart(8, "0")} 之间`;
          ki.parentElement.appendChild(hint);
          const cur = ki.value.trim();
          if (!cur || cur === KEY_PREFIX) {
            try {
              const r = await api(`/api/boxes/next?name=${encodeURIComponent(b.name)}`);
              if (r.key) { ki.value = r.key; hint.textContent = `已选 ${b.name}：已预填该盒下一个可用编号 ${r.key}`; }
              else if (r.full) { S._addBoxErr = `盒「${b.name}」已用满，请先在标本盒管理中登记新盒`; hint.textContent = S._addBoxErr; hint.style.color = "var(--danger)"; }
            } catch { }
          }
        };
        sel.addEventListener("change", applyBox);
        applyBox().then(() => updatePos());
      });
    }
  }
  if (isSpec && keyName) {
    const keyInput = $("modal-body").querySelector(`input[data-col="${CSS.escape(keyName)}"]`);
    if (keyInput) {
      keyInput.value = KEY_PREFIX;
      keyInput.addEventListener("input", () => {
        const v = keyInput.value;
        if (v && !v.toUpperCase().startsWith("MHBU")) {
          // 用户从数字开始输入：自动补前缀
          const pos = keyInput.selectionStart;
          keyInput.value = KEY_PREFIX + v;
          keyInput.setSelectionRange(pos + KEY_PREFIX.length, pos + KEY_PREFIX.length);
        }
      });
      // 盒先行：编号实时校验是否落在所选盒号段内
      S._validateAddKey = () => {
        const old = keyInput.parentElement.querySelector(".key-err");
        if (old) old.remove();
        S._addBoxErr = "";
        if (!S._addBox) return true;
        const m = keyInput.value.trim().match(/^MHBU-ARA-(\d{8})$/);
        const pad = (n) => String(n).padStart(8, "0");
        if (!m) {
          S._addBoxErr = `已选盒「${S._addBox.name}」：请输入 8 位编号（系统自动补前缀）`;
        } else {
          const num = +m[1];
          if (num < S._addBox.num_start || num > S._addBox.num_end)
            S._addBoxErr = `编号 ${keyInput.value.trim()} 不在盒「${S._addBox.name}」号段内（${pad(S._addBox.num_start)} – ${pad(S._addBox.num_end)}）`;
        }
        if (S._addBoxErr) {
          const err = document.createElement("div");
          err.className = "key-err";
          err.style.cssText = "font-size:12px;color:var(--danger);margin-top:3px";
          err.textContent = "✕ " + S._addBoxErr;
          keyInput.parentElement.appendChild(err);
          return false;
        }
        return true;
      };
      keyInput.addEventListener("input", () => {
        if (S._addBox) S._validateAddKey();
      });
      keyInput.addEventListener("blur", () => { if (S._addBox) S._validateAddKey(); });
      // 按编号即时预览自动匹配的标本盒
      keyInput.addEventListener("input", async () => {
        await loadBoxes();
        const m = keyInput.value.match(/(\d{8})\s*$/);
        const sel = document.getElementById("add-box-select");
        if (!m || !sel) return;
        const num = +m[1];
        const b = (S.boxes.boxes || []).find(x => x.num_start <= num && num <= x.num_end);
        if (b) {
          sel.value = b.name;
          const opt = sel.selectedOptions[0];
          if (opt && !opt.textContent.includes("自动匹配"))
            opt.textContent += "（自动匹配）";
        }
        if (typeof S._updatePos === "function") S._updatePos();
      });
    }
  }
  wireGenusLookup($("modal-body"));
  // 临时鉴定名：联想 + cf. 切换按钮（鉴定信心不足）
  const tni = $("modal-body").querySelector(`input[data-col="${CSS.escape(taxCols().temp)}"]`);
  if (tni) wireTempNameField(tni);
  const first = $("modal-body").querySelector("input");
  if (first) first.focus();
  $("add-cancel").onclick = closeModal;
  const saveAdd = async (again) => {
    const go = again ? $("add-save-next") : $("add-ok");
    if (go.disabled) return;
    go.disabled = true;
    go.textContent = again ? "保存中…" : "保存中…";
    const after = () => { go.disabled = false; go.textContent = again ? "保存并继续录入" : "保存"; };
    const data = {};
    $("modal-body").querySelectorAll("input[data-col], select[data-locked]").forEach((i) => {
      if (i.value.trim() === "") return;
      let v = i.value;
      if (isSpec && i.dataset.col === keyName) v = normalizeSpecKey(v);
      data[i.dataset.col] = v;
    });
    if (isSpec) {
      const sel = document.getElementById("add-box-select");
      if (sel && sel.value.trim() !== "") data["Box # (Start#-End#)"] = sel.value;
    }
    if (isSpec && typeof S._validateAddKey === "function" && !S._validateAddKey()) {
      after();
      return toast("编号与所选标本盒不匹配，请核对后保存", "err");
    }
    // 手动采集信息：未填事件号且逐项手填了采集要素 → 先建档采集事件（自动生成/复用 M-XXXX）再保存标本
    if (lk && !data[lk]) {
      const evData = {};
      $("modal-body").querySelectorAll("input[data-evcol]").forEach((i) => {
        if (i.value.trim() !== "") evData[i.dataset.evcol] = i.value;
      });
      if (Object.keys(evData).length) {
        try {
          const ev = await api("/api/rows/events", { method: "POST", body: JSON.stringify({ data: evData }) });
          data[lk] = ev.key;
        } catch (e) {
          if (e.duplicate) {
            // 采集要素与已有记录完全相同：直接复用该编号（推荐行为，避免重复建档）
            data[lk] = e.duplicate.key;
            toast(`采集要素与已有记录相同，复用编号 ${e.duplicate.key}；如需分开建档请在采集地信息表手动新建`, "ok");
          } else { after(); return toast(e.message, "err"); }
        }
      }
    }
    try {
      const r = await api(`/api/rows/${S.t}`, { method: "POST", body: JSON.stringify({ data, force_name: !!S._fsForceOk, force_dup: !!S._forceDup }) });
      S._fsForceOk = false; S._forceDup = false;
      if (again) {
        // 连续录入：表单不关，保留已填公共字段，主键编号 +1
        toast(`已保存：${r.key || ""}（已继续录入）`, "ok");
        if (keyName) {
          const ki = $("modal-body").querySelector(`input[data-col="${CSS.escape(keyName)}"]`);
          if (ki && ki.value.trim()) ki.value = plusOne(ki.value.trim());
        }
        $("modal-body").querySelectorAll("input[data-col]").forEach((i) => {
          if (["AdultMale", "AdultFemale", "Subadult", "Juvenile", "标本数量", "雄性数量", "雌性数量", "幼体数量"].includes(i.dataset.col)) i.value = "";
        });
        const first2 = $("modal-body").querySelector(`input[data-col="${CSS.escape(keyName)}"]`);
        if (first2) { first2.focus(); first2.select && first2.select(); }
      } else {
        closeModal();
        toast(`已新增 1 行${r.key ? `：${r.key}` : ""}${r.key && r.key.startsWith("M-") ? "（系统生成的内部编号）" : ""}`, "ok");
        S.page = 1; S.q = ""; $("search").value = "";
      }
      await loadTable();
    } catch (err) {
      if (err.suspect_dup) {
        const dupKey = err.suspect_dup.key;
        const keepGoing = await confirmBox(`疑似重复录入：同采集事件下已有标本「${dupKey}」的属/种与本次相同。

· 同一标本录了两次 → 「返回检查」去核对编号
· 确为不同个体（同地同日同种多号）→ 「继续保存」`, { okText: "返回检查", danger: true });
        if (keepGoing) { after(); return; }
        S._forceDup = true;
        return saveAdd(again);
      }
      if (err.duplicate) {
        openModal("发现相同的采集信息", `
          <p>系统中已有<b>采集要素完全相同</b>（地点、采集人、日期一致）的记录，编号
            <b class="key-cell">${esc(err.duplicate.key)}</b>。</p>
          <p class="hint">标本表中直接填这个编号即可自动带出该条采集信息，无需重复建档。
            若确属另一批次（如同地同日不同批次），可选择仍新建一条。</p>
          <div class="form-actions">
            <button class="btn primary" id="dup-use">知道了，使用已有编号</button>
            <button class="btn" id="dup-force">仍新建一条</button>
          </div>`);
        $("dup-use").onclick = closeModal;
        $("dup-force").onclick = async () => {
          try {
            const r = await api(`/api/rows/${S.t}`, { method: "POST", body: JSON.stringify({ data, force: true, force_name: !!S._fsForceOk }) });
            S._fsForceOk = false;
            closeModal();
            toast(`已新增 1 行：${r.key || ""}`, "ok");
            S.page = 1; S.q = ""; $("search").value = "";
            await loadTable();
          } catch (e2) { toast(e2.message, "err"); }
        };
      } else { toast(err.message, "err"); }
    } finally { after(); }
  };
  $("add-ok").onclick = () => saveAdd(false);
  $("add-save-next").onclick = () => saveAdd(true);
};

/* 弹窗误关保护：表单有已填内容时，第一次点取消/✕ 只进入确认态（✕ 变红），再点一次才真正关闭 */
document.addEventListener("click", (e) => {
  const m = $("modal");
  if (!m || m.hidden) return;
  const t = e.target;
  const isCancel = t.closest && t.closest(".form-actions") && t.tagName === "BUTTON" && t.textContent.trim() === "取消";
  const isX = t.id === "modal-close" || t.id === "modal-overlay";
  if (!isCancel && !isX) return;
  const filled = [...m.querySelectorAll("input[data-col], input[data-evcol], textarea")]
    .some((i) => (i.value || "").trim() !== "");
  if (!filled) return;
  const x = $("modal-close");
  if (x.dataset.armed === "1") {  // 第二次点击：放行关闭
    delete x.dataset.armed;
    clearTimeout(x._armT);
    x.textContent = "✕";
    x.style.background = "";
    x.style.color = "";
    x.title = "";
    return;
  }
  e.stopImmediatePropagation();
  e.preventDefault();
  x.dataset.armed = "1";
  x.style.background = "#c0392b";
  x.style.color = "#fff";
  x.title = "再点一次放弃已填内容";
  toast("已填内容未保存：再点一次 ✕（或「取消」）确认放弃", "err");
  x._armT = setTimeout(() => {
    delete x.dataset.armed;
    x.textContent = "✕";
    x.style.background = "";
    x.style.color = "";
    x.title = "";
  }, 3000);
}, true);

/* 属名输入预测：基于 WSC 属级名录（wsc_genera.json），边输入边建议 */
async function confirmTempName(name, inputEl = null) {
  const core = name.replace(/^\s*cf\.?\s*/i, "").trim();
  if (!core) return true;
  if (/(gen\.|sp\.|ssp\.|nov\.?)/i.test(core)) return true;  // 开放命名不提醒
  try {
    const r = await api(`/api/species-check?name=${encodeURIComponent(core)}`);
    if (r.in_wsc) {
      if (r.accepted && r.accepted.toLowerCase() !== core.toLowerCase())
        return await confirmBox(`「${esc(core)}」在 WSC 中为异名，当前接受名是「${esc(r.accepted)}」。
暂定名按您输入保留，仍要这样保存吗？`, { okText: "保留" });
      return true;
    }
  } catch { }
  // 未收录 → 模糊查重：给拼写相近的 WSC 名称
  let sugs = [];
  try {
    const fz = await api(`/api/species-fuzzy?name=${encodeURIComponent(core)}`);
    sugs = fz.suggestions || [];
  } catch { }
  if (sugs.length && inputEl) {
    const pick = await new Promise((resolve) => {
      openModal("种名拼写检查 · 未在 WSC 收录", `
        <p class="hint">「<b>${esc(core)}</b>」未在 WSC（World Spider Catalog）收录。以下是拼写相近的收录名称，点击可直接替换：</p>
        <div class="fuzzy-list">${sugs.map((s, i) => `
          <button class="btn fuzzy-item" data-i="${i}" type="button">${esc(s.accepted)}
            <span class="hint">${s.name !== s.accepted ? "（经异名）· " : ""}${esc(s.family || "")}</span></button>`).join("")}</div>
        <div class="form-actions">
          <button class="btn primary" id="fz-keep" type="button">保留我的输入（如为暂定新种）</button>
          <button class="btn" id="fz-cancel" type="button">取消，我自己修改</button>
        </div>`);
      $("modal-body").querySelectorAll(".fuzzy-item").forEach((b) =>
        b.onclick = () => { closeModal(); resolve(sugs[+b.dataset.i].accepted); });
      $("fz-keep").onclick = () => { closeModal(); resolve(null); };
      $("fz-cancel").onclick = () => { closeModal(); resolve(false); };
    });
    if (pick === false) return false;
    if (pick) { inputEl.value = pick; }
    return true;
  }
  return await confirmBox(`⚠「${esc(core)}」未在 WSC（World Spider Catalog）收录。${sugs.length ? `
您是不是要找：${sugs.map((s) => s.accepted).join("、")}？` : ""}
若为暂定新种（sp. nov.）可点「保留」；若怀疑写错，请点「取消」修改。`, { okText: "保留" });
}

function attachTempNameInput(input) {
  const dl = input._dl || ensureDatalist(input, "_tndl");
  let timer = null;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const q = input.value.replace(/^\s*cf\.?\s*/i, "").trim();
      if (!q) return;
      try {
        const r = await api(`/api/species-suggest?q=${encodeURIComponent(q)}`);
        if (!r.items.length) return;
        dl.innerHTML = r.items.map((it) =>
          `<option value="${esc(it.name)}">${esc(it.status ? it.status + " · " : "")}${esc(it.family)}</option>`).join("");
      } catch { }
    }, 250);
  });
  const cf = document.createElement("button");
  cf.type = "button";
  cf.className = "btn";
  cf.textContent = "cf.";
  const hasCf = () => /^\s*cf\.?\s*/i.test(input.value);
  const syncCf = () => {
    const on = hasCf();
    cf.style.background = on ? "var(--primary)" : "";
    cf.style.color = on ? "#fff" : "";
    cf.title = on ? "已标记鉴定不确定（cf.）。再次点击取消" : "鉴定信心不足：点击加 cf. 前缀（条目显示淡红底色、进入复查队列）";
  };
  cf.onclick = (e) => {
    e.preventDefault();
    if (hasCf()) input.value = input.value.replace(/^\s*cf\.?\s*/i, "");
    else input.value = "cf. " + input.value.replace(/^\s+/, "");
    syncCf();
    input.focus();
    input.dispatchEvent(new Event("input"));
  };
  input.addEventListener("input", syncCf);
  syncCf();
  return cf;
}

/* TemporarySpeciesName 挂载（新增/克隆/抽屉）：联想 + cf. 按钮悬浮于输入框右内侧 */
function wireTempNameField(input) {
  const cf = attachTempNameInput(input);
  const wrap = document.createElement("div");
  wrap.style.cssText = "position:relative";
  input.after(wrap);
  wrap.appendChild(input);
  wrap.appendChild(cf);
  input.style.paddingRight = "56px";
  cf.style.cssText = "position:absolute;right:4px;top:4px;bottom:4px;width:46px;padding:0;font-weight:600";
  return cf;
}

function isCfTempName(v) {
  return /^\s*cf\.?/i.test(String(v || ""));
}

function ensureDatalist(input, key) {
  if (!input[key]) {
    const dl = document.createElement("datalist");
    dl.id = key.replace(/^_/, "") + "-" + Math.random().toString(36).slice(2, 8);
    input.after(dl);
    input.setAttribute("list", dl.id);
    input[key] = dl;
  }
  return input[key];
}

function attachSpeciesSuggest(input) {
  const dl = input._dl || ensureDatalist(input, "_spsdl");
  let timer = null;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const q = input.value.replace(/^\s*cf\.?\s*/i, "").trim();
      if (!q) return;
      try {
        const r = await api(`/api/species-suggest?q=${encodeURIComponent(q)}`);
        if (!r.items.length) return;
        dl.innerHTML = r.items.map((it) =>
          `<option value="${esc(it.name)}">${esc(it.status ? it.status + " · " : "")}${esc(it.family)}</option>`).join("");
      } catch { /* 预测失败不影响录入 */ }
    }, 250);
  });
  // 最终名只接受 WSC 正式名称：异名即时转为接受名；未收录时显示内联提示（名录滞后可保留强制保存）
  const enforceValidName = async () => {
    const v = input.value.trim();
    const parent = input.parentElement;
    if (parent) { const old = parent.querySelector(".fs-warn"); if (old) old.remove(); }
    if (!v) { S._fsForceOk = false; return; }
    try {
      const r = await api(`/api/species-check?name=${encodeURIComponent(v)}`);
      if (r.in_wsc) {
        S._fsForceOk = false;
        if (r.accepted && r.accepted.toLowerCase() !== v.toLowerCase()) {
          input.value = r.accepted;
          toast(`最终名已按 WSC 转为接受名：${r.accepted}`, "ok");
          dl.innerHTML = "";
        }
        return;
      }
      // 内联提示（不用弹窗，避免覆盖正在填写的表单）
      const tip = document.createElement("div");
      tip.className = "fs-warn";
      tip.style.cssText = "display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:12px;color:#b4553f;margin-top:4px";
      const label = document.createElement("span");
      label.textContent = `「${v}」未在当前 WSC 名录中（名录可能滞后于新发表物种）`;
      const bFix = document.createElement("button");
      bFix.type = "button"; bFix.className = "btn"; bFix.textContent = "返回修改";
      bFix.onclick = () => { tip.remove(); input.value = ""; S._fsForceOk = false; input.focus(); };
      const bKeep = document.createElement("button");
      bKeep.type = "button"; bKeep.className = "btn"; bKeep.textContent = "确为正式名，保留";
      bKeep.onclick = () => { tip.remove(); S._fsForceOk = true; toast("保存时将按「名录未同步」强制提交该名称", "ok"); };
      tip.append(label, bFix, bKeep);
      parent.appendChild(tip);
    } catch { /* 校验失败不阻塞录入 */ }
  };
  input.addEventListener("change", enforceValidName);
  input.addEventListener("blur", enforceValidName);
}

/* Tribe/Subtribe/Clade 联想：数据源为分类信息表唯一值；选定后带出其余空缺阶元 */
const TAX_SUGGEST_COLS = ["Tribe", "Subtribe", "Clade1", "Clade2", "Clade3"];
async function taxValueData() {
  if (!S.taxvals) {
    try { S.taxvals = await api("/api/taxon-values"); } catch { S.taxvals = { values: {}, lookup: {} }; }
  }
  return S.taxvals;
}
function attachTaxValueSuggest(input, col) {
  let timer = null;
  input.addEventListener("input", async () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const q = input.value.trim().toLowerCase();
      if (!q) return;
      const { values } = await taxValueData();
      const all = values[col] || [];
      const starts = all.filter((v) => v.toLowerCase().startsWith(q));
      const contains = all.filter((v) => !v.toLowerCase().startsWith(q) && v.toLowerCase().includes(q));
      const opts = [...starts, ...contains].slice(0, 25);
      const dl = ensureDatalist(input, "_taxdl");
      dl.innerHTML = opts.map((o) => `<option value="${esc(o)}">`).join("");
    }, 200);
  });
  const fillUp = async () => {
    const v = input.value.trim();
    if (!v) return;
    const scope = input.closest("#modal-body, #drawer-body");
    if (!scope) return;  // 网格编辑由保存时的服务端向上补齐处理
    const { lookup } = await taxValueData();
    const rec = (lookup[col] || {})[v.toLowerCase()];
    if (!rec) return;
    let filled = [];
    for (const [k, val] of Object.entries(rec)) {
      if (k === col || !val) continue;
      const t2 = scope.querySelector(`input[data-col="${CSS.escape(k)}"], select[data-col="${CSS.escape(k)}"]`);
      if (t2 && !(t2.value || "").trim()) { t2.value = val; filled.push(k); }
    }
    if (filled.length) toast(`已按${col}带出：${filled.join("、")}`, "ok");
  };
  input.addEventListener("change", fillUp);
  input.addEventListener("blur", fillUp);
}

function attachGenusSuggest(input) {
  const dl = input._dl || ensureDatalist(input, "_gsdl");
  let timer = null;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const q = input.value.trim();
      if (!q) return;
      if (!q) return;  // 清空后由级联逻辑恢复默认建议
      try {
        const r = await api(`/api/genus-suggest?q=${encodeURIComponent(q)}`);
        if (!r.items.length) return;
        dl.innerHTML = r.items.map((it) =>
          `<option value="${esc(it.genus)}">${esc(it.family)}</option>`).join("");
      } catch { /* 预测失败不影响录入 */ }
    }, 220);
  });
}

/* 列默认值展开（v4.03）：{{me}} = 当前账号名（鉴定人默认本人，可手动更改） */
function colDefaultVal(col) {
  const d = col.default;
  if (d === undefined || d === null) return "";
  return d === "{{me}}" ? ((S.me && S.me.username) || "") : String(d);
}

function orderedAddFields(fields) {
  const first = curCfg().add_first;
  if (!first || !first.length) return fields;
  const pri = first.filter((n) => fields.some((f) => f.name === n));
  if (!pri.length) return fields;
  return [...pri.map((n) => fields.find((f) => f.name === n)),
          ...fields.filter((f) => !first.includes(f.name))];
}

/* 属名核对：未收录于 WSC/分类信息表时在输入框下方显示警告（不阻断保存） */
function attachGenusCheck(input) {
  const gCheck = async () => {
    const parent = input.parentElement;
    if (parent) { const old = parent.querySelector(".genus-warn"); if (old) old.remove(); }
    const g = input.value.trim();
    if (!g) return;
    try {
      const r = await api(`/api/genus-check?genus=${encodeURIComponent(g)}`);
      if (r.known) return;
      const tip = document.createElement("div");
      tip.className = "genus-warn";
      tip.style.cssText = "font-size:12px;color:var(--warn);margin-top:3px;line-height:1.5";
      tip.textContent = `⚠ 属名「${g}」未在 WSC/分类信息表收录——请核对拼写`
        + (r.suggestion ? `（相近：${r.suggestion}${r.suggestion_family ? "，" + r.suggestion_family : ""}）` : "")
        + "；确为新属可继续保存";
      parent.appendChild(tip);
    } catch { }
  };
  let gTimer = null;
  input.addEventListener("input", () => {
    clearTimeout(gTimer);
    gTimer = setTimeout(gCheck, 600);
  });
  input.addEventListener("change", gCheck);
  input.addEventListener("blur", gCheck);
}

/* 属名→分类链联动（覆盖式）：属名变更时按分类信息/WSC 更新全部上级阶元 */
function wireGenusChainFill(input, container) {
  input.addEventListener("change", async () => {
    const g = input.value.trim();
    if (!g) return;
    try {
      const r = await api(`/api/taxonomy-lookup?genus=${encodeURIComponent(g)}`);
      if (!r.found) return;
      let filled = [];
      for (const [k, v] of Object.entries(r.record)) {
        if (k === "Genus" || !v) continue;
        const el = container.querySelector(`input[data-col="${CSS.escape(k)}"], select[data-col="${CSS.escape(k)}"]`);
        if (el && el.value.trim() !== String(v)) { el.value = v; filled.push(k); }
      }
      if (filled.length) toast(`已按属名「${g}」更新分类链：${filled.join("、")}`, "ok");
    } catch { /* 查询失败不影响录入 */ }
  });
}

function wireGenusLookup(container) {
  if (!curCfg().genus_lookup) return;
  const gEl = container.querySelector('input[data-col="Genus"]');
  if (gEl) {
    attachGenusSuggest(gEl);
    attachGenusCheck(gEl);
    wireGenusChainFill(gEl, container);
  }
  // 最终名联想 + 仅正式名校验；Tribe/Clade 联想 + 向上带出
  const fEl = container.querySelector(`input[data-col="${CSS.escape(taxCols().final)}"]`);
  if (fEl) attachSpeciesSuggest(fEl);
  if (S.cascade && S.cascade[S.t]) {
    TAX_SUGGEST_COLS.forEach((n) => {
      const el = container.querySelector(`input[data-col="${CSS.escape(n)}"]`);
      if (el) attachTaxValueSuggest(el, n);
    });
  }
}

function wireCascade(container) {
  if (!S.cascade[S.t]) return;
  const get = (n) => container.querySelector(`input[data-col="${CSS.escape(n)}"]`);
  const setDl = (input, opts) => {
    if (!input) return;
    let dl = input._dl;
    if (!dl) {
      dl = document.createElement("datalist");
      dl.id = "dl" + Math.random().toString(36).slice(2);
      container.appendChild(dl);
      input.setAttribute("list", dl.id);
      input._dl = dl;
    }
    dl.innerHTML = opts.filter(Boolean).slice(0, 600).map((o) => `<option value="${esc(o)}">`).join("");
  };
  const upd = (changed) => {
    const v = (n) => (get(n)?.value || "").trim();
    if (changed !== "Order") setDl(get("Order"), S.cascade[S.t].orders);
    if (changed !== "Family") setDl(get("Family"), v("Order") && S.cascade[S.t].tree[v("Order")] ? Object.keys(S.cascade[S.t].tree[v("Order")]) : Object.values(S.cascade[S.t].tree).flatMap((f) => Object.keys(f)));
    if (changed !== "Subfamily") {
      const o = v("Order"), f = v("Family");
      let subs = [];
      if (o && S.cascade[S.t].tree[o]?.[f]) subs = Object.keys(S.cascade[S.t].tree[o][f]);
      else for (const oo of Object.keys(S.cascade[S.t].tree)) if (S.cascade[S.t].tree[oo][f]) subs.push(...Object.keys(S.cascade[S.t].tree[oo][f]));
      setDl(get("Subfamily"), subs);
    }
    if (changed !== "Genus") {
      const o = v("Order"), f = v("Family"), sf = v("Subfamily");
      const gs = new Set();
      const collect = (fammap) => { for (const s2 of Object.keys(fammap)) if (!sf || s2 === sf) fammap[s2].forEach((g) => gs.add(g)); };
      if (o && S.cascade[S.t].tree[o]?.[f]) collect(S.cascade[S.t].tree[o][f]);
      else for (const oo of Object.keys(S.cascade[S.t].tree)) if (S.cascade[S.t].tree[oo][f]) collect(S.cascade[S.t].tree[oo][f]);
      setDl(get("Genus"), [...gs]);
    }
  };
  ["Order", "Family", "Subfamily"].forEach((n) => get(n)?.addEventListener("input", () => upd(n)));
  upd(null);
  // Tribe/Subtribe/Clade 联想 + 选定后带出其余阶元
  TAX_SUGGEST_COLS.forEach((n) => {
    const el = get(n);
    if (el) attachTaxValueSuggest(el, n);
  });
}

/* ---- 版本管理 ---- */
$("btn-versions").onclick = openVersions;

async function openVersions() {
  const r = await api(`/api/versions/${S.t}`);
  const canW = canWriteTable(S.t);
  const items = r.versions.map((v) => `
    <div class="ver-item">
      <div class="v-main">
        <div class="v-name">${esc(v.name)} <span class="tag">${v.total.toLocaleString()} 行</span></div>
        <div class="v-sub">${esc(v.note || "")} · ${esc(v.created_by)} · ${esc(v.created_at)}</div>
      </div>
      <button class="btn" data-op="export" data-vid="${v.id}">导出</button>
      <button class="btn" data-op="preview" data-vid="${v.id}">预览</button>
      <button class="btn" data-op="compare" data-vid="${v.id}">对比</button>
      ${canW ? `<button class="btn" data-op="restore" data-vid="${v.id}">恢复</button>` : ""}
      ${S.me.admin || v.created_by === S.me.username ? `<button class="btn danger" data-op="del" data-vid="${v.id}">删除</button>` : ""}
    </div>`).join("") || `<p class="hint">还没有版本快照。保存一个快照后，可以随时导出或恢复到这个时间点。</p>`;
  openModal(`版本管理 · ${curCfg().label}`, `
    <div class="toolbar" style="margin-bottom:0">
      <select id="cmp-a"></select><span class="stat">对比</span><select id="cmp-b"></select>
      <button class="btn" id="cmp-go">查看差异</button>
    </div>
    <div class="ver-list-wrap">${items}</div>
    ${canW ? `
    <div class="ver-create">
      <h4>保存当前数据为新版本</h4>
      <div class="grid-form">
        <label>版本名称<input id="ver-name" placeholder="如：投稿前定稿 / 2026年9月版"></label>
        <label>备注（可选）<input id="ver-note" placeholder="这个版本做了哪些修改"></label>
      </div>
      <div class="form-actions"><button class="btn primary" id="ver-save">保存快照</button></div>
    </div>` : ""}`);
  const opts = [`<option value="current">当前数据</option>`]
    .concat(r.versions.map((v) => `<option value="${v.id}">${esc(v.name)}</option>`));
  $("cmp-a").innerHTML = opts.join("");
  $("cmp-b").innerHTML = opts.join("");
  $("cmp-b").value = "current";
  $("cmp-a").value = r.versions[0] ? String(r.versions[0].id) : "current";
  $("cmp-go").onclick = () => openVersionCompare($("cmp-a").value, $("cmp-b").value);
  $("modal-body").querySelectorAll("button[data-op]").forEach((b) => {
    b.onclick = async () => {
      const vid = +b.dataset.vid;
      const v = r.versions.find((x) => x.id === vid);
      if (b.dataset.op === "export") downloadFile(`/api/export/${S.t}?version=${vid}`).then(() => toast("导出成功", "ok")).catch((e) => toast(e.message, "err"));
      else if (b.dataset.op === "preview") openVersionPreview(vid);
      else if (b.dataset.op === "compare") openVersionCompare(String(vid), "current");
      else if (b.dataset.op === "restore") restoreVersion(vid, v);
      else if (b.dataset.op === "del") {
        if (await confirmBox(`删除版本「${esc(v.name)}」？该操作不影响数据本身。`, { okText: "删除" })) {
          try { await api(`/api/versions/${S.t}/${vid}`, { method: "DELETE" }); toast("已删除版本"); refreshMetaCounts(); openVersions(); }
          catch (e) { toast(e.message, "err"); }
        }
      }
    };
  });
  if (canW) {
    $("ver-save").onclick = async () => {
      try {
        const rr = await api(`/api/versions/${S.t}`, {
          method: "POST",
          body: JSON.stringify({ name: $("ver-name").value, note: $("ver-note").value }),
        });
        toast(`已保存快照（${rr.total} 行）`, "ok");
        refreshMetaCounts();
        openVersions();
      } catch (err) { toast(err.message, "err"); }
    };
  }
}

async function openVersionCompare(a, b) {
  try {
    const d = await api(`/api/versions/${S.t}/compare?a=${a}&b=${b}`);
    const rows = d.changed.map((c) => `
      <tr><td class="key-cell">${esc(c.key)}</td><td>${esc(c.field)}</td>
      <td><span class="old" style="color:#b4443f">${fmtV(c.a)}</span></td>
      <td><span class="new" style="color:#1d7a4f;font-weight:600">${fmtV(c.b)}</span></td></tr>`).join("");
    openModal(`版本差异 · ${esc(d.a_name)} ↔ ${esc(d.b_name)}`, `
      <p class="hint">
        字段变化 <b>${d.changed_total}</b> 处${d.changed_total > d.changed.length ? "（仅显示前 2000 条）" : ""} ·
        仅「${esc(d.b_name)}」有的行 <b>${d.added_total}</b> ·
        仅「${esc(d.a_name)}」有的行 <b>${d.removed_total}</b>
      </p>
      ${rows ? `
      <div class="grid-wrap" style="max-height:46vh">
        <table class="grid"><thead><tr><th>主键</th><th>字段</th><th>${esc(d.a_name)}</th><th>${esc(d.b_name)}</th></tr></thead>
        <tbody>${rows}</tbody></table>
      </div>` : `<p class="hint ck-ok">两个版本的主键数据完全一致 ✔</p>`}
      ${d.added_total ? `<p class="hint" style="margin-top:8px">新增行（主键，最多显示 300）：${d.added.map(esc).join("、")}</p>` : ""}
      ${d.removed_total ? `<p class="hint" style="margin-top:4px">移除行（主键，最多显示 300）：${d.removed.map(esc).join("、")}</p>` : ""}`);
  } catch (e) { toast(e.message, "err"); }
}

async function restoreVersion(vid, v) {
  try {
    const d = await api(`/api/versions/${S.t}/${vid}/diff`);
    if (!(await confirmBox(
      `把「${tblName(S.meta.tables[S.t])}」恢复到版本「${esc(v.name)}」（${esc(v.created_at)}）？\n\n将执行：\n· 更新 ${d.updates} 行（恢复为该版本的内容）\n· 恢复 ${d.creates} 行（该版本有、现在没有）\n· 移除 ${d.deletes} 行（现在有、该版本没有）\n\n恢复本身也会记入变更历史；恢复前建议先保存一个当前快照。`,
      { okText: "恢复" }))) return;
    const r = await api(`/api/versions/${S.t}/${vid}/restore`, { method: "POST" });
    toast(`已恢复：更新 ${r.updates}、恢复 ${r.creates}、移除 ${r.deletes}`, "ok");
    await loadTable();
  } catch (e) { toast(e.message, "err"); }
}

async function openVersionPreview(vid) {
  const st = { page: 1, per: 50, q: "" };
  async function load() {
    const r = await api(`/api/versions/${S.t}/${vid}/rows?page=${st.page}&per=${st.per}&q=${encodeURIComponent(st.q)}`);
    const pages = Math.max(1, Math.ceil(r.total / st.per));
    const cols = r.columns;
    openModal(`预览版本：${r.version.name}`, `
      <p class="hint">${esc(r.version.note || "")} · ${esc(r.version.created_by)} · ${esc(r.version.created_at)} · 共 ${r.total} 行（只读）</p>
      <div class="toolbar">
        <input type="search" id="vp-q" placeholder="搜索…" value="${esc(st.q)}">
        <span class="stat">第 ${r.page}/${pages} 页</span>
        <button class="btn" id="vp-prev" ${r.page <= 1 ? "disabled" : ""}>‹ 上一页</button>
        <button class="btn" id="vp-next" ${r.page >= pages ? "disabled" : ""}>下一页 ›</button>
      </div>
      <div class="grid-wrap" style="max-height:52vh">
        <table class="grid"><thead><tr><th class="rownum">#</th>${cols.map((c) => `<th title="${esc(c.name)}">${esc(c.name)}</th>`).join("")}</tr></thead>
        <tbody>${r.rows.map((row, i) => `<tr><td class="rownum">${(r.page - 1) * st.per + i + 1}</td>${cols.map((c) =>
          `<td class="cell${c.linked ? " linked" : ""}">${fmtV(row.data[c.name])}</td>`).join("")}</tr>`).join("")}</tbody></table>
      </div>`);
    $("vp-q").addEventListener("input", (e) => { clearTimeout(st._t); st._t = setTimeout(() => { st.q = e.target.value.trim(); st.page = 1; load(); }, 300); });
    $("vp-prev").onclick = () => { st.page--; load(); };
    $("vp-next").onclick = () => { st.page++; load(); };
  }
  await load();
}

/* ---- 附件 ---- */
let _attPasteHandler = null;   // 文档级粘贴监听：弹窗重开时替换，弹窗关闭后自然失效
async function openFiles(rid, tname = S.t, keyLabel = null) {
  const row = tname === S.t ? rowById(rid) : null;
  const key = keyLabel || (tname === S.t ? curCfg().key : null);
  const writable = canWriteTable(tname);
  // 弹窗已开（上传/删除后就地刷新）：只重渲染列表，绝不再压一层——
  // openModal 会把当前内容存成「返回层」，重复调用会让关闭时恢复上一份快照（关一次弹窗要关 N 次）。
  // 须同时要求弹窗可见：关闭后 modal-body 仍残留 att-list（隐藏态），不能误判为开着
  const inPlace = !$("modal").hidden && !!$("att-list");
  if (!inPlace) {
    openModal(`附件 · ${esc(key ? (row ? row.data[key] || keyLabel || "#" + rid : keyLabel || "#" + rid) : "#" + rid)}`, `
      ${writable ? `
      <div id="att-drop" class="att-drop" title="支持多选与多文件拖入">
        ${icon("upload")} 把文件<b>拖到这里</b>，或直接 <b>Ctrl+V</b> 粘贴图片，或 <a href="javascript:void 0" id="att-pick">点击选择</a>
        <div class="hint" style="margin-top:4px">上传自动改名为「标本编号_原文件名」，重名自动加序号；剪贴板图片按「粘贴_日期_时间」命名</div>
        <span class="hint" id="att-progress" style="display:none"></span>
        <input type="file" id="att-file" hidden multiple>
      </div>` : ""}
      <div id="att-list" style="margin-top:12px"><p class="hint">加载中…</p></div>`);
    if (writable) {
      async function attUpload(fileList) {
        const files = [...fileList];
        if (!files.length) return;
        const drop = $("att-drop"), prog = $("att-progress");
        const stamp = (f) => {
          // 剪贴板截图通常叫 image.png（或无名 Blob）：改为「粘贴_日期_时间」便于区分
          const named = f.name && !/^image\.(png|jpe?g|gif|webp|bmp)$/i.test(f.name);
          if (named) return f.name;
          const d = new Date(), p = (n) => String(n).padStart(2, "0");
          const ext = f.name && f.name.includes(".") ? f.name.split(".").pop()
            : ((f.type || "").split("/")[1] || "png").replace("jpeg", "jpg");
          return `粘贴_${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}_${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}.${ext}`;
        };
        if (drop) { drop.style.pointerEvents = "none"; drop.style.opacity = ".6"; }
        if (prog) { prog.style.display = ""; prog.textContent = `上传中…（0/${files.length}）`; }
        let ok = 0; const errs = [];
        for (let i = 0; i < files.length; i++) {
          const fd = new FormData();
          fd.append("file", files[i], stamp(files[i]));
          try {
            await api(`/api/rows/${tname}/${rid}/files`, { method: "POST", body: fd });
            ok++;
          } catch (e) { errs.push(`${files[i].name || "文件"}：${e.message}`); }
          if (prog) prog.textContent = `上传中…（${i + 1}/${files.length}）`;
        }
        if (errs.length) toast(`部分失败：${errs.join("；")}`, "err");
        if (ok) toast(`已上传 ${ok} 个附件`, "ok");
        if (ok) renderAttList(rid, tname);   // 就地刷新列表（弹窗层不动）
      }
      const fileInput = $("att-file"), drop = $("att-drop");
      $("att-pick").onclick = (e) => { e.stopPropagation(); fileInput.click(); };
      drop.onclick = () => fileInput.click();
      fileInput.onchange = () => { const fs = [...fileInput.files]; if (fs.length) attUpload(fs); fileInput.value = ""; };
      let dragDepth = 0;
      drop.addEventListener("dragenter", (e) => { e.preventDefault(); dragDepth++; drop.classList.add("drag"); });
      drop.addEventListener("dragover", (e) => e.preventDefault());
      drop.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; drop.classList.remove("drag"); } });
      drop.addEventListener("drop", (e) => {
        e.preventDefault(); dragDepth = 0; drop.classList.remove("drag");
        const fs = [...(e.dataTransfer?.files || [])];
        if (fs.length) attUpload(fs);
      });
      // 粘贴：附件弹窗打开期间 Ctrl+V 即上传（每次打开时挂一次；弹窗关闭后元素消失自动失效）
      if (_attPasteHandler) document.removeEventListener("paste", _attPasteHandler);
      _attPasteHandler = (e) => {
        if ($("modal").hidden || !$("att-drop")) return;
        const fs = [...(e.clipboardData?.files || [])];
        if (!fs.length) return;
        e.preventDefault();
        attUpload(fs);
      };
      document.addEventListener("paste", _attPasteHandler);
    }
  }
  renderAttList(rid, tname);
}

async function renderAttList(rid, tname = S.t) {
  const box = $("att-list");
  if (!box) return;   // 弹窗已被用户关闭（上传/删除进行中）
  const own = await api(`/api/rows/${tname}/${rid}/files`);
  let linked = { linked: [] };
  try { linked = await api(`/api/linked-files/${tname}/${rid}`); } catch { }
  const list = { files: [...(own.files || []), ...(linked.linked || [])] };
  const SRC_LBL = { specimens: "标本", mygalomorph: "原蛛", dna_extract: "DNA 提取", uce: "UCE 文库" };
  box.innerHTML = list.files.length ? list.files.map((f) => `
    <div class="user-row">
      ${IMG_EXT.test(f.fname) ? `<img class="att-thumb" src="${fileUrl(f.id, true)}" data-name="${esc(f.fname)}" style="width:44px;height:44px;object-fit:cover;border-radius:5px;cursor:zoom-in">` : ""}
      <span class="u-name" style="width:auto">${esc(f.fname)}</span>
      <span class="u-sub">${(f.size / 1024).toFixed(1)} KB · ${esc(f.uploaded_by)} · ${esc(f.uploaded_at)}</span>
      ${f.src_table ? `<span class="tag" style="background:var(--primary-soft);color:var(--primary)">来自${esc(SRC_LBL[f.src_table] || f.src_table)}</span>` : ""}
      <button class="btn" data-f="dl" data-id="${f.id}">下载</button>
      ${canWriteTable(f.src_table || tname) ? `<button class="btn danger" data-f="rm" data-id="${f.id}">删除</button>` : ""}
    </div>`).join("") : `<p class="hint">还没有附件。</p>`;
  box.querySelectorAll("img.att-thumb").forEach((im) => {
    im.onclick = () => lightbox(im.src, im.dataset.name);
  });
  box.querySelectorAll("button[data-f]").forEach((b) => {
    b.onclick = async () => {
      if (b.dataset.f === "dl") downloadFile(`/api/files/${b.dataset.id}/download`).catch((e) => toast(e.message, "err"));
      else {
        try { await api(`/api/files/${b.dataset.id}`, { method: "DELETE" }); toast("已删除"); renderAttList(rid, tname); }
        catch (e) { toast(e.message, "err"); }
      }
    };
  });
}

/* ---- 鉴定工作台：把待办从统计数字变成可逐条推进的队列 ---- */
let _wbTab = "identify";
async function loadWorkbench() {
  const v = $("view-workbench");
  v.innerHTML = `<p class="hint">正在加载待办…</p>`;
  let d;
  try { d = await api("/api/workbench"); } catch (e) { v.innerHTML = `<p class="hint">${esc(e.message)}</p>`; return; }
  S._wb = d;
  const tabs = [
    ["identify", "待鉴定", "有暂定名、无最终名 —— 逐条定名后自动移出队列"],
    ["cf_review", "cf. 待复查", "标记鉴定信心不足的标本，复查后去掉 cf. 或定名"],
    ["no_event", "缺采集信息", "无采集事件号 —— 用表格行上的 🌏 补采集，或新增表单手填区建档"],
    ["dna_gap", "DNA 凭证断链", "UCE 文库已有建库记录，但 DNA 提取台账没有对应 DNAVoucherID"],
  ];
  v.innerHTML = `
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px">
      <h2 style="font-size:17px;margin:0">鉴定工作台</h2>
      <span class="hint">把待办从统计数字变成可推进的队列；处理完的条目自动移出</span>
      <span style="flex:1"></span>
      <button class="btn" id="wb-refresh">刷新</button>
    </div>
    <div class="mem-tabs" style="margin-bottom:10px">
      ${tabs.map(([k, t, tip]) => `<button class="mem-tab ${k === _wbTab ? "active" : ""}" data-wt="${k}" title="${esc(tip)}">${t} <span class="pill">${d[k].n}</span></button>`).join("")}
    </div>
    <div id="wb-list"></div>`;
  v.querySelectorAll(".mem-tab").forEach((b) => b.onclick = () => { _wbTab = b.dataset.wt; loadWorkbench(); });
  $("wb-refresh").onclick = loadWorkbench;
  renderWbList();
}

function renderWbList() {
  const d = S._wb || {};
  const info = d[_wbTab] || { n: 0, items: [] };
  const box = $("wb-list");
  if (!info.n) {
    box.innerHTML = `<div class="dash-card" style="padding:26px;text-align:center;color:var(--ok)"><b>此类待办已全部完成 ✔</b></div>`;
    return;
  }
  const shown = info.items;
  const metaHTML = (it) => {
    const bits = [];
    if (it.type) bits.push(`★${esc(it.type)}`);
    if (it.loc) bits.push(esc(it.loc));
    if (it.date) bits.push(esc(it.date));
    if (it.sex) bits.push(esc(it.sex));
    return bits.length ? `<div class="wb-meta">${bits.join(" · ")}</div>` : `<div class="wb-meta wb-empty">暂无采集/性别信息</div>`;
  };
  const attBtn = (it) => `<button class="btn" data-att="${it.rid}" title="鉴定常用标本照：点击上传/查看该标本的附件">${it.att ? `图片 ${it.att}` : `＋图片`}</button>`;
  const rows = shown.map((it, idx) => {
    if (_wbTab === "identify") {
      return `<div class="wb-row" data-rid="${it.rid}">
        <div class="wb-info">
          <div class="wb-line1"><span class="wb-key">${esc(it.key)}</span><span class="wb-temp" title="暂定名">${esc(it.temp) || '<span class="none">（无暂定名）</span>'}</span></div>
          ${metaHTML(it)}
        </div>
        <span class="wb-op">
          <input class="wb-name" data-idx="${idx}" placeholder="输入 WSC 正式名（联想），回车保存">
          <button class="btn primary" data-save="${idx}">定名</button>
          ${attBtn(it)}
          <button class="btn" data-open="${it.rid}" data-key="${esc(it.key || "")}" title="在表格中定位并打开详情">查看</button>
        </span>
      </div>`;
    }
    if (_wbTab === "cf_review") {
      return `<div class="wb-row" data-rid="${it.rid}">
        <div class="wb-info">
          <div class="wb-line1"><span class="wb-key">${esc(it.key)}</span><span class="wb-temp">${esc(it.temp) || ""}</span></div>
          ${metaHTML(it)}
        </div>
        <span class="wb-op">
          ${attBtn(it)}
          <button class="btn" data-open="${it.rid}" data-key="${esc(it.key || "")}">去处理</button>
        </span>
      </div>`;
    }
    const main = _wbTab === "dna_gap"
      ? `<span class="wb-key">${esc(it.key)}</span><span class="wb-temp">DNAVoucherID：${esc(it.sample)}${it.species ? " · " + esc(it.species) : ""}</span>`
      : `<span class="wb-key">${esc(it.key)}</span><span class="wb-temp">${esc(it.temp || "")}${it.event !== undefined && it.event === "" ? '<span class="loan-open">无采集事件号</span>' : ""}</span>`;
    return `<div class="wb-row">
      ${main}
      <span class="wb-op"><button class="btn" data-open="${it.rid}" data-key="${esc(it.key || "")}">去处理</button></span>
    </div>`;
  }).join("");
  box.innerHTML = `
    ${info.n > shown.length ? `<p class="hint">显示前 ${shown.length} 条（共 ${info.n} 条），处理完刷新看下一批</p>` : ""}
    <div class="wb-list">${rows}</div>`;
  /* 图片/附件：直接在该行打开上传与查看 */
  box.querySelectorAll("button[data-att]").forEach((b) => b.onclick = () => {
    const it = shown.find((x) => x.rid === +b.dataset.att);
    openFiles(+b.dataset.att, "specimens", it ? it.key : null);
  });
  /* 去处理/查看：跳到对应表并打开行详情（表格是服务端分页，按键值过滤把目标行收进第一页） */
  box.querySelectorAll("button[data-open]").forEach((b) => b.onclick = () => {
    const rid = +b.dataset.open;
    const t = _wbTab === "dna_gap" ? "uce" : "specimens";
    switchView(t);
    S.page = 1; S.q = b.dataset.key || ""; S.preset = null; S.showDeleted = false;
    const sb = $("search");
    if (sb) sb.value = S.q;
    setTimeout(async () => {
      await loadTable();
      if (!rowById(rid)) {
        toast("未能定位到该行（键值为空或匹配过多），请在该表内手动搜索", "err");
        return;
      }
      openRowDetail(rid);
    }, 300);
  });
  /* 快速定名 */
  box.querySelectorAll("input.wb-name").forEach((inp) => {
    attachSpeciesSuggest(inp);
    const doSave = async () => {
      const idx = +inp.dataset.idx;
      const it = shown[idx];
      const name = inp.value.trim();
      if (!name) return;
      const btn = box.querySelector(`button[data-save="${idx}"]`);
      btn.disabled = true;
      try {
        await api(`/api/rows/specimens/${it.rid}`, {
          method: "PUT",
          body: JSON.stringify({ changes: { FinalSpeciesName: name }, base_version: (rowById(it.rid) || {}).row_version }),
        });
        toast(`已定名：${name}（${it.key}）`, "ok");
        inp.closest(".wb-row").remove();
        if (S._wb && S._wb.identify) S._wb.identify.n = Math.max(0, S._wb.identify.n - 1);
        refreshMetaCounts();
        const pill = document.querySelector('.mem-tab[data-wt="identify"] .pill');
        if (pill && S._wb) pill.textContent = S._wb.identify.n;
      } catch (e) {
        btn.disabled = false;
        if (e.message && e.message.includes("未在 WSC")) toast(e.message + "；若确为正式名请到表格编辑（支持名录未同步强制保存）", "err");
        else toast(e.message, "err");
      }
    };
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); doSave(); } });
    box.querySelector(`button[data-save="${inp.dataset.idx}"]`).onclick = doSave;
  });
}

/* ---- 标本盒示意图（详情抽屉顶部）：正方形盒（36 孔 = 6×6 / 100 孔 = 10×10），
   按编号顺延定位行列；其余孔淡灰、本标本淡绿高亮 ---- */
async function renderBoxMap(rid) {
  const holder = $("det-boxmap");
  if (!holder) return;
  holder.style.display = "none";
  holder.innerHTML = "";
  const row = rowById(rid);
  const m = String(row?.data["MHBU #"] || "").trim().match(/^MHBU-ARA-(\d{8})$/);
  if (!m) return;  // 无规范编号：不画
  const bl = await loadBoxes().catch(() => null);
  const num = +m[1];
  const b = ((bl && bl.boxes) || []).find((x) => x.num_start <= num && num <= x.num_end);
  if (!b) return;  // 编号不在任何已登记盒号段内：不画
  const total = b.num_end - b.num_start + 1;
  let side = Math.round(Math.sqrt(total));
  if (side < 1 || side * side < total) side = Math.ceil(Math.sqrt(total));  // 非正方形号段按近方网格兜底
  const rowsN = Math.ceil(total / side);
  const idx = num - b.num_start;  // 盒内第几孔（0 起，编号顺延）
  const r = Math.floor(idx / side) + 1, c = (idx % side) + 1;
  let grid = `<span></span>${Array.from({ length: side }, (_, i) => `<span class="bm-guide">${i + 1}</span>`).join("")}`;
  for (let ri = 0; ri < rowsN; ri++) {
    grid += `<span class="bm-guide">${ri + 1}</span>`;
    for (let ci = 0; ci < side; ci++) {
      const i = ri * side + ci;
      if (i >= total) { grid += "<span></span>"; continue; }
      const hit = i === idx;
      grid += `<span class="bh${hit ? " hit" : ""}" title="${hit ? "本标本 " + esc(m.input) : "第 " + (i + 1) + " 孔"}"></span>`;
    }
  }
  const w = Math.min(176, 13 + side * 26 + (side + 1) * 2);
  holder.innerHTML = `
    <h3>${icon("box")} 标本盒示意 · ${esc(b.name)}</h3>
    <div class="boxmap-wrap">
      <div class="boxmap" style="grid-template-columns:13px repeat(${side},1fr);width:${w}px">${grid}</div>
      <div class="boxmap-info">
        <p>${total} 孔（${side}×${rowsN}）· 编号顺延</p>
        <p class="boxmap-pos">本标本在 <b>第 ${r} 行 · 第 ${c} 列</b><br>盒内第 ${idx + 1} 孔，共 ${total} 孔</p>
        <p class="boxmap-legend"><span class="bh hit"></span>淡绿 = 本标本位置</p>
      </div>
    </div>`;
  holder.style.display = "";
}

/* ---- 历次鉴定（详情抽屉）：按鉴定序号展示每一条鉴定及其名录核对结果 ---- */
const DET_TABLES = ["specimens", "mygalomorph", "dali", "swu"];
const _CK_BADGE = {
  accepted: ["已收录", "var(--ok, #2e7d32)"],
  synonym: ["异名→接受名", "#b26a00"],
  not_found: ["未收录", "#b00020"],
  service_unavailable: ["名录不可用", "#6b7280"],
};
function detBadge(st) {
  const b = _CK_BADGE[st] || [st || "—", "#6b7280"];
  return `<span style="border:1px solid currentColor;border-radius:10px;padding:0 7px;font-size:11px;color:${b[1]}">${b[0]}</span>`;
}
async function loadDeterminations(rid) {
  try {
    const r = await api(`/api/rows/${S.t}/${rid}/determinations`);
    const box = $("det-detlist");
    if (!box) return;
    box.innerHTML = renderDeterminations(r.items || [], r.protected);
  } catch { /* 抽屉已关闭或无权限：静默 */ }
}
function renderDeterminations(items, prot) {
  if (!items.length) {
    return `<div class="ck-card" style="padding:12px 14px"><h3 style="font-size:13px">${icon("tag")} 历次鉴定</h3>
      <p class="hint" style="margin:6px 0 0">尚无鉴定记录（填写 FinalSpeciesName / TemporarySpeciesName 后自动记录）</p></div>`;
  }
  const srcLabel = { manual: "人工修改", create: "新增录入", import: "导入", calibration: "名录校准", backfill: "迁移回填" };
  const rows = items.map((d) => {
    const name = d.name_accepted && d.name_accepted !== d.name_raw
      ? `${esc(d.name_raw)} → ${esc(d.name_accepted)}` : esc(d.name_raw);
    return `
    <tr>
      <td style="text-align:right">${d.seq}</td>
      <td>${name}${d.det_detail ? `<div class="hint" style="font-size:11px">${esc(d.det_detail)}</div>` : ""}</td>
      <td>${esc(d.det_status || "—")}${d.confirmed_by ? `<div class="hint" style="font-size:11px">确认：${esc(d.confirmed_by)}</div>` : ""}</td>
      <td>${detBadge(d.checklist_status)}${d.checklist_ver ? `<div class="hint" style="font-size:11px">名录 ${esc(d.checklist_ver)}</div>` : ""}</td>
      <td>${esc(d.determiner || "—")}${d.det_date ? `<div class="hint" style="font-size:11px">${esc(d.det_date)}</div>` : ""}</td>
      <td>${esc(d.evidence || "—")}</td>
      <td><span class="hint">${esc(srcLabel[d.source] || d.source || "—")}</span></td>
    </tr>`;
  }).join("");
  return `<div class="ck-card" style="padding:12px 14px">
    <h3 style="font-size:13px">${icon("tag")} 历次鉴定（${items.length}）${prot ? `<span class="hint" style="font-weight:400;margin-left:8px">受保护：${esc(prot)}</span>` : ""}</h3>
    <div style="overflow-x:auto;margin-top:6px">
    <table class="tbl" style="width:100%;font-size:12px">
      <thead><tr><th>#</th><th>名称（原文 → 接受名）</th><th>复核状态</th><th>名录核对</th><th>鉴定人/日期</th><th>依据</th><th>来源</th></tr></thead>
      <tbody>${rows}</tbody></table></div>
    <p class="hint" style="margin:6px 0 0">名录收录与复核状态相互独立：「名录不可用」只是暂时无法核对，不构成分类结论。</p></div>`;
}
async function openDeterminations(rid) {
  try {
    const r = await api(`/api/rows/${S.t}/${rid}/determinations`);
    openModal("历次鉴定 · " + (tblName(S.meta.tables[S.t]) || S.t), `
      ${renderDeterminations(r.items || [], r.protected)}
      <div class="form-actions"><button class="btn" id="dets-close">关闭</button></div>`);
    $("dets-close").onclick = closeModal;
  } catch (e) { toast(e.message, "err"); }
}

/* ---- 行详情抽屉（单条记录全部字段，分组排版） ---- */
/* ---- 详情抽屉导航（v3.75）：固定头/上一条下一条/深链/返回栈 ---- */
let drawerNav = null;
let drawerStack = [];
let lastHash = location.hash || "";
function recordHashOf(t, rid) { return "#/r/" + encodeURIComponent(t) + "/" + rid; }
function parseRecordHash() { const m = /^#\/r\/([^/]+)\/(\d+)$/.exec(location.hash); return m ? { t: decodeURIComponent(m[1]), rid: +m[2] } : null; }
function setRecordHash(t, rid) {
  const h = recordHashOf(t, rid);
  if (location.hash !== h) location.hash = h; else lastHash = h;
}
function clearRecordHash() {
  if (parseRecordHash()) history.pushState(null, "", location.pathname + location.search);
  lastHash = location.hash || "";
}
function drawerQueryContext(tname) {
  if (S.t === tname && S.meta.tables[tname])
    return { q: S.q || "", filters: JSON.parse(JSON.stringify(S.filters || {})),
             preset: S.preset || "", sort: S.sort || "", dir: S.dir || "asc", deleted: !!S.showDeleted };
  return { q: "", filters: {}, preset: "", sort: "", dir: "asc", deleted: false };
}
async function drawerNavGuard(action) { return closeGate(action); }


function setupDrawerTop2(tname, row, key) {
  const d = row.data;
  const tc = taxCols(S.meta.tables[tname]);
  const sci = d[tc.final] || d[tc.temp] || d.Species || d[tc.genus] || "";
  const st = d.DetStatus || "";
  const badges = [];
  if (row.deleted) badges.push(`<span class="st st-risk">回收站</span>`);
  if (st) badges.push(`<span class="st ${({ "已复核": "st-saved", "暂定": "st-pending", "有争议": "st-risk" }[st] || "st-readonly")}">${esc(st)}</span>`);
  if (d.TypeSpecimens && String(d.TypeSpecimens).toLowerCase() !== "no") badges.push(`<span class="st st-pending">★ 模式</span>`);
  const top = $("drawer-top2");
  top.hidden = false;
  top.innerHTML = `
    <b>${esc(d[key] ?? "#" + row.id)}</b>
    ${sci ? `<span class="dt-sci">${esc(sci)}</span>` : ""}
    ${badges.join("")}
    <span style="flex:1"></span>
    <button class="btn" id="det-prev" type="button" title="上一条（PageUp）">↑</button>
    <span class="dt-pos" id="dt-pos"></span>
    <button class="btn" id="det-next" type="button" title="下一条（PageDown）">↓</button>
    <span id="dt-notice"></span>`;
  $("det-prev").onclick = () => navRecord(-1);
  $("det-next").onclick = () => navRecord(1);
  const prevNav = drawerNav && drawerNav.tname === tname ? drawerNav : null;
  drawerNav = { tname, rid: row.id, key,
                query: prevNav ? prevNav.query : drawerQueryContext(tname) };
  refreshDrawerNav();
}
async function refreshDrawerNav() {
  if (!drawerNav) return;
  const q = drawerNav.query;
  const p = new URLSearchParams();
  if (q.q) p.set("q", q.q);
  if (Object.keys(q.filters || {}).length) p.set("filters", JSON.stringify(q.filters));
  if (q.preset) p.set("preset", q.preset);
  if (q.sort) { p.set("sort", q.sort); p.set("dir", q.dir); }
  if (q.deleted) p.set("deleted", "1");
  try {
    const d = await api(`/api/rows/${drawerNav.tname}/${drawerNav.rid}/neighbors?` + p);
    Object.assign(drawerNav, { prev: d.prev, next: d.next, index: d.index, total: d.total,
                               inScope: d.in_scope, suggestion: d.suggestion });
    const pos = $("dt-pos");
    if (!pos) return;
    if (d.in_scope) pos.textContent = `第 ${d.index} / ${d.total} 条`;
    else pos.innerHTML = `<span style="color:var(--c-risk)">该记录已不在当前筛选范围内</span>`;
    const pv = $("det-prev"), nx = $("det-next");
    pv.innerHTML = d.prev ? `↑ ${esc(d.prev.key)}` : "↑ 上一条";
    nx.innerHTML = d.next ? `${esc(d.next.key)} ↓` : "下一条 ↓";
    pv.disabled = !d.prev;
    nx.disabled = !d.next && !!d.in_scope;
    const notice = $("dt-notice");
    if (notice) {
      if (!d.in_scope && d.suggestion) {
        notice.innerHTML = `<button class="btn" id="dt-jump" type="button">定位下一条（${esc(d.suggestion.key)}）</button>`;
        $("dt-jump").onclick = () => navTo(d.suggestion.rid);
      } else notice.textContent = "";
    }
  } catch { }
}
async function navTo(rid) {
  if (!(await drawerNavGuard("切换记录"))) return;
  await openDrawerRecord(drawerNav.tname, rid);
}
function navRecord(delta) {
  if (!drawerNav) return;
  const t = delta < 0 ? drawerNav.prev : drawerNav.next;
  if (!t) { toast(delta < 0 ? "已是范围内第一条" : "已是范围内最后一条"); return; }
  navTo(t.rid);
}
async function openDrawerRecord(tname, rid) {
  if (S.t !== tname) { await switchView(tname); await new Promise((r) => setTimeout(r, 500)); }
  let row = rowById(rid);
  if (!row) {
    try {
      const d = await api(`/api/rows/${tname}/${rid}`);
      row = { id: d.row.id, data: d.row.data, deleted: d.row.deleted,
              row_version: d.row.row_version, updated_at: d.row.updated_at, updated_by: d.row.updated_by };
      if (!S.rows.some((r2) => r2.id === rid)) S.rows.unshift(row);
    } catch (e) {
      toast(e.message, "err");
      if (parseRecordHash()) history.pushState(null, "", location.pathname + location.search);
      return;
    }
  }
  drawerStack = [];
  openRowDetail(rid);
}
function returnToGridRow(tname, rid) {
  try {
    const ck = document.querySelector(`input[data-ckrid="${rid}"]`);
    if (ck) {
      const tr = ck.closest("tr");
      tr.scrollIntoView({ block: "center" });
      tr.style.background = "var(--c-saved-soft, #e7f3e9)";
      setTimeout(() => { tr.style.background = ""; }, 1400);
    }
  } catch { }
}
window.addEventListener("hashchange", async () => {
  const rec = parseRecordHash();
  if (rec) {
    if (!$("drawer").hidden && S.drawerRid === rec.rid && drawerNav && drawerNav.tname === rec.t) { lastHash = location.hash; return; }
    if (!(await drawerNavGuard("打开链接的记录"))) {
      history.replaceState(null, "", lastHash || (location.pathname + location.search));
      return;
    }
    await openDrawerRecord(rec.t, rec.rid);
    lastHash = location.hash;
    return;
  }
  if (!$("drawer").hidden) {
    if (!(await drawerNavGuard("关闭详情（浏览器后退）"))) {
      history.replaceState(null, "", lastHash || (location.pathname + location.search));
      return;
    }
    closeDrawer();
  }
  lastHash = location.hash || "";
});

function openRowDetail(rid) {
  S.drawerRid = rid;
  const row = rowById(rid);
  if (!row) return;
  const key = curCfg().key;
  const cols = S.columns;
  const cfgG = curCfg().groups;
  const lkeyAll = curCfg().linked_key;
  const ceNumAll = lkeyAll ? String(row.data[lkeyAll] || "").trim() : "";
  const unlinked = curCfg().linked_source && cols.some((c) => c.linked) && (!ceNumAll || row.miss);
  let groups = cfgG && cfgG.length
    ? cfgG.map((g) => ({ title: g.title, cols: cols.filter((c) => g.cols.includes(c.name)) }))
        .filter((g) => g.cols.length)
    : [{ title: "全部字段", cols }];
  const sections = groups.map((g) => `
    <div class="ck-card" style="padding:12px 14px">
      <h3 style="font-size:13px">${esc(g.title)}</h3>
      ${unlinked && g.cols.some((c) => c.linked) ? `<p class="hint" style="margin:0 0 8px">${ceNumAll
        ? "⚠ 该事件号在采集地信息中不存在（失配）——点击下方任一采集字段即可补建事件、补充信息"
        : "未关联采集事件——点击下方任一采集字段即可直接填写采集信息（自动建事件并关联）"}</p>` : ""}
      ${g.cols.map((c) => {
        const editable = !c.linked && canWriteTable(S.t) && !S.showDeleted;
        const dv = displayVal(c.name, row.data[c.name]);
        return `<div class="det-row" data-col="${esc(c.name)}"><span class="det-k" title="${esc(detTitle(c.name))}">${esc(c.label || c.name)}</span>
        <span class="det-v ${c.linked ? "linked" : ""} ${editable ? "editable" : ""}"
          ${editable ? `title="点击编辑"` : ""}>${SCI_COLS.has(c.name) ? sciV(dv) : fmtV(dv)}</span></div>`; }).join("")}
    </div>`).join("");
  const typeV = curCfg().type_field ? String(row.data[curCfg().type_field] || "").trim() : "";
  const typeBadge = typeV && typeV.toLowerCase() !== "no" ? `（★ 模式标本：${typeV}）` : "";
  openDrawer(`详情 · ${esc(key ? row.data[key] || "#" + rid : "#" + rid)}${esc(typeBadge)}`);
  $("drawer-body").innerHTML = `
    <input id="det-filter" type="search" placeholder="输入字段名快速定位（如：备注 / 坐标 / 鉴定）…"
      style="width:100%;padding:6px 10px;border:1px solid var(--border);border-radius:8px;font-size:12.5px;outline:none">
    <div class="form-actions" style="margin:0 0 4px">
      <button class="btn" id="det-hist">${icon("clock")} 修改历史</button>
      <button class="btn" id="det-dets">${icon("tag")} 历次鉴定</button>
      ${["specimens", "dali", "swu"].includes(S.t) && canWriteTable(S.t) ? `<button class="btn" id="det-mov">${icon("box")} 登记流转</button>` : ""}
      ${["specimens", "dali", "swu"].includes(S.t) ? `<button class="btn" id="det-cites">${icon("copy")} 被引文献</button>` : ""}
      ${(S.t === "uce" || S.t === "dna_extract") ? `<button class="btn" id="det-chain">${icon("linked")} 样本链</button>` : ""}
      <button class="btn" id="det-files">${icon("paperclip")} 附件</button>
      <button class="btn" id="det-copy">${icon("copy")} 复制整行</button>
      ${canWriteTable(S.t) && !S.showDeleted ? `<button class="btn primary" id="det-clone">${icon("plus")} 以此行新增</button>` : ""}
    </div>
    <div id="det-boxmap" class="boxmap-card" style="display:none"></div>
    ${sections}
    <div id="det-movlist" style="margin-top:8px"></div>
    <div id="det-citelist" style="margin-top:8px"></div>
    <div id="det-detlist" style="margin-top:8px"></div>
    <div id="det-att" style="margin-top:8px"></div>`;
  if (S.t === "specimens") renderBoxMap(rid);
  if (DET_TABLES.includes(S.t)) loadDeterminations(rid);
  if (["specimens", "dali", "swu"].includes(S.t)) loadDrawerMovements(S.t, rid);
  if (["specimens", "dali", "swu"].includes(S.t)) loadDrawerCitations(S.t, rid);
  const movBtn = $("det-mov");
  if (movBtn) movBtn.onclick = () => movDialog({ tname: S.t, rid });
  const citesBtn = $("det-cites");
  if (citesBtn) citesBtn.onclick = () => {
    const num = String(row.data[key] || "#" + rid);
    bibCiteForm(null, { title: "（从标本详情登记）" }, { tname: S.t, rid, num });
  };
  // v3.75：固定头（编号/学名/状态/上下条）+ 深链 + 视图栈基座
  setupDrawerTop2(S.t, row, key);
  drawerStack = [{ type: "detail", tname: S.t, rid }];
  $("drawer-back").hidden = true;
  setRecordHash(S.t, rid);
  // 字段快速定位：按字段名过滤显示
  const detFilter = $("det-filter");
  if (detFilter) {
    detFilter.oninput = () => {
      const q = detFilter.value.trim().toLowerCase();
      $("drawer-body").querySelectorAll(".ck-card").forEach((card) => {
        let vis = 0;
        card.querySelectorAll(".det-row").forEach((dr) => {
          const col = dr.dataset.col || "";
          const doc = S.docs[col] || "";
          const hit = !q || col.toLowerCase().includes(q) || doc.toLowerCase().includes(q);
          dr.style.display = hit ? "" : "none";
          if (hit) vis++;
        });
        card.style.display = vis ? "" : "none";
      });
    };
  }
  $("det-hist").onclick = () => openRowHistory(rid);
  $("det-dets").onclick = () => openDeterminations(rid);
  const chainBtn = $("det-chain");
  if (chainBtn) chainBtn.onclick = () => {
    const vid = S.t === "uce"
      ? String(row.data["Sample Code"] || "").trim()
      : String(row.data["DNAVoucherID"] || "").trim();
    if (!vid) return toast("该行没有凭证号（Sample Code / DNAVoucherID 为空）", "err");
    openLineage(vid);
  };
  $("det-files").onclick = () => openFiles(rid);
  $("det-copy").onclick = () => copyRow(rid);
  const cloneBtn = $("det-clone");
  if (cloneBtn) cloneBtn.onclick = () => { closeDrawer(); cloneRow(rid); };
  // 字段值点击编辑（与表格同一保存链路：校验/属名联动/冲突检测/留痕）
  $("drawer-body").querySelectorAll(".det-row[data-col]").forEach((dr) => {
    const col = S.columns.find((c) => c.name === dr.dataset.col);
    const vSpan = dr.querySelector(".det-v");
    if (!col || !vSpan) return;
    if (col.linked) {
      // 采集关联字段（v3.91/v3.94）：
      //   已填事件号 → 点击写穿透改事件行（失配时引导补建）；
      //   未填事件号 → 点击直接填写采集信息（自动建事件并关联本标本）
      const lkey = curCfg().linked_key;
      const ceNum = lkey ? String(row.data[lkey] || "").trim() : "";
      if (S.showDeleted) return;
      if (!ceNum) {
        if (!canWriteTable(S.t)) return;
        vSpan.classList.add("editable");
        vSpan.title = "未关联采集事件：点击填写采集信息（自动建立事件并关联，编号可留空自动生成）";
        vSpan.onclick = () => healEventModal("", rid);
        return;
      }
      if (!canWriteTable("events")) return;
      vSpan.classList.add("editable");
      vSpan.title = "点击编辑采集地信息（改动记入事件「" + ceNum + "」，留痕在采集地表）";
      vSpan.onclick = () => editLinkedEventValue(vSpan, rid, col, ceNum);
      return;
    }
    if (!vSpan.classList.contains("editable")) return;
    vSpan.onclick = () => editDetValue(vSpan, rid, col);
  });
  // 附件预览条
  api(`/api/linked-files/${S.t}/${rid}`).then((fl) => {
    const box = $("det-att");
    const SRC_LBL = { specimens: "标本", mygalomorph: "原蛛", dna_extract: "DNA 提取", uce: "UCE 文库" };
    // own 含已删除（还原入口在附件对话框）；预览条只显示在架的 own + 跨表共享
    const isDel = (f) => Number(f.deleted) === 1;   // 历史行 deleted 可能是 TEXT '0'/'1'，归一判断
    const delN = (fl.own || []).filter(isDel).length;
    const files = [...(fl.own || []).filter((f) => !isDel(f)), ...(fl.linked || [])];
    if (!files.length) {
      box.innerHTML = delN
        ? `<p class="hint" style="margin:0">本记录有 ${delN} 个附件在回收站——点上方「附件」可查看并还原</p>`
        : `<p class="hint" style="margin:0">暂无附件，点上方「附件」上传标本照等文件</p>`;
      return;
    }
    const head = delN ? `<p class="hint" style="margin:0 0 6px">另有 ${delN} 个附件在回收站（点上方「附件」管理）</p>` : "";
    const srcTag = (f) => f.src_table ? ` · 来自${SRC_LBL[f.src_table] || f.src_table}` : "";
    // 轮播（v4.09）：一次显示一个附件——图片整幅、PDF 显示首页（内嵌查看器），左右箭头切换
    const slides = files
      .filter((f) => IMG_EXT.test(f.fname) || /\.pdf$/i.test(f.fname))
      .map((f) => ({ f, kind: /\.pdf$/i.test(f.fname) ? "pdf" : "img" }));
    const others = files.filter((f) => !IMG_EXT.test(f.fname) && !/\.pdf$/i.test(f.fname));
    box.innerHTML = head + `
      ${slides.length ? `<div class="att-carousel">
        <div class="ac-stage">
          ${slides.map((s, i) => s.kind === "img"
            ? `<img class="ac-slide" data-i="${i}" src="${fileUrl(s.f.id, true)}" alt="${esc(s.f.fname)}"
                 title="${esc(s.f.fname)}${esc(srcTag(s.f))}（点击放大）" data-name="${esc(s.f.fname)}" ${i ? "hidden" : ""}>`
            : `<embed class="ac-slide" data-i="${i}" type="application/pdf" title="${esc(s.f.fname)}${esc(srcTag(s.f))}"
                 src="${fileUrl(s.f.id, true)}" ${i ? "hidden" : ""}>`).join("")}
          ${slides.length > 1 ? `
          <button class="ac-nav ac-prev" type="button" aria-label="上一个附件">‹</button>
          <button class="ac-nav ac-next" type="button" aria-label="下一个附件">›</button>
          <span class="ac-count"><b class="ac-cur">1</b> / ${slides.length}</span>` : ""}
        </div>
        <div class="ac-cap">
          <span class="fchip" style="max-width:70%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${icon("paperclip")} <span class="ac-name">${esc(slides[0].f.fname)}${esc(srcTag(slides[0].f))}</span></span>
          <span class="ac-actions"></span>
        </div>
      </div>` : ""}
      ${others.length ? `<div>${others.map((f) =>
        `<span class="fchip">${icon("paperclip")} <a href="${fileUrl(f.id)}" target="_blank" style="color:var(--primary)">${esc(f.fname)}</a>${esc(srcTag(f))}</span>`).join(" ")}</div>` : ""}`;
    const car = box.querySelector(".att-carousel");
    if (car) {
      const slideEls = car.querySelectorAll(".ac-slide");
      const cur = car.querySelector(".ac-cur");
      const nameEl = car.querySelector(".ac-name");
      const act = car.querySelector(".ac-actions");
      let ci = 0;
      const renderCap = () => {
        const s = slides[ci];
        nameEl.textContent = s.f.fname + srcTag(s.f);
        act.innerHTML = s.kind === "pdf"
          ? `<button class="btn" data-pv="${s.f.id}" data-name="${esc(s.f.fname)}">预览</button>
             <a class="btn" href="${fileUrl(s.f.id)}" target="_blank">新标签打开</a>`
          : `<a class="btn" href="${fileUrl(s.f.id)}" target="_blank">下载原图</a>`;
        act.querySelectorAll("button[data-pv]").forEach((b) => {
          b.onclick = () => openPdfPreview(+b.dataset.pv, b.dataset.name);
        });
      };
      if (slides.length > 1) {
        const show = (n) => {
          ci = (n + slides.length) % slides.length;
          slideEls.forEach((el) => { el.hidden = +el.dataset.i !== ci; });
          if (cur) cur.textContent = ci + 1;
          renderCap();
        };
        car.querySelector(".ac-prev").onclick = () => show(ci - 1);
        car.querySelector(".ac-next").onclick = () => show(ci + 1);
      }
      renderCap();
      car.querySelectorAll("img.ac-slide").forEach((im) => {
        im.onclick = () => lightbox(im.src, im.dataset.name);
      });
    }
  }).catch(() => { });
}

/* ---- PDF 大面板预览 ---- */
function openPdfPreview(fid, fname) {
  const url = fileUrl(fid, true);
  openModal("预览 · " + (fname || "PDF"), `
    <iframe src="${url}" style="width:100%;height:76vh;border:1px solid var(--border);border-radius:8px"
      title="PDF 预览"></iframe>
    <div class="form-actions">
      <a class="btn" href="${url}" target="_blank">新标签打开</a>
      <button class="btn" id="pdf-close">关闭</button>
    </div>`);
  $("pdf-close").onclick = closeModal;
}

/* ---- 样本链：采集事件→标本→DNA 提取→UCE 文库→SRR（只显示有权限的表节点） ---- */
const _LINEAGE_CLS = {
  traced: ["记录可追溯", "st-saved", "链上任一环有同凭证实体"],
  alias_candidate: ["编号差异候选", "st-pending", "大小写/编号归一后唯一命中——仅候选，需人工确认别名后才参与匹配"],
  pending: ["待补历史记录", "st-pending", "本实验室体系（JXZ 系）凭证未在提取台账收录"],
  external: ["外部来源", "st-readonly", "该凭证前缀从未在提取台账出现（合作方/外部样本）"],
  unknown: ["未明", "st-readonly", "空值/占位符/无法归类"],
};
async function openLineage(vid) {
  try {
    const ch = await api(`/api/lineage/voucher/${encodeURIComponent(vid)}`);
    const cls = _LINEAGE_CLS[ch.classification] || [ch.classification, "st-readonly", ""];
    const sec = (title, items, fmt) => !items || !items.length ? "" :
      `<h4 style="font-size:12.5px;margin:10px 0 4px">${esc(title)}（${items.length}）</h4>` +
      items.map((x) => `<div class="det-row" style="padding:3px 0">${fmt(x)}</div>`).join("");
    const cands = (ch.candidates || []).map((c) => `
      <div class="edit-bar" style="margin:6px 0">
        <div class="edit-msg">候选：${esc(c.original)} → ${esc(c.canonical)}（${esc(c.basis)}）——非唯一匹配绝不自动选择</div>
        <div class="edit-btns">
          <button class="btn primary" data-cand="${esc(c.canonical)}" data-orig="${esc(c.original)}">确认为别名</button>
        </div></div>`).join("");
    openModal(`样本链 · ${esc(vid)}`, `
      <p><span class="st ${cls[1]}">${cls[0]}</span> <span class="hint">${esc(cls[2])}</span></p>
      ${sec("DNA 提取", ch.extracts, (x) => `#${x.rid} · 标本「${esc(x.specimen || "外部/未填")}」 · ${esc(x.time || "时间未记")} · ${esc(x.qubit || "—")} ng/ul`)}
      ${sec("UCE 文库（同一凭证可多次建库）", ch.libraries, (x) => `#${x.rid} · ${esc(x.library_code || "—")} · ${esc(x.sample_key || "—")}
        ${x.accession ? ` · 提交号 <b>${esc(x.accession)}</b>${x.acc_kind !== "srr" ? ` <span class="hint">（格式待解析：仅 SRR/DRR/ERR 视为有效提交号）</span>` : ""}` : ` · <span class="hint">暂无测序提交号</span>`}`)}
      ${sec("标本（DNAVoucherID 指向该凭证）", ch.specimens, (x) => `${esc(x.key || "#" + x.rid)} · ${esc(x.genus || "")} ${esc(x.species || "")}`)}
      ${sec("测序 accession", ch.accessions, (x) => `${esc(x.accession)} · 样本 ${esc(x.sample_key || "")} · ${esc(x.species)} <span class="hint">匹配依据：${esc(x.basis)}</span>`)}
      ${ch.matched ? "" : `<p class="hint" style="margin:8px 0 0">该凭证当前<b>暂未匹配</b>到 DNA 提取台账（与「记录可追溯」状态区分显示）。</p>`}
      ${cands}
      <div class="form-actions"><button class="btn" id="ln-close">关闭</button></div>`);
    $("ln-close").onclick = closeModal;
    document.querySelectorAll("#modal-body [data-cand]").forEach((b) => {
      b.onclick = async () => {
        try {
          const r = await api("/api/lineage/alias", { method: "POST", body: JSON.stringify({
            original: b.dataset.orig, canonical: b.dataset.cand }) });
          toast(`已确认别名 ${r.original} → ${r.canonical}（原文/规则/确认人已留痕，可撤销批次 ${r.batch}）`, "ok");
          closeModal();
          openLineage(vid);
        } catch (e) { toast(e.message, "err"); }
      };
    });
  } catch (e) { toast(e.message, "err"); }
}

/* ---- 全局搜索：跨所有有权限的表查找（Ctrl+K / 页头按钮） ---- */
function openGlobalSearch() {
  openModal("全局搜索", `
    <input id="gs-q" type="search" autocomplete="off"
      placeholder="输入关键字，在您有权限的所有表中查找（编号 / 属名 / 地名 / 事件号 / 条码 …）"
      style="width:100%;padding:8px 12px;border:1px solid var(--border);border-radius:8px;font-size:14px;outline:none">
    <div id="gs-results" style="margin-top:10px;max-height:58vh;overflow-y:auto">
      <p class="hint" style="margin:0">键入关键字即搜：跳蛛标本、采集地、分类信息、UCE 文库、原蛛、伪蝎、UDI 条码等您有权限的表会同时查找；标本表中由采集事件带出的字段（省份、经纬度等）也在搜索范围内。</p>
    </div>
    <div class="form-actions"><button class="btn" id="gs-close">关闭</button></div>`);
  $("gs-close").onclick = closeModal;
  const input = $("gs-q");
  input.focus();
  let timer = null;
  const run = async () => {
    const q = input.value.trim();
    const box = $("gs-results");
    if (!q) return;
    box.innerHTML = `<p class="hint" style="margin:0">搜索中…</p>`;
    try {
      renderGsResults(await api(`/api/global_search?q=${encodeURIComponent(q)}`), q);
    } catch (e) {
      box.innerHTML = `<p class="hint" style="margin:0">${esc(e.message || "搜索失败")}</p>`;
    }
  };
  input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(run, 300); });
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); clearTimeout(timer); run(); } });
}

function renderGsResults(r, q) {
  const box = $("gs-results");
  if (!r.results || !r.results.length) {
    box.innerHTML = `<p class="hint" style="margin:0">没有找到包含「${esc(q)}」的记录（仅搜索您有权限的表）</p>`;
    return;
  }
  box.innerHTML = r.results.map((g) => `
    <div class="gs-group">
      <h4>${esc(g.label)} <span class="gs-count">${g.total} 条</span></h4>
      ${g.total > g.hits.length ? `<p class="hint" style="margin:2px 0 4px">显示前 ${g.hits.length} 条，请细化关键字</p>` : ""}
      <div>${g.hits.map((h) => `
        <div class="gs-row">
          <span class="gs-key" title="${esc(h.field)}">${esc(h.key)}</span>
          <span class="gs-snip"><i>${esc(h.field)}</i>：${esc(h.snippet)}</span>
          <button class="btn" data-gs-t="${esc(g.table)}" data-gs-rid="${h.rid}" data-gs-key="${esc(h.key)}">查看</button>
        </div>`).join("")}</div>
    </div>`).join("");
  box.querySelectorAll("button[data-gs-rid]").forEach((b) => {
    b.onclick = () => {
      closeModal();
      switchView(b.dataset.gsT);
      // 表格是服务端分页：用键值过滤把目标行收进第一页，再打开详情
      S.page = 1; S.q = b.dataset.gsKey; S.preset = null; S.showDeleted = false;
      const sb = $("search");
      if (sb) sb.value = S.q;
      setTimeout(async () => {
        await loadTable();
        if (!rowById(+b.dataset.gsRid)) {
          toast("未能定位到该行（该键值匹配行过多），请在该表内细化搜索", "err");
          return;
        }
        openRowDetail(+b.dataset.gsRid);
      }, 300);
    };
  });
}

/* ---- 手动补充采集信息（无编号早期标本）：
   创建采集事件（事件号留空自动生成 M-XXXX）并把编号回填到本标本 ---- */
function openSupplementGeo(rid) {
  if (!canWriteTable("events")) {
    return toast("您的账号无权编辑采集地信息表，请联系管理员补充", "err");
  }
  const evCfg = S.meta.tables["events"];
  if (!evCfg) return toast("未找到采集地信息表", "err");
  const fields = evCfg.columns
    .filter((c) => !c.linked && c.name !== evCfg.key && c.name !== "采集批次备注")
    .map((c) => `<label class="${c.wide ? "span2" : ""}">${esc(c.name)}
      <input data-col="${esc(c.name)}"></label>`).join("");
  const row = rowById(rid);
  const key = curCfg().key;
  const label = esc(key ? row?.data[key] || "#" + rid : "#" + rid);
  openModal(`补充采集信息 · ${label}`, `
    <p class="hint">为该标本手动建档采集信息（适合没有采集编号的早期标本）。
    保存后系统自动生成采集事件号（M-XXXX 格式）并<b>回填到本标本</b>，此后该标本的省份、经纬度等自动带出。</p>
    <div class="grid-form">${fields}</div>
    <div class="form-actions">
      <button class="btn" id="sup-cancel">取消</button>
      <button class="btn primary" id="sup-ok">保存并关联</button>
    </div>`);
  $("sup-cancel").onclick = closeModal;
  $("sup-ok").onclick = async () => {
    const data = {};
    $("modal-body").querySelectorAll("input[data-col]").forEach((i) => {
      if (i.value.trim() !== "") data[i.dataset.col] = i.value;
    });
    if (!Object.keys(data).length) return toast("请至少填写一项采集信息", "err");
    const linkEvent = async (evNo) => {
      await api(`/api/rows/${S.t}/${rid}`, {
        method: "PUT",
        body: JSON.stringify({ changes: { [curCfg().linked_key]: evNo } }),
      });
      closeModal();
      toast(`已关联采集事件 ${evNo} 到本标本`, "ok");
      loadTable();
      refreshMetaCounts();
    };
    $("sup-ok").disabled = true;
    try {
      const ev = await api("/api/rows/events", { method: "POST", body: JSON.stringify({ data }) });
      await linkEvent(ev.key);
    } catch (e) {
      if (e.duplicate) {
        $("sup-ok").disabled = false;
        openModal("发现相同的采集信息", `
          <p>系统中已有<b>采集要素完全相同</b>（地点、采集人、日期一致）的记录，编号
            <b class="key-cell">${esc(e.duplicate.key)}</b>。</p>
          <p class="hint">「复用该编号」= 本标本直接关联到已有记录，不再重复建档（推荐）；
            「仍新建一条」用于确实需要分开记录的场景（如同地同日不同批次）。</p>
          <div class="form-actions">
            <button class="btn primary" id="dup-reuse">复用该编号</button>
            <button class="btn" id="dup-force">仍新建一条</button>
            <button class="btn" id="dup-cancel">取消</button>
          </div>`);
        $("dup-cancel").onclick = closeModal;
        $("dup-reuse").onclick = () => linkEvent(e.duplicate.key);
        $("dup-force").onclick = async () => {
          try {
            const ev = await api("/api/rows/events", { method: "POST", body: JSON.stringify({ data, force: true }) });
            await linkEvent(ev.key);
          } catch (err) { toast(err.message, "err"); }
        };
      } else {
        $("sup-ok").disabled = false;
        toast(e.message, "err");
      }
    }
  };
}

/* ---- 详情抽屉内字段编辑 ---- */
function renderDetValue(vSpan, col, row) {
  vSpan.innerHTML = `<span class="val">${fmtV(displayVal(col.name, row.data[col.name]))}</span>`;
}

function editDetValue(vSpan, rid, col) {
  if (S.detEditing) return;  // 同一时刻只编辑一个字段
  const row = rowById(rid);
  if (!row) return;
  const useSelect = col.enum_locked && col.options;
  // 联想列强制单行 input（textarea 不支持 datalist 联想）
  const forceInput = (curCfg().genus_lookup && (col.name === "Genus" || isFinalNameCol(col.name) || isTempNameCol(col.name)))
    || (curCfg().wac_lookup && (col.name === "属" || col.name === "种"));
  const input = document.createElement(useSelect ? "select" : ((col.wide && !forceInput) ? "textarea" : "input"));
  if (input.tagName === "INPUT") input.type = "text";
  // 锁定枚举：无值自动填默认值
  if (col.default && (row.data[col.name] === null || row.data[col.name] === undefined || row.data[col.name] === "")) {
    row.data[col.name] = colDefaultVal(col);   // {{me}} 展开为当前账号（v4.03）
  }
  input.value = row.data[col.name] ?? "";
  vSpan.innerHTML = "";
  vSpan.appendChild(input);
  input.focus();
  if (input.select) input.select();
  if (S.t === "specimens" && col.name === "Box # (Start#-End#)" && S.boxes) {
    const sel = document.createElement("select");
    sel.style.cssText = "width:100%;border:2px solid var(--primary);border-radius:4px;padding:2px 6px;font-size:13px;";
    sel.innerHTML = availableBoxOptions(row.data[col.name] ?? "");
    vSpan.innerHTML = "";
    vSpan.appendChild(sel);
    sel.focus();
    const state2 = { done: false };
    const saveSel = async () => {
      if (state2.done) return;
      state2.done = true;
      const nv = sel.value;
      const ov = row.data[col.name];
      if ((ov ?? "") === nv) { renderDetValue(vSpan, col, row); S.detEditing = null; return; }
      try {
        const r = await api(`/api/rows/${S.t}/${rid}`, {
          method: "PUT",
          body: JSON.stringify({ changes: { [col.name]: nv }, base_version: row.row_version,
                                 base_updated_at: row.updated_at }),
        });
        if (r.noop) { renderDetValue(vSpan, col, row); S.detEditing = null; return; }
        Object.assign(row.data, Object.fromEntries(Object.entries(r.changes).map(([k, p]) => [k, p[1]])));
        row.updated_at = r.updated_at;
        row.row_version = r.row_version;
        if (r.merged_fields) toast(r.warn || "该行在您编辑期间有其他修改，已按字段合并保存", "warn");
        renderDetValue(vSpan, col, row);
        S.detEditing = null;
        const td = $("grid-body").querySelector(`td.cell[data-rid="${rid}"][data-col="${CSS.escape(col.name)}"] .val`);
        if (td) td.innerHTML = fmtV(row.data[col.name]);
      } catch (err) {
        renderDetValue(vSpan, col, row);
        S.detEditing = null;
        if (err.conflict) showConflict(row, col.name, nv, err.conflict);
        else toast(err.message, "err");
      }
    };
    sel.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { e.stopPropagation(); renderDetValue(vSpan, col, row); S.detEditing = null; }
      else if (e.key === "Enter") { e.preventDefault(); saveSel(); }
      e.stopPropagation();
    });
    sel.addEventListener("change", () => saveSel());
    sel.addEventListener("blur", () => setTimeout(() => { if (!state2.done) saveSel(); }, 120));
    return;
  }
  if (col.options || (col.cascade && S.cascade[S.t]) || col.name === "Genus" || isFinalNameCol(col.name)) {
    const dl = document.createElement("datalist");
    dl.id = "dl" + Math.random().toString(36).slice(2);
    let opts = [];
    if (col.options) opts = col.options.map(String);
    else if (col.cascade) opts = cascadeOptions(col.name, row.data) || [];
    dl.innerHTML = opts.map((o) => `<option value="${esc(o)}">`).join("");
    vSpan.appendChild(dl);
    input.setAttribute("list", dl.id);
    input._dl = dl;
    if (col.name === "CollectionEventNumber" || col.name === "采集编号") attachEventSuggest(input);
  }
  // 分类联想挂载：非级联列也生效（函数内部自建 datalist）
  if (curCfg().genus_lookup) {
    if (col.name === "Genus") { attachGenusSuggest(input); attachGenusCheck(input); }
    else if (isFinalNameCol(col.name)) attachSpeciesSuggest(input);
  }
  if (S.cascade && S.cascade[S.t] && TAX_SUGGEST_COLS.includes(col.name)) attachTaxValueSuggest(input, col.name);
  if (curCfg().wac_lookup) {
    if (col.name === "属") attachWacGenus(input);
    else if (col.name === "种") attachWacEpithet(input, () => rowById(rid)?.data["属"] || "");
  }
  if (useSelect) {
    const cur = row.data[col.name];
    input.innerHTML = `<option value="">（空）</option>` +
      col.options.map((o) => {
        const sel = String(o) === String(cur) || (Number(o) === Number(cur) && cur !== null && cur !== "" && !isNaN(Number(cur)));
        return `<option value="${esc(String(o))}" ${sel ? "selected" : ""}>${esc(o)}</option>`;
      }).join("");
  }
  input.style.cssText = "width:100%;border:2px solid var(--primary);border-radius:4px;padding:2px 6px;font-size:13px;";
  if (input.tagName === "TEXTAREA") { input.rows = 3; input.style.resize = "vertical"; }
  // 临时鉴定名：cf. 切换按钮（悬浮右内侧；需在 cssText 之后挂以免样式被覆盖）
  if (isTempNameCol(col.name) && input.tagName === "INPUT") {
    wireTempNameField(input);
  }

  const state = { done: false };
  const restore = () => { renderDetValue(vSpan, col, row); S.detEditing = null; };
  const cancel = () => { if (!state.done) { state.done = true; restore(); } };

  S.detEditing = { cancel };

  const save = async () => {
    if (state.done) return;
    let nv = input.value;
    const ov = row.data[col.name];
    if ((ov ?? "") === nv) { state.done = true; restore(); return; }
    if (isTempNameCol(col.name) && nv.trim()) {
      const chk = await confirmTempName(nv, input);
      if (chk === false) { state.done = true; restore(); return; }
      nv = input.value;
      if ((ov ?? "") === nv) { state.done = true; restore(); return; }
    }
    state.done = true;
    try {
      const r = await api(`/api/rows/${S.t}/${rid}`, {
        method: "PUT",
        body: JSON.stringify({ changes: { [col.name]: nv }, base_version: row.row_version,
                               base_updated_at: row.updated_at, force_name: !!S._fsForceOk }),
      });
      S._fsForceOk = false;
      if (r.noop) { restore(); return; }
      Object.assign(row.data, Object.fromEntries(Object.entries(r.changes).map(([k, p]) => [k, p[1]])));
      row.updated_at = r.updated_at;      // 服务器权威时间
      row.row_version = r.row_version;    // 服务器权威修订号
      if (r.merged_fields) toast(r.warn || "该行在您编辑期间有其他修改，已按字段合并保存", "warn");
      renderDetValue(vSpan, col, row);
      // 编号变更自动带出标本盒 → 同步刷新网格里的 Box 格与详情抽屉的盒示意图
      if (S.t === "specimens" && r.changes["MHBU #"]) renderBoxMap(rid);
      if (r.changes["Box # (Start#-End#)"]) {
        row.data["Box # (Start#-End#)"] = r.changes["Box # (Start#-End#)"][1];
        const bTd = document.querySelector(`#grid-body td.cell[data-rid="${rid}"][data-col="Box # (Start#-End#)"] .val`);
        if (bTd) bTd.innerHTML = fmtV(row.data["Box # (Start#-End#)"]);
      }
      S.detEditing = null;
      if (r.warn) toast(r.warn, "err");
      toastIdentSync(r);
      if (col.name === "Genus" && curCfg().genus_lookup) {
        toast("已按分类信息带出上级分类", "ok");
        await loadTable();
        openRowDetail(rid);  // 刷新整条链的显示
      } else {
        // 同字段在网格里也刷新
        const td = $("grid-body").querySelector(`td.cell[data-rid="${rid}"][data-col="${CSS.escape(col.name)}"] .val`);
        if (td) td.innerHTML = fmtV(row.data[col.name]);
      }
    } catch (err) {
      restore();
      if (err.conflict) showConflict(row, col.name, nv, err.conflict);
      else toast(err.message, "err");
    }
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.stopPropagation(); cancel(); }
    else if (e.key === "Enter" && (input.tagName === "INPUT" || e.ctrlKey)) { e.preventDefault(); save(); }
    e.stopPropagation();
  });
  input.addEventListener("blur", () => setTimeout(() => { if (!state.done) save(); }, 120));
}

/* ---- 采集关联字段写穿透（v3.91）：标本详情里点关联采集字段 = 直接改采集地信息行。
   审计与行版本落在事件行；标本行缓存同步刷新（事件未提供该字段时不动本行存量回退值）。
   事件号不存在（失配）→ 提供「补建采集事件」，用引用号建事件修好引用，绝不猜填地点。 ---- */
function _applyEventDataToRow(row, evData) {
  for (const c of S.columns) {
    if (c.linked && evData[c.linked] !== undefined && evData[c.linked] !== null) row.data[c.name] = evData[c.linked];
  }
  row.miss = false;
}
function _refreshRowCells(row) {
  for (const c of S.columns) {
    const td = $("grid-body").querySelector(`td.cell[data-rid="${row.id}"][data-col="${CSS.escape(c.name)}"] .val`);
    if (td) td.innerHTML = cellHtml(c.name, displayVal(c.name, row.data[c.name]), row);
  }
}
async function editLinkedEventValue(vSpan, rid, col, ceNum) {
  if (S.detEditing) return;
  const row = rowById(rid);
  if (!row) return;
  let ev;
  try {
    ev = await api(`/api/events/by-number/${encodeURIComponent(ceNum)}`);
  } catch (e) {
    healEventModal(ceNum, rid);   // 事件不存在（失配）→ 补建
    return;
  }
  const input = document.createElement(col.wide ? "textarea" : "input");
  if (input.tagName === "INPUT") input.type = "text";
  const cur = row.data[col.name] ?? "";
  input.value = cur;
  input.style.cssText = "width:100%;border:2px solid var(--primary);border-radius:4px;padding:2px 6px;font-size:13px;";
  if (input.tagName === "TEXTAREA") { input.rows = 3; input.style.resize = "vertical"; }
  vSpan.innerHTML = "";
  vSpan.appendChild(input);
  input.focus();
  if (input.select) input.select();
  const state = { done: false };
  const restore = () => { renderDetValue(vSpan, col, row); S.detEditing = null; };
  const cancel = () => { if (!state.done) { state.done = true; restore(); } };
  S.detEditing = { cancel };
  const save = async () => {
    if (state.done) return;
    const nv = input.value;
    if ((cur ?? "") === nv) { state.done = true; restore(); return; }
    state.done = true;
    try {
      const r = await api(`/api/rows/events/${ev.rid}`, {
        method: "PUT",
        body: JSON.stringify({ changes: { [col.linked]: nv }, base_version: ev.row_version }),
      });
      if (r.noop) { restore(); return; }
      const evData = { [col.linked]: r.changes[col.linked][1] };
      _applyEventDataToRow(row, evData);
      S.detEditing = null;
      renderDetValue(vSpan, col, row);
      _refreshRowCells(row);
      toast(`已更新采集地信息（事件 ${ceNum}，改动与留痕记在采集地表）`, "ok");
    } catch (err) {
      restore();
      if (err.conflict) showConflict(row, col.name, nv, err.conflict);
      else toast(err.message, "err");
    }
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.stopPropagation(); cancel(); }
    else if (e.key === "Enter" && (input.tagName === "INPUT" || e.ctrlKey)) { e.preventDefault(); save(); }
    e.stopPropagation();
  });
  input.addEventListener("blur", () => setTimeout(() => { if (!state.done) save(); }, 120));
}

function healEventModal(ceNum, specRid) {
  const evCfg = (S.meta.tables || {})["events"];
  if (!evCfg) return toast("您没有采集地信息的查看权限", "err");
  const fields = evCfg.columns.filter((c) => !c.linked && c.name !== evCfg.key && c.name !== "采集批次备注");
  const hasRef = !!ceNum;
  const numField = hasRef
    ? `<p class="hint">事件编号：<b>${esc(ceNum)}</b>（沿用此引用号）</p>`
    : `<label>事件编号<input id="heal-num" class="input" placeholder="留空自动生成 M-XXXX 编号">
       <span class="hint" style="font-weight:normal">只填编号（如 HB190812）；地点/省份等请填下方字段</span></label>`;
  openModal(hasRef ? `补建采集事件 · ${ceNum}` : `新建采集事件并关联标本`, `
    ${hasRef
      ? `<p class="hint">该标本引用的事件号 <b>${esc(ceNum)}</b> 在采集地信息中不存在（失配）。
         下方填写已知信息后建立该事件（<b>沿用此编号</b>），标本引用即恢复正常；</p>`
      : `<p class="hint">该标本尚未关联采集事件。下方填写采集信息建立事件后，自动把本标本的事件号指向它；
         编号可留空由系统生成 M-XXXX。若想挂到已有事件，直接点上方「CollectionEventNumber」字段输入即可。</p>`}
    <p class="hint">未填字段留空，系统<b>绝不猜测补充</b>地点/日期；建好后可随时在采集地信息页继续完善。</p>
    ${numField}
    <div class="grid-form" style="grid-template-columns:1fr 1fr">${fields.map((c) =>
      `<label>${esc(c.label || c.name)}<input data-evf="${esc(c.name)}" class="input"></label>`).join("")}
    </div>
    <div class="form-actions">
      <button class="btn" id="heal-cancel">取消</button>
      <button class="btn primary" id="heal-ok">${hasRef ? `建立事件「${esc(ceNum)}」` : "建立事件并关联"}</button>
    </div>`);
  $("heal-cancel").onclick = closeModal;
  $("heal-ok").onclick = async () => {
    const data = {};
    document.querySelectorAll("input[data-evf]").forEach((i) => {
      const v = i.value.trim();
      if (v) data[i.dataset.evf] = v;
    });
    try {
      let evNum;
      if (hasRef) {
        evNum = ceNum;
        await api("/api/rows/events", { method: "POST", body: JSON.stringify({ data: { CollectionEventNumber: ceNum, ...data } }) });
      } else {
        let typed = ($("heal-num").value || "").trim();
        // 防呆（v4.02）：编号框键入长地点文本（含中文且无编号形态）时拦下——这类值几乎都是误填，
        // 建出"以地点为编号"的事件后还要手动改名。地点请填到下方字段，编号留空自动生成即可。
        if (typed && /[\u4e00-\u9fff]{4,}/.test(typed) && !/\d/.test(typed)) {
          toast(`「${typed}」看起来是地点而不是事件编号——请把它填到下方 Locality 等字段，编号留空即可自动生成`, "err");
          return;
        }
        const row = rowById(specRid);
        if (typed) {
          // 用户指定编号：建事件 + 把标本事件号指向它（第二步失败时明确提示，不静默丢失关联）
          await api("/api/rows/events", { method: "POST", body: JSON.stringify({ data: { CollectionEventNumber: typed, ...data } }) });
          try {
            await api(`/api/rows/specimens/${specRid}`, { method: "PUT",
              body: JSON.stringify({ changes: { CollectionEventNumber: typed }, base_version: row.row_version }) });
          } catch (e2) {
            toast(`事件「${typed}」已建立，但关联标本失败（${e2.message}）。请在标本详情点 CollectionEventNumber 手动填入该编号`, "warn");
            closeModal();
            return;
          }
          evNum = typed;
        } else {
          // 留空：走失配处置「建立待完善事件」（服务端生成 M-XXXX 并关联，双方留痕可撤销）
          const r = await api("/api/refs/resolve-mismatch", { method: "POST",
            body: JSON.stringify({ rid: specRid, action: "pending", event_data: data }) });
          evNum = r.event_number;
        }
      }
      closeModal();
      const row = rowById(specRid);
      if (row) {
        try {
          const ev = await api(`/api/events/by-number/${encodeURIComponent(evNum)}`);
          _applyEventDataToRow(row, ev.data);
        } catch { }
        row.data[evCfg.linked_key || curCfg().linked_key] = evNum;
        _refreshRowCells(row);
        openRowDetail(specRid);
      }
      toast(`采集事件「${evNum}」已建立，标本引用已恢复`, "ok");
    } catch (e) {
      if ((e.message || "").includes("已存在")) toast("该编号的事件刚被建立，刷新后即可编辑", "warn");
      else toast(e.message, "err");
    }
  };
}

/* ---- 行历史抽屉 ---- */
function openDrawer(title) {
  const t = $("drawer-title");
  t.textContent = title;
  t.title = title;  // 标题过长省略显示时，悬停可看全称
  $("drawer").hidden = false;
  $("drawer-overlay").hidden = false;
}
function closeDrawer() {
  $("drawer").hidden = true;
  $("drawer-overlay").hidden = true;
  $("drawer-top2").hidden = true;
  const _t = drawerNav ? drawerNav.tname : null;
  const _rid = S.drawerRid;
  drawerStack = [];
  drawerNav = null;
  clearRecordHash();
  if (_t && _rid) returnToGridRow(_t, _rid);
}
$("drawer-close").onclick = async () => {
  if (!(await drawerNavGuard("关闭详情"))) return;
  closeDrawer();
};
$("drawer-back").onclick = () => {
  const prev = drawerStack.pop();
  $("drawer-back").hidden = drawerStack.length === 0;
  if (!prev) return;
  if (prev.type === "detail") {
    $("drawer-top2").hidden = false;
    openDrawer("详情");
    openRowDetail(prev.rid);
    $("drawer-body").scrollTop = prev.scroll || 0;
    $("drawer-body").focus && $("drawer-body").focus();
  } else if (prev.type === "history") {
    openRowHistory(prev.rid, { noPush: true });
    $("drawer-body").scrollTop = prev.scroll || 0;
  }
};
$("drawer-overlay").onclick = async () => { if (await closeGate("关闭详情")) closeDrawer(); };

async function openRowHistory(rid, opts = {}) {
  if (!opts.noPush) drawerStack.push({ type: "detail", tname: S.t, rid, scroll: $("drawer-body").scrollTop });
  $("drawer-top2").hidden = true;
  openDrawer("行修改历史");
  $("drawer-back").hidden = drawerStack.length === 0;
  $("drawer-body").innerHTML = `<p class="hint">加载中…</p>`;
  try {
    const r = await api(`/api/edits/row/${S.t}/${rid}`);
    const row = rowById(rid);
    const key = curCfg().key;
    $("drawer-body").innerHTML = `
      <p class="hint">记录：${esc(key ? row?.data[key] || `#${rid}` : `#${rid}`)}（仅显示直接针对此行的操作）</p>
      ${r.edits.length ? r.edits.map((e) => editItemHTML(e)).join("") : `<p class="hint">此行还没有修改记录。</p>`}`;
  } catch (e) {
    $("drawer-body").innerHTML = `<p class="hint">${esc(e.message)}</p>`;
  }
}

/* ---- 变更历史视图 ---- */
function buildHistoryFilters() {
  $("h-table").innerHTML = `<option value="">全部表</option>` +
    Object.entries(S.meta.tables).map(([t, c]) => `<option value="${t}">${esc(tblName(c))}</option>`).join("");
  api("/api/edit-users").then((r) => {
    $("h-user").innerHTML = `<option value="">全部操作人</option>` +
      r.users.map((u) => `<option>${esc(u)}</option>`).join("");
  }).catch(() => { });
}

function histParams(offset) {
  const p = new URLSearchParams({
    t: $("h-table").value, user: $("h-user").value, action: $("h-action").value,
    q: $("h-search").value.trim(),
  });
  if ($("h-from").value) p.set("from", $("h-from").value);
  if ($("h-to").value) p.set("to", $("h-to").value);
  if (offset !== undefined) { p.set("limit", 50); p.set("offset", offset); }
  return p;
}

let _histSeq = 0;
async function loadActivity() {
  try {
    const a = await api("/api/activity?days=30");
    const maxU = Math.max(1, ...(a.by_user || []).map((u) => u.n));
    $("activity-panel").innerHTML = `
      <div class="dash-card" style="margin-bottom:12px"><h3>近 30 天协作活跃度
        <span class="hint">共 ${a.total.toLocaleString()} 次行级修改 · 谁在改什么</span></h3>
        ${a.by_user.length ? `<div>${a.by_user.map((u) => `
          <div class="bar-row"><span class="bar-label" title="${esc(u.user)}">${esc(u.user)}</span>
          <span class="bar-track"><span class="bar-fill" style="width:${(u.n / maxU * 100).toFixed(1)}%"></span></span>
          <span class="bar-val">${u.n}</span></div>`).join("")}
        <p class="hint" style="margin:8px 0 0">按表分布：${a.by_table.map((t) => `${esc(t.label)} ${t.n}`).join(" · ")}</p>`
        : `<p class="hint" style="margin:0">近 30 天暂无行级修改。</p>`}
      </div>`;
  } catch { $("activity-panel").innerHTML = ""; }
}

async function loadHistory() {
  const seq = ++_histSeq;
  const p = histParams((S.histPage - 1) * 50);
  const r = await api("/api/edits?" + p);
  if (seq !== _histSeq) return;
  const pages = Math.max(1, Math.ceil(r.total / 50));
  $("h-stat").textContent = `共 ${r.total} 条记录 · 第 ${S.histPage}/${pages} 页`;
  $("history-list").innerHTML = r.edits.length
    ? r.edits.map((e) => editItemHTML(e, true)).join("")
    : `<p class="hint">没有符合条件的记录。</p>`;
  $("h-pager").innerHTML = `
    <button class="btn" id="hp-prev" ${S.histPage <= 1 ? "disabled" : ""}>‹ 上一页</button>
    <button class="btn" id="hp-next" ${S.histPage >= pages ? "disabled" : ""}>下一页 ›</button>`;
  $("hp-prev").onclick = () => { S.histPage--; loadHistory(); };
  $("hp-next").onclick = () => { S.histPage++; loadHistory(); };
}

["h-table", "h-user", "h-action", "h-from", "h-to"].forEach((id) => $(id).onchange = () => { S.histPage = 1; loadHistory(); });
let hTimer = null;
$("h-search").addEventListener("input", (e) => {
  clearTimeout(hTimer);
  hTimer = setTimeout(() => { S.histPage = 1; loadHistory(); }, 300);
});
$("h-refresh").onclick = () => loadHistory();
$("h-export").onclick = () => downloadFile("/api/edits/export?" + histParams())
  .then(() => toast("导出成功", "ok")).catch((e) => toast(e.message, "err"));
$("h-audit").onclick = async () => {
  $("h-audit").disabled = true;
  try {
    const r = await api("/api/audit/verify");
    const st = r.ok ? "✔" : "✕";
    const b = r.broken_at;
    openModal("审计链校验", `
      <p class="hint" style="${r.ok ? "color:var(--c-saved)" : "color:var(--c-risk)"}"><b>${st} ${r.ok ? "审计链完整" : `发现断点：edit #${b ? b.id : "?"}（${b ? b.ts : ""}）`}</b></p>
      <div class="hint" style="line-height:1.8">
        总记录 ${r.total.toLocaleString()} 条${r.legacy ? `；legacy 无哈希 ${r.legacy} 条` : ""}；链尾 hash：<code style="word-break:break-all">${esc(r.last_hash || "")}</code>
      </div>
      ${b ? `<p class="hint warn">断点详情：期望 prev_hash ${esc(b.expect_prev || "")}，实际存储 ${esc(b.stored_prev || "")}；hash 本身${b.hash_match ? "一致" : "不一致"}。请核对备份与访问日志。</p>` : `<p class="hint">每条审计记录的 prev_hash 指向上一条的 hash；链连续即未发生绕过应用的改动。</p>`}
      <p class="hint">${esc(r.limitation || "")}</p>
      <div class="form-actions">
        <button class="btn" onclick="closeModal()">关闭</button>
        ${S.me.admin ? `<button class="btn" id="au-ckpt">导出校验清单（独立介质留存）</button>` : ""}
      </div>`);
    const ck = $("au-ckpt");
    if (ck) ck.onclick = () => postDownload("/api/audit/checkpoint", {}, "审计校验清单.txt")
      .then(() => toast("校验清单已下载，请保存到独立介质", "ok"))
      .catch((e) => toast(e.message, "err"));
  } catch (e) { toast(e.message, "err"); }
  $("h-audit").disabled = false;
};

const ACTION_TXT = { create: "新增", update: "修改", delete: "删除", import: "导入", restore: "恢复", version: "版本", undelete: "还原", purge: "彻底删除", file: "附件", catalog_update: "名录更新", print: "打印" };

/* ---- 数据体检工具菜单：目录 / 引用治理 / 事件合并 / 字段扫描 / 名录更新 / 附件诊断 / 压缩（v3.70 接线） ---- */
$("ck-fieldcat").onclick = async () => {
  try {
    const d = await api("/api/field-catalog");
    const rows = d.fields.map((f) => `
      <tr><td><code>${esc(f.field_id)}</code></td><td><b>${esc(f.display_name || f.name || "")}</b>
        <div class="hint">${esc(f.display_name_en || f.name_en || "")}</div></td>
        <td class="hint">${esc(f.definition || "")}</td>
        <td>${Object.entries(f.tables || {}).map(([t, k]) => `${esc(t)}:${esc(k)}`).join("；")}</td>
        <td class="hint">${esc(JSON.stringify(f.compatible_headers || f.aliases || []))}</td></tr>`).join("");
    openModal("字段语义目录（稳定 field_id）", `
      <p class="hint">${esc(d.note || "")}</p>
      <div style="overflow:auto;max-height:60vh"><table class="about-table" style="width:100%">
      <thead><tr><th>field_id</th><th>显示名</th><th>定义</th><th>各表内部键</th><th>兼容表头</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`);
    $("modal").classList.add("modal-wide");
  } catch (e) { toast(e.message, "err"); }
};

$("ck-evmerge").onclick = () => {
  openModal("合并重复采集事件", `
    <label>保留事件的编号<input id="em-keep" class="input" placeholder="如 YNDL2020-018"></label>
    <label>被合并事件的编号<input id="em-dup" class="input"></label>
    <label>原因<input id="em-reason" class="input" value="重复合并"></label>
    <div class="form-actions"><button class="btn" id="em-cancel">取消</button>
      <button class="btn primary" id="em-preview">预览差异与影响</button></div>
    <div id="em-out"></div>
    <p class="hint">合并后：受影响标本逐条改指保留事件（逐条留痕、可随批次撤销）→ 被合并事件软删 → 被合并编号重定向为别名继续可解析。</p>`);
  $("em-cancel").onclick = closeModal;
  $("em-preview").onclick = async () => {
    const resolve = async (num) => {
      const r = await api(`/api/rows/events?q=${encodeURIComponent(num)}&per=5`);
      const hit = (r.rows || []).find((x) => String(x.data.CollectionEventNumber || "").trim() === num);
      if (!hit) throw new Error(`事件「${num}」未找到`);
      return hit.id;
    };
    try {
      const keep = await resolve($("em-keep").value.trim());
      const dup = await resolve($("em-dup").value.trim());
      if (keep === dup) { toast("两个编号解析为同一事件", "err"); return; }
      const pv = await api(`/api/events/merge-preview?keep=${keep}&dup=${dup}`);
      $("em-out").innerHTML = `
        <div class="dash-card"><b>字段差异 ${pv.diff.length} 处</b>
          <table class="about-table" style="margin-top:6px"><thead><tr><th>字段</th><th>保留事件</th><th>被合并事件</th></tr></thead>
          <tbody>${pv.diff.map((x) => `<tr><td>${esc(x.field)}</td><td>${esc(x.keep ?? "")}</td><td>${esc(x.dup ?? "")}</td></tr>`).join("") || `<tr><td colspan="3" class="hint">无差异</td></tr>`}</tbody></table></div>
        <p class="hint warn">受影响标本 <b>${pv.affected_specimens}</b> 条${pv.affected_specimens ? `（样例：${pv.affected_sample.slice(0, 6).join("、")}${pv.affected_specimens > 6 ? "…" : ""}）` : ""}——合并前请确认被合并编号确为重复。</p>
        <div class="form-actions"><button class="btn danger" id="em-go">确认合并（原子执行，可撤销）</button></div>`;
      $("em-go").onclick = async () => {
        try {
          const r = await api("/api/events/merge", { method: "POST", body: JSON.stringify({
            keep, dup, reason: $("em-reason").value.trim() || "重复合并", base_versions: pv.base_versions }) });
          closeModal(); toast(`已合并：改指 ${r.repointed ?? "?"} 条${r.batch ? `（批次 ${r.batch}）` : ""}`, "ok");
        } catch (e) { toast(e.message, "err"); }
      };
    } catch (e) { toast(e.message, "err"); }
  };
};

$("ck-refs").onclick = async () => {
  try {
    const d = await api("/api/refs/audit/specimens");
    const sec = (title, items, fmt) => `<div class="dash-card" style="margin-bottom:8px"><b>${title}：${items.length}</b>
      ${items.length ? `<div class="hint">${items.slice(0, 8).map(fmt).join("；")}${items.length > 8 ? "…" : ""}</div>` : ""}</div>`;
    openModal("稳定引用体检（specimens）", `
      ${sec("重复编号", d.duplicates || [], (x) => esc(x.key ?? x.number ?? JSON.stringify(x).slice(0, 40)))}
      ${sec("空业务键", d.empty_keys || [], (x) => "#" + (x.rid ?? x.id ?? ""))}
      ${sec("引用失配", d.mismatches || d.mismatch || [], (x) => `${esc(x.key || x.rid || "")}→${esc(x.event || x.ref || "")}`)}
      <p class="hint">体检只读不修复。「审阅式迁移」为唯一匹配的编号建立稳定别名绑定；重复/空号/软删行进入待确认清单，绝不自动处理。</p>
      <div class="form-actions"><button class="btn" id="rf-close">关闭</button>
        <button class="btn primary" id="rf-mig" ${canWriteTable("specimens") ? "" : "disabled"}>执行审阅式迁移（幂等留痕）</button></div>`);
    $("rf-close").onclick = closeModal;
    $("rf-mig").onclick = async () => {
      try {
        const r = await api("/api/refs/migrate/specimens", { method: "POST", body: "{}" });
        toast(`迁移完成：绑定 ${r.bound ?? 0} 个别名，${r.already ?? 0} 个已存在；待确认 ${((r.pending || []).length)} 项（详见返回）`, "ok");
        if ((r.pending || []).length) console.log("refs migrate pending:", r.pending);
        closeModal();
      } catch (e) { toast(e.message, "err"); }
    };
  } catch (e) { toast(e.message, "err"); }
};

$("ck-fields").onclick = () => {
  openModal("字段类型扫描与应用", `
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px 14px">
      <label>目标表<select id="fs-t" class="input">${["specimens", "events", "uce", "dna_extract"].filter((t) => S.meta.tables[t]).map((t) => `<option>${t}</option>`).join("")}</select></label>
      <label>字段组<select id="fs-g" class="input"><option value="date">日期（精度/越界）</option><option value="coord">坐标（歧义/半球/范围）</option><option value="alcohol">保存浓度</option><option value="counts">个体数量</option></select></label>
    </div>
    <div class="form-actions"><button class="btn" id="fs-cancel">取消</button>
      <button class="btn primary" id="fs-go">只读扫描</button></div>
    <div id="fs-out"></div>
    <p class="hint">扫描不写入；应用前逐条人工确认（歧义坐标默认绝不自动转换）。</p>`);
  $("fs-cancel").onclick = closeModal;
  $("fs-go").onclick = async () => {
    try {
      const d = await api(`/api/fields/scan/${$("fs-t").value}?group=${$("fs-g").value}`);
      const groups = d.groups || d.items || {};
      const lines = Object.entries(groups).map(([k, v]) => `<div class="hint"><b>${esc(k)}</b>：${Array.isArray(v) ? v.length + " 项" : JSON.stringify(v).slice(0, 120)}</div>`).join("");
      $("fs-out").innerHTML = `<div class="dash-card" style="margin-top:8px">${lines || `<span class="hint">扫描完成：没有需要关注的值。</span>`}</div>`;
    } catch (e) { toast(e.message, "err"); }
  };
};

$("ck-wscrefresh").onclick = async () => {
  try {
    const r = await api("/api/catalogs/refresh", { method: "POST", body: "{}" });
    if (r.task && r.task.id) {
      const t = r.task;
      openModal("WSC 名录更新（后台任务）", `
        <p class="hint">阶段：${esc((t.stages || ["拉取 WSC 名录", "拉取 WAC 名录", "热重载缓存"]).join(" → "))}</p>
        <div id="cu-out" class="hint">启动中…</div>
        <div class="form-actions"><button class="btn" id="cu-close">关闭</button>
          <button class="btn danger" id="cu-cancel">请求取消</button></div>`);
      $("cu-close").onclick = closeModal;
      $("cu-cancel").onclick = async () => {
        try { await api(`/api/tasks/${t.id}/cancel`, { method: "POST", body: "{}" }); toast("已请求取消（在阶段边界生效）", "ok"); } catch (e) { toast(e.message, "err"); }
      };
      const poll = setInterval(async () => {
        let cur;
        try { cur = await api(`/api/tasks/${t.id}`); } catch { clearInterval(poll); return; }
        $("cu-out").textContent = `状态：${cur.status} · 阶段 ${cur.stage_index ?? 0}/${(cur.stages || []).length} ${cur.detail ? "· " + cur.detail : ""}`;
        if (["done", "failed", "cancelled"].includes(cur.status)) {
          clearInterval(poll);
          $("cu-out").innerHTML = cur.status === "done"
            ? `✔ 更新完成${cur.result ? "：" + esc(JSON.stringify(cur.result).slice(0, 160)) : ""}`
            : `✕ ${cur.status}：${esc(cur.error || "")}`;
          $("cu-cancel").hidden = true;
        }
      }, 3000);
    } else {
      toast(r.msg || "名录更新已开始", "ok");
    }
  } catch (e) { toast(e.message, "err"); }
};

$("ck-att").onclick = async () => {
  try {
    const d = await api("/api/attachments/diagnostics");
    openModal("附件一致性诊断", `
      <p class="hint">完好 ${d.ok ?? "—"} 个${d.missing ? `；<b style="color:var(--c-risk)">缺失 ${d.missing.length}</b>` : ""}${d.orphans ? `；孤儿文件 ${d.orphans.length}` : ""}</p>
      ${d.missing && d.missing.length ? `<div class="hint warn">缺失（DB 有行、文件不在）：${d.missing.slice(0, 10).map((x) => `${x.tname}#${x.rid} ${esc(x.fname)}`).join("；")}</div>` : ""}
      ${d.orphans && d.orphans.length ? `<div class="hint warn">孤儿（磁盘有、无 DB 行）：${d.orphans.slice(0, 10).join("；")}</div>` : ""}
      <div class="form-actions"><button class="btn" onclick="closeModal()">关闭</button></div>`);
  } catch (e) { toast(e.message, "err"); }
};

$("ck-vacuum").onclick = async () => {
  if (!(await confirmBox("压缩数据库（VACUUM）：重建数据库文件以回收空间。\n\n仅限计划维护窗口执行；执行前建议先「立即备份」。继续？"))) return;
  try {
    const r = await api("/api/admin/vacuum", { method: "POST", body: JSON.stringify({ confirm: "VACUUM" }) });
    toast(`压缩完成：${((r.before_bytes || 0) / 1048576).toFixed(1)}MB → ${((r.after_bytes || 0) / 1048576).toFixed(1)}MB`, "ok");
  } catch (e) { toast(e.message, "err"); }
};

document.addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-batch]");
  const rb = e.target.closest("button[data-batch-rb]");
  if (rb) {
    const bid = rb.dataset.batchRb;
    if (!(await confirmBox(`整批回滚 ${esc(bid)}？
将按批次日志逐行逆操作（增量）：新增的行移入回收站、被还原的字段恢复原值；
若某行在批次之后又被修改过，该行将标记为冲突并跳过（不会被覆盖）。`, { okText: "回滚" }))) return;
    try {
      const r = await api(`/api/edits/batch/${bid}/rollback`, { method: "POST" });
      const sk = (r.skipped_conflicts?.length || 0) + (r.skipped?.length || 0);
      toast(`已增量回滚：回退 ${r.applied} 行` + (sk ? `、跳过冲突/缺失 ${sk} 行` : ""), "ok");
      if (S.t !== "home" && S.meta.tables[S.t]) loadTable();
      if (S.t === "history") loadHistory();
    } catch (err) { toast(err.message, "err"); }
    return;
  }
  if (b) openBatchDetail(b.dataset.batch);
});

async function openBatchDetail(bid) {
  openDrawer("批次变动明细");
  $("drawer-body").innerHTML = `<p class="hint">加载中…</p>`;
  try {
    const d = await api(`/api/edits/batch/${bid}`);
    const head = d.head;
    const rb = d.rollback_version;
    $("drawer-body").innerHTML = `
      <div class="ck-card" style="padding:12px 14px">
        <h3 style="font-size:13px">${ACTION_TXT[head.action] || head.action}批次 · ${esc(head.tname ? (S.meta.tables[head.tname]?.label || head.tname) : "")}</h3>
        <div class="h-meta" style="margin:6px 0">
          <span class="who">${esc(head.user)}</span><span>🕒 ${esc(head.ts)}</span>
          <span class="ids">批次 ${esc(bid)}</span>
        </div>
        <div style="font-size:13px">${esc(head.changes?._summary || "")}
          ${head.changes?._file ? `（${esc(head.changes._file)}）` : ""}</div>
        <div class="h-meta" style="margin-top:6px">
          <span class="badge import">新增 ${head.changes?._added ?? "?"}</span>
          <span class="badge update">更新 ${head.changes?._updated ?? "?"}</span>
          ${head.changes?._skipped ? `<span class="badge version">跳过 ${head.changes._skipped}</span>` : ""}
        </div>
        ${rb && canWriteTable(head.tname) ? `<div style="margin-top:8px"><button class="btn danger" data-batch-rb="${esc(bid)}">整批回滚（恢复到 ${esc(rb.name)}）</button></div>` : ""}
      </div>
      <p class="hint" style="margin:0">以下为该批次产生的逐行变动（${d.count} 条${d.truncated ? "，仅显示前 1000 条" : ""}）：</p>
      ${d.items.map((it) => editItemHTML({ ...it, ts: head.ts, user: head.user, tname: head.tname, batch_id: bid })).join("")}
      ${d.items.length ? "" : `<p class="hint">没有逐行明细。</p>`}`;
    $("drawer-body").querySelectorAll("button[data-act]").forEach(() => { });
  } catch (err) {
    $("drawer-body").innerHTML = `<p class="hint">${esc(err.message)}</p>`;
  }
}

function editItemHTML(e, withTable = false) {
  const cfg = S.meta?.tables[e.tname];
  let changesHTML = "";
  try {
    const c = typeof e.changes === "string" ? JSON.parse(e.changes || "{}") : (e.changes || {});
    if (e.action === "update" && !c._summary) {
      changesHTML = Object.entries(c).map(([f, [o, n]]) =>
        `<div><span class="fld">${esc(f)}：</span><span class="old">${fmtV(o)}</span><span class="new">${fmtV(n)}</span></div>`).join("");
    } else if (c._summary) {
      changesHTML = `<div>${esc(c._summary)}${c._file ? `（文件：${esc(c._file)}${c._mode ? " · " + esc(c._mode) : ""}）` : ""}</div>`;
    } else if (c._data) {
      const d = c._data;
      const preview = Object.entries(d).filter(([, v]) => v !== null && v !== "").slice(0, 4)
        .map(([k, v]) => `${esc(k)}=${esc(String(v).slice(0, 40))}`).join("；");
      changesHTML = `<div>${preview || "（空行）"}</div><details><summary>完整数据</summary><pre>${esc(JSON.stringify(d, null, 2))}</pre></details>`;
    }
  } catch { changesHTML = `<pre>${esc(e.changes || "")}</pre>`; }
  const batchBtn = e.batch_id && ["import", "restore"].includes(e.action) ? `
    <div style="margin-top:6px;display:flex;gap:8px">
      <button class="btn" data-batch="${esc(e.batch_id)}">${icon("view")} 查看全部变动</button>
      ${e.action === "import" ? `<button class="btn danger" data-batch-rb="${esc(e.batch_id)}">整批回滚</button>` : ""}
    </div>` : "";
  return `<div class="h-item">
    <span class="badge ${esc(e.action)}">${ACTION_TXT[e.action] || esc(e.action)}</span>
    <div class="h-main">
      <div class="h-meta">
        <span class="who">${esc(e.user)}</span>
        <span>🕒 ${esc(e.ts)}</span>
        ${withTable && cfg && e.action !== "catalog_update" ? `<span>${esc(tblName(cfg))}</span>` : ""}
        ${e.rkey ? `<span class="h-key">#${esc(e.rkey)}</span>` : ""}
        ${e.rid ? `<span>ID ${e.rid}</span>` : ""}
        ${e.batch_id ? `<span class="ids">批次 ${esc(e.batch_id)}</span>` : ""}
      </div>
      <div class="h-changes">${changesHTML}</div>
      ${batchBtn}
    </div>
  </div>`;
}

/* ---- 数据体检 ---- */
function userPrimaryT() {
  for (const perm of ["rw", "r"]) {
    for (const [t, c] of Object.entries(S.meta.tables)) {
      if (c.primary && (S.me.admin || c.perm === perm)) return t;
    }
  }
  return "specimens";
}

async function loadCheckup() {
  $("ck-stat").textContent = "体检中…";
  $("checkup-body").innerHTML = `<p class="hint">正在按统一规则检查…</p>`;
  const d = await api("/api/checkup/summary");
  const jump = `data-jump`;
  const fixable = canWriteTable("specimens");
  const secNote = {
    blocking: "影响引用/唯一性/数据可信，优先处理",
    fact: "数据可能有误，需人工核对（不自动改）",
    missing: "资料未录全 —— 进入「资料完善队列」，可声明例外不反复提醒",
    format: "值可机械解析/转换；歧义数据默认不自动修复",
    history: "历史数据按政策允许保留，只计数不计错",
  };
  const sections = d.categories.map((sec) => {
    const rules = sec.rules.filter((r) => r.in_scope);
    if (!rules.length) return "";
    const cards = rules.map((r) => {
      const iss = r.issues || {};
      const openN = (iss.open || 0) + (iss.processing || 0);
      const fixMode = (r.fix || "").startsWith("auto:");
      const neverFix = r.fix === "never";
      const items = (r.items || []).map((it) => {
        const t = it.tname;
        const keyTxt = it.rkey || (it.detail && (it.detail.ce || "")) || "";
        const what = Object.entries(it.vals || {})
          .map(([f, v]) => `${esc(f)}="${esc(v === null || v === "" ? "（空）" : v)}"`).join("、")
          + (it.detail && it.detail.why ? `（${esc(it.detail.why)}）` : "")
          + (it.detail && it.detail.suggestion ? ` → 相近：${esc(it.detail.suggestion)}` : "")
          + (it.detail && it.detail.suggest !== undefined && it.detail.suggest !== null ? ` → ${esc(JSON.stringify(it.detail.suggest))}` : "");
        return `
        <div class="ck-item">
          <span class="ck-key">${esc(keyTxt || "(无编号)")}</span>
          <span class="ids">${what}</span>
          <button class="btn" ${jump} data-t="${t}" data-q="${esc(keyTxt || "")}">定位</button>
        </div>`;
      }).join("");
      return `
      <div class="ck-card">
        <h3><span class="count-badge ${r.count ? "some" : "zero"}">${r.count}</span> ${esc(r.title)}
          <span class="hint" style="font-weight:400">（规则 ${esc(r.rule_id)} v${r.ver} · 分母 ${r.denominator} · 例外 ${r.exception_n}）</span>
          ${fixMode && fixable && r.count ? `<button class="btn primary" style="margin-left:10px" data-rulefix="${esc(r.rule_id)}">预览修复</button>` : ""}
          ${neverFix ? `<span class="hint" style="margin-left:8px">默认不自动修复</span>` : ""}
          <button class="btn" style="margin-left:6px" data-ruleiss="${esc(r.rule_id)}">工单（${openN}）</button>
        </h3>
        <div class="ck-sub">${esc(r.rationale)}${r.error ? ` <b>（检测出错：${esc(r.error)}）</b>` : ""}</div>
        ${items || `<div class="ck-item ck-ok">没有发现问题 ✔</div>`}
      </div>`;
    }).join("");
    return `<h2 style="font-size:15px;margin:14px 0 6px">${esc(sec.label)}<span class="hint" style="font-weight:400;margin-left:8px">${secNote[sec.key] || ""}</span></h2>${cards}`;
  }).join("");
  $("checkup-body").innerHTML = `
    <div class="form-actions" style="margin:0 0 8px">
      <button class="btn primary" id="ck-scan">扫描入账（生成/更新工单）</button>
      <button class="btn" id="ck-issues">全部工单</button>
      <span class="hint">规则注册表 ${esc(d.registry_version)} · 按维度展示计数/分母/例外数，不提供综合评分</span>
    </div>
    ${sections}`;
  // ck-bar 归位到体检页顶部
  const bar = document.getElementById("ck-bar");
  if (bar && bar.parentElement !== document.getElementById("view-checkup")) {
    document.getElementById("view-checkup").prepend(bar);
  }
  bar.hidden = false;
  wireCkDone();
  $("ck-scan").onclick = async () => {
    const b = $("ck-scan");
    b.disabled = true;
    try {
      const r = await api("/api/checkup/scan", { method: "POST" });
      const s = r.stats || {};
      toast(`扫描完成：新增 ${s.created || 0}、重开 ${s.reopened || 0}、自动关闭 ${s.autoclosed || 0}、例外保留 ${s.kept || 0}`, "ok");
      loadCheckup();
    } catch (e) { toast(e.message, "err"); b.disabled = false; }
  };
  $("ck-issues").onclick = () => openIssuesModal({});
  document.querySelectorAll("#checkup-body button[data-ruleiss]").forEach((b) => {
    b.onclick = () => openIssuesModal({ rule_id: b.dataset.ruleiss });
  });
  document.querySelectorAll("#checkup-body button[data-rulefix]").forEach((b) => {
    b.onclick = () => previewRuleFix(b.dataset.rulefix);
  });
  const prio = d.priority_order.filter((r) => r.in_scope && r.count);
  $("ck-stat").textContent = prio.length
    ? `优先处理：${prio.slice(0, 3).map((r) => `${r.title} ${r.count}`).join("、")}${prio.length > 3 ? " 等 " + prio.length + " 类" : ""}`
    : "全部检查通过 ✔";
  S._ckBack = { remaining: (() => {
    try {
      const done = ckDoneMap();
      let n = 0;
      document.querySelectorAll("#checkup-body .ck-card").forEach((card) => {
        const ctype = ckCardType(card.querySelector("h3")?.textContent || "");
        card.querySelectorAll(".ck-item").forEach((item, idx) => {
          const key = ctype + ":" + (item.querySelector(".ck-key")?.textContent || "#" + idx).trim();
          if (!done[key]) n++;
        });
      });
      return n;
    } catch { return 0; }
  })() };
  $("checkup-body").querySelectorAll("button[data-jump]").forEach((b) => {
    b.onclick = () => {
      switchView(b.dataset.t);
      $("search").value = b.dataset.q;
      S.q = b.dataset.q;
      S.page = 1;
      loadTable();
    };
  });
}

/* ---- 规则修复：先预览 before/after、规则与影响 → 确认后统一写入（单批次可撤销） ---- */
async function previewRuleFix(ruleId) {
  try {
    const p = await api("/api/checkup/fix", { method: "POST", body: JSON.stringify({ rule_id: ruleId, dry_run: true }) });
    if (!p.preview || !p.preview.length) { toast("没有待修复的工单", "ok"); return; }
    const rows = p.preview.map((it) => {
      const ch = Object.entries(it.changes || {});
      if (!ch.length) return "";
      return `<tr><td>${esc(it.key || ("#" + it.rid))}</td>${ch.map(([f, pair]) =>
        `<td>${esc(f)}：<s>${esc(pair[0] === null || pair[0] === "" ? "（空）" : pair[0])}</s> → <b>${esc(pair[1])}</b></td>`).join("")}</tr>`;
    }).join("");
    const ok = await confirmBox(
      `规则「${esc(p.title)}」（${esc(p.rule_id)}）\n${esc(p.rationale)}\n\n影响 ${p.impact} 行，修复将写入以下变更（单批次、可整体撤销）：\n\n` +
      `<div style="max-height:38vh;overflow:auto"><table class="tbl" style="width:100%;font-size:12px"><tbody>${rows}</tbody></table></div>`,
      { okText: `修复 ${p.impact} 行`, danger: false });
    if (!ok) return;
    const r = await api("/api/checkup/fix", { method: "POST", body: JSON.stringify({ rule_id: ruleId, dry_run: false }) });
    toast(`修复完成：${r.fixed} 行（批次 ${r.batch}，可 Ctrl+Z 撤销；对应工单已标记解决）`, "ok");
    loadCheckup();
  } catch (e) { toast(e.message, "err"); }
}

/* ---- 工单列表：状态流转（解决/暂缓/例外须写理由）、分派；处理后列表原位刷新 ---- */
async function openIssuesModal(filter) {
  try {
    const q = new URLSearchParams(Object.entries(filter).filter(([, v]) => v)).toString();
    const d = await api("/api/checkup/issues?" + q);
    const rows = d.items.map((it) => `
      <tr data-iid="${it.id}">
        <td>${esc(it.rkey || (it.rid ? "#" + it.rid : ""))}</td>
        <td>${esc(it.rule_id)}</td>
        <td><span class="hint">${esc(it.status_label)}</span></td>
        <td>${esc(it.note || "")}${it.assigned_to ? `<div class="hint">负责：${esc(it.assigned_to)} · 重开 ${it.reopen_count} 次</div>` : ""}</td>
        <td>
          <select class="iss-st" style="font-size:12px">
            ${Object.entries(d.status_labels).map(([k, v]) =>
              `<option value="${k}" ${k === it.status ? "selected" : ""}>${esc(v)}</option>`).join("")}
          </select>
          <input class="iss-note" placeholder="理由/备注（解决/暂缓/例外必填）" style="width:150px;font-size:12px">
          <button class="btn iss-save">保存</button>
          ${it.assigned_to ? "" : `<button class="btn iss-assign">认领</button>`}
        </td>
      </tr>`).join("");
    openModal("问题工单" + (filter.rule_id ? " · " + filter.rule_id : ""),
      `<div style="max-height:60vh;overflow:auto"><table class="tbl" style="width:100%;font-size:12px">
        <thead><tr><th>记录</th><th>规则</th><th>状态</th><th>备注</th><th>处理</th></tr></thead>
        <tbody>${rows || `<tr><td colspan="5" class="hint">没有符合条件的工单</td></tr>`}</tbody></table></div>
      <div class="form-actions"><button class="btn" id="iss-close">关闭</button></div>`);
    $("iss-close").onclick = closeModal;
    document.querySelectorAll("#modal-body tr[data-iid]").forEach((tr) => {
      const iid = tr.dataset.iid;
      const save = tr.querySelector(".iss-save");
      if (save) save.onclick = async () => {
        try {
          await api(`/api/checkup/issues/${iid}`, { method: "POST", body: JSON.stringify({
            action: "status", status: tr.querySelector(".iss-st").value,
            note: tr.querySelector(".iss-note").value }) });
          toast("工单已更新", "ok");
          openIssuesModal(filter); // 原位刷新（同一筛选条件重新拉取）
        } catch (e) { toast(e.message, "err"); }
      };
      const as = tr.querySelector(".iss-assign");
      if (as) as.onclick = async () => {
        try {
          await api(`/api/checkup/issues/${iid}`, { method: "POST", body: JSON.stringify({ action: "assign" }) });
          toast("已认领", "ok");
          openIssuesModal(filter);
        } catch (e) { toast(e.message, "err"); }
      };
    });
  } catch (e) { toast(e.message, "err"); }
}
$("ck-refresh").onclick = () => loadCheckup();
/* 数据工具下拉开合 */
const ckToolsBtn = $("ck-tools-btn"), ckToolsMenu = $("ck-tools-menu");
if (ckToolsBtn && ckToolsMenu) {
  ckToolsBtn.onclick = (e) => { e.stopPropagation(); ckToolsMenu.hidden = !ckToolsMenu.hidden; };
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#ck-tools-menu") && e.target !== ckToolsBtn) ckToolsMenu.hidden = true;
  });
  ckToolsMenu.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => { ckToolsMenu.hidden = true; }));
}

/* 体检条目"已处理"标记：以卡片类型+条目主键文本为身份，localStorage 记忆 */
function ckDoneMap() {
  try { return JSON.parse(localStorage.getItem("ck_done") || "{}"); } catch { return {}; }
}
function ckCardType(h3) {
  const t = h3 || "";
  if (t.includes("重复主键")) return "dups";
  if (t.includes("不存在的采集事件号")) return "miss";
  if (t.includes("鉴定待复查")) return "rq";
  if (t.includes("已变为异名")) return "syn";
  if (t.includes("经纬度异常")) return "gb";
  if (t.includes("日期不完整")) return "dp";
  if (t.includes("疑似填写有误的国家")) return "ga";
  return "other";
}
function wireCkDone() {
  const bar = document.getElementById("ck-bar");
  const doneMap = ckDoneMap();
  const hide = localStorage.getItem("ck_hide_done") === "1";
  if (bar) bar.innerHTML = `
    <label style="display:flex;align-items:center;gap:6px;font-size:12.5px">
      <input type="checkbox" id="ck-hide" ${hide ? "checked" : ""}>
      隐藏已处理（${Object.keys(doneMap).length} 项）
    </label>`;
  const hideCb = document.getElementById("ck-hide");
  if (hideCb) hideCb.onchange = () => {
    localStorage.setItem("ck_hide_done", hideCb.checked ? "1" : "0");
    loadCheckup();
  };
  document.querySelectorAll("#checkup-body .ck-card").forEach((card) => {
    const ctype = ckCardType(card.querySelector("h3")?.textContent || "");
    card.querySelectorAll(".ck-item").forEach((item, idx) => {
      const key = ctype + ":" + (item.querySelector(".ck-key")?.textContent || "#" + idx).trim();
      if (item.querySelector(".ck-done-btn")) return;
      const isDone = !!doneMap[key];
      const btn = document.createElement("button");
      btn.className = "btn ck-done-btn";
      btn.textContent = isDone ? "↺" : "✓";
      btn.title = isDone ? "撤销已处理标记" : "标记为已处理（本地记住）";
      btn.onclick = () => {
        const m = ckDoneMap();
        if (m[key]) delete m[key]; else m[key] = 1;
        localStorage.setItem("ck_done", JSON.stringify(m));
        loadCheckup();
      };
      item.appendChild(btn);
      if (isDone) item.classList.add("ck-done");
      else if (hide) item.style.display = "none";
    });
  });
}
$("ck-coord").onclick = async () => {
  $("ck-coord").disabled = true;
  try {
    const r = await api("/api/geo/convert-preview");
    if (!r.count && !r.unparsed_count) {
      toast("没有需要转换的坐标（全部已是规范小数度）", "ok");
      return;
    }
    const items = (r.items || []).map((it) => `
      <div class="ck-item">
        <span class="ck-key">${esc(it.ce || "(无事件号)")}</span>
        <span class="ids">${Object.entries(it.fields).map(([k, v]) => `${esc(k)}：${esc(v)} → <b>${esc(it.converted[k])}</b>`).join("；")}</span>
        ${it.plausible ? '<span class="tag" style="background:#e5f3e8;color:#2f7d44">核实通过</span>'
                       : `<span class="tag" style="background:#fdeaea;color:#b04a4a">⚠ ${esc(it.why || "坐标超出该国常见范围")}</span>`}
      </div>`).join("");
    const unparsed = r.unparsed_count ? `
      <p class="hint" style="margin-top:10px;color:#a05a00">⚠ 无法自动解析 ${r.unparsed_count} 处（歧义书写，系统不猜不改，需人工核对后直接编辑原字段）：</p>
      <div style="max-height:22vh;overflow-y:auto;border:1px dashed var(--border);border-radius:8px;padding:8px">
        ${(r.unparsed || []).map((u) => `
          <div class="ck-item"><span class="ck-key">${esc(u.ce || ("#" + u.id))}</span>
          <span class="ids">${esc(u.field)}：${esc(u.raw)}</span></div>`).join("")}
        ${r.unparsed_count > (r.unparsed || []).length ? `<div class="ck-item"><span class="ids">…其余 ${r.unparsed_count - r.unparsed.length} 处同类</span></div>` : ""}
      </div>` : "";
    openModal(`坐标转换预览 · ${r.count} 行可转`, `
      <p class="hint">以下度分(秒)坐标将转换为小数度、装饰小数度将去掉 °/半球符号（数值不变，保留 6 位小数）。转换后按国别做粗范围核实，带 ⚠ 的行建议人工核对。</p>
      <p class="hint">不合理的行默认<b>跳过不转</b>；应用后记为一个批次，可整体撤销（变更历史）。</p>
      ${r.count ? `<div style="max-height:34vh;overflow-y:auto;border:1px solid var(--border);border-radius:8px;padding:8px">${items}</div>` : '<p class="hint">没有可自动转换的坐标。</p>'}
      ${unparsed}
      <div class="form-actions">
        <button class="btn" id="cv-cancel">取消</button>
        ${r.count ? `<button class="btn" id="cv-safe">仅应用核实通过的 ${r.count - r.warn} 行</button>
        <button class="btn primary" id="cv-all">全部应用（${r.count} 行）</button>` : ""}
      </div>`);
    $("cv-cancel").onclick = closeModal;
    if (r.count) {
      const apply = async (safeOnly) => {
        const rr = await api("/api/geo/convert-apply", { method: "POST", body: JSON.stringify({ safe_only: safeOnly }) });
        closeModal();
        toast(`坐标整理完成：${rr.rows} 行 · ${rr.fields} 个字段值已转为规范小数度${rr.skipped ? `（跳过核实异常 ${rr.skipped} 行）` : ""}${r.unparsed_count ? `；无法解析 ${r.unparsed_count} 处保持原样` : ""}`, "ok");
        loadCheckup();
      };
      $("cv-safe").onclick = () => apply(true).catch((e) => toast(e.message, "err"));
      $("cv-all").onclick = () => apply(false).catch((e) => toast(e.message, "err"));
    }
  } catch (e) { toast(e.message, "err"); }
  $("ck-coord").disabled = false;
};

$("ck-wsc").onclick = async () => {
  $("ck-wsc").disabled = true;
  try {
    // 两段式：先预览差异（不写库），管理员核对后逐条/成批接受
    const r = await api("/api/wsc/sync_species", { method: "POST" });
    const items = r.preview || [];
    if (!items.length) {
      toast(`WSC 校准：官方变更种 ${r.changed_lsids} 个，库内无需更新的行`, "ok");
    } else {
      const rows = items.map((it) => `
        <tr>
          <td>${esc(it.key || ("#" + it.rid))}</td>
          <td>${esc(it.from)}</td>
          <td>→ ${esc(it.to)}</td>
          <td>${it.protected ? `<span class="hint">受保护：${esc(it.protected)}</span>` : "可接受"}</td>
        </tr>`).join("");
      const ok = await confirmBox(
        `按 WSC 官方更新流核对 FinalSpeciesName：官方变更种 ${r.changed_lsids} 个，库内 ${items.length} 行需要更名（差异未写库）。\n\n` +
        `受保护行（人工锁定 / 已确认 / 有依据）不会被覆盖。\n接受后每行追加一条「校准」鉴定记录，可随批次撤销。\n\n` +
        `<div style="max-height:38vh;overflow:auto"><table class="tbl" style="width:100%;font-size:12px">
         <thead><tr><th>编号</th><th>现名</th><th>接受名</th><th>状态</th></tr></thead><tbody>${rows}</tbody></table></div>`,
        { okText: `接受全部可接受行（${items.filter((x) => !x.protected).length}）` });
      if (!ok) { $("ck-wsc").disabled = false; return; }
      const ar = await api("/api/wsc/sync_species/apply", { method: "POST", body: JSON.stringify({ all_pending: true }) });
      toast(`WSC 校准已接受：更新 ${ar.applied} 行` +
        (ar.skipped && ar.skipped.length ? `、跳过 ${ar.skipped.length} 行（受保护/不符）` : "") +
        `（批次 ${ar.batch}，可撤销）`, "ok");
      loadCheckup();
    }
  } catch (e) { toast(e.message, "err"); }
  $("ck-wsc").disabled = false;
};

$("ck-geo").onclick = async () => {
  if (!(await confirmBox("把采集地信息表里的国家、省份、城市统一为官方正式地名？\n（如「广西」→「广西壮族自治区」、「新疆」→「新疆维吾尔自治区」）\n\n整理会记入变更历史；未知的地名保持原样不动。", { okText: "开始整理" }))) return;
  try {
    const r = await api("/api/geo/normalize", { method: "POST" });
    toast(`整理完成：更新 ${r.updated} 行（国家 ${r.fields.Country}、省份 ${r.fields.Province}、城市 ${r.fields.City} 处）`, "ok");
    loadCheckup();
  } catch (e) { toast(e.message, "err"); }
};

/* ---- 统计 ---- */
function barChart(title, items) {
  const max = Math.max(0, ...items.map((i) => i.value));
  return `
  <div class="stat-card">
    <h3>${esc(title)}</h3>
    ${items.map((it) => `
      <div class="bar-row">
        <span class="bar-label" title="${esc(it.name)}">${esc(it.name || "（空）")}</span>
        <span class="bar-track"><span class="bar-fill" style="width:${max ? (it.value / max * 100).toFixed(1) : 0}%"></span></span>
        <span class="bar-val">${it.value.toLocaleString()}</span>
      </div>`).join("") || `<p class="hint">暂无数据</p>`}
  </div>`;
}

/* ---- 可选统计图表（v4.15）：条形/环形/柱状/折线四类渲染器（离线 SVG，不引外部库），
   每张图卡片头部可切换类型，选择按浏览器记忆（localStorage）。 ---- */
const CHART_PAL = ["#3f6b99", "#6b8cae", "#8fb3d6", "#b9cfe4", "#d9e5f0",
  "#b08d3f", "#7ba05b", "#a2636b", "#6b5b95", "#4a8c7a", "#9a6b4f", "#5a5a8c"];
const CHART_TYPE_LBL = { bar: "条形", donut: "环形", col: "柱状", line: "折线" };

function stChartTypes() {
  try { return JSON.parse(localStorage.getItem("sm_chart_types") || "{}"); } catch { return {}; }
}

function barBody(items) {
  const data = (items || []).filter((i) => i.value > 0);
  const max = Math.max(0, ...data.map((i) => i.value));
  return data.map((it) => `
    <div class="bar-row">
      <span class="bar-label" title="${esc(it.name)}">${esc(it.name || "（空）")}</span>
      <span class="bar-track"><span class="bar-fill" style="width:${max ? (it.value / max * 100).toFixed(1) : 0}%"></span></span>
      <span class="bar-val">${it.value.toLocaleString()}</span>
    </div>`).join("") || `<p class="hint">暂无数据</p>`;
}

function donutBody(items) {
  let data = (items || []).filter((i) => i.value > 0);
  if (!data.length) return `<p class="hint" style="margin:6px 2px">暂无数据</p>`;
  if (data.length > 8) {
    const rest = data.slice(8).reduce((s, i) => s + i.value, 0);
    data = data.slice(0, 8).concat([{ name: `其他（${data.length - 8} 项）`, value: rest }]);
  }
  const total = data.reduce((s, i) => s + i.value, 0);
  const R = 58, C = 2 * Math.PI * R;
  let off = 0;
  const slices = data.map((it, i) => {
    const len = it.value / total * C;
    const seg = `<circle r="${R}" cx="80" cy="80" fill="none" stroke="${CHART_PAL[i % CHART_PAL.length]}"
      stroke-width="30" stroke-dasharray="${len.toFixed(2)} ${(C - len).toFixed(2)}"
      stroke-dashoffset="${(-off).toFixed(2)}" style="transform:rotate(-90deg);transform-origin:80px 80px">
      <title>${esc(it.name)}：${it.value.toLocaleString()}（${(it.value / total * 100).toFixed(1)}%）</title></circle>`;
    off += len;
    return seg;
  }).join("");
  const legend = data.map((it, i) => `<div class="dg-leg" title="${esc(it.name)}">
      <span class="dg-dot" style="background:${CHART_PAL[i % CHART_PAL.length]}"></span>
      <span class="dg-name">${esc(it.name)}</span>
      <span class="dg-val">${it.value.toLocaleString()}</span>
      <span class="dg-pct">${(it.value / total * 100).toFixed(1)}%</span></div>`).join("");
  return `<div class="dg-wrap"><svg viewBox="0 0 160 160" class="dg-svg">${slices}
    <text x="80" y="76" text-anchor="middle" class="dg-total">${total.toLocaleString()}</text>
    <text x="80" y="94" text-anchor="middle" class="dg-sub">总计</text></svg>
    <div class="dg-legend">${legend}</div></div>`;
}

function colBody(items) {
  const data = (items || []).filter((i) => i.value > 0).slice(0, 20);
  if (!data.length) return `<p class="hint" style="margin:6px 2px">暂无数据</p>`;
  // v4.18：加大底部/左侧留白——斜排标签完整显示（此前长名被视口下缘裁掉开头）
  const W = 560, H = 300, padL = 52, padB = 96, padT = 16;
  const max = Math.max(...data.map((d) => d.value));
  const step = (W - padL - 14) / data.length;
  const bw = Math.max(7, step * 0.62);
  const bars = data.map((d, i) => {
    const h = Math.max(2, (H - padB - padT) * (d.value / max));
    const x = padL + i * step + (step - bw) / 2;
    const cx = (x + bw / 2).toFixed(1);
    const nm = String(d.name);
    return `<rect x="${x.toFixed(1)}" y="${(H - padB - h).toFixed(1)}" width="${bw.toFixed(1)}" height="${h.toFixed(1)}" rx="2" class="cl-bar"><title>${esc(nm)}：${d.value.toLocaleString()}</title></rect>
      <text x="${cx}" y="${H - padB + 15}" text-anchor="end" class="cl-lb" transform="rotate(-40 ${cx} ${H - padB + 15})">${esc(nm.length > 16 ? nm.slice(0, 15) + "…" : nm)}</text>
      <text x="${cx}" y="${(H - padB - h - 4).toFixed(1)}" text-anchor="middle" class="cl-vv">${d.value.toLocaleString()}</text>`;
  }).join("");
  const grid = [0.5, 1].map((f) => {
    const y = (H - padB - (H - padB - padT) * f).toFixed(1);
    return `<line x1="${padL}" x2="${W - 10}" y1="${y}" y2="${y}" class="cl-grid"/>
      <text x="${padL - 6}" y="${Number(y) + 4}" text-anchor="end" class="cl-gv">${Math.round(max * f)}</text>`;
  }).join("");
  return `<svg viewBox="0 0 ${W} ${H}" class="cl-svg">${grid}${bars}</svg>`;
}

function lineBody(items) {
  const data = (items || []).filter((i) => i.value > 0);
  if (!data.length) return `<p class="hint" style="margin:6px 2px">暂无数据</p>`;
  if (data.length < 2) return colBody(items);   // 单点退化为柱状
  const W = 520, H = 216, padL = 42, padB = 40, padT = 14;
  const max = Math.max(...data.map((d) => d.value));
  const xs = (i) => padL + (W - padL - 12) * (i / (data.length - 1));
  const ys = (v) => H - padB - (H - padB - padT) * (v / max);
  const pts = data.map((d, i) => `${xs(i).toFixed(1)},${ys(d.value).toFixed(1)}`).join(" ");
  const area = `${padL},${H - padB} ${pts} ${xs(data.length - 1).toFixed(1)},${H - padB}`;
  const every = Math.ceil(data.length / 8);
  const labels = data.map((d, i) => (i % every === 0 || i === data.length - 1)
    ? `<text x="${xs(i).toFixed(1)}" y="${H - 10}" text-anchor="middle" class="cl-lb">${esc(d.name)}</text>` : "").join("");
  const dots = data.map((d, i) => `<circle cx="${xs(i).toFixed(1)}" cy="${ys(d.value).toFixed(1)}" r="3.2" class="ln-pt"><title>${esc(d.name)}：${d.value.toLocaleString()}</title></circle>`).join("");
  const grid = [0.25, 0.5, 0.75, 1].map((f) => {
    const y = ys(max * f).toFixed(1);
    return `<line x1="${padL}" x2="${W - 10}" y1="${y}" y2="${y}" class="cl-grid"/>
      <text x="${padL - 6}" y="${Number(y) + 4}" text-anchor="end" class="cl-gv">${Math.round(max * f)}</text>`;
  }).join("");
  return `<svg viewBox="0 0 ${W} ${H}" class="cl-svg">${grid}
    <polygon points="${area}" class="ln-area"/><polyline points="${pts}" class="ln-line"/>${dots}${labels}</svg>`;
}

const CHART_RENDERERS = { bar: barBody, donut: donutBody, col: colBody, line: lineBody };

function stCard(key, title, items, types, note) {
  const cur = stChartTypes()[key] || types[0];
  const body = (CHART_RENDERERS[cur] || barBody)(items);
  const sw = types.length > 1
    ? `<span class="ct-swbox">${types.map((t) =>
        `<button class="ct-sw ${t === cur ? "on" : ""}" data-ctk="${esc(key)}" data-ctt="${t}"
          title="切换为${CHART_TYPE_LBL[t]}图">${CHART_TYPE_LBL[t]}</button>`).join("")}</span>`
    : "";
  return `<div class="stat-card ${cur === "donut" ? "dg-card" : ""}">
    <h3>${esc(title)}${sw}</h3>
    ${body}${note ? `<p class="hint" style="margin-top:6px">${esc(note)}</p>` : ""}</div>`;
}

function stRenderChartsGrid(en) {
  const grid = document.getElementById("stats-grid");
  if (!grid) return;
  const sex = en.sex || {};
  const specs = [
    ["genera", "标本数 · 按属", en.genera, ["bar", "donut", "col"]],
    ["tribes", "标本数 · 按族", en.tribes, ["bar", "donut", "col"]],
    ["families", "标本数 · 按科", en.families, ["bar", "donut", "col"]],
    ["provinces", "标本数 · 按省份（按采集事件关联）", en.provinces, ["bar", "donut", "col"]],
    ["countries", "标本数 · 按国家", en.countries, ["bar", "donut", "col"]],
    ["types", "模式标本类型分布", en.types, ["bar", "donut"]],
    ["years", "标本数 · 按采集年份", en.years, ["col", "line", "bar"]],
    ["sex", "个体数量 · 按性别（个体数求和；空值不当 0）",
      [{ name: "雄", value: sex.male || 0 }, { name: "雌", value: sex.female || 0 },
       { name: "亚成", value: sex.subadult || 0 }, { name: "幼体", value: sex.juvenile || 0 }],
      ["donut", "bar"]],
  ];
  grid.innerHTML = specs.map(([k, t, d, types]) => stCard(k, t, d || [], types)).join("");
  grid.querySelectorAll(".ct-sw").forEach((b) => {
    b.onclick = () => {
      const m = stChartTypes();
      m[b.dataset.ctk] = b.dataset.ctt;
      try { localStorage.setItem("sm_chart_types", JSON.stringify(m)); } catch { }
      stRenderChartsGrid(loadStats._lastEn || en);
    };
  });
}

/* ---- 可筛选统计（v4.11）：范围（默认主表/单表/类群联合）+ 搜索 + 列筛选 → 统计与图表 ---- */
S.statsF = S.statsF || { table: "", q: "", filters: {} };

function stScopeTables() {
  const f = S.statsF.table || "";
  if (f.startsWith("group:")) {
    const g = f.slice(6);
    return Object.entries(S.meta.tables).filter(([t, c]) => c.taxon_group === g && c.linked_key);
  }
  if (f) { const c = S.meta.tables[f]; return c ? [[f, c]] : []; }
  const pt = Object.entries(S.meta.tables).find(([, c]) => c.primary);
  return pt ? [pt] : [];
}

function stFieldOptions() {
  const tabs = stScopeTables();
  if (!tabs.length) return [];
  let cols = tabs[0][1].columns.map((c) => c.name);
  for (const [, c] of tabs.slice(1)) {
    const set = new Set(c.columns.map((x) => x.name));
    cols = cols.filter((n) => set.has(n));
  }
  return cols;
}

function stScopeOptions() {
  const groups = {};
  for (const [, c] of Object.entries(S.meta.tables)) {
    if (c.linked_key && c.taxon_group && c.taxon_group !== "shared")
      groups[c.taxon_group] = c.taxon_group_label || c.taxon_group;
  }
  return [["", "默认（首页主表）"],
    ...Object.entries(groups).map(([g, lbl]) => [`group:${g}`, `全部${lbl}标本表（联合）`]),
    ...Object.entries(S.meta.tables).map(([t, c]) => [t, `表：${tblName(c)}`])];
}

function stRenderFilters() {
  const box = $("stf-rows");
  if (!box) return;
  const cols = stFieldOptions();
  const entries = Object.entries(S.statsF.filters);
  box.innerHTML = entries.map(([col, f]) => `
    <div class="stf-row" style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin:4px 0">
      <select data-sfcol="${esc(col)}" class="input" style="width:auto">${cols.map((n) => {
        const c0 = (stScopeTables()[0][1].columns || []).find((x) => x.name === n);
        return `<option value="${esc(n)}" ${n === col ? "selected" : ""}>${esc((c0 && (c0.label || c0.name)) || n)}</option>`;
      }).join("")}</select>
      <select data-sfmode="${esc(col)}" class="input" style="width:auto">
        <option value="in" ${f.mode === "in" ? "selected" : ""}>属于（逗号分隔多值）</option>
        <option value="contains" ${f.mode === "contains" ? "selected" : ""}>包含文本</option>
        <option value="empty" ${f.mode === "empty" ? "selected" : ""}>为空</option>
        <option value="notempty" ${f.mode === "notempty" ? "selected" : ""}>不为空</option>
      </select>
      <input data-sfval="${esc(col)}" class="input" list="stf-dl" style="flex:1;min-width:140px"
        placeholder="值（为空/不为空时留空）" value="${esc(f.mode === "in" ? (f.values || []).join(",") : (f.text || ""))}"
        ${f.mode === "empty" || f.mode === "notempty" ? "disabled" : ""}>
      <button class="btn" data-sfdel="${esc(col)}">移除</button>
    </div>`).join("")
    || `<p class="hint" style="margin:4px 0 0">暂无筛选条件——统计范围内全部数据。点「添加条件」可连续添加多个字段条件（同时满足=并且）。</p>`;
  box.querySelectorAll("[data-sfdel]").forEach((b) => {
    b.onclick = () => { delete S.statsF.filters[b.dataset.sfdel]; stRenderFilters(); };
  });
  box.querySelectorAll("[data-sfcol]").forEach((sel) => {
    sel.onchange = () => {
      const old = sel.dataset.sfcol;
      if (old !== sel.value && S.statsF.filters[sel.value]) {
        toast("该字段已有条件（同一字段只保留一条）", "err");
        stRenderFilters();
        return;
      }
      const f = S.statsF.filters[old];
      delete S.statsF.filters[old];
      S.statsF.filters[sel.value] = f || { mode: "in", values: [] };
      stRenderFilters();
    };
  });
  box.querySelectorAll("[data-sfmode]").forEach((sel) => {
    sel.onchange = () => {
      const f = S.statsF.filters[sel.dataset.sfmode];
      f.mode = sel.value;
      stRenderFilters();
    };
  });
  box.querySelectorAll("[data-sfval]").forEach((inp) => {
    inp.oninput = () => {
      const f = S.statsF.filters[inp.dataset.sfval];
      if (f.mode === "in") f.values = inp.value.split(/[,，;；]/).map((s) => s.trim()).filter(Boolean);
      else f.text = inp.value;
    };
    inp.onfocus = () => stFillValues(inp);
  });
}

async function stFillValues(inp) {
  const col = inp.dataset.sfval;
  const tabs = stScopeTables();
  if (!tabs.length) return;
  try {
    const r = await api(`/api/rows/${tabs[0][0]}/values?col=${encodeURIComponent(col)}`);
    let dl = document.getElementById("stf-dl");
    if (!dl) {
      dl = document.createElement("datalist");
      dl.id = "stf-dl";
      $("stats-body").appendChild(dl);
    }
    dl.innerHTML = (r.values || []).slice(0, 300)
      .map(([v, n]) => `<option value="${esc(String(v))}">${n}</option>`).join("");
  } catch { }
}

function stFilterQuery() {
  const p = new URLSearchParams();
  if (S.statsF.table) {
    if (S.statsF.table.startsWith("group:")) p.set("group", S.statsF.table.slice(6));
    else p.set("table", S.statsF.table);
  }
  if (S.statsF.q) p.set("q", S.statsF.q);
  if (Object.keys(S.statsF.filters).length) p.set("filters", JSON.stringify(S.statsF.filters));
  const qs = p.toString();
  return qs ? "?" + qs : "";
}

async function loadStats() {
  $("st-stat").textContent = "统计中…";
  $("stats-body").innerHTML = `<p class="hint">正在计算…</p>`;
  let en;
  const qs = stFilterQuery();
  try {
    en = await api("/api/stats/enhanced" + qs);
  } catch (e) { $("stats-body").innerHTML = `<p class="hint warn">${esc(e.message)}</p>`; return; }
  if (en.empty) {
    $("stats-body").innerHTML = `<div class="empty">没有可见的数据表可供统计。</div>`;
    $("st-stat").textContent = "共 0 条";
    return;
  }
  const scope = en.scope || {};
  const metrics = en.metrics || {};
  const metricRows = Object.entries(metrics).map(([k, m]) => `
    <div class="dash-card" style="margin-bottom:8px"><b>${esc(m.label || k)}</b> = <b>${(m.value ?? 0).toLocaleString()}</b>
      <div class="hint">定义：${esc(m.definition || "")}；分母：${esc(m.denominator || "")}</div></div>`).join("");
  const tiers = en.tiers || {};
  const tierRows = ["precise", "province", "unknown"].map((k) => {
    const v = tiers[k];
    if (v === undefined) return "";
    const lbl = { precise: "精确（有经纬度，含事件源回退）", province: "仅知省份（按省名区域聚合）", unknown: "完全未知（不计入地图）" }[k];
    const n = (typeof v === "object" && v !== null) ? (v.count ?? 0) : v;  // {count,...} 或裸数字
    return `<tr><td>${lbl}</td><td><b>${Number(n || 0).toLocaleString()}</b></td></tr>`;
  }).join("");
  const sex = en.sex || {};
  const spc = en.species || {};
  const perTable = (scope.tables || []).map((p) => `
    <span class="tag">${esc(p.label)}：<b>${p.records.toLocaleString()}</b> 行</span>`).join(" ");
  const condChips = Object.entries(scope.filters || {}).map(([col, f]) => {
    const txt = f.mode === "in" ? `∈ ${(f.values || []).join("/")}`
      : f.mode === "contains" ? `含「${f.text || ""}」` : f.mode === "empty" ? "为空" : "非空";
    return `<span class="tag">${esc(col)} ${esc(txt)}</span>`;
  }).join(" ") + (scope.filter_q ? `<span class="tag">搜索「${esc(scope.filter_q)}」</span>` : "");
  $("stats-body").innerHTML = `
    <div class="dash-card" style="margin-bottom:12px"><h3>统计筛选（对哪张表、哪些行做统计）</h3>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:6px 0">
        <label>数据范围 <select id="stf-table" class="input" style="width:auto">
          ${stScopeOptions().map(([v, lbl]) => `<option value="${esc(v)}" ${S.statsF.table === v ? "selected" : ""}>${esc(lbl)}</option>`).join("")}
        </select></label>
        <input id="stf-q" class="input" placeholder="搜索全部列…" style="max-width:220px" value="${esc(S.statsF.q)}">
        <button class="btn" id="stf-add" type="button">${icon("plus")} 添加条件</button>
        <button class="btn primary" id="stf-apply" type="button">应用统计</button>
        <button class="btn" id="stf-clear" type="button" ${S.statsF.table || S.statsF.q || Object.keys(S.statsF.filters).length ? "" : "disabled"}>清空筛选</button>
      </div>
      <div id="stf-rows"></div>
      <div class="hint" style="margin-top:6px">
        可添加多个条件（同时满足=并且，如 Tribe 属于 Chrysillini ＋ 省份 包含 云南）；
        数据范围只包含<b>您有权限的数据表</b>（无权限的表不出现、也不参与统计）。<br>
        ${perTable ? perTable : "—"} ${condChips ? "· 条件：" + condChips : ""}
      </div>
    </div>
    <div class="stats-grid" id="stats-grid" style="margin-bottom:12px"></div>
    ${spc ? `<div class="dash-card" style="margin-bottom:12px"><h3>物种（按最终种名去重）</h3>
      <div class="sp-grid">
        <div class="st-tiles" style="flex-direction:column">
          <div class="st-tile"><div class="num">${Number(spc.count || 0).toLocaleString()}</div>
            <div class="lbl">物种<span class="sub">最终名去重；跨表同名算一个</span></div></div>
          <div class="st-tile"><div class="num">${Number(spc.temp_count || 0).toLocaleString()}</div>
            <div class="lbl">暂定名去重<span class="sub">含未正式鉴定名，单列不并入</span></div></div>
        </div>
        <div class="sp-top">${(spc.top || []).length ? `<table class="about-table"><thead><tr><th>物种（Top ${Math.min(12, spc.top.length)}）</th><th>记录数</th></tr></thead>
          <tbody>${spc.top.slice(0, 12).map((s0) => `<tr><td>${esc(s0.name)}</td><td class="num-cell">${s0.value.toLocaleString()}</td></tr>`).join("")}</tbody></table>` : `<p class="hint">范围内没有已定名的记录。</p>`}</div>
      </div>
    </div>` : ""}
    ${sex ? `<div class="dash-card" style="margin-bottom:12px"><h3>个体数量分解<span class="st-sub">各表数量列求和；空值不当 0</span></h3>
      <div class="st-tiles">
        <div class="st-tile"><div class="num">${Number(sex.male || 0).toLocaleString()}</div><div class="lbl">雄</div></div>
        <div class="st-tile"><div class="num">${Number(sex.female || 0).toLocaleString()}</div><div class="lbl">雌</div></div>
        <div class="st-tile"><div class="num">${Number(sex.subadult || 0).toLocaleString()}</div><div class="lbl">亚成</div></div>
        <div class="st-tile"><div class="num">${Number(sex.juvenile || 0).toLocaleString()}</div><div class="lbl">幼体</div></div>
        <div class="st-tile"><div class="num">${Number(sex.unknown_records || 0).toLocaleString()}</div><div class="lbl">数量列全空的记录<span class="sub">不当 0</span></div></div>
      </div>
      <p class="hint">${esc(sex.note || "")}</p></div>` : ""}
    <div class="dash-card" style="margin-bottom:12px"><h3>空间精度三级<span class="st-sub">按省名区域表达，不用省会坐标冒充点位精度</span></h3>
      <div class="st-tiles">
        <div class="st-tile"><div class="num">${Number((tiers.precise || {}).count || 0).toLocaleString()}</div>
          <div class="lbl">精确<span class="sub">有经纬度（含事件源回退），计入地图</span></div></div>
        <div class="st-tile"><div class="num">${Number((tiers.province || {}).count || 0).toLocaleString()}</div>
          <div class="lbl">仅知省份<span class="sub">按省名区域聚合，计入地图</span></div></div>
        <div class="st-tile"><div class="num">${Number(typeof tiers.unknown === "object" ? tiers.unknown.count : tiers.unknown || 0).toLocaleString()}</div>
          <div class="lbl">完全未知<span class="sub">无经纬度且无省份，不计入地图</span></div></div>
      </div></div>
    <div class="dash-card" style="margin-bottom:12px"><h3>统计范围（口径元数据）</h3>
      <div class="hint" style="line-height:1.8">
        统计对象：<b>${esc(scope.table_label || "")}</b>；分母：<b>${esc(scope.denominator || "")}</b>；
        排除：${esc((scope.exclusions || []).join("；"))}；
        ${scope.taxon_group ? `类群范围：${esc(scope.taxon_group)}；` : ""}查询指纹：<code>${esc(scope.query_fingerprint || "")}</code><br>
        ${scope.no_composite_score ? "不做综合得分（各指标独立呈现，不加权合成）。" : ""}
        ${metrics.unknown_quantity ? `数量列全空的记录 ${metrics.unknown_quantity.value.toLocaleString()} 条——缺少个体数量不当 0（unknown_quantity 如实呈现）。` : ""}
      </div></div>
    <div class="dash-card"><h3>三种计数口径（各口径的定义、分母与排除）</h3>${metricRows}</div>`;
  // ---- 筛选交互 ----
  $("stf-table").onchange = () => { S.statsF.table = $("stf-table").value;
    S.statsF.filters = {};   // 换范围后字段集不同：清空条件（防跨表脏筛选）
    stRenderFilters(); };
  $("stf-q").addEventListener("input", () => {
    clearTimeout(loadStats._qt);
    loadStats._qt = setTimeout(() => { S.statsF.q = $("stf-q").value.trim(); loadStats(); }, 400);
  });
  $("stf-add").onclick = () => {
    const cols = stFieldOptions();
    if (!cols.length) return toast("当前范围没有可用字段", "err");
    const unused = cols.find((n) => !S.statsF.filters[n]);
    if (!unused) return toast("范围内各表共有字段都已添加条件（同一字段只保留一条）", "err");
    S.statsF.filters[unused] = { mode: "in", values: [] };
    stRenderFilters();
  };
  $("stf-apply").onclick = () => loadStats();
  $("stf-clear").onclick = () => { S.statsF = { table: "", q: "", filters: {} }; loadStats(); };
  stRenderFilters();
  loadStats._lastEn = en;
  stRenderChartsGrid(en);
  const total = (scope.tables || []).reduce((s0, p) => s0 + p.records, 0);
  $("st-stat").textContent = `共 ${total.toLocaleString()} 条记录（${scope.table_label || ""}${qs ? " · 已筛选" : ""}）`;
  loadStats._lastQs = qs;
}
$("st-refresh").onclick = () => loadStats();
$("st-dl").onclick = () => downloadFile("/api/stats/chart-download" + (loadStats._lastQs || ""))
  .then(() => toast("图表数据已下载（CSV，附范围/筛选条件、口径定义与排除数量）", "ok"))
  .catch((e) => toast(e.message, "err"));

/* ---- 用户菜单 ---- */
$("user-btn").onclick = (e) => {
  e.stopPropagation();
  $("user-dropdown").hidden = !$("user-dropdown").hidden;
};
$("mi-perm").onclick = () => {
  const me = S.me;
  const roleTxt = me.admin ? "管理员" : (ROLE_TXT[me.role] || me.role || "编辑者");
  const groupLbl = me.group
    ? ((S.meta.taxon_groups || []).find((g) => g.key === me.group) || {}).label || me.group
    : "全部（不限类群）";
  const rows = Object.entries(S.meta.tables).map(([t, c]) => {
    const editable = canWriteTable(t);
    return `<tr><td>${esc(tblName(c))}</td>
      <td><span class="tag ${editable ? "perm-ok" : ""}">${editable ? "可编辑" : "只读"}</span></td>
      <td>${(c.total || 0).toLocaleString()}</td></tr>`;
  }).join("");
  openModal("我的权限", `
    <p style="margin:0 0 6px"><b>${esc(me.username)}</b> · ${roleTxt} · 可见类群：<b>${esc(groupLbl)}</b>
      ${me.can_boxes ? " · 标本盒管理 ✔" : ""}</p>
    <p class="hint" style="margin:0 0 10px">权限按表三态划分：<b>可编辑</b>=可查看且可增删改、导入、批量修改、版本管理、传附件；
    <b>只读</b>=仅可检索、筛选、导出、打印；<b>不可见</b>=完全隐藏（表中未列出的表即不可见）。
    删除的行进回收站可还原，彻底删除仅管理员。</p>
    <table class="about-table" style="width:100%"><thead><tr><th>数据表</th><th>权限</th><th>行数</th></tr></thead><tbody>${rows}</tbody></table>`);
};
$("mi-logout").onclick = async () => {
  if (!(await closeGate("退出登录"))) return;
  draftClearMine();   // 登出清除本账号本机草稿（敏感内容不留在这台电脑）
  try { await api("/api/auth/logout", { method: "POST" }); } catch { }
  showLogin();
};
$("mi-drafts").onclick = () => {
  const n = pendingDraftCount();
  if (!n) return toast("本机没有保存的草稿", "ok");
  openModal("清除本机草稿", `
    <p style="line-height:1.8">本机保存了 <b>${n}</b> 条草稿（按账号/表/记录隔离，留存 7 天，不上传服务器）。</p>
    <div class="form-actions">
      <button class="btn" id="dr-cancel">取消</button>
      <button class="btn danger" id="dr-ok">全部清除</button>
    </div>`);
  $("dr-cancel").onclick = closeModal;
  $("dr-ok").onclick = () => { const m = draftClearMine(); closeModal(); toast(`已清除 ${m} 条本机草稿`, "ok"); };
};
$("mi-password").onclick = () => {
  openModal("修改密码", `
    <div class="grid-form">
      <label class="span2">原密码<input id="pw-old" type="password"></label>
      <label>新密码<input id="pw-new" type="password"></label>
      <label>确认新密码<input id="pw-new2" type="password"></label>
    </div>
    <div class="form-actions"><button class="btn primary" id="pw-ok">保存</button></div>`);
  $("pw-ok").onclick = async () => {
    if ($("pw-new").value !== $("pw-new2").value) return toast("两次新密码不一致", "err");
    try {
      await api("/api/auth/change_password", {
        method: "POST",
        body: JSON.stringify({ old_password: $("pw-old").value, new_password: $("pw-new").value }),
      });
      closeModal();
      toast("密码已修改", "ok");
    } catch (e) { toast(e.message, "err"); }
  };
};
$("mi-backup").onclick = () => downloadFile("/api/backup").then(() => toast("备份已下载", "ok")).catch((e) => toast(e.message, "err"));
$("mi-boxes").onclick = openBoxes;
$("mi-tokens").onclick = openTokens;
$("mi-users").onclick = openUsers;

/* ---- 标本盒管理（需特别权限） ---- */
async function openBoxes() {
  const r = await loadBoxes(true);
  const items = r.boxes.map((b) => `
    <div class="user-row">
      <span class="u-name" style="width:auto">${esc(b.name)}</span>
      <span class="u-sub">${b.used}/${b.total} ${b.full ? "· 已用满" : "· 余 " + b.free}${b.note ? " · " + esc(b.note) : ""}</span>
      ${S.me.admin ? `<button class="btn danger" data-box-del="${b.id}">删除</button>` : ""}
    </div>`).join("") || `<p class="hint">还没有登记标本盒。可点「从现有数据导入」一键登记，或手动添加。</p>`;
  openModal("标本盒管理", `
    <p class="hint">登记标本盒的名称与号段区间（如 Box98(30000-30099)）。登记后，操作者编辑标本时只能从<b>未用满</b>的盒中选择；号段内编号全部用完后该盒自动不可选。</p>
    <div class="grid-form">
      <label>盒名称<input id="bx-name" placeholder="如 Box98(30000 -30099)"></label>
      <label>起始编号<input id="bx-start" type="number"></label>
      <label>结束编号<input id="bx-end" type="number"></label>
      <label>备注<input id="bx-note" placeholder="可选"></label>
    </div>
    <div class="form-actions">
      <button class="btn" id="bx-import">从现有数据导入</button>
      <button class="btn primary" id="bx-add">添加标本盒</button>
    </div>
    <div style="height:8px"></div>
    <div style="max-height:38vh;overflow-y:auto">${items}</div>`);
  $("bx-add").onclick = async () => {
    try {
      await api("/api/boxes", { method: "POST", body: JSON.stringify({
        name: $("bx-name").value, num_start: +$("bx-start").value,
        num_end: +$("bx-end").value, note: $("bx-note").value }) });
      toast("已添加标本盒", "ok");
      openBoxes();
    } catch (e) { toast(e.message, "err"); }
  };
  $("bx-import").onclick = async () => {
    try {
      const rr = await api("/api/boxes/import_from_data", { method: "POST" });
      toast(`已从现有数据登记 ${rr.added} 个标本盒`, "ok");
      openBoxes();
    } catch (e) { toast(e.message, "err"); }
  };
  $("modal-body").querySelectorAll("button[data-box-del]").forEach((b) => {
    b.onclick = async () => {
      if (!(await confirmBox("删除该标本盒登记？（不影响已有标本数据）", { okText: "删除" }))) return;
      try { await api(`/api/boxes/${b.dataset.boxDel}`, { method: "DELETE" }); toast("已删除"); openBoxes(); }
      catch (e) { toast(e.message, "err"); }
    };
  });
}

/* ---- 邀请码管理（管理员） ---- */
/* ---- 成员与邀请码（整合管理器） ---- */
let _memTab = "members";

function groupLabel(g) {
  return (S.meta.taxon_groups || []).find((x) => x.key === g)?.label ||
    { spider: "跳蛛系统", pseudo: "伪蝎系统", mygalomorph: "原蛛系统" }[g] || g;
}

async function openMembers(tab) {
  _memTab = tab || _memTab || "members";
  $("modal").classList.add("modal-wide");
  const [ur, ir] = await Promise.all([api("/api/users"), api("/api/invites")]);

  /* ---- 成员 Tab ---- */
  const activeN = ur.users.filter((u) => !u.disabled).length;
  const roN = ur.users.filter((u) => !u.is_admin && !u.disabled &&
    !Object.values(u.perms || {}).some((p) => p === "rw")).length;
  const viewerGA = !!ur.viewer_is_ga;
  const userCards = ur.users.map((u) => {
    const permline = u.is_admin
      ? '<span class="tag admin">全部权限</span>'
      : Object.entries(S.meta.tables).map(([t, c]) => {
          const cur = (u.perms || {})[t] || "none";
          return `<label class="p3-item" title="${esc(tblName(c))}：✏ 可查看且可编辑 / 👁 只读 / ✕ 不可见">
            <select data-pt3="${t}" data-uid="${u.id}" ${u.disabled ? "disabled" : ""}>
              <option value="rw" ${cur === "rw" ? "selected" : ""}>✏ 可编辑</option>
              <option value="r" ${cur === "r" ? "selected" : ""}>👁 只读</option>
              <option value="none" ${cur === "none" ? "selected" : ""}>✕ 不可见</option>
            </select><span class="p3-tbl">${esc(tblName(c))}</span></label>`;
        }).join("");
    return `
    <div class="user-card ${u.disabled ? "u-off" : ""}">
      <div class="user-head">
        ${u.is_admin || (viewerGA && u.role === "group_admin") ? "" : `<input type="checkbox" class="mem-ck" data-memid="${u.id}" style="accent-color:var(--primary);cursor:pointer">`}
        <span class="u-name">${esc(u.username)}</span>
        ${u.is_admin ? '<span class="tag admin">管理员</span>' : ""}
        ${u.role === "group_admin" ? '<span class="tag admin">类群管理员</span>' : ""}
        ${u.disabled ? '<span class="tag off">已停用</span>' : ""}
        <span class="u-sub">注册于 ${esc(u.created_at)}${u.last_edit ? ` · 最近编辑 ${esc(u.last_edit)}` : " · 尚无编辑"}</span>
        <span style="flex:1"></span>
        ${u.disabled ? `<button class="btn" data-u="on" data-id="${u.id}">启用</button>` : `<button class="btn" data-u="off" data-id="${u.id}">停用</button>`}
        <button class="btn" data-u="pw" data-id="${u.id}">重置密码</button>
      </div>
      <div class="user-permline">
        ${S.me.admin && !u.is_admin ? `<label class="perm-item" title="类群管理员：可为本类群生成邀请码并管理学生账号（不改变下方表格权限，权限始终以此处为准）">
          <input type="checkbox" data-ga="${u.id}" ${u.role === "group_admin" ? "checked" : ""} style="accent-color:var(--primary)"> 类群管理员
        </label>` : ""}
        <label class="perm-item" title="所属类群系统：决定首页统计视角">
          类群 <select data-ugroup="${u.id}" ${u.is_admin || u.role === "group_admin" || u.disabled || (S.me.group_admin && !S.me.admin) ? "disabled" : ""} style="padding:2px 4px;font-size:12px">
            ${["spider", "pseudo", "mygalomorph"].map((g) =>
              `<option value="${g}" ${u.taxon_group === g ? "selected" : ""}>${esc(groupLabel(g))}</option>`).join("")}
          </select>
        </label>
        <label class="perm-item" title="可登记/删除标本盒号段（跳蛛系统）">
          <input type="checkbox" data-bxperm="${u.id}" ${u.can_boxes || u.is_admin ? "checked" : ""}
            ${u.is_admin ? "disabled" : ""}> 盒管理
        </label>
        <label class="perm-item" title="允许使用组合表构建器：可创建/复制/配置自己的组合表并发布（管理员默认拥有；收回后已建定义保留，是否继续开放由管理员单独管理）">
          <input type="checkbox" data-ctperm="${u.id}" ${u.can_composite || u.is_admin ? "checked" : ""}
            ${u.is_admin ? "disabled" : ""}> 组合表构建器
        </label>
        <span class="perm-grid">${permline}</span>
      </div>
    </div>`;
  }).join("");
  const newMemberForm = ur.allow_register
    ? `<p class="hint" style="margin:0 0 10px">当前允许任何人在注册页自助注册。如需关闭，请在服务器 config.py 将 ALLOW_REGISTER 改为 False。</p>`
    : `<div class="user-card" style="background:#f8fafc">
        <div class="user-head" style="margin-bottom:8px"><b>直接添加成员</b></div>
        <div class="grid-form" style="grid-template-columns:1fr 1fr 1fr;gap:10px">
          <label>用户名<input id="nu-name" placeholder="登录名"></label>
          <label>初始密码<input id="nu-pw" placeholder="至少 4 位"></label>
          ${S.me.group_admin ? `<input type="hidden" id="nu-group" value="${esc(S.me.group || "spider")}">`
            : `<label>所属类群<select id="nu-group" style="padding:6px 8px">
            ${["spider", "pseudo", "mygalomorph"].map((g) => `<option value="${g}">${esc(groupLabel(g))}</option>`).join("")}
          </select></label>`}
        </div>
        <div class="form-actions" style="margin:8px 0 0"><button class="btn primary" id="nu-ok">添加成员</button>
        <span class="hint">添加后在下方卡片中逐表设置权限。</span></div>
      </div>`;

  /* ---- 邀请码 Tab ---- */
  const invRows = ir.invites.map((v) => `
    <div class="user-card ${v.disabled ? "u-off" : ""}">
      <div class="user-head">
        <span class="u-name" style="font-family:Consolas,monospace;letter-spacing:1px">${esc(v.code)}</span>
        <span class="tag ${v.disabled ? "off" : "admin"}">${v.disabled ? "已停用" : "有效"}</span>
        <span class="u-sub">${esc(v.note || "（未注明用途）")} · 已用 ${v.used}${v.max_uses ? "/" + v.max_uses : "（不限）"} · ${esc(groupLabel(v.taxon_group))}</span>
        <span style="flex:1"></span>
        <button class="btn" data-iv="copy" data-code="${esc(v.code)}">复制</button>
        <button class="btn" data-iv="dis" data-code="${esc(v.code)}" data-to="${v.disabled ? "0" : "1"}">${v.disabled ? "启用" : "停用"}</button>
        <button class="btn danger" data-iv="del" data-code="${esc(v.code)}">删除</button>
      </div>
      <div class="user-permline"><span class="perm-grid">
        ${(v.tables || []).includes("*") ? '<span class="tag admin">全部表可编辑</span>'
          : (v.tables || []).length
            ? (v.tables || []).map((t) => `<span class="tag">${esc(tblName(S.meta.tables[t]) || t)}</span>`).join("")
            : '<span class="tag off">只读</span>'}
      </span></div>
    </div>`).join("") || `<p class="hint">还没有邀请码。在上方生成一个发给协作者即可。</p>`;

  openModal("成员与邀请码", `
    <div class="mem-tabs">
      <button class="mem-tab" data-mt="members">成员 <span class="pill">${activeN}</span></button>
      <button class="mem-tab" data-mt="invites">邀请码 <span class="pill">${ir.invites.filter((v) => !v.disabled).length}</span></button>
      <span style="flex:1"></span>
      <span class="hint">共 ${ur.users.length} 名成员（${roN} 个只读账号）</span>
    </div>
    <div id="tab-members" class="mem-pane">
      ${newMemberForm}
      <div style="height:8px"></div>
      <div class="mem-batch" style="display:flex;gap:8px;align-items:center;margin:6px 0">
        <button class="btn" id="mem-ckall" type="button">全选/全不选</button>
        <button class="btn danger" id="mem-batch-del" type="button">删除所选</button>
        <span class="hint">批量删除用于清理测试/离职账号；编辑日志保留可审计；管理员与当前账号受保护</span>
      </div>
      <div class="mem-list">${userCards}</div>
    </div>
    <div id="tab-invites" class="mem-pane" hidden>
      <div class="user-card" style="background:#f8fafc">
        <div class="user-head" style="margin-bottom:8px"><b>生成新邀请码</b>
          <span class="hint">发给协作者：对方在注册页填入即获得对应权限，无需逐个建号</span></div>
        <div class="grid-form" style="grid-template-columns:1fr 1fr;gap:10px">
          <label>可用次数（0 = 不限）<input id="iv-max" type="number" min="0" value="1"></label>
          ${S.me.group_admin ? `<input type="hidden" id="iv-group" value="${esc(S.me.group || "spider")}">`
            : `<label>所属类群系统<select id="iv-group" style="padding:6px 8px">
            ${(S.meta.taxon_groups || []).map((g) => `<option value="${esc(g.key)}">${esc(g.label)}</option>`).join("")}
          </select></label>`}
          <label class="span2">备注（发给谁、用途）<input id="iv-note" placeholder="如：张同学 · 标本录入"></label>
          <label class="span2">注册后可编辑的表（全不勾 = 只读）
            <div class="perm-grid">
              ${Object.entries(S.meta.tables).map(([t, c]) =>
                `<label class="perm-item"><input type="checkbox" class="iv-t" value="${t}" checked> ${esc(tblName(c))}</label>`).join("")}
            </div>
          </label>
        </div>
        <div class="form-actions" style="margin:8px 0 0"><button class="btn primary" id="iv-new">生成邀请码</button></div>
      </div>
      <div style="height:8px"></div>
      <div class="mem-list">${invRows}</div>
    </div>`);

  /* ---- Tab 切换 ---- */
  const switchTab = (name) => {
    _memTab = name;
    document.querySelectorAll(".mem-tab").forEach((b) =>
      b.classList.toggle("active", b.dataset.mt === name));
    $("tab-members").hidden = name !== "members";
    $("tab-invites").hidden = name !== "invites";
  };
  document.querySelectorAll(".mem-tab").forEach((b) => b.onclick = () => switchTab(b.dataset.mt));
  switchTab(_memTab);

  /* ---- 批量删除成员 ---- */
  const memList = $("modal-body");
  $("mem-ckall").onclick = () => {
    const cks = [...memList.querySelectorAll(".mem-ck")];
    const allOn = cks.every((c) => c.checked);
    cks.forEach((c) => c.checked = !allOn);
  };
  $("mem-batch-del").onclick = async () => {
    const ids = [...memList.querySelectorAll(".mem-ck:checked")].map((c) => +c.dataset.memid);
    if (!ids.length) return toast("先勾选要删除的成员", "err");
    if (!(await confirmBox(`确定删除选中的 ${ids.length} 个账号？
其登录会话立即失效；编辑日志保留（审计可查）。`, { okText: "删除" }))) return;
    try {
      const r = await api("/api/users/batch_delete", { method: "POST", body: JSON.stringify({ ids }) });
      toast(`已删除 ${r.removed_n} 个账号${r.skipped_n ? `，跳过 ${r.skipped_n} 个（${r.skipped.map((s) => s.username + "：" + s.reason).join("；")}）` : ""}`, "ok");
      openMembers(_memTab);
    } catch (e) { toast(e.message, "err"); }
  };

  /* ---- 成员操作 ---- */
  if (!ur.allow_register) {
    $("nu-ok").onclick = async () => {
      try {
        await api("/api/users", { method: "POST", body: JSON.stringify({ username: $("nu-name").value, password: $("nu-pw").value, taxon_group: $("nu-group").value }) });
        toast("已添加，请在下方卡片设置逐表权限", "ok");
        openMembers(_memTab);
      } catch (e) { toast(e.message, "err"); }
    };
  }
  $("modal-body").querySelectorAll("input[data-bxperm]").forEach((cb) => {
    cb.onchange = async () => {
      try {
        await api(`/api/users/${cb.dataset.bxperm}`, { method: "PATCH", body: JSON.stringify({ can_boxes: cb.checked }) });
        toast("盒管理权限已更新", "ok");
      } catch (e) { toast(e.message, "err"); openMembers(_memTab); }
    };
  });
  $("modal-body").querySelectorAll("input[data-ctperm]").forEach((cb) => {
    cb.onchange = async () => {
      try {
        await api(`/api/users/${cb.dataset.ctperm}`, { method: "PATCH", body: JSON.stringify({ can_composite: cb.checked }) });
        toast(cb.checked ? "已授予组合表构建器权限" : "已收回组合表构建器权限（其已建定义保留，是否继续开放由管理员逐表管理）", "ok");
      } catch (e) { toast(e.message, "err"); openMembers(_memTab); }
    };
  });
  $("modal-body").querySelectorAll("input[data-ga]").forEach((cb) => {
    cb.onchange = async () => {
      try {
        const rr = await api(`/api/users/${cb.dataset.ga}`, { method: "PATCH", body: JSON.stringify({ group_admin: cb.checked }) });
        toast(rr.role === "group_admin" ? "已设为类群管理员（本类群全部表可编辑）" : "已取消类群管理员", "ok");
        openMembers(_memTab);  // 重渲染使角色徽章与勾选态与服务器一致
      } catch (e) { toast(e.message, "err"); openMembers(_memTab); }
    };
  });
  $("modal-body").querySelectorAll("select[data-pt3]").forEach((sel) => {
    sel.onchange = async () => {
      const uid = +sel.dataset.uid;
      const perms = {};
      document.querySelectorAll(`select[data-pt3][data-uid="${uid}"]`).forEach((s) =>
        perms[s.dataset.pt3] = s.value);
      try {
        await api(`/api/users/${uid}`, { method: "PATCH", body: JSON.stringify({ perms }) });
        toast("权限已更新", "ok");
        if (uid === S.me.id) enterApp();
      } catch (e) { toast(e.message, "err"); openMembers(_memTab); }
    };
  });
  $("modal-body").querySelectorAll("select[data-ugroup]").forEach((sel) =>
    sel.onchange = async () => {
      const uid = +sel.dataset.ugroup;
      try {
        await api(`/api/users/${uid}`, { method: "PATCH", body: JSON.stringify({ taxon_group: sel.value }) });
        toast("类群已更新", "ok");
        if (uid === S.me.id) enterApp();
      } catch (e) { toast(e.message, "err"); }
    });
  $("modal-body").querySelectorAll("button[data-u]").forEach((b) => {
    b.onclick = async () => {
      const id = +b.dataset.id;
      try {
        if (b.dataset.u === "pw") {
          const np = prompt("输入该账号的新密码（至少 4 位）：");
          if (!np) return;
          await api(`/api/users/${id}`, { method: "PATCH", body: JSON.stringify({ new_password: np }) });
          toast("已重置密码", "ok");
        } else {
          await api(`/api/users/${id}`, { method: "PATCH", body: JSON.stringify({ disabled: b.dataset.u === "off" }) });
          openMembers(_memTab);
        }
      } catch (e) { toast(e.message, "err"); }
    };
  });

  /* ---- 邀请码操作 ---- */
  const ivNew = $("iv-new");
  if (ivNew) ivNew.onclick = async () => {
    try {
      const tsel = [...document.querySelectorAll(".iv-t:checked")].map((x) => x.value);
      const rr = await api("/api/invites", { method: "POST", body: JSON.stringify({
        max_uses: +$("iv-max").value, note: $("iv-note").value, tables: tsel,
        taxon_group: $("iv-group") ? $("iv-group").value : "spider" }) });
      openModal("邀请码已生成", `
        <div class="conflict-val" style="font-size:24px;text-align:center;font-family:Consolas,monospace"><b>${esc(rr.code)}</b></div>
        <p class="hint">可编辑的表：${rr.tables.length ? rr.tables.map((t) => esc(tblName(S.meta.tables[t]) || t)).join("、") : "无（只读）"}。请立即复制发给协作者（关闭后仍可在列表中复制）。</p>
        <div class="form-actions">
          <button class="btn primary" id="iv-copy">复制邀请码</button>
          <button class="btn" id="iv-back">返回管理</button>
        </div>`);
      $("iv-copy").onclick = () => copyText(rr.code);
      $("iv-back").onclick = () => openMembers("invites");
    } catch (e) { toast(e.message, "err"); }
  };
  $("modal-body").querySelectorAll("button[data-iv]").forEach((b) => {
    b.onclick = async () => {
      const code = b.dataset.code;
      try {
        if (b.dataset.iv === "copy") copyText(code);
        else if (b.dataset.iv === "dis") {
          await api(`/api/invites/${code}`, { method: "PATCH", body: JSON.stringify({ disabled: b.dataset.to === "1" }) });
          openMembers("invites");
        } else {
          await api(`/api/invites/${code}`, { method: "DELETE" });
          toast("已删除");
          openMembers("invites");
        }
      } catch (e) { toast(e.message, "err"); }
    };
  });
}

function openInvites() { openMembers("invites"); }
function openUsers() { openMembers("members"); }
async function openTokens() {
  const r = await api("/api/tokens");
  openModal("API Token（给脚本用的访问凭证）", `
    <p class="hint">用 Token 可以写脚本批量回填数据（如测序结果回填 DNAVoucherID）。请求时带请求头
    <code>X-Auth: &lt;token&gt;</code>。示例：<br>
    <code>PUT /api/rows/specimens/by-key?key=MHBU-ARA-00020001</code>，内容
    <code>{"changes": {"DNAVoucherID": "DNA-001"}}</code></p>
    <div class="grid-form">
      <label class="span2">备注（这个 Token 用在哪）<input id="tk-note" placeholder="如：测序结果回填脚本"></label>
    </div>
    <div class="form-actions" style="margin-top:8px"><button class="btn primary" id="tk-new">生成新 Token</button></div>
    <div style="height:10px"></div>
    <div>${r.tokens.map((t) => `
      <div class="user-row">
        <span class="u-name" style="width:auto">${esc(t.token.slice(0, 10))}…</span>
        <span class="u-sub">${esc(t.note || "")} · ${esc(t.username)} · ${esc(t.created_at)}</span>
        <button class="btn danger" data-tk="${esc(t.token)}">删除</button>
      </div>`).join("") || `<p class="hint">还没有 Token。</p>`}</div>`);
  $("tk-new").onclick = async () => {
    try {
      const rr = await api("/api/tokens", { method: "POST", body: JSON.stringify({ note: $("tk-note").value }) });
      openModal("Token 已生成（仅此一次显示完整内容）", `
        <div class="conflict-val" style="word-break:break-all"><b>${esc(rr.token)}</b></div>
        <p class="hint">请立即复制保存，关闭后将无法再次查看。</p>
        <div class="form-actions"><button class="btn primary" id="tk-copy">复制</button></div>`);
      $("tk-copy").onclick = () => { copyText(rr.token); };
    } catch (e) { toast(e.message, "err"); }
  };
  $("modal-body").querySelectorAll("button[data-tk]").forEach((b) => {
    b.onclick = async () => {
      try { await api(`/api/tokens/${b.dataset.tk}`, { method: "DELETE" }); toast("已删除"); openTokens(); }
      catch (e) { toast(e.message, "err"); }
    };
  });
}


async function refreshMetaCounts() {
  try {
    S.meta = await api("/api/meta");
    const cfg = curCfg();
    if (cfg) {
      $("ver-count").textContent = cfg.versions || "";
      $("del-count").textContent = cfg.deleted || "";
      $("table-stat").textContent = `共 ${cfg.total.toLocaleString()} 行（${tblName(cfg)}${S.showDeleted ? " · 回收站" : S.preset === "loaned" ? " · 借出中" : ""}）`;
    }
  } catch { }
}

/* ---- 最近使用（v3.78）：按账号存偏好（服务端），容量上限 5；权限失效条目渲染时隐藏并静默修剪 ---- */
function recentList(kind) {
  const r = S.prefs.recents || {};
  return (r[kind] || []).slice(0, 5);
}
function pushRecent(kind, id, meta) {
  const r = S.prefs.recents = S.prefs.recents || {};
  const list = (r[kind] = r[kind] || []).filter((x) => x.id !== id);
  list.unshift({ id, at: Date.now(), ...(meta || {}) });
  r[kind] = list.slice(0, 5);
  savePrefs(["recents"]);
}
function pinProject(pid) {
  const p = S.prefs.pinned = S.prefs.pinned || [];
  const i = p.indexOf(pid);
  if (i >= 0) p.splice(i, 1); else { p.unshift(pid); if (p.length > 5) p.length = 5; }
  savePrefs(["pinned"]);
  return p.includes(pid);
}

/* ---------------- 启动 ---------------- */
(async function boot() {
  if (!token()) return showLogin();
  try {
    await enterApp();
    startDashTicker();
    const rec = parseRecordHash();
    if (rec) {
      lastHash = location.hash;
      await openDrawerRecord(rec.t, rec.rid);
    }
  } catch (e) { showLogin(); }
})();

/* ======================= 自定义组合表（v4.07） =======================
   导航入口（renderCompositeNav）→ 组合表视图（ctOpen/ctRender，独立网格：
   搜索/列筛选/排序/分页/勾选/批量/导出）→ 行详情（源实体身份+受控编辑+源记录深链）
   → 可视化构建器（ctBuilderOpen）→ 管理与授权面板（ctAdminOpen）。
   权限由服务器执行：任何 403 都如实呈现原因并锁定操作，不假装可点。 */

function ctDefById(id) { return (S.ctDefs || []).find((d) => d.ct_id === id); }

/* ---- 导航：组合表分组（按定义的 nav_group 归并）+ 构建器/管理入口 ---- */
function renderCompositeNav() {
  const defs = S.ctDefs || [];
  const usable = defs.filter((d) => d.status !== "disabled" || d.is_owner || S.me.admin);
  if (!usable.length && !S.ctCanBuild) return;
  const nav = $("table-tabs");
  const groups = {};
  for (const d of usable) {
    const g = d.nav_group || "组合表";
    (groups[g] = groups[g] || []).push(d);
  }
  if (S.ctCanBuild) (groups["组合表"] = groups["组合表"] || []).push({ _entry: "builder" });
  for (const [g, items] of Object.entries(groups)) {
    const box = document.createElement("div");
    box.className = "nav-group";
    const title = document.createElement("div");
    title.className = "nav-group-title";
    title.textContent = g;
    box.appendChild(title);
    for (const d of items) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "nav-btn";
      if (d._entry === "builder") {
        b.dataset.t = "ctbuilder";
        b.innerHTML = icon("plus") + '<span class="nav-label">组合表构建器</span>';
        b.title = "用已有字段组合新表（需管理员授权）";
        b.onclick = () => ctBuilderOpen();
      } else {
        b.dataset.t = "ct:" + d.ct_id;
        const ro = d.my_access === "r" ? " · 只读" : "";
        b.innerHTML = icon("table") + `<span class="nav-label">${esc(d.name)}</span>`;
        b.title = `${d.name}（${d.base_label}${ro}${d.status === "disabled" ? " · 已停用" : ""}）`;
        b.onclick = () => switchView("ct:" + d.ct_id);
      }
      box.appendChild(b);
    }
    nav.appendChild(box);
  }
}

async function refreshCtDefs() {
  try {
    const r = await api("/api/composite/definitions");
    S.ctDefs = r.definitions || [];
    S.ctCanBuild = !!r.can_build;
  } catch { }
  buildTabs();
}

/* ---- 组合表视图状态 ---- */
let CS = null;   // {def, fields, derived, rows, total, page, per, q, filters, sort, dir, sel:Set, access}

async function ctOpen(ct_id) {
  const box = $("view-composite");
  box.innerHTML = `<p class="hint" style="padding:20px">正在加载组合表…</p>`;
  let det;
  try {
    det = await api(`/api/composite/definitions/${ct_id}`);
  } catch (e) {
    box.innerHTML = `<div class="ct-empty"><b>无法打开组合表</b><br><span class="hint">${esc(e.message)}</span><br><br>
      <button class="btn" onclick="switchView('home')">返回首页</button></div>`;
    if (/403|开放|停用/.test(e.message)) toast(e.message, "err");
    return;
  }
  CS = { def: det, page: 1, per: 100, q: "", filters: {}, sort: "", dir: "asc",
         sel: new Set(), rows: [], total: 0, access: det.my_access };
  box.innerHTML = "";
  ctBuildSkeleton();
  updateCrumb("ct:" + ct_id);
  document.title = det.name + " · " + S.meta.system_name;
  document.querySelectorAll("#table-tabs button").forEach((b) =>
    b.classList.toggle("active", b.dataset.t === "ct:" + ct_id));
  ctLoad();
}

function ctBuildSkeleton() {
  const cfg = CS.def.config || {};
  CS.fields = cfg.fields || [];
  CS.derived = cfg.derived || [];
  const canCfg = CS.def.can_configure;
  $("view-composite").innerHTML = `
    <div class="toolbar">
      <input id="ct-q" type="search" placeholder="搜索全部列…" style="max-width:260px">
      <button id="ct-refresh" class="btn" type="button">刷新</button>
      <span class="sep"></span>
      <span class="tmenu-wrap">
        <button id="ct-export-btn" class="btn" type="button">导出 ▾</button>
        <div id="ct-export-menu" class="tmenu" hidden>
          <button id="ct-exp-sel" type="button">导出勾选记录</button>
          <button id="ct-exp-filtered" type="button">导出当前筛选全部</button>
          <button id="ct-exp-all" type="button">导出全表（主动选择）</button>
        </div>
      </span>
      ${canCfg ? `<button id="ct-edit-def" class="btn" type="button" title="调整字段/关联/显示（生成新修订，旧配置可恢复）">编辑定义</button>` : ""}
      <span id="ct-filter-chips" class="filter-chips"></span>
      <span style="flex:1"></span>
      <span class="pill perm-chip">${CS.access === "rw"
        ? `<span class="st st-saved">${icon("check")} 可编辑</span>`
        : `<span class="st st-readonly">${icon("view")} 只读</span>`}</span>
      <span id="ct-stat" class="stat"></span>
    </div>
    <div id="ct-selbar" class="ck-bar" hidden></div>
    <div class="grid-wrap" style="max-height:calc(100vh - 220px)">
      <table class="ct-grid" id="ct-grid"><thead id="ct-head"></thead><tbody id="ct-body"></tbody></table>
      <div id="ct-empty" class="empty" hidden></div>
    </div>
    <div class="pager" id="ct-pager"></div>`;
  $("ct-q").addEventListener("input", () => {
    clearTimeout(CS._qt);
    CS._qt = setTimeout(() => { CS.q = $("ct-q").value.trim(); CS.page = 1; ctLoad(); }, 300);
  });
  $("ct-refresh").onclick = () => ctLoad();
  wireTmenu("ct-export-btn", "ct-export-menu");
  $("ct-exp-sel").onclick = () => ctExport("selected");
  $("ct-exp-filtered").onclick = () => ctExport("filtered");
  $("ct-exp-all").onclick = () => ctExport("all");
  if (canCfg) $("ct-edit-def").onclick = () => ctBuilderOpen(CS.def.ct_id);
}

async function ctLoad() {
  const p = new URLSearchParams({ page: CS.page, per: CS.per });
  if (CS.q) p.set("q", CS.q);
  if (Object.keys(CS.filters).length) p.set("filters", JSON.stringify(CS.filters));
  if (CS.sort) { p.set("sort", CS.sort); p.set("dir", CS.dir); }
  let r;
  try {
    r = await api(`/api/composite/${CS.def.ct_id}/rows?${p}`);
  } catch (e) {
    $("ct-empty").hidden = false;
    $("ct-empty").innerHTML = `<b>查询失败</b><br><span class="hint">${esc(e.message)}</span>
      <br><br><button class="btn" id="ct-retry">重试</button>`;
    $("ct-retry").onclick = () => ctLoad();
    if (/403/.test(e.message) || /开放|停用|权限/.test(e.message)) toast(e.message, "err");
    return;
  }
  CS.rows = r.rows || [];
  CS.total = r.total || 0;
  CS.access = r.my_access || CS.access;
  CS.expansion = r.expansion || [];
  CS.inputRows = r.input_rows;
  ctRenderHead();
  ctRenderBody();
  ctRenderPager();
  ctRenderSelbar();
  $("ct-stat").textContent = `共 ${CS.total.toLocaleString()} 行`
    + (CS.inputRows != null && CS.expansion.length ? `（${CS.inputRows.toLocaleString()} 条${esc(CS.def.base_label)}，按关联展开）` : "");
}

function ctColKey(fid) { return fid; }

function ctRenderHead() {
  const cols = [...CS.fields, ...CS.derived];
  const head = $("ct-head");
  head.innerHTML = "<tr>" + cols.map((f) => {
    const fid = f.id;
    const active = CS.sort === fid;
    return `<th class="sortable ${active ? "sort-active" : ""}" data-fid="${esc(fid)}"
      title="${esc(f.help || f.id)}">${esc(f.label || f.alias || fid.split(":")[1] || fid)}
      <span class="fv-btn" data-fv="${esc(fid)}" title="筛选本列">▾</span>
      ${active ? (CS.dir === "asc" ? " ↑" : " ↓") : ""}</th>`;
  }).join("") + "</tr>";
  head.querySelectorAll("th.sortable").forEach((th) => {
    th.onclick = (ev) => {
      if (ev.target.dataset.fv != null) return;
      const fid = th.dataset.fid;
      if (CS.sort === fid) CS.dir = CS.dir === "asc" ? "desc" : "asc";
      else { CS.sort = fid; CS.dir = "asc"; }
      CS.page = 1;
      ctLoad();
    };
  });
  head.querySelectorAll(".fv-btn").forEach((b) => {
    b.onclick = (ev) => { ev.stopPropagation(); ctFilterPop(b.dataset.fv, b); };
  });
  ctRenderChips();
}

function ctRenderChips() {
  const box = $("ct-filter-chips");
  const items = Object.entries(CS.filters || {});
  if (!items.length) { box.hidden = true; box.innerHTML = ""; return; }
  box.hidden = false;
  box.innerHTML = items.map(([fid, f]) => {
    const col = [...CS.fields, ...CS.derived].find((x) => x.id === fid);
    const name = col ? (col.label || col.alias || fid) : fid;
    const txt = f.mode === "in" ? `∈ ${(f.values || []).join("/")}` :
      f.mode === "contains" ? `含「${f.text}」` : f.mode === "empty" ? "为空" : "非空";
    return `<span class="tag">${esc(name)} ${esc(txt)} <a data-rm="${esc(fid)}" style="cursor:pointer">✕</a></span>`;
  }).join("") + `<a style="cursor:pointer" id="ct-clr-f">清空</a>`;
  box.querySelectorAll("[data-rm]").forEach((a) => {
    a.onclick = () => { delete CS.filters[a.dataset.rm]; CS.page = 1; ctLoad(); };
  });
  const clr = box.querySelector("#ct-clr-f");
  if (clr) clr.onclick = () => { CS.filters = {}; CS.page = 1; ctLoad(); };
}

async function ctFilterPop(fid, anchor) {
  const col = [...CS.fields, ...CS.derived].find((x) => x.id === fid);
  const name = col ? (col.label || col.alias || fid) : fid;
  let vals = [];
  let emptyN = 0;
  try {
    const r = await api(`/api/composite/${CS.def.ct_id}/values?field_id=${encodeURIComponent(fid)}`);
    vals = r.values || [];
    emptyN = r.empty || 0;
  } catch (e) { toast(e.message, "err"); return; }
  const cur = CS.filters[fid] || {};
  const checked = new Set(cur.mode === "in" ? (cur.values || []) : []);
  openModal(`筛选：${esc(name)}`, `
    <p class="hint">在您有权查看的 ${vals.reduce((s, v) => s + v.count, 0) + emptyN} 行上统计（带计数）。</p>
    <input id="ctfv-q" class="input" type="search" placeholder="搜索值…" style="width:100%;margin-bottom:6px">
    <div style="max-height:40vh;overflow:auto;border:1px solid var(--border);border-radius:8px;padding:6px" id="ctfv-list">
      ${vals.map((v) => `<label class="bldr-col"><input type="checkbox" value="${esc(v.value)}" ${checked.has(v.value) ? "checked" : ""}>
        <span>${esc(v.value)} <span class="bc-doc">（${v.count}）</span></span></label>`).join("") || `<p class="hint">无值</p>`}
      <label class="bldr-col"><input type="checkbox" value="（空）" ${checked.has("（空）") ? "checked" : ""}>
        <span>（空） <span class="bc-doc">（${emptyN}）</span></span></label>
    </div>
    <div class="grid-form" style="margin-top:8px">
      <label>包含文本 <input class="input" id="ctfv-contains" value="${esc(cur.mode === "contains" ? cur.text || "" : "")}"></label>
    </div>
    <div class="form-actions">
      <label style="margin-right:auto;font-size:13px"><input type="checkbox" id="ctfv-empty" ${cur.mode === "empty" ? "checked" : ""}> 只看为空</label>
      <button class="btn" id="ctfv-clear">清除本列筛选</button>
      <button class="btn primary" id="ctfv-ok">应用</button>
    </div>`, { wide: false });
  const list = $("ctfv-list");
  $("ctfv-q").addEventListener("input", () => {
    const k = $("ctfv-q").value.trim().toLowerCase();
    list.querySelectorAll("label").forEach((lb) => {
      lb.style.display = !k || lb.textContent.toLowerCase().includes(k) ? "" : "none";
    });
  });
  $("ctfv-clear").onclick = () => { delete CS.filters[fid]; closeModal(); CS.page = 1; ctLoad(); };
  $("ctfv-ok").onclick = () => {
    const picked = [...list.querySelectorAll("input:checked")].map((c) => c.value);
    const contains = $("ctfv-contains").value.trim();
    const onlyEmpty = $("ctfv-empty").checked;
    if (onlyEmpty) CS.filters[fid] = { mode: "empty" };
    else if (picked.length) CS.filters[fid] = { mode: "in", values: picked };
    else if (contains) CS.filters[fid] = { mode: "contains", text: contains };
    else delete CS.filters[fid];
    closeModal();
    CS.page = 1;
    ctLoad();
  };
}

function ctRenderBody() {
  const body = $("ct-body");
  const cols = [...CS.fields, ...CS.derived];
  if (!CS.rows.length) {
    body.innerHTML = "";
    $("ct-empty").hidden = false;
    $("ct-empty").innerHTML = CS.total === 0
      ? `没有匹配的行。<br><span class="hint">调整搜索词或清除筛选；若为关联展开表，主记录可能均无匹配关联。</span>`
      : `本页无数据。`;
    return;
  }
  $("ct-empty").hidden = true;
  body.innerHTML = CS.rows.map((r) => {
    const tds = cols.map((f) => {
      const v = r.cells[f.id];
      let flagHtml = "";
      if (r.flags[f.id] === "unmatched") flagHtml = `<span class="ct-flags" title="关联未匹配（不留空猜测）">未匹配</span>`;
      else if (String(r.flags[f.id] || "").startsWith("multi:"))
        flagHtml = `<span class="ct-flags ct-flag-multi" title="多个候选（${esc(r.flags[f.id].slice(6))} 条），不自动选择">多候选</span>`;
      const sv = v == null || v === "" ? "—" : esc(String(v));
      return `<td class="ct-cell" data-rid="${esc(r.id)}" data-fid="${esc(f.id)}" title="${esc(f.help || "")}">${sv}${flagHtml}</td>`;
    }).join("");
    const ck = CS.sel.has(r.id);
    return `<tr data-rid="${esc(r.id)}" class="${ck ? "sel-row" : ""}">
      <td style="width:26px"><input type="checkbox" data-ck="${esc(r.id)}" ${ck ? "checked" : ""} style="accent-color:var(--primary)"></td>${tds}</tr>`;
  }).join("");
  body.querySelectorAll("input[data-ck]").forEach((cb) => {
    cb.onclick = (ev) => ev.stopPropagation();
    cb.onchange = () => {
      if (cb.checked) CS.sel.add(cb.dataset.ck); else CS.sel.delete(cb.dataset.ck);
      ctRenderSelbar();
      cb.closest("tr").classList.toggle("sel-row", cb.checked);
    };
  });
  body.querySelectorAll("td.ct-cell").forEach((td) => {
    td.onclick = () => ctRowDetail(td.dataset.rid);
  });
}

function ctRenderPager() {
  const per = CS.per, total = CS.total;
  const pages = Math.max(1, Math.ceil(total / per));
  const pg = $("ct-pager");
  if (pages <= 1) { pg.innerHTML = `<span class="hint">共 ${total.toLocaleString()} 行</span>`; return; }
  let btns = "";
  const cur = CS.page;
  const mk = (p, label, dis, on) =>
    `<button class="btn ${on ? "primary" : ""}" data-pg="${p}" ${dis ? "disabled" : ""}>${label}</button>`;
  btns += mk(cur - 1, "‹", cur <= 1);
  const win = [];
  for (let i = 1; i <= pages; i++) {
    if (i === 1 || i === pages || Math.abs(i - cur) <= 2) win.push(i);
  }
  let last = 0;
  for (const i of win) {
    if (i - last > 1) btns += `<span class="hint">…</span>`;
    btns += mk(i, String(i), false, i === cur);
    last = i;
  }
  btns += mk(cur + 1, "›", cur >= pages);
  pg.innerHTML = btns + `<span class="hint">第 ${cur} / ${pages} 页 · 共 ${total.toLocaleString()} 行 · 每页
    <select id="ct-per" style="padding:2px">${[50, 100, 200, 500].map((n) =>
      `<option value="${n}" ${n === per ? "selected" : ""}>${n}</option>`).join("")}</select></span>`;
  pg.querySelectorAll("button[data-pg]").forEach((b) => {
    b.onclick = () => { CS.page = +b.dataset.pg; ctLoad(); };
  });
  const sel = pg.querySelector("#ct-per");
  if (sel) sel.onchange = () => { CS.per = +sel.value; CS.page = 1; ctLoad(); };
}

function ctRenderSelbar() {
  const bar = $("ct-selbar");
  if (!bar) return;
  const n = CS.sel.size;
  if (!n) { bar.hidden = true; bar.innerHTML = ""; return; }
  const w = CS.access === "rw";
  bar.hidden = false;
  bar.style.display = "flex";
  bar.innerHTML = `
    <span>已勾选 ${n} 行（展开行按其实体归并后修改）</span>
    <button class="btn" id="ct-sel-page" type="button">全选本页</button>
    <button class="btn" id="ct-sel-none" type="button">清除勾选</button>
    ${w ? `<button class="btn primary" id="ct-sel-edit" type="button">批量修改字段</button>` : ""}
    <button class="btn" id="ct-sel-exp" type="button">导出勾选记录</button>
    <span class="hint">展开出来的多行不会被误当成多个独立标本：按源记录归并</span>`;
  $("ct-sel-page").onclick = () => { CS.rows.forEach((r) => CS.sel.add(r.id)); ctRenderBody(); ctRenderSelbar(); };
  $("ct-sel-none").onclick = () => { CS.sel.clear(); ctRenderBody(); ctRenderSelbar(); };
  $("ct-sel-exp").onclick = () => ctExport("selected");
  const eb = $("ct-sel-edit");
  if (eb) eb.onclick = () => ctBatchEdit();
}

/* ---- 行详情：源实体身份 + 受控编辑 + 源记录深链 ---- */
async function ctRowDetail(rowId) {
  let r;
  try {
    r = await api(`/api/composite/${CS.def.ct_id}/row/${encodeURIComponent(rowId)}`);
  } catch (e) { toast(e.message, "err"); return; }
  const row = r.row;
  const writable = r.writable || {};
  const roReason = r.readonly_reason || {};
  const cols = [...CS.fields, ...CS.derived];
  const groups = {};
  for (const f of cols) {
    const g = f.group || "字段";
    (groups[g] = groups[g] || []).push(f);
  }
  const idChips = row.identity.map((x) => `
    <span class="identity-chip">${esc(x.label)}：<b>${esc(String(x.key == null ? "（无编号）" : x.key))}</b>
      <a data-open-src="${x.tname}:${x.rid}" title="在源表中打开该记录（沿用源表权限）">打开</a></span>`).join("");
  const fieldRows = Object.entries(groups).map(([g, fs]) => `
    <tr><td colspan="2" style="padding-top:10px;font-weight:600;color:var(--primary)">${esc(g)}</td></tr>
    ${fs.map((f) => {
      const v = row.cells[f.id];
      const isDer = (CS.derived || []).some((d) => d.id === f.id);
      const sv = v == null || v === "" ? "—" : esc(String(v));
      let flag = "";
      if (row.flags[f.id] === "unmatched") flag = ` <span class="ct-flags">未匹配</span>`;
      else if (String(row.flags[f.id] || "").startsWith("multi:"))
        flag = ` <span class="ct-flags ct-flag-multi">多候选 ${esc(row.flags[f.id].slice(6))} 条，未自动选择</span>`;
      const w = writable[f.id];
      const reason = roReason[f.id] || (isDer ? "派生字段（只读，由来源字段计算）" : "");
      const rvAttr = `data-f="${esc(f.id)}" data-tname="${esc(f.table || "")}" data-col="${esc(f.column || "")}"`;
      return `<tr><td class="k" title="${esc(f.help || "")}">${esc(f.label || f.alias || f.id)}${w ? ' <span style="color:var(--primary)" title="可编辑">✏</span>' : ""}</td>
        <td class="v"><span ${rvAttr} ${w ? `class="ct-editable" style="cursor:text;border-bottom:1px dashed var(--primary)" title="点击修改"` : ""}>${sv}</span>${flag}
          ${!w && reason ? `<div class="bc-doc">${esc(reason)}</div>` : ""}</td></tr>`;
    }).join("")}`).join("");
  const sharedEvents = row.identity.filter((x) => x.tname === "events");
  openModal(`${esc(CS.def.name)} · 行详情`, `
    <p class="hint" style="margin-bottom:6px">行身份由实际源实体组成（编辑定位到源记录，走源记录的版本与审计；撤销作用于真实源记录）：</p>
    <div style="margin-bottom:8px">${idChips}</div>
    ${sharedEvents.length ? `<p class="hint">⚠ 本行包含共享采集信息（事件 ${esc(String(sharedEvents[0].key || ""))}）：修改它会影响引用同一事件的所有记录，保存前请核对。</p>` : ""}
    <div style="max-height:46vh;overflow:auto"><table class="ct-detail-grid" style="width:100%">${fieldRows}</table></div>
    <div class="form-actions"><button class="btn" id="ctd-close">关闭</button></div>`, { wide: true });
  $("ctd-close").onclick = () => closeModal();
  $("modal-body").querySelectorAll("[data-open-src]").forEach((a) => {
    a.onclick = () => {
      const [t, rid] = a.dataset.openSrc.split(":");
      if (S.meta.tables[t]) { closeModal(); location.hash = `#/r/${t}/${rid}`; }
      else toast("您对源表没有查看权限", "err");
    };
  });
  $("modal-body").querySelectorAll(".ct-editable").forEach((sp) => {
    sp.onclick = () => ctInlineEdit(sp, row);
  });
}

function ctInlineEdit(sp, row) {
  if (sp.querySelector("input")) return;
  const fid = sp.dataset.f, tname = sp.dataset.tname, col = sp.dataset.col;
  const cur = row.cells[fid];
  const f = CS.fields.find((x) => x.id === fid);
  const opts = (f && (S.meta.tables[tname] || {}).columns || []).find((c) => c.name === col)?.options;
  sp.innerHTML = opts
    ? `<select class="ct-edit-input">${`<option value="">（空）</option>` + opts.map((o) =>
        `<option ${String(cur) === String(o) ? "selected" : ""}>${esc(String(o))}</option>`).join("")}</select>`
    : `<input class="ct-edit-input" value="${esc(cur == null ? "" : String(cur))}">`;
  const inp = sp.querySelector("input, select");
  inp.focus();
  if (inp.select) inp.select();
  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    const nv = save ? inp.value : undefined;
    if (!save || String(nv) === String(cur == null ? "" : cur)) { sp.textContent = cur == null || cur === "" ? "—" : String(cur); return; }
    try {
      const rr = await api(`/api/composite/${CS.def.ct_id}/row/${encodeURIComponent(row.id)}/update`, {
        method: "POST", body: JSON.stringify({ changes: { [fid]: nv },
          base_versions: row.row_versions || {} }) });
      toast(`已保存到源记录（${rr.results.map((x) => x.key || x.rid).join("、")}；批次 ${rr.batch} 可撤销）`, "ok");
      closeModal();
      ctLoad();
    } catch (e) {
      if (e.conflict) {
        toast(`该源记录刚被 ${e.conflict.by || "他人"} 修改过，请在源表中核对后重试`, "err");
      } else toast(e.message, "err");
      sp.textContent = cur == null || cur === "" ? "—" : String(cur);
    }
  };
  inp.onkeydown = (ev) => {
    if (ev.key === "Enter") { ev.preventDefault(); finish(true); }
    if (ev.key === "Escape") { ev.preventDefault(); finish(false); }
  };
  inp.onblur = () => finish(false);
}

/* ---- 批量修改（按源记录归并；冲突 409 由服务器报告） ---- */
async function ctBatchEdit() {
  const editable = CS.fields.filter((f) => {
    const c = ((S.meta.tables[f.table] || {}).columns || []).find((x) => x.name === f.column);
    return f.editable && c && !c.linked;
  });
  if (!editable.length) return toast("本表没有可批量修改的字段（其余为关联展示列/只读）", "err");
  openModal(`批量修改（${CS.sel.size} 行）`, `
    <p class="hint">先按源记录和字段归并：同一源记录只更新一次；展开的多行不会被误当成多条标本。
    修改遵守您在源表的权限；组合表「可编辑」不会扩展源表权限。</p>
    <div class="grid-form">
      <label>字段<select class="input" id="ctb-f">${editable.map((f) =>
        `<option value="${esc(f.id)}">${esc(f.label || f.alias || f.id)}（${esc(f.table)}）</option>`).join("")}</select></label>
      <label>新值<input class="input" id="ctb-v" placeholder="留空 = 清空该字段"></label>
    </div>
    <div class="form-actions"><button class="btn" id="ctb-cancel">取消</button>
      <button class="btn primary" id="ctb-ok">应用到 ${CS.sel.size} 行</button></div>`);
  const refreshOpts = () => {
    const f = editable.find((x) => x.id === $("ctb-f").value);
    const c = ((S.meta.tables[f.table] || {}).columns || []).find((x) => x.name === f.column);
    $("ctb-v").outerHTML = c && c.options
      ? `<select class="input" id="ctb-v">${`<option value="">（空）</option>` + c.options.map((o) => `<option>${esc(String(o))}</option>`).join("")}</select>`
      : `<input class="input" id="ctb-v" placeholder="留空 = 清空该字段">`;
  };
  $("ctb-f").onchange = refreshOpts;
  $("ctb-cancel").onclick = () => closeModal();
  $("ctb-ok").onclick = async () => {
    try {
      const rr = await api(`/api/composite/${CS.def.ct_id}/batch_update`, { method: "POST",
        body: JSON.stringify({ row_ids: [...CS.sel], field_id: $("ctb-f").value, value: $("ctb-v").value }) });
      toast(`已应用：${rr.merged_records} 条源记录（批次 ${rr.batch}，可整体撤销）`, "ok");
      closeModal();
      CS.sel.clear();
      ctLoad();
    } catch (e) {
      if (e.conflict) toast(`部分源记录已被他人修改（${e.conflict.by || "并发冲突"}），本次未执行；请刷新后重试`, "err");
      else toast(e.message, "err");
    }
  };
}

/* ---- 导出：显式范围（勾选/筛选全部/全表） ---- */
async function ctExport(scope) {
  if (scope === "selected" && !CS.sel.size) return toast("先勾选要导出的行", "err");
  if (scope === "all" && !(await confirmBox(
    `将导出「${esc(CS.def.name)}」当前<b>全表</b> ${CS.total.toLocaleString()} 行（遵守您当前有效权限）。继续？`, { okText: "导出全表", danger: false }))) return;
  toast("正在生成导出文件…");
  try {
    const rr = await fetch(`/api/composite/${CS.def.ct_id}/export`, {
      method: "POST",
      headers: { "X-Auth": token(), "Content-Type": "application/json" },
      body: JSON.stringify({ scope, q: CS.q, filters: CS.filters, sort: CS.sort, dir: CS.dir,
                              row_ids: [...CS.sel] }) });
    if (!rr.ok) {
      let msg = `导出失败 (${rr.status})`;
      try { msg = (await rr.json()).error || msg; } catch { }
      throw new Error(msg);
    }
    const blob = await rr.blob();
    const cd = rr.headers.get("Content-Disposition") || "";
    const m = /filename\*=(?:UTF-8'')?([^;]+)/i.exec(cd);
    const name = m ? decodeURIComponent(m[1].trim()) : `${CS.def.name}.xlsx`;
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 8000);
    toast(`已导出「${CS.def.name}」（${scope === "all" ? "全表" : scope === "filtered" ? "当前筛选全部" : "勾选记录"}）`, "ok");
  } catch (e) { toast(e.message, "err"); }
}

/* ---- 可视化构建器：选择主数据源 → 添加字段与关联 → 配置显示和编辑 → 预览 → 保存/发布 ---- */
let BS = null;

async function ctBuilderOpen(ctId) {
  if (!S.ctCanBuild) { toast("使用构建器需要管理员授权（成员与邀请码中勾选「允许组合表构建器」）", "err"); return; }
  let av, det = null;
  try {
    av = await api("/api/composite/available");
    if (ctId) det = await api(`/api/composite/definitions/${ctId}`);
  } catch (e) { toast(e.message, "err"); return; }
  const cfg = det ? (det.config || {}) : {};
  BS = {
    ct_id: ctId || null, baseRevision: det ? det.revision : undefined,
    name: det ? det.name : "", description: det ? det.description : "",
    nav_group: det ? det.nav_group : "组合表",
    base_table: det ? det.base_table : (av.base_tables[0] || {}).t || "specimens",
    joins: (cfg.joins || []).map((j) => ({ ...j })),
    fields: (cfg.fields || []).map((f) => ({ ...f })),
    derived: (cfg.derived || []).map((d) => ({ ...d })),
    default_sort: { ...(cfg.default_sort || {}) },
    default_filters: { ...(cfg.default_filters || {}) },
    av, det, status: det ? det.status : "draft",
    srcTab: det ? det.base_table : (av.base_tables[0] || {}).t || "specimens",
    search: "", preview: null,
  };
  ctBRender();
}

function ctBRelList() {
  return (BS.av.relations || []).filter((r) => r.from_table === BS.base_table
    && !BS.joins.some((j) => j.path === r.id));
}

function ctBTargetTables() {
  return BS.joins.map((j) => (BS.av.relations.find((r) => r.id === j.path) || {}).to_table).filter(Boolean);
}

function ctBAllFields() {
  // 全部可选字段（主表 + 各关联目标），含来源标注
  const out = [];
  const bt = BS.av.fields[BS.base_table] || [];
  for (const c of bt) out.push({ t: BS.base_table, id: `${BS.base_table}:${c.name}`, c, base: true });
  for (const t of ctBTargetTables()) {
    for (const c of (BS.av.fields[t] || [])) out.push({ t, id: `${t}:${c.name}`, c });
  }
  return out;
}

function ctBRender() {
  const tabs = [BS.base_table, ...ctBTargetTables()];
  const tabName = (t) => (t === BS.base_table ? `主表：${BS.av.base_tables.find((b) => b.t === t)?.label || t}`
    : `关联表：${(BS.av.base_tables.find((b) => b.t === t) || {}).label || t}`);
  const pickedIds = new Set(BS.fields.map((f) => f.id));
  const searchHit = (c, t) => !BS.search
    || (c.name + (c.label || "") + (c.doc || "")).toLowerCase().includes(BS.search.toLowerCase());
  const candList = (BS.av.fields[BS.srcTab] || []).filter((c) => searchHit(c)).map((c) => {
    const fid = `${BS.srcTab}:${c.name}`;
    const dis = c.linked && BS.srcTab !== BS.base_table ? "" : "";
    return `<label class="bldr-col" title="${esc(c.doc || "")}${c.linked ? "（关联列：实时取自来源表）" : ""}">
      <input type="checkbox" data-pick="${esc(fid)}" ${pickedIds.has(fid) ? "checked" : ""} ${dis}>
      <span>${esc(c.label || c.name)} <span class="bc-doc">${esc(c.name)}${c.linked ? " · 关联列（来源表实时值）" : ""}${c.numeric ? " · 数字" : ""}</span></span></label>`;
  }).join("") || `<p class="hint">无匹配字段</p>`;
  const relRows = BS.joins.map((j) => {
    const r = BS.av.relations.find((x) => x.id === j.path);
    if (!r) return "";
    const canExpand = r.cardinality === "one_to_many";
    return `<div class="def-row" data-join="${esc(j.path)}">
      <b>${esc(r.name)}</b>
      <span class="bc-doc">依据：${esc(r.basis)}${r.note ? "；" + esc(r.note) : ""}</span>
      <span style="margin-left:auto">
        ${canExpand
        ? `<label><input type="radio" name="jm-${esc(j.path)}" value="expand" ${j.mode !== "keep" ? "checked" : ""}> 展开多行</label>
           <label><input type="radio" name="jm-${esc(j.path)}" value="keep" ${j.mode === "keep" ? "checked" : ""}> 保留主记录</label>`
        : `<span class="bc-doc">多对一：每行至多匹配一条（未匹配留空，多候选明确标注）</span>`}
      </span>
      <button class="btn danger" data-rmjoin="${esc(j.path)}">移除</button></div>`;
  }).join("");
  const addRelOpts = ctBRelList().map((r) =>
    `<option value="${esc(r.id)}">${esc(r.name)}（依据：${esc(r.basis.slice(0, 40))}…）</option>`).join("");
  const fieldRows = BS.fields.map((f, i) => `
    <div class="bldr-pick" data-fid="${esc(f.id)}">
      <span>${i === 0 ? "" : `<a class="mv" data-mv="-1" style="cursor:pointer">↑</a>`}${i === BS.fields.length - 1 ? "" : ` <a class="mv" data-mv="1" style="cursor:pointer">↓</a>`}</span>
      <span title="${esc(f.id)}">${esc(f.id.split(":")[1])}</span>
      <input type="text" data-falias="${esc(f.id)}" value="${esc(f.alias || "")}" placeholder="显示别名（可空）">
      <input type="number" data-fw="${esc(f.id)}" value="${f.width || ""}" placeholder="列宽" style="width:60px">
      <input type="text" data-fgroup="${esc(f.id)}" value="${esc(f.group || "")}" placeholder="分组" style="width:80px">
      <label style="white-space:nowrap"><input type="checkbox" data-fedit="${esc(f.id)}" ${f.editable ? "checked" : ""}> 可编辑</label>
      <button class="btn danger" data-ffix="1" data-rmf="${esc(f.id)}">移除</button>
    </div>`).join("");
  const derivedRows = BS.derived.map((d, i) => `
    <div class="def-row" data-did="${esc(d.id)}">
      <b>${esc(d.label || d.id)}</b><span class="bc-doc">${esc(BS.av.derived_kinds[d.kind] || d.kind)}</span>
      <span class="bc-doc">${d.kind === "concat" ? `连接 ${d.sources.join(" + ")}` :
      d.kind === "sum" ? `合计 ${d.sources.join(" + ")}` : d.kind === "count_join" ? `计数 ${d.join}` :
      `条件 ${(d.conditions || []).map((c) => `${c.field}:${c.op}${c.value ? "=" + c.value : ""}`).join(" 且 ")}`}</span>
      <span style="margin-left:auto"><label>标签/别名 <input type="text" data-dlabel="${esc(d.id)}" value="${esc(d.label || "")}" style="width:120px"></label></span>
      <button class="btn danger" data-rmd="${esc(d.id)}">移除</button></div>`).join("");
  const sortOpts = [...BS.fields, ...BS.derived].map((f) =>
    `<option value="${esc(f.id)}" ${BS.default_sort.field === f.id ? "selected" : ""}>${esc(f.label || f.alias || f.id)}</option>`).join("");
  const filterFields = [...BS.fields].map((f) =>
    `<option value="${esc(f.id)}" ${(BS.default_filters.__field || "") === f.id ? "selected" : ""}>${esc(f.label || f.alias || f.id)}</option>`).join("");
  const curFlt = Object.entries(BS.default_filters)[0];
  openModal(`${BS.ct_id ? "编辑组合表定义" : "组合表构建器"}${BS.ct_id ? `：${esc(BS.name)}` : ""}`, `
    <div style="max-height:66vh;overflow:auto;padding-right:4px">
    <div class="grid-form">
      <label>表名（必填）<input class="input" id="b-name" value="${esc(BS.name)}" placeholder="如：系统发育样本总表"></label>
      <label>导航分组<input class="input" id="b-nav" value="${esc(BS.nav_group)}" placeholder="组合表"></label>
      <label class="span2">用途说明<input class="input" id="b-desc" value="${esc(BS.description || "")}" placeholder="这张表给谁用、看什么（会显示在导航悬停提示）"></label>
      <label>主数据源（行粒度：一行 = 一条此表记录）<select class="input" id="b-base" ${BS.ct_id ? "disabled" : ""}>
        ${BS.av.base_tables.map((b) => `<option value="${b.t}" ${BS.base_table === b.t ? "selected" : ""}>${esc(b.label)}（${b.total.toLocaleString()} 行）</option>`).join("")}
      </select></label>
    </div>
    <h4 style="margin:12px 0 4px">关联（只用已确认的关系；编号相似/物种相同不构成关系）</h4>
    <div id="b-joins">${relRows || `<p class="hint">尚未添加关联（单表组合也可以）。</p>`}</div>
    ${addRelOpts ? `<div class="form-actions"><select class="input" id="b-addrel">${addRelOpts}</select>
      <button class="btn" id="b-addrel-ok">添加关联</button></div>` : `<p class="hint">当前主数据源没有更多可用关联。</p>`}
    <h4 style="margin:12px 0 4px">字段（来源：主表与各关联表；只列出您有权使用的表）</h4>
    <div class="mem-tabs">
      ${tabs.map((t) => `<button class="mem-tab ${BS.srcTab === t ? "active" : ""}" data-srctab="${esc(t)}">${esc(tabName(t))}</button>`).join("")}
      <span style="flex:1"></span>
      <input type="search" class="input" id="b-search" placeholder="搜索：中文名/原字段名/含义" style="max-width:220px" value="${esc(BS.search)}">
    </div>
    <div class="bldr-cols">${candList}</div>
    <div class="bldr-hdr" style="margin-top:8px"><span></span><b>已选字段（↑↓ 调序）</b><b>显示别名</b><b>列宽</b><b>分组</b><b>可编辑</b><span></span></div>
    <div class="bldr-picked">${fieldRows || `<p class="hint">尚未选择字段。</p>`}</div>
    ${BS.fields.some((f) => { const c = ((BS.av.fields[f.table]) || []).find((x) => x.name === f.id.split(":")[1]); return c && c.linked; })
    ? `<p class="hint">关联列（如标本表的省份/采集人）是来源表实时值：展示无碍，但不可在此编辑，请到来源表修改。</p>` : ""}
    <h4 style="margin:12px 0 4px">派生字段（白名单函数；默认只读，未知不补 0）</h4>
    <div id="b-derived">${derivedRows || `<p class="hint">无派生字段。</p>`}</div>
    <div class="form-actions">
      <select class="input" id="b-adddk"><option value="">添加派生字段…</option>
        ${Object.entries(BS.av.derived_kinds).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("")}</select>
    </div>
    <div id="b-adddcfg"></div>
    <h4 style="margin:12px 0 4px">默认排序与筛选（进入表格时生效）</h4>
    <div class="grid-form">
      <label>默认排序<select class="input" id="b-sort"><option value="">（不排序）</option>${sortOpts}</select></label>
      <label>方向<select class="input" id="b-sortdir"><option value="asc" ${BS.default_sort.dir !== "desc" ? "selected" : ""}>升序</option><option value="desc" ${BS.default_sort.dir === "desc" ? "selected" : ""}>降序</option></select></label>
      <label>默认筛选字段<select class="input" id="b-fltf"><option value="">（无）</option>${filterFields}</select></label>
      <label>条件<select class="input" id="b-fltm">
        <option value="contains" ${curFlt && curFlt[1].mode === "contains" ? "selected" : ""}>包含文本</option>
        <option value="empty" ${curFlt && curFlt[1].mode === "empty" ? "selected" : ""}>为空</option>
        <option value="notempty" ${curFlt && curFlt[1].mode === "notempty" ? "selected" : ""}>不为空</option></select></label>
      <label class="span2">文本<input class="input" id="b-fltv" value="${curFlt && curFlt[1].text ? esc(curFlt[1].text) : ""}" placeholder="包含文本模式的匹配词"></label>
    </div>
    <h4 style="margin:12px 0 4px">预览检查</h4>
    <div class="form-actions"><button class="btn" id="b-preview">预览样例与结构检查</button>
      <span class="hint">预览通过后再保存/发布；检查包含字段存在性、类型、关联路径与权限</span></div>
    <div id="b-pv"></div>
    </div>
    <div class="form-actions">
      <button class="btn" id="b-cancel">取消</button>
      <button class="btn" id="b-save">保存为草稿</button>
      <button class="btn primary" id="b-save-pub">保存并${BS.status === "published" ? "保持发布" : "发布"}</button>
      ${BS.status === "published" ? `<span class="hint">修改配置后会回到草稿，需再次发布才对使用者生效。</span>` : ""}
    </div>`, { wide: true });
  ctBWire();
}

function ctBWire() {
  $("b-cancel").onclick = () => closeModal();
  $("b-name").oninput = (e) => { BS.name = e.target.value; };
  $("b-desc").oninput = (e) => { BS.description = e.target.value; };
  $("b-nav").oninput = (e) => { BS.nav_group = e.target.value; };
  if (!BS.ct_id) $("b-base").onchange = (e) => {
    BS.base_table = e.target.value; BS.srcTab = e.target.value;
    BS.joins = []; BS.fields = []; BS.derived = []; BS.default_sort = {}; BS.default_filters = {};
    ctBRender();
  };
  document.querySelectorAll("[data-srctab]").forEach((b) => {
    b.onclick = () => { BS.srcTab = b.dataset.srctab; ctBRender(); };
  });
  $("b-search").oninput = (e) => {
    BS.search = e.target.value;
    // 只重渲染候选区，不丢已聚焦状态
    const holder = document.querySelector(".bldr-cols");
    const pickedIds = new Set(BS.fields.map((f) => f.id));
    const list = (BS.av.fields[BS.srcTab] || []).filter((c) => !BS.search
      || (c.name + (c.label || "") + (c.doc || "")).toLowerCase().includes(BS.search.toLowerCase()));
    holder.innerHTML = list.map((c) => {
      const fid = `${BS.srcTab}:${c.name}`;
      return `<label class="bldr-col" title="${esc(c.doc || "")}${c.linked ? "（关联列：实时取自来源表）" : ""}">
        <input type="checkbox" data-pick="${esc(fid)}" ${pickedIds.has(fid) ? "checked" : ""}>
        <span>${esc(c.label || c.name)} <span class="bc-doc">${esc(c.name)}${c.linked ? " · 关联列（来源表实时值）" : ""}${c.numeric ? " · 数字" : ""}</span></span></label>`;
    }).join("") || `<p class="hint">无匹配字段</p>`;
    ctBWirePicks(holder);
  };
  ctBWirePicks($("modal-body"));
  document.querySelectorAll("[data-rmjoin]").forEach((b) => {
    b.onclick = () => {
      const p = b.dataset.rmjoin;
      BS.joins = BS.joins.filter((j) => j.path !== p);
      const tt = (BS.av.relations.find((r) => r.id === p) || {}).to_table;
      BS.fields = BS.fields.filter((f) => f.table !== tt);
      BS.derived = BS.derived.filter((d) => d.join !== p && (d.sources || []).every((s) => BS.fields.some((f) => f.id === s)));
      ctBRender();
    };
  });
  const addr = $("b-addrel-ok");
  if (addr) addr.onclick = () => {
    const p = $("b-addrel").value;
    if (!p || BS.joins.some((j) => j.path === p)) return;
    const r = BS.av.relations.find((x) => x.id === p);
    BS.joins.push({ path: p, mode: r.cardinality === "one_to_many" ? "expand" : "keep" });
    ctBRender();
  };
  document.querySelectorAll("[data-join]").forEach((row) => {
    const p = row.dataset.join;
    row.querySelectorAll(`input[name="jm-${p}"]`).forEach((rd) => {
      rd.onchange = () => { const j = BS.joins.find((x) => x.path === p); if (j) j.mode = rd.value; };
    });
  });
  document.querySelectorAll("[data-rmf]").forEach((b) => {
    b.onclick = () => {
      BS.fields = BS.fields.filter((f) => f.id !== b.dataset.rmf);
      ctBRender();
    };
  });
  document.querySelectorAll(".bldr-pick .mv").forEach((a) => {
    a.onclick = () => {
      const fid = a.closest("[data-fid]").dataset.fid;
      const i = BS.fields.findIndex((f) => f.id === fid);
      const j = i + (+a.dataset.mv);
      if (i < 0 || j < 0 || j >= BS.fields.length) return;
      [BS.fields[i], BS.fields[j]] = [BS.fields[j], BS.fields[i]];
      ctBRender();
    };
  });
  document.querySelectorAll("[data-falias]").forEach((inp) => inp.oninput = () => {
    const f = BS.fields.find((x) => x.id === inp.dataset.falias); if (f) f.alias = inp.value;
  });
  document.querySelectorAll("[data-fw]").forEach((inp) => inp.oninput = () => {
    const f = BS.fields.find((x) => x.id === inp.dataset.fw); if (f) f.width = +inp.value || null;
  });
  document.querySelectorAll("[data-fgroup]").forEach((inp) => inp.oninput = () => {
    const f = BS.fields.find((x) => x.id === inp.dataset.fgroup); if (f) f.group = inp.value;
  });
  document.querySelectorAll("[data-fedit]").forEach((cb) => cb.onchange = () => {
    const f = BS.fields.find((x) => x.id === cb.dataset.fedit); if (f) f.editable = cb.checked;
  });
  document.querySelectorAll("[data-rmd]").forEach((b) => {
    b.onclick = () => { BS.derived = BS.derived.filter((d) => d.id !== b.dataset.rmd); ctBRender(); };
  });
  document.querySelectorAll("[data-dlabel]").forEach((inp) => inp.oninput = () => {
    const d = BS.derived.find((x) => x.id === inp.dataset.dlabel); if (d) d.label = inp.value;
  });
  $("b-adddk").onchange = () => ctBDerivedCfg($("b-adddk").value);
  $("b-sort").onchange = (e) => { BS.default_sort = { field: e.target.value, dir: $("b-sortdir").value }; };
  $("b-sortdir").onchange = (e) => { BS.default_sort.dir = e.target.value; };
  const syncFlt = () => {
    const fid = $("b-fltf").value, m = $("b-fltm").value, v = $("b-fltv").value.trim();
    BS.default_filters = fid ? (m === "contains" ? { [fid]: { mode: "contains", text: v } }
      : { [fid]: { mode: m } }) : {};
  };
  ["b-fltf", "b-fltm", "b-fltv"].forEach((id) => { $(id).onchange = syncFlt; $(id).oninput = syncFlt; });
  $("b-preview").onclick = () => ctBPreview();
  $("b-save").onclick = () => ctBSave(false);
  $("b-save-pub").onclick = () => ctBSave(true);
}

function ctBWirePicks(scope) {
  scope.querySelectorAll("[data-pick]").forEach((cb) => {
    cb.onchange = () => {
      const fid = cb.dataset.pick;
      if (cb.checked) {
        const [t, col] = fid.split(":");
        const meta = (BS.av.fields[t] || []).find((c) => c.name === col);
        BS.fields.push({ id: fid, table: t, column: col, alias: "", editable: !!(meta && !meta.linked) });
      } else BS.fields = BS.fields.filter((f) => f.id !== fid);
      ctBRender();
    };
  });
}

function ctBDerivedCfg(kind) {
  const box = $("b-adddcfg");
  if (!kind) { box.innerHTML = ""; return; }
  const fields = [...BS.fields];
  const fieldOpts = (multi) => fields.map((f) =>
    `<option value="${esc(f.id)}">${esc(f.label || f.alias || f.id)}（${esc(f.table)}）</option>`).join("");
  const joinOpts = BS.joins.map((j) => {
    const r = BS.av.relations.find((x) => x.id === j.path);
    return `<option value="${esc(j.path)}">${esc(r ? r.name : j.path)}</option>`;
  }).join("");
  let html = "";
  if (kind === "concat" || kind === "sum") {
    html = `<div class="grid-form">
      <label>${kind === "concat" ? "连接" : "合计"}来源字段（按住 Ctrl 多选）
        <select class="input" id="bd-srcs" multiple size="4">${fieldOpts()}</select></label>
      ${kind === "concat" ? `<label>分隔符<input class="input" id="bd-sep" value=" "></label>` : `<label class="hint" style="align-self:end">仅数值列参与；全部缺失=未记录，未知不补 0</label>`}
      <label>列名<input class="input" id="bd-label" placeholder="如：完整产地 / 合计数量"></label>
      <button class="btn primary" id="bd-ok">添加</button></div>`;
  } else if (kind === "count_join") {
    html = `<div class="grid-form">
      <label>统计哪条关联的记录数<select class="input" id="bd-join">${joinOpts}</select></label>
      <label>列名<input class="input" id="bd-label" placeholder="如：文库数"></label>
      <button class="btn primary" id="bd-ok">添加</button></div>`;
  } else {
    html = `<div class="grid-form">
      <label>条件字段<select class="input" id="bd-cf">${fieldOpts()}</select></label>
      <label>条件<select class="input" id="bd-cop"><option value="notempty">不为空</option><option value="empty">为空</option><option value="equals">等于</option></select></label>
      <label>等于值<input class="input" id="bd-cv" placeholder="仅「等于」时填写"></label>
      <label>满足时显示<input class="input" id="bd-label" placeholder="如：待复核"></label>
      <button class="btn primary" id="bd-ok">添加</button></div>`;
  }
  box.innerHTML = html;
  $("bd-ok").onclick = () => {
    const label = $("bd-label").value.trim() || kind;
    const did = `derived_${BS.derived.length + 1}_${Math.random().toString(36).slice(2, 5)}`;
    let d = null;
    if (kind === "concat") {
      const srcs = [...$("bd-srcs").selectedOptions].map((o) => o.value);
      if (!srcs.length) return toast("请选择来源字段", "err");
      d = { id: did, kind, sources: srcs, sep: $("bd-sep").value, missing: "dash", label };
    } else if (kind === "sum") {
      const srcs = [...$("bd-srcs").selectedOptions].map((o) => o.value);
      if (!srcs.length) return toast("请选择来源字段", "err");
      d = { id: did, kind, sources: srcs, label };
    } else if (kind === "count_join") {
      d = { id: did, kind, join: $("bd-join").value, label };
    } else {
      d = { id: did, kind, conditions: [{ field: $("bd-cf").value, op: $("bd-cop").value, value: $("bd-cv").value }], label };
    }
    BS.derived.push(d);
    $("b-adddk").value = "";
    ctBRender();
  };
}

async function ctBPreview() {
  if (!BS.fields.length) return toast("先选择字段", "err");
  $("b-pv").innerHTML = `<p class="hint">正在预览…</p>`;
  try {
    const r = await api("/api/composite/preview", { method: "POST", body: JSON.stringify(ctBPayload()) });
    BS.preview = r;
    const meta = r.meta || {};
    const checks = (r.checks || []).map((c) => `<p class="pv-check pv-ok">✓ ${c.kind === "join"
      ? `关联 ${esc(c.path)}：${esc(c.basis)}（${c.mode === "expand" ? "展开多行" : "保留主记录"}）`
      : c.kind === "linked_field" ? `${esc(c.id)}：${esc(c.basis)}`
      : `${esc(c.id)}：${esc(c.reason || "只读")}`}</p>`).join("");
    const warns = (r.warnings || []).map((w) => `<p class="pv-check pv-warn">⚠ ${esc(w)}</p>`).join("");
    const expl = meta.expanded_any
      ? `<p class="pv-check pv-warn">⚠ 存在一对多展开：${(meta.expansion || []).join("、")}——${meta.input_rows} 条主记录会输出多于 ${meta.input_rows} 行；全量共 ${meta.output_rows} 行。请确认这正是想要的行粒度。</p>`
      : `<p class="pv-check pv-ok">✓ 无组合膨胀：${meta.input_rows} 条主记录 → ${meta.output_rows} 行。</p>`;
    const flagLine = Object.keys(meta.flags || {}).length
      ? `<p class="pv-check pv-warn">⚠ 样例中未匹配 ${meta.flags.unmatched || 0} 处、多候选 ${meta.flags.multi || 0} 处（如实标注，不静默取第一条）。</p>`
      : `<p class="pv-check pv-ok">✓ 样例无未匹配/多候选。</p>`;
    const cols = [...(BS.fields || []), ...(BS.derived || [])];
    const pvRows = (r.rows || []).map((row) => `<tr>${cols.map((f) => {
      let flag = "";
      if (row.flags[f.id] === "unmatched") flag = `<span class="ct-flags">未匹配</span>`;
      else if (String(row.flags[f.id] || "").startsWith("multi:")) flag = `<span class="ct-flags ct-flag-multi">多候选</span>`;
      const v = row.cells[f.id];
      return `<td>${v == null || v === "" ? "—" : esc(String(v))}${flag}</td>`;
    }).join("")}</tr>`).join("");
    $("b-pv").innerHTML = `
      ${expl}${flagLine}${checks}${warns}
      <div style="overflow:auto;border:1px solid var(--border);border-radius:8px;max-height:220px">
      <table class="ct-grid" style="font-size:12px"><thead><tr>${cols.map((f) =>
        `<th>${esc(f.label || f.alias || f.id.split(":")[1] || f.id)}</th>`).join("")}</tr></thead>
      <tbody>${pvRows || `<tr><td colspan="${cols.length}" class="ct-empty">无数据行</td></tr>`}</tbody></table></div>
      <p class="hint">预览为前 10 行；检查基于全量数据。</p>`;
  } catch (e) {
    $("b-pv").innerHTML = `<p class="pv-check pv-warn">✗ ${esc(e.message)}</p>`;
  }
}

function ctBPayload() {
  return { name: BS.name.trim(), description: BS.description.trim(), nav_group: BS.nav_group.trim(),
           base_table: BS.base_table,
           config: { joins: BS.joins, fields: BS.fields, derived: BS.derived,
                     default_sort: BS.default_sort, default_filters: BS.default_filters, options: {} } };
}

async function ctBSave(publish) {
  if (!BS.name.trim()) return toast("请填写表名", "err");
  if (!BS.fields.length) return toast("至少选择一个字段", "err");
  const body = ctBPayload();
  try {
    let r;
    if (BS.ct_id) {
      body.base_revision = BS.baseRevision;
      r = await api(`/api/composite/definitions/${BS.ct_id}`, { method: "PUT", body: JSON.stringify(body) });
    } else {
      r = await api("/api/composite/definitions", { method: "POST", body: JSON.stringify(body) });
      BS.ct_id = r.ct_id;
    }
    if (publish) await api(`/api/composite/definitions/${BS.ct_id}/publish`, { method: "POST" });
    const warns = (r.warnings || []).length ? `（注意：${r.warnings.join("；")}）` : "";
    toast(`${publish ? "已保存并发布" : "已保存为草稿"}${warns}`, "ok");
    closeModal();
    await refreshCtDefs();
    switchView("ct:" + BS.ct_id);
  } catch (e) {
    if (e.conflict || /409|他人修改/.test(e.message)) toast(e.message, "err");
    else toast(e.message, "err");
  }
}

/* ---- 管理与授权面板（管理员=全部定义；构建者=自己的定义；授权仅管理员） ---- */
async function ctAdminOpen() {
  if (!S.ctCanBuild) { toast("需要组合表构建器权限", "err"); return; }
  let r, ur;
  try {
    r = await api("/api/composite/definitions");
    ur = S.me.admin ? await api("/api/users") : null;
  } catch (e) { toast(e.message, "err"); return; }
  const defs = r.definitions || [];
  const rows = defs.map((d) => {
    const stTag = d.status === "published" ? `<span class="tag admin">已发布</span>`
      : d.status === "draft" ? `<span class="tag">草稿</span>` : `<span class="tag off">已停用</span>`;
    const grants = d.grants ? Object.entries(d.grants) : [];
    const grantTxt = grants.length
      ? grants.map(([u, p]) => `${esc(u)}(${p === "rw" ? "可编辑" : "只读"})`).join("、")
      : `<span class="bc-doc">未向其他用户开放</span>`;
    const canCfg = d.is_owner || S.me.admin;
    return `<div class="def-row">
      <span class="dr-name">${esc(d.name)}</span> ${stTag}
      <span class="bc-doc">${esc(d.base_label)} · ${d.field_count} 字段 · 修订 r${d.revision} · 所有者 ${esc(d.owner)}</span>
      <span class="bc-doc">开放：${grantTxt}</span>
      <span style="margin-left:auto;display:flex;gap:6px;flex-wrap:wrap">
        <button class="btn" data-open="${d.ct_id}">打开</button>
        ${canCfg ? `<button class="btn" data-edit="${d.ct_id}">编辑</button>` : ""}
        ${canCfg ? `<button class="btn" data-copy="${d.ct_id}">复制</button>` : ""}
        ${canCfg && d.status === "draft" ? `<button class="btn primary" data-pub="${d.ct_id}">发布</button>` : ""}
        ${canCfg && d.status === "published" ? `<button class="btn" data-dis="${d.ct_id}">停用</button>` : ""}
        ${canCfg && d.status === "disabled" ? `<button class="btn" data-en="${d.ct_id}">启用</button>` : ""}
        ${S.me.admin ? `<button class="btn" data-perm="${d.ct_id}">访问授权</button>` : ""}
        ${canCfg ? `<button class="btn" data-rev="${d.ct_id}">修订历史</button>` : ""}
        ${canCfg ? `<button class="btn danger" data-del="${d.ct_id}">删除</button>` : ""}
      </span></div>`;
  }).join("") || `<p class="hint">还没有组合表定义。用「组合表构建器」创建第一个。</p>`;
  openModal(`组合表管理${S.me.admin ? "（管理员：全部定义与授权）" : "（我的定义）"}`, `
    <p class="hint">权限分两层：构建器使用权（成员与邀请码勾选）决定谁能建表；这里的「访问授权」决定每张表对谁开放（只读/可编辑）。
    删除定义不影响源数据；停用后从普通导航隐藏。</p>
    <div style="max-height:56vh;overflow:auto">${rows}</div>
    <div class="form-actions"><button class="btn" id="ctadm-close">关闭</button>
      <button class="btn primary" id="ctadm-new">新建组合表</button></div>`, { wide: true });
  $("ctadm-close").onclick = () => closeModal();
  $("ctadm-new").onclick = () => ctBuilderOpen();
  const wire = (attr, fn, confirmMsg) => {
    $("modal-body").querySelectorAll(`[data-${attr}]`).forEach((b) => {
      b.onclick = async () => {
        const id = b.dataset[attr];
        if (confirmMsg && !(await confirmBox(confirmMsg))) return;
        try {
          await fn(id);
          toast("已完成", "ok");
          await refreshCtDefs();
          ctAdminOpen();
        } catch (e) { toast(e.message, "err"); }
      };
    });
  };
  wire("open", (id) => { closeModal(); switchView("ct:" + id); });
  wire("edit", (id) => { closeModal(); ctBuilderOpen(id); });
  wire("copy", (id) => api(`/api/composite/definitions/${id}/copy`, { method: "POST" }));
  wire("pub", (id) => api(`/api/composite/definitions/${id}/publish`, { method: "POST" }));
  wire("dis", (id) => api(`/api/composite/definitions/${id}/disable`, { method: "POST" }),
    "停用后该表从普通用户导航隐藏（管理员仍可查看/启用），源数据不受影响。继续？");
  wire("en", (id) => api(`/api/composite/definitions/${id}/enable`, { method: "POST" }));
  wire("del", (id) => api(`/api/composite/definitions/${id}`, { method: "DELETE" }),
    "删除组合表定义（仅删除配置，不触碰任何源数据；撤销不了，需要可先停用）。继续？");
  $("modal-body").querySelectorAll("[data-perm]").forEach((b) => {
    b.onclick = () => ctPermPanel(b.dataset.perm, (ur || {}).users || []);
  });
  $("modal-body").querySelectorAll("[data-rev]").forEach((b) => {
    b.onclick = () => ctRevisionPanel(b.dataset.rev);
  });
}

async function ctPermPanel(ctId, users) {
  let det;
  try { det = await api(`/api/composite/definitions/${ctId}`); } catch (e) { toast(e.message, "err"); return; }
  const grants = det.grants || {};
  openModal(`访问授权：${esc(det.name)}`, `
    <p class="hint">逐用户设置该组合表的访问档位：不可访问（导航不出现）/ 只读 / 可编辑数据。
    用户实际数据权限 = 此授权 × 其源表权限 × 记录规则——「可编辑」不会扩展源表权限。</p>
    <div style="max-height:44vh;overflow:auto">
      ${users.map((u) => `
        <div class="def-row">
          <span class="dr-name">${esc(u.username)}</span>
          ${u.is_admin ? `<span class="tag admin">管理员（默认全部权限）</span>` : ""}
          ${u.disabled ? `<span class="tag off">已停用</span>` : ""}
          <span style="margin-left:auto">
            <select data-gu="${esc(u.username)}" ${u.is_admin ? "disabled" : ""}>
              <option value="none" ${!grants[u.username] ? "selected" : ""}>✕ 不可访问</option>
              <option value="r" ${grants[u.username] === "r" ? "selected" : ""}>👁 只读</option>
              <option value="rw" ${grants[u.username] === "rw" ? "selected" : ""}>✏ 可编辑数据</option>
            </select></span></div>`).join("")}
    </div>
    <div class="form-actions"><button class="btn" id="ctp-close">取消</button>
      <button class="btn primary" id="ctp-save">保存授权</button></div>`, { wide: true });
  $("ctp-close").onclick = () => closeModal();
  $("ctp-save").onclick = async () => {
    const perms = {};
    $("modal-body").querySelectorAll("select[data-gu]").forEach((s) => {
      perms[s.dataset.gu] = s.value === "none" ? null : s.value;
    });
    try {
      await api(`/api/composite/definitions/${ctId}/permissions`, { method: "PUT",
        body: JSON.stringify({ perms }) });
      toast("授权已更新（前后值已入审计；被移除用户的下一次查询即被拒绝）", "ok");
      closeModal();
      ctAdminOpen();
    } catch (e) { toast(e.message, "err"); }
  };
}

async function ctRevisionPanel(ctId) {
  let r;
  try { r = await api(`/api/composite/definitions/${ctId}/revisions`); } catch (e) { toast(e.message, "err"); return; }
  openModal(`修订历史（当前 r${r.current_revision}）`, `
    <p class="hint">每次配置修改前自动留存快照。恢复旧配置只恢复表定义（存为新修订），不恢复源数据库。</p>
    <div style="max-height:44vh;overflow:auto">
      ${(r.revisions || []).map((v) => `
        <div class="def-row"><b>r${v.revision}</b>
          <span class="bc-doc">${esc(v.changed_by || "")} · ${esc(v.created_at)} · ${esc(v.note || "")}</span>
          <span style="margin-left:auto"><button class="btn" data-restore="${v.revision}">恢复此配置</button></span></div>`).join("")
    || `<p class="hint">还没有修订（首次修改后会自动留存）。</p>`}
    </div>
    <div class="form-actions"><button class="btn" id="ctr-close">关闭</button></div>`);
  $("ctr-close").onclick = () => closeModal();
  $("modal-body").querySelectorAll("[data-restore]").forEach((b) => {
    b.onclick = async () => {
      try {
        const rr = await api(`/api/composite/definitions/${ctId}/restore-revision`, { method: "POST",
          body: JSON.stringify({ revision: +b.dataset.restore }) });
        toast(`已恢复到修订 r${b.dataset.restore}（现为草稿，新修订 r${rr.revision}；需再次发布生效）`, "ok");
        closeModal();
        await refreshCtDefs();
        ctRevisionPanel(ctId);
      } catch (e) { toast(e.message, "err"); }
    };
  });
}
