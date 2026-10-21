/* C-LAB Pilot Evaluation Tracker — front end.
 * Everything rendered here is driven by catalog/framework_catalog.json.
 */
(async function () {
  const { createDataLayer, STAGES } = window.CLAB;
  const $main = document.getElementById("main");
  const $nav = document.getElementById("nav");
  const $who = document.getElementById("who");
  const $toast = document.getElementById("toast");

  let catalog, data, user;
  const stageWarned = new Set();   // "pilotId|stage" — warned once per session about editing a completed stage

  /* ---------------- utilities ---------------- */
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const titleCase = s => String(s).replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase());
  const fmtDate = iso => iso ? new Date(iso).toLocaleString("en-GB", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" }) : "";
  const money = n => (n == null || isNaN(n)) ? "—" : "USD " + Number(n).toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: 2 });
  const stageLabel = k => catalog.stages.find(s => s.key === k)?.label || k;
  const stageIdx = k => STAGES.indexOf(k);
  const nextStage = k => STAGES[stageIdx(k) + 1] || null;

  function toast(msg) { $toast.textContent = msg; $toast.classList.add("show"); clearTimeout(toast.t); toast.t = setTimeout(() => $toast.classList.remove("show"), 2200); }

  function modal({ title, body, confirm = "OK", cancel = null, danger = false }) {
    return new Promise(resolve => {
      const bg = document.createElement("div"); bg.className = "modal-bg";
      bg.innerHTML = `<div class="modal"><h2>${esc(title)}</h2><div>${body}</div>
        <div class="actions">${cancel ? `<button class="btn" data-x="0">${esc(cancel)}</button>` : ""}
        <button class="btn ${danger ? "amber" : "primary"}" data-x="1">${esc(confirm)}</button></div></div>`;
      bg.addEventListener("click", e => { const b = e.target.closest("button[data-x]"); if (!b) return; bg.remove(); resolve(b.dataset.x === "1"); });
      document.body.appendChild(bg);
    });
  }

  /* ---------------- catalog helpers ---------------- */
  const dimFields = dim => Array.isArray(dim.fields) ? dim.fields : catalog.field_sets[dim.fields];
  function fieldsForStage(dim, stage) {
    const keys = dim.stages[stage]; if (!keys) return [];
    const all = dimFields(dim);
    return keys.includes("*") ? all : all.filter(f => keys.includes(f.key));
  }
  const applies = (dim, pilot) => dim.applies_to.includes("all") || dim.applies_to.includes(pilot.pilot_type);
  const dimsForStage = (stage, pilot) => catalog.dimensions.filter(d => d.stages[stage] && applies(d, pilot));
  const typeLabel = k => catalog.pilot_types.find(t => t.key === k)?.label || k;
  const respKey = (dim, field, hyp) => `${dim}|${field}|${hyp || ""}`;

  /* ---------------- shell ---------------- */
  function renderShell() {
    const u = data.currentUser();
    $who.innerHTML = u && !u.unauthorised
      ? `<span class="who">${esc(u.full_name || u.email)}</span> ${data.mode === "local" ? '<span class="mode">LOCAL MODE</span>' : ""} <a href="#" id="signout" class="btn small ghost">Sign out</a>`
      : (data.mode === "local" ? '<span class="mode">LOCAL MODE</span>' : "");
    $nav.innerHTML = u && !u.unauthorised ? `<a href="#/pilots" data-nav="pilots">Pilots</a><a href="#/pilots/new" data-nav="new">New pilot</a><a href="#/export" data-nav="export">Export</a>` : "";
    const so = document.getElementById("signout");
    if (so) so.onclick = async e => { e.preventDefault(); await data.signOut(); user = null; location.hash = "#/login"; render(); };
  }
  function setNav(key) { $nav.querySelectorAll("a").forEach(a => a.classList.toggle("active", a.dataset.nav === key)); }

  /* ---------------- views: login ---------------- */
  function viewLogin() {
    setNav("");
    const u = data.currentUser();
    if (u && u.unauthorised) {
      $main.innerHTML = `<div class="login card"><h1>Not authorised</h1><p>${esc(u.email)} is signed in but is not on the team whitelist. Ask the administrator to add you, then sign in again.</p>
        <button class="btn" id="so2">Sign out</button></div>`;
      document.getElementById("so2").onclick = async () => { await data.signOut(); render(); };
      return;
    }
    $main.innerHTML = `<div class="login card"><h1>C-LAB Pilot Tracker</h1><p class="sub">Sign in with your team email. You will receive a one-time link.</p>
      <form id="login"><div class="field"><label>Email</label><input type="email" id="email" required placeholder="name@iadb.org" autofocus></div>
      <div style="margin-top:12px"><button class="btn primary" type="submit">Send sign-in link</button></div></form><div id="msg" style="margin-top:14px"></div></div>`;
    document.getElementById("login").onsubmit = async e => {
      e.preventDefault();
      const email = document.getElementById("email").value.trim();
      const msg = document.getElementById("msg");
      msg.innerHTML = `<div class="banner">Sending…</div>`;
      const r = await data.signIn(email);
      msg.innerHTML = r.ok ? `<div class="banner ok">Check your inbox at <strong>${esc(email)}</strong> and open the link.</div>`
        : `<div class="banner err">${esc(r.message)}${/database error|not authorised/i.test(r.message) ? " — this email is not on the whitelist." : ""}</div>`;
    };
  }

  /* ---------------- views: pilot list ---------------- */
  async function viewPilots() {
    setNav("pilots");
    $main.innerHTML = `<div class="toolbar"><div><h1>Pilots</h1><p class="sub" style="margin:0">One row per pilot. Open one to continue data collection.</p></div>
      <div class="spacer"></div><button class="btn" id="import">Import workbook…</button><a class="btn primary" href="#/pilots/new">+ New pilot</a></div><div id="list" class="empty">Loading…</div>`;
    document.getElementById("import").onclick = importDialog;
    const pilots = await data.listPilots();
    if (!pilots.length) { document.getElementById("list").innerHTML = `<div class="empty">No pilots yet. <a href="#/pilots/new">Register the first one.</a></div>`; return; }
    const progress = await Promise.all(pilots.map(p => data.currentResponses(p.id).then(rows => levelProgress(p, rows))));
    document.getElementById("list").className = "";
    document.getElementById("list").innerHTML = `<table><thead><tr><th>Pilot</th><th>TC</th><th>Location</th><th>Type</th><th>Mode</th><th>Stage</th><th>Progress by level</th><th>Updated</th></tr></thead><tbody>
      ${pilots.map((p, i) => `<tr class="clickable" data-id="${p.id}"><td><strong>${esc(p.name)}</strong></td><td class="code">${esc(p.tc_code)}</td><td>${esc(p.city)}, ${esc(p.country)}</td>
        <td>${esc(typeLabel(p.pilot_type))}</td><td>${esc(titleCase(p.mode))}</td><td>${esc(stageLabel(p.current_stage))}</td>
        <td><span class="levelbar">${progress[i].map(l => `<span class="${l.done === l.total ? "full" : l.done ? "part" : ""}" title="${l.done} of ${l.total} dimensions started">L${l.level} ${l.done}/${l.total}</span>`).join("")}</span></td>
        <td style="white-space:nowrap">${esc(fmtDate(p.updated_at))}</td></tr>`).join("")}</tbody></table>`;
    $main.querySelectorAll("tr.clickable").forEach(tr => tr.onclick = () => location.hash = `#/pilots/${tr.dataset.id}`);
  }
  function levelProgress(pilot, rows) {
    const started = new Set(rows.map(r => r.dimension_code));
    return catalog.levels.map(l => {
      const dims = catalog.dimensions.filter(d => d.level === l.key && applies(d, pilot));
      return { level: l.key, total: dims.length, done: dims.filter(d => started.has(d.code)).length };
    });
  }

  /* ---------------- views: new / edit pilot ---------------- */
  function pilotFieldHtml(f, value) {
    const lbl = `<label class="${f.required ? "req" : ""}">${esc(f.label)}</label>`;
    const help = f.help ? `<div class="help">${esc(f.help)}</div>` : "";
    let input;
    if (f.type === "enum") {
      const opts = catalog[f.options_from];
      input = `<select name="${f.key}" ${f.required ? "required" : ""}><option value="">— select —</option>${opts.map(o => `<option value="${o.key}" ${value === o.key ? "selected" : ""}>${esc(o.label)}</option>`).join("")}</select>`;
    } else if (f.type === "longtext") input = `<textarea name="${f.key}" ${f.required ? "required" : ""}>${esc(value)}</textarea>`;
    else if (f.type === "date") input = `<input type="date" name="${f.key}" value="${esc(value)}">`;
    else input = `<input type="text" name="${f.key}" value="${esc(value)}" ${f.required ? "required" : ""} ${f.pattern ? `pattern="${f.pattern}"` : ""}>`;
    return `<div class="field ${f.type === "longtext" ? "wide" : ""}">${lbl}${input}${help}</div>`;
  }
  async function viewPilotForm(id) {
    setNav(id ? "pilots" : "new");
    const pilot = id ? await data.getPilot(id) : {};
    $main.innerHTML = `<h1>${id ? "Edit pilot details" : "New pilot"}</h1><p class="sub">${id ? esc(pilot.name) : "The cover sheet: who, where and what kind of pilot. Evidence is entered stage by stage afterwards."}</p>
      <form id="pf" class="card"><div class="form-grid">${catalog.pilot_fields.map(f => pilotFieldHtml(f, pilot[f.key] ?? "")).join("")}</div>
      <div id="err"></div><div class="toolbar" style="margin-top:18px"><button class="btn primary" type="submit">${id ? "Save changes" : "Create pilot"}</button>
      <a class="btn" href="${id ? `#/pilots/${id}` : "#/pilots"}">Cancel</a></div></form>`;
    const form = document.getElementById("pf");
    const typeSel = form.querySelector('[name=pilot_type]'), attrSel = form.querySelector('[name=attribution_category]');
    typeSel.onchange = () => { const t = catalog.pilot_types.find(t => t.key === typeSel.value); if (t) attrSel.value = t.attribution; };
    form.onsubmit = async e => {
      e.preventDefault();
      const fields = {};
      for (const f of catalog.pilot_fields) { const v = form.querySelector(`[name=${f.key}]`).value.trim(); fields[f.key] = v === "" ? null : v; }
      try {
        if (id) { await data.updatePilot(id, fields); toast("Pilot updated"); location.hash = `#/pilots/${id}`; }
        else { const p = await data.createPilot(fields); await data.addHypothesis(p.id); toast("Pilot created"); location.hash = `#/pilots/${p.id}`; }
      } catch (err) { document.getElementById("err").innerHTML = `<div class="banner err">${esc(err.message)}</div>`; }
    };
  }

  /* ---------------- views: pilot page (stage form) ---------------- */
  async function viewPilot(id, stage) {
    setNav("pilots");
    const pilot = await data.getPilot(id);
    if (!pilot) { $main.innerHTML = `<div class="empty">Pilot not found.</div>`; return; }
    stage = STAGES.includes(stage) ? stage : pilot.current_stage;
    const [hyps, rows, completions] = await Promise.all([data.listHypotheses(id), data.currentResponses(id), data.listCompletions(id)]);
    const current = new Map(rows.map(r => [respKey(r.dimension_code, r.field_key, r.hypothesis_id), r]));
    const completedAt = s => completions.find(c => c.stage === s);
    const ctx = { pilot, stage, hyps, current, completions };

    const tabs = STAGES.map(s => {
      const disabled = pilot.mode === "prospective" && stageIdx(s) > stageIdx(pilot.current_stage);
      return `<a href="#/pilots/${id}/${s}" class="${s === stage ? "active" : ""} ${disabled ? "disabled" : ""}">${stageIdx(s) + 1}. ${stageLabel(s)}${completedAt(s) ? '<span class="tick">✓</span>' : ""}</a>`;
    }).join("");
    const stageDef = catalog.stages.find(s => s.key === stage);
    const done = completedAt(stage);

    $main.innerHTML = `
      <div class="toolbar"><div><h1>${esc(pilot.name)}</h1><p class="sub" style="margin:0"><span class="code">${esc(pilot.tc_code)}</span> · ${esc(pilot.city)}, ${esc(pilot.country)} ·
        <span class="badge">${esc(typeLabel(pilot.pilot_type))}</span> <span class="badge">${esc(titleCase(pilot.attribution_category))}-based</span> <span class="badge">${esc(titleCase(pilot.mode))}</span></p></div>
        <div class="spacer"></div><a class="btn" href="#/pilots/${id}/summary">Summary sheet</a><a class="btn" href="#/pilots/${id}/edit">Edit details</a></div>
      <details class="card" style="padding:10px 20px"><summary style="cursor:pointer;color:var(--blue)">Problem, target population, lead</summary>
        <dl class="kv" style="margin-top:10px"><dt>Problem definition</dt><dd>${esc(pilot.problem_definition || "—")}</dd><dt>Target population</dt><dd>${esc(pilot.target_population || "—")}</dd>
        <dt>Lead / responsible</dt><dd>${esc(pilot.lead || "—")}</dd><dt>Counterpart</dt><dd>${esc(pilot.counterpart || "—")}</dd><dt>Dates</dt><dd>${esc(pilot.date_started || "?")} → ${esc(pilot.date_ended || "ongoing")}</dd>${pilot.import_source ? `<dt>Imported from</dt><dd>${esc(pilot.import_source)}</dd>` : ""}</dl></details>
      <div class="stages">${tabs}</div>
      <div class="stage-intro"><strong>${esc(stageDef.label)}.</strong> ${esc(stageDef.instructions)}</div>
      ${done ? `<div class="banner warn">This stage was completed on ${esc(fmtDate(done.completed_at))} by ${esc(done.completer?.full_name || done.completer?.email || "")}. Fields stay editable; you will be asked to confirm the first change.</div>` : ""}
      <div id="dims"></div>
      <div class="toolbar" style="margin-top:24px"><div class="help">Every field saves automatically when you leave it. Mark a field as <em>No evidence</em> or <em>Not applicable</em> rather than leaving it blank.</div>
        <div class="spacer"></div><button class="btn amber" id="complete">${done ? "Re-complete stage" : "Complete stage"}: ${esc(stageDef.label)}</button></div>`;

    const $dims = document.getElementById("dims");
    $dims.innerHTML = dimsForStage(stage, pilot).map(d => dimCard(d, ctx)).join("");
    wireDims($dims, ctx);
    document.getElementById("complete").onclick = () => completeStage(ctx);
  }

  function dimCard(dim, ctx) {
    const fields = fieldsForStage(dim, ctx.stage);
    const head = `<div class="card-head"><span class="code">${esc(dim.code)}</span><h2>${esc(dim.name)}</h2>
      <span class="badge ${dim.origin === "OECD" ? "oecd" : "clab"}">${esc(dim.origin)}</span>${dim.core ? '<span class="badge core">Core</span>' : ""}
      ${dim.applies_note ? `<span class="badge na">${esc(dim.applies_note)}</span>` : ""}</div>
      ${dim.definition ? `<p class="definition">${esc(dim.definition)}</p>` : ""}
      ${dim.guiding_questions?.length ? `<details class="guide"><summary>Guiding questions</summary><ul>${dim.guiding_questions.map(q => `<li>${esc(q)}</li>`).join("")}</ul></details>` : ""}`;
    let body;
    if (dim.repeatable === "hypothesis") {
      const editable = new Set(fields.map(f => f.key));
      const context = h => ["hypothesis", "counterfactual", "indicator", "baseline_t0", "midterm_t1"].filter(k => !editable.has(k)).map(k => {
        const r = ctx.current.get(respKey(dim.code, k, h.id)); if (!r) return "";
        const f = dimFields(dim).find(x => x.key === k);
        return `<dt>${esc(f.label)}</dt><dd>${r.status === "filled" ? esc(valueOf(r, f)) : `<em>${esc(titleCase(r.status))}</em> — ${esc(r.note || "")}`}</dd>`;
      }).join("");
      body = `<p class="help">${esc(dim.repeatable_note || "")}</p>` + ctx.hyps.map(h => `<div class="hyp" data-hyp="${h.id}"><div class="hyp-head"><strong>Hypothesis ${h.ordinal}</strong><div class="spacer"></div>
          ${ctx.hyps.length > 1 ? `<button class="btn small ghost remove-hyp" data-hyp="${h.id}">Remove</button>` : ""}</div>
          ${context(h) ? `<dl class="kv context">${context(h)}</dl>` : ""}
          ${fields.map(f => fieldRow(dim, f, h.id, ctx)).join("")}</div>`).join("") +
        `<button class="btn small add-hyp" data-dim="${dim.code}">+ Add hypothesis</button>`;
    } else if (dim.code === "L2.1") {
      body = costBlock(dim, ctx) + fields.filter(f => f.type !== "money_usd").map(f => fieldRow(dim, f, null, ctx)).join("");
    } else {
      body = fields.map(f => fieldRow(dim, f, null, ctx)).join("");
    }
    return `<section class="card level-${dim.level}" data-dim="${dim.code}">${head}${body}</section>`;
  }

  function valueOf(row, f) {
    if (!row) return "";
    if (f.type === "money_usd") return row.value_num ?? "";
    if (f.type === "date") return row.value_date ?? "";
    return row.value_text ?? "";
  }
  function inputHtml(f, value, disabled) {
    const d = disabled ? "disabled" : "";
    if (f.type === "degree") return `<select class="value" ${d}><option value="">— select —</option>${catalog.degree_scale.map(o => `<option value="${o.key}" ${value === o.key ? "selected" : ""}>${esc(o.label)}</option>`).join("")}</select>`;
    if (f.type === "enum") return `<select class="value" ${d}><option value="">— select —</option>${f.options.map(o => `<option value="${o}" ${value === o ? "selected" : ""}>${esc(f.option_labels?.[o] || titleCase(o))}</option>`).join("")}</select>`;
    if (f.type === "longtext") return `<textarea class="value" ${d}>${esc(value)}</textarea>`;
    if (f.type === "date") return `<input type="date" class="value" value="${esc(value)}" ${d}>`;
    if (f.type === "money_usd") return `<input type="number" class="value" min="0" step="0.01" value="${esc(value)}" ${d} placeholder="0.00">`;
    return `<input type="text" class="value" value="${esc(value)}" ${d}>`;
  }
  function metaHtml(row) {
    if (!row) return `Not yet filled`;
    const who = row.author?.full_name || row.author?.email || "";
    return `Saved ${esc(fmtDate(row.created_at))}${who ? " by " + esc(who) : ""}${row.stage ? " · stage: " + esc(stageLabel(row.stage)) : ""}`;
  }
  function fieldRow(dim, f, hypId, ctx) {
    const row = ctx.current.get(respKey(dim.code, f.key, hypId));
    const status = row?.status || "filled";
    const value = valueOf(row, f);
    const needNote = status !== "filled" || (f.type === "degree" && value === "na");
    return `<div class="frow ${status !== "filled" ? "off" : ""}" data-dim="${dim.code}" data-field="${f.key}" data-hyp="${hypId || ""}" data-type="${f.type}">
      <div class="field"><label class="${f.required ? "req" : ""}">${esc(f.label)}</label>${inputHtml(f, value, status !== "filled")}${f.help ? `<div class="help">${esc(f.help)}</div>` : ""}</div>
      <div class="status"><label>Status</label><select class="status-sel">${catalog.field_statuses.map(s => `<option value="${s.key}" ${s.key === status ? "selected" : ""}>${esc(s.label)}</option>`).join("")}</select></div>
      <div class="note" style="${needNote ? "" : "display:none"}"><label class="req">Note</label><input type="text" class="note-in" value="${esc(row?.note || "")}" placeholder="Why there is no evidence / why it does not apply / why NA"></div>
      <div class="meta">${metaHtml(row)}</div></div>`;
  }

  function costBlock(dim, ctx) {
    const fields = dimFields(dim).filter(f => f.type === "money_usd");
    const group = g => fields.filter(f => f.group === g).map(f => {
      const row = ctx.current.get(respKey(dim.code, f.key, null));
      return `<div class="cost-line frow" data-dim="${dim.code}" data-field="${f.key}" data-hyp="" data-type="money_usd" style="border:0;padding:4px 0">
        <label title="${esc(f.help || "")}">${esc(f.label)}</label><input type="number" class="value" min="0" step="0.01" value="${esc(row?.value_num ?? "")}" placeholder="0.00"></div>`;
    }).join("");
    return `<p class="help" style="margin-bottom:10px">${esc(dim.currency_rule)}</p><div class="costs">
      <div><h3>Capex — one-time</h3>${group("capex")}<div class="cost-total"><span>Total capex</span><span data-total="capex">—</span></div></div>
      <div><h3>Opex — recurring</h3>${group("opex")}<div class="cost-total"><span>Total opex</span><span data-total="opex">—</span></div></div></div>
      <div class="grand"><span>Total pilot cost (capex + opex)</span><span data-total="all">—</span></div>
      <p class="help" style="margin-top:8px">Itemised breakdown with original currency: coming in the next phase.</p>`;
  }
  function updateTotals(card) {
    if (!card) return;
    const sum = g => [...card.querySelectorAll(`.cost-line[data-field^="${g}."] input.value`)].reduce((a, i) => a + (parseFloat(i.value) || 0), 0);
    const c = sum("capex"), o = sum("opex");
    card.querySelector('[data-total=capex]').textContent = money(c);
    card.querySelector('[data-total=opex]').textContent = money(o);
    card.querySelector('[data-total=all]').textContent = money(c + o);
  }

  function wireDims($dims, ctx) {
    $dims.querySelectorAll('section[data-dim="L2.1"]').forEach(updateTotals);
    $dims.addEventListener("change", async e => {
      const frow = e.target.closest(".frow"); if (!frow) return;
      if (e.target.classList.contains("status-sel")) {
        const off = e.target.value !== "filled";
        frow.classList.toggle("off", off);
        frow.querySelector(".value").disabled = off;
        frow.querySelector(".note").style.display = off || (frow.dataset.type === "degree" && frow.querySelector(".value").value === "na") ? "" : "none";
      }
      if (e.target.classList.contains("value") && frow.dataset.type === "degree") {
        frow.querySelector(".note").style.display = e.target.value === "na" ? "" : "none";
      }
      if (e.target.classList.contains("value") && frow.dataset.type === "money_usd") updateTotals(frow.closest("section"));
      await commit(frow, ctx);
    });
    $dims.addEventListener("click", async e => {
      const add = e.target.closest(".add-hyp");
      if (add) { await data.addHypothesis(ctx.pilot.id); toast("Hypothesis added"); render(); return; }
      const rm = e.target.closest(".remove-hyp");
      if (rm) {
        const ok = await modal({ title: "Remove this hypothesis?", body: "<p>Its entries are kept in the history but it will no longer be shown or exported.</p>", confirm: "Remove", cancel: "Keep", danger: true });
        if (ok) { await data.archiveHypothesis(rm.dataset.hyp); render(); }
      }
    });
  }

  async function commit(frow, ctx) {
    const { dim, field, hyp, type } = frow.dataset;
    const statusSel = frow.querySelector(".status-sel");
    const status = statusSel ? statusSel.value : "filled";
    const raw = frow.querySelector(".value").value;
    const noteIn = frow.querySelector(".note-in");
    const note = noteIn ? noteIn.value.trim() : "";
    const meta = frow.querySelector(".meta");
    const key = respKey(dim, field, hyp || null);
    const prev = ctx.current.get(key);

    const needNote = status !== "filled" || (type === "degree" && raw === "na");
    if (needNote && !note) { if (meta) { meta.className = "meta err"; meta.textContent = "A note is required for this status — not saved yet."; } return; }

    const row = { pilot_id: ctx.pilot.id, dimension_code: dim, field_key: field, hypothesis_id: hyp || null, stage: ctx.stage, status, note: note || null,
      value_text: null, value_num: null, value_date: null };
    if (status === "filled") {
      if (raw === "") { if (!prev) return; }   // nothing to save
      if (type === "money_usd") row.value_num = raw === "" ? null : parseFloat(raw);
      else if (type === "date") row.value_date = raw || null;
      else row.value_text = raw || null;
    }
    const same = prev && prev.status === row.status && (prev.note || null) === row.note &&
      (prev.value_text ?? null) === row.value_text && (prev.value_num == null ? null : Number(prev.value_num)) === row.value_num && (prev.value_date ?? null) === row.value_date;
    if (same) return;

    const done = ctx.completions.find(c => c.stage === ctx.stage);
    const warnKey = `${ctx.pilot.id}|${ctx.stage}`;
    if (done && !stageWarned.has(warnKey)) {
      const ok = await modal({ title: `Edit a completed stage?`, body: `<p><strong>${esc(stageLabel(ctx.stage))}</strong> was completed on ${esc(fmtDate(done.completed_at))}. Changes are allowed and will be recorded in the history, but make sure this is not accidental.</p>`, confirm: "Save changes", cancel: "Discard" });
      if (!ok) { render(); return; }
      stageWarned.add(warnKey);
    }
    try {
      const saved = await data.saveResponse(row);
      ctx.current.set(key, saved);
      if (meta) { meta.className = "meta"; meta.innerHTML = metaHtml(saved); }
      toast("Saved");
    } catch (err) { if (meta) { meta.className = "meta err"; meta.textContent = "Not saved: " + err.message; } }
  }

  async function completeStage(ctx) {
    const { pilot, stage, current, hyps } = ctx;
    const missing = [];
    for (const dim of dimsForStage(stage, pilot)) {
      if (!dim.core) continue;
      const req = fieldsForStage(dim, stage).filter(f => f.required);
      if (dim.repeatable === "hypothesis") {
        for (const h of hyps) for (const f of req) if (!current.has(respKey(dim.code, f.key, h.id))) missing.push(`${dim.code} ${dim.name} — hypothesis ${h.ordinal}: ${f.label}`);
      } else if (dim.code === "L2.1") {
        const any = dimFields(dim).some(f => f.type === "money_usd" && current.has(respKey(dim.code, f.key, null)));
        if (!any) missing.push(`${dim.code} ${dim.name}: at least one cost line (or mark the notes field as No evidence)`);
      } else {
        for (const f of req) if (!current.has(respKey(dim.code, f.key, null))) missing.push(`${dim.code} ${dim.name}: ${f.label}`);
      }
    }
    if (missing.length) {
      await modal({ title: "Core fields still empty", body: `<p>Fill these, or mark them as <em>No evidence</em> / <em>Not applicable</em>, before completing the stage:</p><ul>${missing.map(m => `<li>${esc(m)}</li>`).join("")}</ul>` });
      return;
    }
    const ok = await modal({ title: `Complete ${stageLabel(stage)}?`, body: `<p>A snapshot of every field entered at this stage will be stored with your name and the date. Fields stay editable afterwards.</p>`, confirm: "Complete stage", cancel: "Not yet" });
    if (!ok) return;
    const snapshot = {};
    for (const dim of dimsForStage(stage, pilot)) {
      for (const f of fieldsForStage(dim, stage)) {
        const targets = dim.repeatable === "hypothesis" ? hyps.map(h => h.id) : [null];
        for (const t of targets) {
          const r = current.get(respKey(dim.code, f.key, t)); if (!r) continue;
          const k = t ? `${dim.code}.h${hyps.find(h => h.id === t).ordinal}.${f.key}` : `${dim.code}.${f.key}`;
          snapshot[k] = { status: r.status, value: r.value_text ?? r.value_num ?? r.value_date ?? null, note: r.note, author: r.author?.full_name || r.author_id, at: r.created_at };
        }
      }
    }
    await data.completeStage(pilot.id, stage, snapshot);
    const nx = nextStage(stage);
    if (pilot.mode === "prospective" && stage === pilot.current_stage && nx) await data.updatePilot(pilot.id, { current_stage: nx });
    toast(`${stageLabel(stage)} completed`);
    location.hash = `#/pilots/${pilot.id}/${(pilot.mode === "prospective" && nx) ? nx : stage}`;
    render();
  }

  /* ---------------- import a retrospective workbook ---------------- */
  async function importDialog() {
    const bg = document.createElement("div"); bg.className = "modal-bg";
    bg.innerHTML = `<div class="modal"><h2>Import a data-collection workbook</h2>
      <p class="help">Reads an Excel workbook in the template format (.xlsx / .xlsm) and creates the pilot with its hypotheses and values. Cost figures written as text land in the L2.1 notes; enter the USD amounts afterwards.</p>
      <input type="file" id="wb" accept=".xlsx,.xlsm"><div id="preview" style="margin-top:12px"></div>
      <div class="actions"><button class="btn" id="cancel">Cancel</button><button class="btn primary" id="go" disabled>Import</button></div></div>`;
    document.body.appendChild(bg);
    let parsed = null;
    bg.querySelector("#cancel").onclick = () => bg.remove();
    bg.querySelector("#wb").onchange = async e => {
      const file = e.target.files[0]; if (!file) return;
      try {
        parsed = window.CLAB.importWorkbook(new Uint8Array(await file.arrayBuffer()), catalog, file.name);
        const p = parsed.pilot;
        bg.querySelector("#preview").innerHTML = `<dl class="kv"><dt>Pilot</dt><dd><strong>${esc(p.name)}</strong></dd><dt>TC · place</dt><dd>${esc(p.tc_code)} · ${esc(p.city)}, ${esc(p.country)}</dd>
          <dt>Type</dt><dd>${esc(typeLabel(p.pilot_type))} · ${esc(titleCase(p.attribution_category))}-based · retrospective</dd>
          <dt>Content</dt><dd>${parsed.hypotheses.length} hypotheses · ${parsed.responses.length} values (${parsed.responses.filter(r => r.status !== "filled").length} marked no evidence)</dd></dl>
          ${parsed.warnings.length ? `<div class="banner warn" style="margin-top:10px"><strong>Check after import:</strong><ul>${parsed.warnings.map(w => `<li>${esc(w)}</li>`).join("")}</ul></div>` : ""}`;
        bg.querySelector("#go").disabled = !p.name || !p.tc_code;
      } catch (err) { bg.querySelector("#preview").innerHTML = `<div class="banner err">Could not read this file: ${esc(err.message)}</div>`; }
    };
    bg.querySelector("#go").onclick = async () => {
      const btn = bg.querySelector("#go"); btn.disabled = true; btn.textContent = "Importing…";
      try {
        const { pilot: pf, hypotheses, responses } = parsed;
        const pilot = await data.createPilot(pf);
        const hypIds = [];
        for (const h of hypotheses) hypIds.push((await data.addHypothesis(pilot.id)).id);
        for (const r of responses) await data.saveResponse({ pilot_id: pilot.id, dimension_code: r.dimension_code, field_key: r.field_key, hypothesis_id: r.hypothesis_index == null ? null : hypIds[r.hypothesis_index],
          stage: r.stage, status: r.status, value_text: r.value_text ?? null, value_num: null, value_date: null, note: r.note ?? null });
        bg.remove(); toast(`Imported ${pf.name}`); location.hash = `#/pilots/${pilot.id}/summary`; render();
      } catch (err) { btn.textContent = "Import"; btn.disabled = false; bg.querySelector("#preview").insertAdjacentHTML("beforeend", `<div class="banner err">Import failed: ${esc(err.message)}</div>`); }
    };
  }

  /* ---------------- views: pilot summary (Evaluation Sheet) ---------------- */
  function cell(r, f) {
    if (!r) return `<span class="muted">—</span>`;
    if (r.status !== "filled") return `<span class="badge na">${esc(titleCase(r.status))}</span> ${esc(r.note || "")}`;
    const v = valueOf(r, f);
    if (f.type === "degree") return `<span class="degree ${esc(v)}">${esc(catalog.degree_scale.find(d => d.key === v)?.label || v)}</span>${v === "na" && r.note ? " — " + esc(r.note) : ""}`;
    if (f.type === "money_usd") return money(v);
    if (f.type === "enum") return esc(f.option_labels?.[v] || titleCase(v));
    return esc(v);
  }
  const who = r => r ? `${esc(r.author?.full_name || r.author?.email || "")}<br><span class="muted">${esc(fmtDate(r.created_at))}</span>` : "";

  async function viewSummary(id) {
    setNav("pilots");
    const pilot = await data.getPilot(id);
    if (!pilot) { $main.innerHTML = `<div class="empty">Pilot not found.</div>`; return; }
    const [hyps, rows, completions] = await Promise.all([data.listHypotheses(id), data.currentResponses(id), data.listCompletions(id)]);
    const cur = new Map(rows.map(r => [respKey(r.dimension_code, r.field_key, r.hypothesis_id), r]));
    const sections = catalog.levels.map(level => {
      const dims = catalog.dimensions.filter(d => d.level === level.key && applies(d, pilot));
      const body = dims.map(dim => {
        const fields = dimFields(dim);
        if (dim.repeatable === "hypothesis") {
          return hyps.map(h => `<tr class="dimrow"><td class="code">${esc(dim.code)}</td><td colspan="4"><strong>${esc(dim.name)} — hypothesis ${h.ordinal}</strong></td></tr>` +
            fields.map(f => { const r = cur.get(respKey(dim.code, f.key, h.id)); return r ? `<tr><td></td><td>${esc(f.label)}</td><td colspan="2">${cell(r, f)}</td><td class="who">${who(r)}</td></tr>` : ""; }).join("")).join("");
        }
        if (dim.code === "L2.1") {
          const money_ = fields.filter(f => f.type === "money_usd");
          const sum = g => money_.filter(f => f.group === g).reduce((a, f) => { const r = cur.get(respKey(dim.code, f.key, null)); return a + (r?.status === "filled" && r.value_num != null ? Number(r.value_num) : 0); }, 0);
          const any = money_.some(f => cur.has(respKey(dim.code, f.key, null)));
          return `<tr class="dimrow"><td class="code">${esc(dim.code)}</td><td colspan="4"><strong>${esc(dim.name)}</strong> <span class="muted">USD</span></td></tr>` +
            money_.map(f => { const r = cur.get(respKey(dim.code, f.key, null)); return r ? `<tr><td></td><td>${esc(f.label)}</td><td colspan="2">${cell(r, f)}</td><td class="who">${who(r)}</td></tr>` : ""; }).join("") +
            (any ? `<tr class="total"><td></td><td>Total capex</td><td colspan="3">${money(sum("capex"))}</td></tr><tr class="total"><td></td><td>Total opex</td><td colspan="3">${money(sum("opex"))}</td></tr><tr class="total"><td></td><td>Total pilot cost</td><td colspan="3">${money(sum("capex") + sum("opex"))}</td></tr>` : "") +
            fields.filter(f => f.type !== "money_usd").map(f => { const r = cur.get(respKey(dim.code, f.key, null)); return r ? `<tr><td></td><td>${esc(f.label)}</td><td colspan="2">${cell(r, f)}</td><td class="who">${who(r)}</td></tr>` : ""; }).join("");
        }
        const main = fields.find(f => f.required) || fields[0];
        const r = cur.get(respKey(dim.code, main.key, null));
        const others = fields.filter(f => f !== main).map(f => { const x = cur.get(respKey(dim.code, f.key, null)); return x ? `<div><span class="muted">${esc(f.label)}:</span> ${cell(x, f)}</div>` : ""; }).join("");
        return `<tr class="dimrow"><td class="code">${esc(dim.code)}</td><td><strong>${esc(dim.name)}</strong>${dim.core ? ' <span class="badge core">Core</span>' : ""}</td><td>${cell(r, main)}</td><td>${others}</td><td class="who">${who(r)}</td></tr>`;
      }).join("");
      return `<h2 class="level-h level-${level.key}">Level ${level.key} — ${esc(level.label)}</h2><table class="summary"><thead><tr><th style="width:60px">Code</th><th style="width:220px">Dimension</th><th>Assessment</th><th>Detail</th><th style="width:170px">Last saved by</th></tr></thead><tbody>${body}</tbody></table>`;
    }).join("");

    const timeline = STAGES.map(s => { const c = completions.filter(x => x.stage === s).sort((a, b) => b.completed_at.localeCompare(a.completed_at))[0]; return `<span class="badge ${c ? "done" : ""}">${esc(stageLabel(s))}${c ? " ✓ " + esc(fmtDate(c.completed_at)) : ""}</span>`; }).join(" ");
    $main.innerHTML = `
      <div class="toolbar noprint"><a class="btn" href="#/pilots/${id}">← Back to data collection</a><div class="spacer"></div>
        <button class="btn" id="print">Print / save as PDF</button><button class="btn" id="csv1">Download this pilot (CSV)</button></div>
      <div class="sheet">
        <div class="sheet-head"><div><div class="muted">CITIES LAB · CSD/HUD · PILOT EVALUATION SHEET</div><h1>${esc(pilot.name)}</h1>
          <p class="sub"><span class="code">${esc(pilot.tc_code)}</span> · ${esc(pilot.city)}, ${esc(pilot.country)} · ${esc(typeLabel(pilot.pilot_type))} · ${esc(titleCase(pilot.attribution_category))}-based · ${esc(titleCase(pilot.mode))}</p></div>
          <div class="muted" style="text-align:right">Generated ${esc(fmtDate(new Date().toISOString()))}<br>Framework v1.3.3</div></div>
        <dl class="kv"><dt>Problem definition</dt><dd>${esc(pilot.problem_definition || "—")}</dd><dt>Target population</dt><dd>${esc(pilot.target_population || "—")}</dd>
          <dt>Lead / responsible</dt><dd>${esc(pilot.lead || "—")}</dd><dt>Counterpart</dt><dd>${esc(pilot.counterpart || "—")}</dd><dt>Dates</dt><dd>${esc(pilot.date_started || "?")} → ${esc(pilot.date_ended || "ongoing")}</dd>
          <dt>Stages</dt><dd>${timeline}</dd></dl>
        ${sections}
      </div>`;
    document.getElementById("print").onclick = () => window.print();
    document.getElementById("csv1").onclick = () => {
      const { columns, rows: wide } = window.CLAB.buildWide(catalog, [{ pilot, hypotheses: hyps, responses: rows, completions }]);
      window.CLAB.download(`${pilot.tc_code}_${pilot.name.replace(/[^\w]+/g, "_").slice(0, 40)}.csv`, window.CLAB.toCsv(columns.map(c => c.key), wide), "text/csv");
    };
  }

  /* ---------------- views: export ---------------- */
  async function viewExport() {
    setNav("export");
    $main.innerHTML = `<h1>Export</h1><p class="sub">One row per pilot, one column per indicator. Each value column is followed by <code>_status</code> and <code>_note</code>; hypotheses appear as <code>h1…hN</code> groups. The codebook maps every column to its dimension and field.</p>
      <div class="card"><div class="toolbar"><button class="btn primary" id="xlsx">Download XLSX (data + codebook + history)</button><button class="btn" id="csv">Download wide CSV</button><button class="btn" id="hist">Download change history CSV</button><button class="btn" id="cb">Download codebook CSV</button></div>
      <div id="status" class="help">Loading…</div></div>`;
    const pilots = await data.listPilots();
    const bundle = await Promise.all(pilots.map(async pilot => {
      const [hypotheses, responses, completions, history] = await Promise.all([data.listHypotheses(pilot.id), data.currentResponses(pilot.id), data.listCompletions(pilot.id), data.allResponses(pilot.id)]);
      return { pilot, hypotheses, responses, completions, history };
    }));
    const wide = window.CLAB.buildWide(catalog, bundle);
    const hist = window.CLAB.buildHistory(catalog, bundle);
    const histCols = ["pilot_id", "pilot", "tc_code", "dimension_code", "dimension", "field", "hypothesis", "stage", "status", "value", "note", "author", "saved_at"];
    const cbCols = ["column", "dimension", "field", "label", "type"];
    document.getElementById("status").textContent = `${pilots.length} pilots · ${wide.columns.length} columns · ${hist.length} saved values in history`;
    const stamp = new Date().toISOString().slice(0, 10);
    document.getElementById("csv").onclick = () => window.CLAB.download(`clab_pilots_wide_${stamp}.csv`, window.CLAB.toCsv(wide.columns.map(c => c.key), wide.rows), "text/csv");
    document.getElementById("hist").onclick = () => window.CLAB.download(`clab_pilots_history_${stamp}.csv`, window.CLAB.toCsv(histCols, hist), "text/csv");
    document.getElementById("cb").onclick = () => window.CLAB.download(`clab_codebook_${stamp}.csv`, window.CLAB.toCsv(cbCols, wide.codebook), "text/csv");
    document.getElementById("xlsx").onclick = () => window.CLAB.download(`clab_pilots_${stamp}.xlsx`, window.CLAB.toXlsx([
      { name: "pilots_wide", columns: wide.columns.map(c => c.key), rows: wide.rows },
      { name: "codebook", columns: cbCols, rows: wide.codebook },
      { name: "history", columns: histCols, rows: hist }]));
  }

  /* ---------------- router ---------------- */
  async function render() {
    renderShell();
    const u = data.currentUser();
    const parts = (location.hash.replace(/^#\/?/, "") || "pilots").split("/");
    if (!u || u.unauthorised) { viewLogin(); return; }
    try {
      if (parts[0] === "login") { location.hash = "#/pilots"; return; }
      if (parts[0] === "pilots" && !parts[1]) return await viewPilots();
      if (parts[0] === "pilots" && parts[1] === "new") return await viewPilotForm(null);
      if (parts[0] === "pilots" && parts[2] === "edit") return await viewPilotForm(parts[1]);
      if (parts[0] === "pilots" && parts[2] === "summary") return await viewSummary(parts[1]);
      if (parts[0] === "export") return await viewExport();
      if (parts[0] === "pilots") return await viewPilot(parts[1], parts[2]);
      $main.innerHTML = `<div class="empty">Page not found.</div>`;
    } catch (err) {
      $main.innerHTML = `<div class="banner err">Something went wrong: ${esc(err.message)}</div>`;
      console.error(err);
    }
  }

  /* ---------------- boot ---------------- */
  try {
    catalog = await (await fetch("../catalog/framework_catalog.json", { cache: "no-store" })).json();
  } catch (e) {
    $main.innerHTML = `<div class="banner err">Could not load the framework catalog (catalog/framework_catalog.json). Serve the repository root, not only web/.</div>`; return;
  }
  data = createDataLayer();
  user = await data.init();
  data.onAuthChange(() => render());
  window.addEventListener("hashchange", render);
  render();
})();
