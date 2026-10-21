/* Workbook importer: reads a data-collection workbook (the Excel template, .xlsx/.xlsm) and maps it
 * to the app's model: {pilot, hypotheses, responses, warnings}. Pure function; no I/O.
 * Works in the browser (global XLSX) and in Node (require('xlsx')).
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory(require("xlsx"));
  else root.CLAB = Object.assign(root.CLAB || {}, factory(root.XLSX));
})(typeof window !== "undefined" ? window : this, function (XLSX) {

  const STAGE_SHEETS = [["ideation", /ideation/i], ["design", /design/i], ["implementation", /implementation/i], ["evaluation", /^(?!.*sheet).*evaluation/i]];
  const NA_RE = /^\s*(na|n\/a|n\.a\.|-|—|not available|not applicable)\s*\.?$/i;
  const clean = v => v == null ? "" : String(v).replace(/‑|‐|‒|–/g, "-").replace(/\s+/g, " ").trim();
  const norm = s => clean(s).toLowerCase();

  function sheetRows(ws) {
    // anchor the grid at A1 so column indexes are absolute even when the used range starts at column B
    const rng = XLSX.utils.decode_range(ws["!ref"] || "A1"); rng.s.c = 0; rng.s.r = 0;
    return XLSX.utils.sheet_to_json(ws, { header: 1, raw: true, defval: null, blankrows: false, range: rng });
  }

  /* ---------- cover ---------- */
  const COVER_MAP = [
    [/^pilot name/i, "name"], [/^tc code/i, "tc_code"], [/^city/i, "city_country"], [/^pilot type/i, "pilot_type"],
    [/^attribution/i, "attribution_category"], [/^problem/i, "problem_definition"], [/^target pop/i, "target_population"],
    [/^lead/i, "lead"], [/^date started/i, "date_started"], [/^last updated|^date ended/i, "date_ended"],
  ];
  function excelDate(v, warnings, label) {
    if (v == null || v === "") return null;
    if (typeof v === "number") {
      if (v < 3000) { warnings.push(`${label}: only a year (${v}) in the workbook; stored as ${v}-01-01.`); return `${v}-01-01`; }
      const d = XLSX.SSF.parse_date_code(v); if (!d) return null;
      if (d.y < 1950) { // "2022" typed into a date-formatted cell becomes 1905-07-14
        const y = Math.round(v); warnings.push(`${label}: workbook had the date ${d.y}-${d.m}-${d.d}, read as the year ${y}; stored as ${y}-01-01.`); return `${y}-01-01`;
      }
      return `${d.y}-${String(d.m).padStart(2, "0")}-${String(d.d).padStart(2, "0")}`;
    }
    const s = clean(v);
    if (/^\d{4}$/.test(s)) { warnings.push(`${label}: only a year (${s}); stored as ${s}-01-01.`); return `${s}-01-01`; }
    const m = s.match(/^(\d{4})-(\d{2})-(\d{2})/); if (m) return m[0];
    const d = new Date(s); if (!isNaN(d)) return d.toISOString().slice(0, 10);
    warnings.push(`${label}: could not parse "${s}".`); return null;
  }
  function parseCover(ws, catalog, warnings) {
    const rows = sheetRows(ws); const raw = {};
    for (const r of rows) {
      const label = norm(r[1]); if (!label) continue;
      for (const [re, key] of COVER_MAP) if (re.test(label)) raw[key] = r[2];
    }
    const pilot = { mode: "retrospective", current_stage: "evaluation" };
    pilot.name = clean(raw.name); pilot.tc_code = clean(raw.tc_code).toUpperCase();
    const cc = clean(raw.city_country); const i = cc.lastIndexOf(",");
    pilot.city = i > 0 ? cc.slice(0, i).trim() : cc; pilot.country = i > 0 ? cc.slice(i + 1).trim() : "";
    if (!pilot.country) warnings.push(`Country not found in "${cc}"; please complete it.`);
    const t = norm(raw.pilot_type);
    pilot.pilot_type = catalog.pilot_types.find(p => t.startsWith(p.key) || p.label.toLowerCase().startsWith(t))?.key ||
      (/method|process/.test(t) ? "methodology" : /instit/.test(t) ? "institutional" : /behav/.test(t) ? "behavioral" : /digit/.test(t) ? "digital" : "urban");
    const a = norm(raw.attribution_category);
    pilot.attribution_category = /process/.test(a) ? "process" : "counterfactual";
    const derived = catalog.pilot_types.find(p => p.key === pilot.pilot_type).attribution;
    if (pilot.attribution_category !== derived) warnings.push(`Attribution category "${clean(raw.attribution_category)}" does not match pilot type "${clean(raw.pilot_type)}" (framework: ${derived}-based). Imported as written; review.`);
    pilot.problem_definition = clean(raw.problem_definition) || null; pilot.target_population = clean(raw.target_population) || null;
    pilot.lead = clean(raw.lead) || null;
    pilot.date_started = excelDate(raw.date_started, warnings, "Date started");
    pilot.date_ended = excelDate(raw.date_ended, warnings, "Last updated → date ended");
    if (!pilot.name || !pilot.tc_code) warnings.push("Pilot name or TC code missing on the cover sheet.");
    if (!/^[A-Z]{2}-T\d{4}$/.test(pilot.tc_code)) warnings.push(`TC code "${pilot.tc_code}" does not match AZ-T####.`);
    return pilot;
  }

  /* ---------- stage sheets ---------- */
  function parseStage(ws, stage) {
    const rows = sheetRows(ws); let headers = null; const out = [];
    for (const r of rows) {
      const a = norm(r[0]);
      if (a === "code") { headers = {}; r.forEach((h, i) => { if (h != null && i >= 4) headers[norm(h)] = i; }); continue; }
      if (/^l\d\.\d$/.test(a) && headers) {
        const fields = {};
        for (const [h, i] of Object.entries(headers)) { const v = r[i]; if (v != null && clean(v) !== "") fields[h] = typeof v === "string" ? clean(v) : v; }
        out.push({ code: a.toUpperCase(), label: clean(r[1]), stage, fields });
      }
    }
    return out;
  }

  const DEG = v => { const s = norm(v); return /^high/.test(s) ? "high" : /^med/.test(s) ? "medium" : /^low/.test(s) ? "low" : NA_RE.test(s) ? "na" : null; };
  const pick = (f, ...names) => { for (const n of names) if (f[n] != null) return f[n]; return null; };

  /* ---------- map to catalog ---------- */
  function importWorkbook(buffer, catalog, sourceName) {
    const wb = XLSX.read(buffer, { type: "array", cellDates: false });
    const warnings = [];
    const coverName = wb.SheetNames.find(n => /cover/i.test(n)) || wb.SheetNames[0];
    const pilot = parseCover(wb.Sheets[coverName], catalog, warnings);
    pilot.import_source = `${sourceName} (retrospective workbook)`;

    const entries = [];
    for (const [stage, re] of STAGE_SHEETS) { const n = wb.SheetNames.find(x => re.test(x)); if (n) entries.push(...parseStage(wb.Sheets[n], stage)); }
    if (!entries.length) warnings.push("No stage sheets recognised (expected tabs named Ideation / Design / Implementation / Evaluation).");

    const byCode = code => entries.filter(e => e.code === code);
    const responses = []; const hypotheses = [];
    const addResp = (code, key, hypIdx, stage, raw, noteRaw, type) => {
      const note = noteRaw != null ? clean(noteRaw) : "";
      if (raw == null || clean(raw) === "") return;
      if (type === "degree") {
        const d = DEG(raw); if (!d) { warnings.push(`${code}: unrecognised degree "${raw}" ignored.`); return; }
        responses.push({ dimension_code: code, field_key: key, hypothesis_index: hypIdx, stage, status: "filled", value_text: d, note: d === "na" ? (note || "Marked NA in the retrospective workbook") : (note || null) });
        return;
      }
      if (typeof raw === "string" && NA_RE.test(raw)) {
        responses.push({ dimension_code: code, field_key: key, hypothesis_index: hypIdx, stage, status: "no_evidence", value_text: null, note: note || "Marked NA in the retrospective workbook" });
        return;
      }
      responses.push({ dimension_code: code, field_key: key, hypothesis_index: hypIdx, stage, status: "filled", value_text: typeof raw === "string" ? raw : String(raw), note: note || null });
    };
    // latest non-empty value across stage tabs wins
    const latest = (rows, ...names) => { let best = null; for (const r of rows) { const v = pick(r.fields, ...names); if (v != null && clean(v) !== "") best = { v, stage: r.stage, note: pick(r.fields, "notes", "note") }; } return best; };

    for (const dim of catalog.dimensions) {
      const rows = byCode(dim.code);
      if (!rows.length) continue;
      if (dim.repeatable === "hypothesis") {
        const labels = [...new Set(rows.map(r => r.label))];
        labels.forEach((label, idx) => {
          const hr = rows.filter(r => r.label === label);
          const map = [["hypothesis", ["hypothesis"]], ["counterfactual", ["counterfactual"]], ["indicator", ["indicator"]], ["description", ["description / formula", "description"]],
            ["data_source", ["data source"]], ["baseline_t0", ["baseline (t0)", "baseline"]], ["midterm_t1", ["t1"]], ["endline_t2", ["t2 / endline", "t2"]]];
          const vals = map.map(([key, names]) => [key, latest(hr, ...names)]).filter(([, b]) => b);
          const anyReal = vals.some(([, b]) => !(typeof b.v === "string" && NA_RE.test(b.v)));
          if (!anyReal) return; // skip an entirely empty/NA hypothesis slot
          const hypIdx = hypotheses.length; hypotheses.push({ ordinal: hypIdx + 1, label });
          for (const [key, b] of vals) addResp(dim.code, key, hypIdx, b.stage, b.v, null, "text");
          const n = latest(hr, "notes"); if (n) addResp(dim.code, "notes", hypIdx, n.stage, n.v, null, "text");
        });
      } else if (dim.code === "L2.1") {
        const parts = []; let stage = "implementation"; const notes = [];
        for (const r of rows) { const d = pick(r.fields, "detail"); if (d != null && clean(d) !== "" && !(typeof d === "string" && NA_RE.test(d))) { parts.push(`${r.label}: ${clean(d)}`); stage = r.stage; } const n = pick(r.fields, "notes"); if (n) notes.push(`${r.label}: ${clean(n)}`); }
        if (parts.length) addResp(dim.code, "notes", null, stage, parts.join("\n") + (notes.length ? "\n\nWorkbook notes — " + notes.join(" | ") : ""), null, "text");
        else addResp(dim.code, "notes", null, stage, "NA", notes.join(" | ") || null, "text");
        if (rows.length) warnings.push("L2.1: cost figures were free text in the workbook; imported into the L2.1 notes field. Enter the USD amounts by subcategory in the app.");
      } else {
        const fields = Array.isArray(dim.fields) ? dim.fields : catalog.field_sets[dim.fields];
        for (const f of fields) {
          let b = null;
          if (f.key === "degree") b = latest(rows, "degree");
          else if (f.key === "justification") b = latest(rows, "justification");
          else if (f.key === "text") b = latest(rows, "detail", "descriptive field", "descriptive fields", "description");
          else if (f.key === "notes") b = latest(rows, "notes");
          if (b) addResp(dim.code, f.key, null, b.stage, b.v, f.key === "degree" ? b.note : null, f.type === "degree" ? "degree" : "text");
        }
      }
    }
    for (const dim of catalog.dimensions) if (!dim.applies_to.includes("all") && !dim.applies_to.includes(pilot.pilot_type) && responses.some(r => r.dimension_code === dim.code))
      warnings.push(`${dim.code} ${dim.name} has values but does not apply to a ${pilot.pilot_type} pilot; imported anyway (hidden in the stage form, visible in the summary).`);
    return { pilot, hypotheses, responses, warnings, source: sourceName };
  }

  return { importWorkbook };
});
