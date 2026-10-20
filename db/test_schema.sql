-- Smoke test for schema.sql. Run in the Supabase SQL editor AFTER applying the schema,
-- seeding the whitelist and signing in once as the admin (so the profile row exists).
-- It impersonates that admin, inserts one pilot with one hypothesis and two saves of the
-- same field, checks that responses_current returns the latest value, then rolls back.
-- Expected output: one row, ok = true.

begin;

select set_config('request.jwt.claim.sub',
                  (select id::text from users where role = 'admin' order by created_at limit 1), true);
set local role authenticated;

insert into pilots (tc_code, name, city, country, pilot_type, attribution_category, mode)
values ('RG-T0000', 'Smoke test pilot', 'Test', 'Test', 'urban', 'counterfactual', 'prospective');

insert into hypotheses (pilot_id, ordinal)
select id, 1 from pilots where tc_code = 'RG-T0000';

insert into responses (pilot_id, dimension_code, field_key, hypothesis_id, stage, value_text)
select h.pilot_id, 'L3.2', 'hypothesis', h.id, 'ideation', 'first draft'
from hypotheses h join pilots p on p.id = h.pilot_id where p.tc_code = 'RG-T0000';

insert into responses (pilot_id, dimension_code, field_key, hypothesis_id, stage, value_text)
select h.pilot_id, 'L3.2', 'hypothesis', h.id, 'ideation', 'second draft'
from hypotheses h join pilots p on p.id = h.pilot_id where p.tc_code = 'RG-T0000';

select (rc.value_text = 'second draft') as ok,
       rc.value_text as current_value,
       (select count(*) from responses r where r.pilot_id = rc.pilot_id) as history_rows
from responses_current rc
join pilots p on p.id = rc.pilot_id
where p.tc_code = 'RG-T0000' and rc.field_key = 'hypothesis';

rollback;
