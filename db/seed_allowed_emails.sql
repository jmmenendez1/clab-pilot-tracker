-- Whitelist of team members allowed to sign in.
-- Run after schema.sql. Re-run any time to add people (upsert).
-- Only emails listed here can request a magic link.

insert into allowed_emails (email, full_name, role) values
  ('jmenendez@iadb.org', 'Juan Manuel Menéndez', 'admin')
  -- ('colleague@iadb.org', 'Full Name', 'editor'),
on conflict (email) do update
  set full_name = excluded.full_name,
      role      = excluded.role;
