-- Licence storage. Mirrors the live Supabase schema as introspected on
-- 2026-08-20, minus device_id (never written) and last_country (never read).
create table if not exists licenses (
  id                       uuid primary key default gen_random_uuid(),
  email                    text not null,
  full_name                text,
  phone_number             text,
  country                  text,
  license_key              text not null unique,
  sale_id                  text unique,
  is_active                boolean not null default true,
  is_beta                  boolean not null default true,
  has_completed_onboarding boolean not null default false,
  last_login               timestamptz,
  created_at               timestamptz not null default now(),
  updated_at               timestamptz not null default now()
);

-- Append-only audit of every inbound webhook payload.
create table if not exists webhook_logs (
  id          uuid primary key default gen_random_uuid(),
  received_at timestamptz not null default now(),
  payload     jsonb,
  success     boolean,
  error       text
);

-- IMMUTABLE because the index below depends on it. Licence keys are compared
-- case-insensitively with dashes and underscores ignored, matching the
-- behaviour the Supabase implementation had.
create or replace function normalize_license_key(p_key text)
returns text language sql immutable strict as $$
  select regexp_replace(upper(p_key), '[-_]', '', 'g')
$$;

-- Validation looks keys up NORMALISED, so a plain index on license_key cannot
-- serve it. Without this expression index every validation is a seq scan.
create unique index if not exists licenses_normalized_key_idx
  on licenses (normalize_license_key(license_key));

create index if not exists licenses_email_idx on licenses (email);

create or replace function set_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

drop trigger if exists licenses_set_updated_at on licenses;
create trigger licenses_set_updated_at
  before update on licenses
  for each row execute function set_updated_at();
