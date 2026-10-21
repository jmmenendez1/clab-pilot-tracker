/* Wide export: one row per pilot, one column per indicator, generated from the catalog.
 * Column naming: <code>_<field> with dots replaced by underscores, e.g. L1_1_degree, L2_1_capex_construction,
 * L3_2_h1_hypothesis. Each value column is followed by _status and _note columns.
 * Also builds a codebook (column → dimension, field, label, type) and the change-history table.
 */
(function () {
  const colName = s => s.replace(/\./g, "_");
  const dimFields = (catalog, dim) => Array.isArray(dim.fields) ? dim.fields : catalog.field_sets[dim.fields];
  const valueOf = (r, type) => !r ? null : type === "money_usd" ? (r.value_num ?? null) : type === "date" ? (r.value_date ?? null) : (r.value_text ?? null);

  /** pilots: [{pilot, hypotheses, responses, completions}] */
  function buildWide(catalog, pilots) {
    const maxH = Math.max(1, ...pilots.map(p => p.hypotheses.length));
    const columns = [];   // {key, label, dimension, field, type}
    const add = (key, label, dimension, field, type) => columns.push({ key, label, dimension, field, type });

    add("pilot_id", "Pilot id", "pilot", "id", "text");
    for (const f of catalog.pilot_fields) add(f.key, f.label, "pilot", f.key, f.type);
    add("current_stage", "Current stage", "pilot", "current_stage", "enum");
    for (const s of catalog.stages) add(`completed_${s.key}_at`, `${s.label} completed at`, "stage", s.key, "datetime");

    const triple = (base, label, dimension, field, type) => {
      add(base, label, dimension, field, type);
      add(base + "_status", label + " — status", dimension, field, "status");
      add(base + "_note", label + " — note", dimension, field, "text");
    };
    for (const dim of catalog.dimensions) {
      const fields = dimFields(catalog, dim);
      if (dim.repeatable === "hypothesis") {
        for (let i = 1; i <= maxH; i++) for (const f of fields)
          triple(`${colName(dim.code)}_h${i}_${colName(f.key)}`, `${dim.name} · hypothesis ${i} · ${f.label}`, dim.code, f.key, f.type);
      } else {
        for (const f of fields) triple(`${colName(dim.code)}_${colName(f.key)}`, `${dim.name} · ${f.label}`, dim.code, f.key, f.type);
        for (const c of dim.computed || []) add(`${colName(dim.code)}_${c.key}`, `${dim.name} · ${c.label}`, dim.code, c.key, "money_usd");
      }
    }

    const rows = pilots.map(({ pilot, hypotheses, responses, completions }) => {
      const cur = new Map(responses.map(r => [`${r.dimension_code}|${r.field_key}|${r.hypothesis_id || ""}`, r]));
      const row = {};
      row.pilot_id = pilot.id;
      for (const f of catalog.pilot_fields) row[f.key] = pilot[f.key] ?? null;
      row.current_stage = pilot.current_stage;
      for (const s of catalog.stages) { const c = completions.filter(x => x.stage === s.key).sort((a, b) => b.completed_at.localeCompare(a.completed_at))[0]; row[`completed_${s.key}_at`] = c ? c.completed_at : null; }
      const put = (base, r, type) => { row[base] = r && r.status === "filled" ? valueOf(r, type) : null; row[base + "_status"] = r ? r.status : null; row[base + "_note"] = r ? (r.note ?? null) : null; };
      for (const dim of catalog.dimensions) {
        const fields = dimFields(catalog, dim);
        if (dim.repeatable === "hypothesis") {
          hypotheses.slice().sort((a, b) => a.ordinal - b.ordinal).forEach((h, i) => {
            for (const f of fields) put(`${colName(dim.code)}_h${i + 1}_${colName(f.key)}`, cur.get(`${dim.code}|${f.key}|${h.id}`), f.type);
          });
        } else {
          for (const f of fields) put(`${colName(dim.code)}_${colName(f.key)}`, cur.get(`${dim.code}|${f.key}|`), f.type);
          if (dim.computed) {
            const sum = g => fields.filter(f => f.type === "money_usd" && f.group === g).reduce((a, f) => { const r = cur.get(`${dim.code}|${f.key}|`); return a + (r && r.status === "filled" && r.value_num != null ? Number(r.value_num) : 0); }, 0);
            const capex = sum("capex"), opex = sum("opex");
            const any = fields.some(f => f.type === "money_usd" && cur.has(`${dim.code}|${f.key}|`));
            const r2 = n => Math.round(n * 100) / 100;
            row[`${colName(dim.code)}_capex_total`] = any ? r2(capex) : null;
            row[`${colName(dim.code)}_opex_total`] = any ? r2(opex) : null;
            row[`${colName(dim.code)}_pilot_total`] = any ? r2(capex + opex) : null;
          }
        }
      }
      return row;
    });

    const codebook = columns.map(c => ({ column: c.key, dimension: c.dimension, field: c.field, label: c.label, type: c.type }));
    return { columns, rows, codebook };
  }

  function buildHistory(catalog, pilots) {
    const dimName = code => catalog.dimensions.find(d => d.code === code)?.name || code;
    const rows = [];
    for (const { pilot, hypotheses, history } of pilots) {
      const hord = new Map(hypotheses.map(h => [h.id, h.ordinal]));
      for (const r of history) rows.push({
        pilot_id: pilot.id, pilot: pilot.name, tc_code: pilot.tc_code, dimension_code: r.dimension_code, dimension: dimName(r.dimension_code),
        field: r.field_key, hypothesis: r.hypothesis_id ? hord.get(r.hypothesis_id) ?? "archived" : null, stage: r.stage, status: r.status,
        value: r.value_text ?? r.value_num ?? r.value_date ?? null, note: r.note ?? null,
        author: r.author?.full_name || r.author?.email || r.author_id, saved_at: r.created_at,
      });
    }
    rows.sort((a, b) => a.pilot.localeCompare(b.pilot) || a.saved_at.localeCompare(b.saved_at));
    return rows;
  }

  function toCsv(columns, rows) {
    const q = v => { if (v == null) return ""; const s = String(v); return /[",\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s; };
    return "﻿" + [columns.join(","), ...rows.map(r => columns.map(c => q(r[c])).join(","))].join("\r\n");
  }

  function download(filename, content, mime) {
    const blob = content instanceof Blob ? content : new Blob([content], { type: mime });
    const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = filename; document.body.appendChild(a); a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500);
  }

  function toXlsx(sheets) {   // sheets: [{name, columns, rows}]
    const wb = XLSX.utils.book_new();
    for (const s of sheets) {
      const aoa = [s.columns, ...s.rows.map(r => s.columns.map(c => r[c] ?? null))];
      const ws = XLSX.utils.aoa_to_sheet(aoa);
      ws["!cols"] = s.columns.map(c => ({ wch: Math.min(40, Math.max(12, c.length + 2)) }));
      XLSX.utils.book_append_sheet(wb, ws, s.name.slice(0, 31));
    }
    return new Blob([XLSX.write(wb, { bookType: "xlsx", type: "array" })], { type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" });
  }

  window.CLAB = Object.assign(window.CLAB || {}, { buildWide, buildHistory, toCsv, toXlsx, download });
})();
