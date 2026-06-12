/* =========================================================
   DB Navigator · agent console — клиент
   Читает SSE-поток шагов агента и рисует живую ленту графа.
   ========================================================= */

const chat    = document.getElementById("chat");
const form    = document.getElementById("form");
const input   = document.getElementById("input");
const sendBtn = document.getElementById("send");
const statusEl = document.getElementById("status");
const emptyEl = document.getElementById("empty");

let sessionId = null;
let busy = false;

/* ----------------------------- утилиты ----------------------------- */

function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function scrollDown() {
  requestAnimationFrame(() => window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" }));
}

const TYPE_RU = {
  navigation: "навигация", schema: "схема", script: "скрипт",
  data: "данные", unsafe: "блокировка", unknown: "вне домена",
};

/* ----------------------------- health ----------------------------- */

async function loadHealth() {
  try {
    const r = await fetch("/api/health");
    const h = await r.json();
    const chips = [];

    chips.push(`<span class="chip ${h.provider ? "ok" : "off"}">${esc(h.provider || "llm")}</span>`);

    const dbOk = (h.servers || []).length > 0 && h.servers.every(s => s.ok);
    const dbName = (h.servers || [])[0]?.database || "db";
    chips.push(`<span class="chip ${dbOk ? "ok" : "bad"} hide-sm">db: ${esc(dbName)}</span>`);

    chips.push(`<span class="chip ${h.rag_ready ? "ok" : "off"} hide-sm">rag</span>`);
    chips.push(`<span class="chip ${h.langfuse ? "live" : "off"} hide-sm">langfuse</span>`);

    statusEl.innerHTML = chips.join("");
  } catch {
    statusEl.innerHTML = `<span class="chip bad">сервер недоступен</span>`;
  }
}

/* --------------------------- рендер шагов --------------------------- */

function detailHTML(ev) {
  const d = ev.detail || {};
  const node = ev.node;
  const pill = (txt, cls = "") => `<span class="pill ${cls}">${esc(txt)}</span>`;

  if (node === "classify_intent") {
    const conf = d.confidence != null ? Math.round(d.confidence * 100) + "%" : "—";
    let html = `тип: ${pill(TYPE_RU[d.query_type] || d.query_type || "—")} · уверенность ${esc(conf)}`;
    if (d.reasoning) html += `<br><span class="k">${esc(d.reasoning)}</span>`;
    return html;
  }
  if (node === "search_metadata") {
    const tables = (d.tables || []).filter(Boolean).map(esc).join(", ");
    return `найдено таблиц: <b>${d.found ?? 0}</b>${tables ? ` · ${tables}` : ""}`;
  }
  if (node === "get_schema") {
    return `таблица ${pill(d.table || "—")} · колонок: <b>${d.columns ?? 0}</b> · ${esc(d.status || "")}`;
  }
  if (node === "generate_sql") {
    const safe = d.is_safe ? pill("safe ✓", "") : pill("unsafe", "err");
    let html = safe;
    if (d.sql) html += `<code>${esc(d.sql)}</code>`;
    return html;
  }
  if (node === "fix_sql") {
    let html = `попытка ${pill("#" + (d.attempt ?? "?"), "warn")}`;
    if (d.sql) html += `<code>${esc(d.sql)}</code>`;
    return html;
  }
  if (node === "execute_query") {
    const map = { success: "", empty: "warn", error: "err" };
    let html = `статус ${pill(d.status || "—", map[d.status] || "")}`;
    if (d.row_count != null) html += ` · строк: <b>${d.row_count}</b>`;
    if (d.truncated) html += ` · ${pill("обрезано", "warn")}`;
    if (d.error) html += `<br><span class="k">${esc(d.error)}</span>`;
    return html;
  }
  return "";
}

function renderNode(railEl, ev) {
  // снять активность с предыдущих узлов
  railEl.querySelectorAll(".node.active").forEach(n => n.classList.remove("active"));

  const li = document.createElement("li");
  li.className = `node kind-${ev.kind || "router"} status-${ev.status || "ok"} active`;

  const toolBadge = ev.tool
    ? `<span class="tool-badge">TOOL</span><span class="tool-name">${esc(ev.tool)}</span>`
    : "";

  const detail = detailHTML(ev);

  li.innerHTML =
    `<div class="node-head">
       <span class="node-label">${esc(ev.label)}</span>
       ${toolBadge}
     </div>` +
    (detail ? `<div class="node-detail">${detail}</div>` : "");

  railEl.appendChild(li);
  scrollDown();
}

/* --------------------------- финальный ответ --------------------------- */

function renderFinal(runEl, ev) {
  runEl.querySelectorAll(".node.active").forEach(n => n.classList.remove("active"));

  const qt = ev.query_type || "unknown";
  const conf = ev.confidence != null ? Math.round(ev.confidence * 100) + "%" : "—";

  const meta = [
    `<span class="tag type ${esc(qt)}">${esc(TYPE_RU[qt] || qt)}</span>`,
    `<span class="tag meta">уверенность <b>${esc(conf)}</b></span>`,
    `<span class="tag meta">${(ev.elapsed_ms / 1000).toFixed(1)}s</span>`,
    `<span class="tag meta">tools: <b>${ev.tool_calls ?? 0}</b></span>`,
  ].join("");

  let blocks = "";

  if (ev.sql) {
    blocks += `<div class="block">
      <div class="block-title">SQL</div>
      <pre class="sql">${esc(ev.sql)}</pre>
    </div>`;
  }

  const srcs = (ev.sources || []).filter(s => s && (s.table || s.database));
  if (srcs.length) {
    // дедупликация по server/database/table
    const seen = new Set();
    const rows = [];
    for (const s of srcs) {
      const key = `${s.server}/${s.database}/${s.table || ""}`;
      if (seen.has(key)) continue;
      seen.add(key);
      rows.push(
        `<div class="source"><span class="srv">${esc(s.server)}/${esc(s.database)}</span>` +
        (s.table ? `.<span class="tbl">${esc(s.table)}</span>` : "") + `</div>`
      );
    }
    blocks += `<div class="block">
      <div class="block-title">Источники</div>
      <div class="sources">${rows.join("")}</div>
    </div>`;
  }

  if (ev.steps && ev.steps.length) {
    const trace = ev.steps.map(esc).join(' <span class="arrow">→</span> ');
    blocks += `<details class="steps">
      <summary>трейс графа (${ev.steps.length})</summary>
      <div class="steps-trace">${trace}</div>
    </details>`;
  }

  const answer = document.createElement("div");
  answer.className = "answer";
  answer.innerHTML =
    `<div class="meta-row">${meta}</div>
     <div class="answer-body">${esc(ev.answer)}</div>
     ${blocks}`;

  runEl.appendChild(answer);
  scrollDown();
}

function renderRunError(runEl, message) {
  runEl.querySelectorAll(".node.active").forEach(n => n.classList.remove("active"));
  const div = document.createElement("div");
  div.className = "run-error";
  div.textContent = message || "Что-то пошло не так. Попробуйте переформулировать запрос.";
  runEl.appendChild(div);
  scrollDown();
}

/* ----------------------------- отправка ----------------------------- */

function addUserTurn(text) {
  const turn = document.createElement("div");
  turn.className = "turn user";
  turn.innerHTML = `<div class="bubble">${esc(text)}</div>`;
  chat.appendChild(turn);
  return turn;
}

function addAgentRun() {
  const turn = document.createElement("div");
  turn.className = "turn agent";
  const run = document.createElement("div");
  run.className = "run";
  const rail = document.createElement("ol");
  rail.className = "rail";
  run.appendChild(rail);
  turn.appendChild(run);
  chat.appendChild(turn);
  return { run, rail };
}

async function sendQuery(text) {
  if (busy || !text.trim()) return;
  busy = true;
  sendBtn.disabled = true;
  if (emptyEl) emptyEl.remove();

  addUserTurn(text);
  const { run, rail } = addAgentRun();
  scrollDown();

  try {
    const resp = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query: text, session_id: sessionId }),
    });

    if (!resp.ok || !resp.body) {
      renderRunError(run, `Сервер ответил ${resp.status}`);
      return;
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });

      let sep;
      while ((sep = buf.indexOf("\n\n")) >= 0) {
        const frame = buf.slice(0, sep);
        buf = buf.slice(sep + 2);
        const dataLine = frame.split("\n").find(l => l.startsWith("data:"));
        if (!dataLine) continue;
        const payload = dataLine.slice(5).trim();
        if (!payload) continue;

        let ev;
        try { ev = JSON.parse(payload); } catch { continue; }
        handleEvent(ev, run, rail);
      }
    }
  } catch (err) {
    renderRunError(run, "Не удалось связаться с агентом: " + err.message);
  } finally {
    busy = false;
    sendBtn.disabled = false;
    input.focus();
  }
}

function handleEvent(ev, run, rail) {
  switch (ev.type) {
    case "run_start":
      if (ev.session_id) sessionId = ev.session_id;
      break;
    case "step":
      renderNode(rail, ev);
      break;
    case "final":
      renderFinal(run, ev);
      break;
    case "error":
      renderRunError(run, ev.message);
      break;
    case "done":
      run.querySelectorAll(".node.active").forEach(n => n.classList.remove("active"));
      break;
  }
}

/* ----------------------------- события UI ----------------------------- */

form.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  autoresize();
  sendQuery(text);
});

input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    form.requestSubmit();
  }
});

function autoresize() {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 160) + "px";
}
input.addEventListener("input", autoresize);

document.getElementById("examples")?.addEventListener("click", (e) => {
  const btn = e.target.closest(".ex");
  if (!btn) return;
  sendQuery(btn.dataset.q);
});

/* ----------------------------- старт ----------------------------- */

loadHealth();
input.focus();
