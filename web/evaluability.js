/* Evaluability (completeness score) of a pilot — same rule as retro/evaluability.py (keep both in step).
 *   score = complete subdimensions / all subdimensions in the catalog (18 in catalog 1.0.0)
 *   - A subdimension is complete when every field the catalog marks `required` is filled
 *     (degree: high / medium / low; `na` does not count).
 *   - L3.2 (one record per hypothesis): at least one active hypothesis with all its required fields filled.
 *   - L2.1 (no required field): at least one cost amount — a USD subcategory, or the L2.1 notes where costs written
 *     as text land (workbook import, retrospective extraction).
 *   - Not applicable counts as incomplete: the denominator is always every subdimension (decision 2026-11-04).
 *   - Status only; machine drafts count like any other value, so the current score can be compared with the
 *     retrospective baseline stored at load time.
 * rows: current responses [{dimension_code, field_key, hypothesis_id, status, value_text, value_num, value_date}]
 * hypotheses: active hypotheses [{id}]
 */
(function () {
  const DEGREES = new Set(["high", "medium", "low"]);
  const fieldsOf = (dim, catalog) => Array.isArray(dim.fields) ? dim.fields : catalog.field_sets[dim.fields];
  const ok = r => r && r.status === "filled" && (r.field_key !== "degree" || DEGREES.has(String(r.value_text || "").toLowerCase()));

  function evaluability(catalog, rows, hypotheses) {
    const active = new Set((hypotheses || []).map(h => h.id));
    const detail = {};
    for (const dim of catalog.dimensions) {
      const req = fieldsOf(dim, catalog).filter(f => f.required).map(f => f.key);
      const rs = rows.filter(r => r.dimension_code === dim.code);
      let complete;
      if (!req.length) {
        complete = rs.some(r => ok(r) && !["opex.period", "funding_note"].includes(r.field_key));
      } else if (dim.repeatable === "hypothesis") {
        complete = [...active].some(h => req.every(k => rs.some(r => r.hypothesis_id === h && r.field_key === k && ok(r))));
      } else {
        complete = req.every(k => rs.some(r => !r.hypothesis_id && r.field_key === k && ok(r)));
      }
      detail[dim.code] = complete ? 1 : 0;
    }
    const n = Object.values(detail).reduce((a, b) => a + b, 0), total = Object.keys(detail).length;
    return { score: Math.round(n / total * 1000) / 1000, complete: n, total, by_subdimension: detail };
  }

  if (typeof module !== "undefined") module.exports = { evaluability };
  if (typeof window !== "undefined") window.CLAB = Object.assign(window.CLAB || {}, { evaluability });
})();
