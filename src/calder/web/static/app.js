"use strict";
(() => {
  const token = window.CALDER_TOKEN;
  const state = { model: null, timeline: null, view: "timeline", session: "" };
  const $ = (sel) => document.querySelector(sel);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  async function api(path, body) {
    const opts = { headers: { "X-Calder-Token": token } };
    if (body !== undefined) {
      opts.method = "POST";
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(path, opts);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || res.statusText);
    return data;
  }

  function toast(text) {
    const t = $("#toast");
    t.textContent = text;
    t.classList.add("show");
    setTimeout(() => t.classList.remove("show"), 2200);
  }

  async function edit(target, action, value) {
    try {
      const res = await api("/api/edit", { target, action, value });
      state.model = res.model;
      toast(`${action}: ${target}`);
      render();
    } catch (err) {
      toast(`Rejected: ${err.message}`);
    }
  }

  const msgName = (id) => (state.model.messages.find((m) => m.id === id) || { name: id }).name;
  const conf = (ev) => {
    const c = ev ? ev.confidence : 0;
    return `<span class="chip ${c < 0.8 ? "uncertain" : ""}" title="${esc(ev?.method)}: ${esc(ev?.notes)}">${(c * 100).toFixed(0)}%${c < 0.8 ? " uncertain" : ""}</span>`;
  };
  const encText = (e) => {
    if (!e) return "-";
    const bits = [e.kind];
    if (e.width) bits.push(`w${e.width}`);
    if (["uint", "int", "float", "bcd"].includes(e.kind) && e.width > 1) bits.push(e.endian);
    if (e.scale_exp) bits.push(`×10^${e.scale_exp}`);
    if (e.decimals !== null && e.decimals !== undefined) bits.push(`${e.decimals} dp`);
    return bits.join(" ");
  };

  function evidenceBlock(ev) {
    if (!ev) return "";
    const alts = (ev.alternatives || []).map((a) => `<li>${esc(a.description)} <span class="muted">(${esc(a.score)}; ${esc(a.reason)})</span></li>`).join("");
    const events = (ev.events || []).slice(0, 8).map((r) => `${esc(r.session)}#${r.index}${r.ui ? " (UI)" : ""}`).join(", ");
    return `<details><summary>evidence</summary><div class="mono">${esc(ev.method)}, support ${ev.support}, contradictions ${ev.contradictions}</div>
      <div>${esc(ev.notes)}</div><div class="muted">events: ${events || "-"}</div>${alts ? `<div>alternatives:<ul>${alts}</ul></div>` : ""}</details>`;
  }

  function viewTimeline() {
    if (!state.timeline) {
      api("/api/timeline").then((rows) => { state.timeline = rows; render(); });
      return "<p class='muted'>Loading timeline…</p>";
    }
    const sessions = [...new Set(state.timeline.map((r) => r.session))];
    if (!state.session && sessions.length) state.session = sessions[0];
    const rows = state.timeline.filter((r) => r.session === state.session && (r.source === "ui" || r.kind === "data" || r.kind === "config" || r.kind === "open"));
    const t0 = rows.length ? rows[0].t : 0;
    const opts = sessions.map((s) => `<option ${s === state.session ? "selected" : ""}>${esc(s)}</option>`).join("");
    const body = rows.slice(0, 3000).map((r) => {
      const time = `<span class="muted mono">${((r.t - t0) / 1e9).toFixed(3)}s</span>`;
      if (r.source === "ui") {
        const el = r.element;
        return `<div class="row"><div class="cell"></div><div class="cell ui">${time} ${esc(r.kind)} <b>${esc(el.label || el.name || el.automation_id)}</b> ${r.value !== null ? "= " + esc(r.value) : ""}</div></div>`;
      }
      const payload = r.text !== null ? `<code>${esc(r.text.replace(/\r/g, "\\r").replace(/\n/g, "\\n"))}</code>` : `<code>${esc(r.hex)}</code>`;
      const label = r.message ? ` <span class="chip">${esc(msgName(r.message))}</span>` : "";
      const meta = r.kind !== "data" ? ` <span class="muted">${esc(r.kind)} ${esc(JSON.stringify(r.meta))}</span>` : "";
      return `<div class="row"><div class="cell ${r.direction}">${time} ${r.direction === "tx" ? "→" : r.direction === "rx" ? "←" : "·"} ${payload}${label}${meta}</div><div class="cell"></div></div>`;
    }).join("");
    return `<div class="toolbar">Session <select id="session">${opts}</select><span class="muted">${rows.length} events · wire left, UI right</span></div>
      <div class="card"><div class="timeline"><div class="cell muted"><b>Wire</b> (→ host to instrument, ← instrument to host)</div><div class="cell muted"><b>UI</b></div>${body}</div></div>`;
  }

  function classSelect(m) {
    const opts = ["read_only", "configuring", "actuating"].map((c) => `<option ${c === m.classification ? "selected" : ""}>${c}</option>`).join("");
    return `<select data-classify="${esc(m.id)}">${opts}</select>`;
  }

  function viewMessages() {
    return state.model.messages.map((m) => {
      const pre = (m.preconditions || []).map((p) => `<li>${esc(msgName(p.message))}${p.disabled_by.length ? ` (disabled by ${p.disabled_by.map(msgName).map(esc).join(", ")})` : ""}
        <span class="chip ${p.status}">${esc(p.status)}</span> ${conf(p.evidence)}
        <button class="btn good" data-edit="precondition:${esc(m.id)}/${esc(p.message)}" data-action="accept">accept</button>
        <button class="btn bad" data-edit="precondition:${esc(m.id)}/${esc(p.message)}" data-action="reject">reject</button></li>`).join("");
      const examples = (m.examples || []).map((e) => `<tr><td class="mono">${esc(e.text ?? e.raw)}</td><td class="mono">${esc(JSON.stringify(e.values))}</td></tr>`).join("");
      const ui = (m.ui_bindings || []).map((b) => `<span class="chip ui">${esc(b.kind)}: ${esc(b.element_name || b.element)}${Object.keys(b.constants).length ? " " + esc(JSON.stringify(b.constants)) : ""}</span>`).join(" ");
      return `<div class="card"><h3><span class="${m.direction}">${m.direction === "tx" ? "→" : "←"}</span> ${esc(m.name)}
        <span class="chip">${esc(m.kind)}</span>
        ${m.direction === "tx" ? `<span class="chip ${m.classification}">${esc(m.classification)}${m.classification_confirmed ? " ✓" : ""}</span>` : ""}
        <span class="chip ${m.status}">${esc(m.status)}</span> ${conf(m.evidence)}
        <span class="muted mono">${esc(m.key)} · ${m.count} frames</span></h3>
        <div class="toolbar">
          <button class="btn" data-rename="message:${esc(m.id)}" data-current="${esc(m.name)}">rename</button>
          <button class="btn good" data-edit="message:${esc(m.id)}" data-action="accept">accept</button>
          <button class="btn bad" data-edit="message:${esc(m.id)}" data-action="reject">reject</button>
          ${m.direction === "tx" ? `classification ${classSelect(m)} <span class="muted">${esc(m.classification_reason)}</span>` : ""}
          ${m.suggested_name ? `<button class="btn" data-edit="message:${esc(m.id)}" data-action="adopt_suggestion">use suggested name “${esc(m.suggested_name)}”</button>` : ""}
        </div>
        ${m.response ? `<div>Response: <b>${esc(msgName(m.response))}</b> (timeout ${m.response_timeout_ms} ms)</div>` : ""}
        ${pre ? `<div>Preconditions (hypotheses):<ul>${pre}</ul></div>` : ""}
        ${ui ? `<div>UI: ${ui}</div>` : ""}
        ${m.manual ? `<div>Manual: <code>${esc(m.manual.mnemonic)}</code> ${esc(m.manual.description)} (p. ${esc(m.manual.page)})</div>` : ""}
        <table><tr><th>Example</th><th>Decoded</th></tr>${examples}</table>
        ${evidenceBlock(m.evidence)}</div>`;
    }).join("");
  }

  function viewFields() {
    const rows = [];
    for (const m of state.model.messages) {
      for (const f of m.fields) {
        rows.push(`<tr><td>${esc(m.name)}</td><td><b>${esc(f.name)}</b><br><span class="muted">${esc(f.label || "")}</span></td>
          <td><span class="chip">${esc(f.role)}</span></td>
          <td class="mono">${f.offset !== null ? `@${f.offset}+${f.length}` : f.token !== null ? `arg ${f.token}` : "-"}</td>
          <td class="mono">${esc(encText(f.encoding))}${f.constant ? ` = ${esc(f.constant)}` : ""}</td>
          <td>${esc(f.units || "")}${f.range ? `<br><span class="muted">${f.range.minimum} … ${f.range.maximum}</span>` : ""}
            ${f.enum.length ? `<br>${f.enum.map((e) => `<span class="chip">${esc(e.wire)}=${esc(e.label)}</span>`).join(" ")}` : ""}
            ${f.echo_of ? `<br><span class="muted">echoes ${esc(f.echo_of)}</span>` : ""}${f.reflects ? `<br><span class="muted">reflects ${esc(msgName(f.reflects[0]))}.${esc(f.reflects[1])}</span>` : ""}</td>
          <td>${conf(f.evidence)} <span class="chip ${f.status}">${esc(f.status)}</span>${evidenceBlock(f.evidence)}</td>
          <td><button class="btn" data-rename="field:${esc(m.id)}/${esc(f.id)}" data-current="${esc(f.name)}">rename</button>
            ${f.encoding ? `<button class="btn" data-retype="field:${esc(m.id)}/${esc(f.id)}">retype</button>` : ""}
            <button class="btn good" data-edit="field:${esc(m.id)}/${esc(f.id)}" data-action="accept">accept</button>
            <button class="btn bad" data-edit="field:${esc(m.id)}/${esc(f.id)}" data-action="reject">reject</button></td></tr>`);
      }
    }
    return `<div class="card"><table><tr><th>Message</th><th>Field</th><th>Role</th><th>Where</th><th>Encoding</th><th>Meaning</th><th>Evidence</th><th></th></tr>${rows.join("")}</table></div>`;
  }

  function viewFraming() {
    const m = state.model;
    const f = m.framing;
    const c = m.checksum;
    const t = m.transport;
    const kv = (k, v) => `<tr><th>${esc(k)}</th><td class="mono">${esc(v)}</td></tr>`;
    return `<div class="card"><h3>Transport ${conf(t.evidence)}</h3><table>${kv("kind", t.kind)}${kv("resource", t.resource)}
        ${t.serial ? kv("line settings", `${t.serial.baudrate ?? "?"} baud ${t.serial.bytesize}${t.serial.parity}${t.serial.stopbits} rtscts=${t.serial.rtscts} xonxoff=${t.serial.xonxoff}`) : ""}</table>${evidenceBlock(t.evidence)}</div>
      <div class="card"><h3>Framing: ${esc(f.kind)} (${esc(f.encoding)}) ${conf(f.evidence)}</h3><table>
        ${f.terminator_tx ? kv("terminator host→instrument", f.terminator_tx) : ""}${f.terminator_rx ? kv("terminator instrument→host", f.terminator_rx) : ""}
        ${f.start ? kv("start sequence", f.start) : ""}
        ${f.length_field ? kv("length field", `offset ${f.length_field.offset}, ${f.length_field.width} byte(s) ${f.length_field.endian}, frame = value ${f.length_field.adjust >= 0 ? "+" : ""}${f.length_field.adjust}`) : ""}
        ${f.frame_length ? kv("frame length", f.frame_length) : ""}${f.gap_ms ? kv("silent interval", `${f.gap_ms} ms`) : ""}
        ${kv("protocol family", m.family)}</table>${evidenceBlock(f.evidence)}</div>
      <div class="card"><h3>Checksum ${c ? conf(c.evidence) : ""}</h3>${c ? `<table>${kv("algorithm", c.algorithm)}${kv("width", `${c.width} bits`)}
        ${c.poly !== null ? kv("parameters", `poly 0x${c.poly.toString(16)} init 0x${(c.init ?? 0).toString(16)} refin ${c.refin} refout ${c.refout} xorout 0x${(c.xorout ?? 0).toString(16)}`) : ""}
        ${kv("coverage", `bytes [${c.start} : end${c.end || ""}]${c.covered === "hex" ? " (hex-decoded)" : ""}`)}${kv("stored", `at end${c.position}, ${c.endian}, ${c.encoding}`)}</table>${evidenceBlock(c.evidence)}` : "<p class='muted'>No checksum validates every frame.</p>"}</div>`;
  }

  function viewStateMachine() {
    const sm = state.model.state_machine;
    if (!sm) return "<p class='muted'>No state machine inferred.</p>";
    const n = sm.states.length;
    const size = Math.max(360, Math.min(900, 120 + n * 70));
    const cx = size / 2, cy = size / 2, r = size / 2 - 60;
    const pos = {};
    sm.states.forEach((s, i) => { const a = (2 * Math.PI * i) / n - Math.PI / 2; pos[s.id] = [cx + r * Math.cos(a), cy + r * Math.sin(a)]; });
    const edges = new Map();
    for (const t of sm.transitions) {
      const key = `${t.source}-${t.target}`;
      edges.set(key, [...(edges.get(key) || []), `${msgName(t.message)} (${t.count})`]);
    }
    let svg = `<svg viewBox="0 0 ${size} ${size}" width="100%" style="max-width:${size}px" role="img" aria-label="state machine">
      <defs><marker id="arrow" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="currentColor"/></marker></defs>`;
    for (const [key, labels] of edges) {
      const [s, t] = key.split("-").map(Number);
      const [x1, y1] = pos[s], [x2, y2] = pos[t];
      const label = esc(labels.slice(0, 3).join(", ") + (labels.length > 3 ? ` +${labels.length - 3}` : ""));
      if (s === t) {
        svg += `<path class="edge" d="M${x1 - 10},${y1 - 18} C${x1 - 40},${y1 - 70} ${x1 + 40},${y1 - 70} ${x1 + 10},${y1 - 18}" marker-end="url(#arrow)"/><text class="edge-label" x="${x1}" y="${y1 - 62}" text-anchor="middle">${label}</text>`;
        continue;
      }
      const dx = x2 - x1, dy = y2 - y1, len = Math.hypot(dx, dy) || 1;
      const ux = dx / len, uy = dy / len;
      const mx = (x1 + x2) / 2 - uy * 24, my = (y1 + y2) / 2 + ux * 24;
      svg += `<path class="edge" d="M${x1 + ux * 20},${y1 + uy * 20} Q${mx},${my} ${x2 - ux * 22},${y2 - uy * 22}" marker-end="url(#arrow)"/><text class="edge-label" x="${mx}" y="${my}" text-anchor="middle">${label}</text>`;
    }
    for (const s of sm.states) {
      const [x, y] = pos[s.id];
      svg += `<circle class="state" cx="${x}" cy="${y}" r="20" ${s.id === sm.initial ? 'stroke-width="3"' : ""}/>${s.label === "accept" ? `<circle class="state" cx="${x}" cy="${y}" r="15"/>` : ""}<text x="${x}" y="${y + 4}" text-anchor="middle">${s.id}</text>`;
    }
    svg += "</svg>";
    return `<div class="card"><h3>Command-sequence automaton (k-tails, k=${sm.k}) ${conf(sm.evidence)}</h3><p class="muted">State ${sm.initial} is initial (bold); double circles accept. Edges list message types and observation counts.</p>${svg}${evidenceBlock(sm.evidence)}</div>`;
  }

  function viewAudit() {
    const rows = (state.model.audit || []).slice().reverse().map((a) => `<tr><td class="mono">${esc(a.time)}</td><td>${esc(a.actor)}</td><td>${esc(a.action)}</td><td class="mono">${esc(a.target)}</td><td class="mono">${esc(JSON.stringify(a.before))} → ${esc(JSON.stringify(a.after))}</td></tr>`).join("");
    return `<div class="card"><h3>Audit trail</h3><p class="muted">Every edit is validated and saved to the model file with this record.</p>
      <table><tr><th>Time</th><th>Actor</th><th>Action</th><th>Target</th><th>Change</th></tr>${rows || "<tr><td colspan=5 class='muted'>No edits yet.</td></tr>"}</table></div>`;
  }

  const views = { timeline: viewTimeline, messages: viewMessages, fields: viewFields, framing: viewFraming, statemachine: viewStateMachine, audit: viewAudit };

  function render() {
    if (!state.model) return;
    $("#model-name").textContent = `· ${state.model.name} · ${state.model.family} · ${state.model.messages.length} message types`;
    $("#warnings").innerHTML = (state.model.warnings || []).slice(0, 6).map((w) => `<div class="warning">${esc(w)}</div>`).join("");
    $("#view").innerHTML = views[state.view]();
  }

  document.addEventListener("click", (ev) => {
    const el = ev.target.closest("button");
    if (!el) return;
    if (el.dataset.view) {
      document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b === el));
      state.view = el.dataset.view;
      render();
    } else if (el.dataset.edit) {
      edit(el.dataset.edit, el.dataset.action, null);
    } else if (el.dataset.rename) {
      const name = prompt("New snake_case name", el.dataset.current);
      if (name) edit(el.dataset.rename, "rename", name);
    } else if (el.dataset.retype) {
      const [mid, fid] = el.dataset.retype.slice(6).split("/");
      const f = state.model.messages.find((m) => m.id === mid).fields.find((x) => x.id === fid);
      const text = prompt("Encoding (JSON)", JSON.stringify(f.encoding));
      if (text) { try { edit(el.dataset.retype, "retype", JSON.parse(text)); } catch (e) { toast("Invalid JSON"); } }
    } else if (el.id === "theme") {
      const root = document.documentElement;
      const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
      root.dataset.theme = dark ? "light" : "dark";
      try { localStorage.setItem("calder-theme", root.dataset.theme); } catch (e) { /* storage unavailable */ }
    }
  });
  document.addEventListener("change", (ev) => {
    const el = ev.target;
    if (el.id === "session") { state.session = el.value; render(); }
    if (el.dataset && el.dataset.classify) edit(`message:${el.dataset.classify}`, "classify", el.value);
  });

  try { const saved = localStorage.getItem("calder-theme"); if (saved) document.documentElement.dataset.theme = saved; } catch (e) { /* storage unavailable */ }
  api("/api/model").then((m) => { state.model = m; render(); }).catch((err) => { $("#view").textContent = `Failed to load the model: ${err.message}`; });
})();
