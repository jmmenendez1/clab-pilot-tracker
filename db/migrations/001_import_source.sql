-- Provenance for pilots loaded from retrospective workbooks (run once; safe to re-run).
alter table pilots add column if not exists import_source text;
