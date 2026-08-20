-- Least-privilege application role. Owns nothing, cannot do DDL, and cannot
-- DELETE anything. The role itself is created by migrate.ensure_app_role,
-- which supplies the password; this file only grants.
grant usage on schema public to dropbeats_app;

grant select, insert, update on licenses     to dropbeats_app;
grant insert                 on webhook_logs to dropbeats_app;
-- Deliberately no DELETE anywhere, and no UPDATE on webhook_logs: the audit
-- log is append-only at the privilege level, not merely by convention.

grant execute on function normalize_license_key(text) to dropbeats_app;

alter table licenses     enable row level security;
alter table licenses     force  row level security;
alter table webhook_logs enable row level security;
alter table webhook_logs force  row level security;

-- SELECT is unrestricted on purpose. Validation must tell "unknown key" apart
-- from "License is not active", and a policy hiding inactive rows collapses
-- those two cases into one error message. Containment here comes from the
-- grants above, not from row filtering.
create policy licenses_app_select on licenses
  for select to dropbeats_app using (true);

create policy licenses_app_insert on licenses
  for insert to dropbeats_app with check (true);

create policy licenses_app_update on licenses
  for update to dropbeats_app using (true) with check (true);

create policy webhook_logs_app_insert on webhook_logs
  for insert to dropbeats_app with check (true);
