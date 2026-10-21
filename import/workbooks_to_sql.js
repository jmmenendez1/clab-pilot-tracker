// Converts data-collection workbooks into a SQL script for the Supabase SQL editor.
//   node import/workbooks_to_sql.js out.sql file1.xlsm file2.xlsm …
// Each pilot goes in its own DO block, attributed to the first admin in `users`,
// and is skipped if a pilot with the same TC code and name already exists.
// Requires: npm i xlsx (or NODE_PATH pointing at a node_modules that has it).
const path = require("path"), fs = require("fs");
const { importWorkbook } = require("../web/import.js");
const catalog = JSON.parse(fs.readFileSync(path.join(__dirname, "../catalog/framework_catalog.json"), "utf8"));

const [out, ...files] = process.argv.slice(2);
if (!out || !files.length) { console.error("usage: node import/workbooks_to_sql.js out.sql file.xlsm …"); process.exit(1); }

const q = v => v == null ? "null" : `$imp$${String(v)}$imp$`;
const lines = [`-- Generated ${new Date().toISOString()} from ${files.length} workbook(s). Run in the Supabase SQL editor.`,
  `alter table pilots add column if not exists import_source text;`, ""];

for (const f of files) {
  const r = importWorkbook(new Uint8Array(fs.readFileSync(f)), catalog, path.basename(f));
  const p = r.pilot;
  lines.push(`-- ${p.tc_code} · ${p.name}`);
  for (const w of r.warnings) lines.push(`--   note: ${w}`);
  lines.push(`do $$`, `declare pid uuid := gen_random_uuid(); uid uuid; h uuid[] := array[]::uuid[];`, `begin`,
    `  select id into uid from users where role = 'admin' order by created_at limit 1;`,
    `  if uid is null then raise exception 'No admin user found: sign in once with the admin email first'; end if;`,
    `  if exists (select 1 from pilots where tc_code = ${q(p.tc_code)} and name = ${q(p.name)}) then raise notice 'skipped (already exists): %', ${q(p.name)}; return; end if;`,
    `  insert into pilots (id, tc_code, name, city, country, pilot_type, attribution_category, mode, problem_definition, target_population, lead, date_started, date_ended, current_stage, created_by, import_source)`,
    `  values (pid, ${q(p.tc_code)}, ${q(p.name)}, ${q(p.city)}, ${q(p.country)}, ${q(p.pilot_type)}, ${q(p.attribution_category)}, 'retrospective', ${q(p.problem_definition)}, ${q(p.target_population)}, ${q(p.lead)}, ${p.date_started ? q(p.date_started) + "::date" : "null"}, ${p.date_ended ? q(p.date_ended) + "::date" : "null"}, 'evaluation', uid, ${q(p.import_source)});`);
  r.hypotheses.forEach((h, i) => lines.push(`  h := h || gen_random_uuid(); insert into hypotheses (id, pilot_id, ordinal, label, created_by) values (h[${i + 1}], pid, ${h.ordinal}, ${q(h.label)}, uid);`));
  if (r.responses.length) {
    lines.push(`  insert into responses (pilot_id, dimension_code, field_key, hypothesis_id, stage, status, value_text, note, author_id) values`);
    lines.push(r.responses.map(x => `    (pid, ${q(x.dimension_code)}, ${q(x.field_key)}, ${x.hypothesis_index == null ? "null" : `h[${x.hypothesis_index + 1}]`}, ${q(x.stage)}, ${q(x.status)}, ${q(x.value_text)}, ${q(x.note)}, uid)`).join(",\n") + ";");
  }
  lines.push(`  raise notice 'imported %: % hypotheses, % values', ${q(p.name)}, ${r.hypotheses.length}, ${r.responses.length};`, `end $$;`, "");
  console.log(`${p.tc_code} ${p.name.slice(0, 50)} — ${r.hypotheses.length} hypotheses, ${r.responses.length} values, ${r.warnings.length} notes`);
}
fs.writeFileSync(out, lines.join("\n"));
console.log("written", out);
