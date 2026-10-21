// Dry run of the importer on workbooks: node import/test_import.js file1.xlsm file2.xlsm …
const path = require("path"), fs = require("fs");
const { importWorkbook } = require("../web/import.js");
const catalog = JSON.parse(fs.readFileSync(path.join(__dirname, "../catalog/framework_catalog.json"), "utf8"));
for (const f of process.argv.slice(2)) {
  const r = importWorkbook(new Uint8Array(fs.readFileSync(f)), catalog, path.basename(f));
  const by = {}; for (const x of r.responses) by[x.dimension_code] = (by[x.dimension_code] || 0) + 1;
  console.log(`\n== ${path.basename(f)}\n   ${r.pilot.tc_code} | ${r.pilot.name} | ${r.pilot.city}, ${r.pilot.country} | ${r.pilot.pilot_type}/${r.pilot.attribution_category} | ${r.pilot.date_started} → ${r.pilot.date_ended}`);
  console.log(`   hypotheses: ${r.hypotheses.length} | responses: ${r.responses.length} | by dim: ${Object.entries(by).map(([k, v]) => k + ":" + v).join(" ")}`);
  console.log(`   statuses: ${JSON.stringify(r.responses.reduce((a, x) => (a[x.status] = (a[x.status] || 0) + 1, a), {}))}`);
  for (const w of r.warnings) console.log("   ! " + w);
  if (process.env.DUMP) for (const x of r.responses) console.log(`   ${x.dimension_code} ${x.field_key} h${x.hypothesis_index ?? "-"} [${x.stage}] ${x.status} ${String(x.value_text || "").slice(0, 60)} | ${x.note || ""}`);
}
