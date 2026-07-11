-- Idempotent fix: adds missing columns to gyms table whether it
-- pre-existed (from another app) or was just created without them.
-- Safe to run multiple times.

create table if not exists gyms (
    id uuid primary key default gen_random_uuid()
);

alter table gyms add column if not exists name text;
alter table gyms add column if not exists slug text;
alter table gyms add column if not exists status text not null default 'active';
alter table gyms add column if not exists subscription_status text not null default 'trial';
alter table gyms add column if not exists created_at timestamptz not null default now();

-- Add the unique constraint on slug only if it isn't already there
do $$
begin
    if not exists (
        select 1 from pg_constraint where conname = 'gyms_slug_key'
    ) then
        alter table gyms add constraint gyms_slug_key unique (slug);
    end if;
end $$;

-- Same defensive pattern for admins, in case it also pre-existed partially
create table if not exists admins (
    id uuid primary key default gen_random_uuid()
);

alter table admins add column if not exists gym_id uuid references gyms(id) on delete cascade;
alter table admins add column if not exists email text;
alter table admins add column if not exists password_hash text;
alter table admins add column if not exists role text not null default 'gym_admin';
alter table admins add column if not exists disabled boolean not null default false;
alter table admins add column if not exists created_at timestamptz not null default now();

do $$
begin
    if not exists (
        select 1 from pg_constraint where conname = 'admins_email_key'
    ) then
        alter table admins add constraint admins_email_key unique (email);
    end if;
end $$;

-- members.login_code, in case it also didn't take
alter table members add column if not exists login_code text;

do $$
begin
    if not exists (
        select 1 from pg_constraint where conname = 'members_login_code_key'
    ) then
        alter table members add constraint members_login_code_key unique (login_code);
    end if;
end $$;

-- members.expiry_date, needed by Membership Payment + dashboard "expiring soon"
alter table members add column if not exists expiry_date date;

-- New payments table for Membership Payment feature
create table if not exists payments (
    id uuid primary key default gen_random_uuid(),
    gym_id uuid references gyms(id) on delete cascade,
    member_id uuid references members(id) on delete cascade,
    amount numeric not null,
    plan text not null,
    months int not null,
    payment_method text not null,
    transaction_id text,
    created_at timestamptz not null default now()
);

