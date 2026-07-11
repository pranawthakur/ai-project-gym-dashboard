-- Additive schema for gym admin dashboard.
-- Does NOT touch your existing members/plans tables.
-- Safe to run against the same Supabase project as promptgen-backend.

create table if not exists gyms (
    id uuid primary key default gen_random_uuid(),
    name text not null,
    slug text unique not null,              -- e.g. "goldgym" -> goldgym.yourapp.com
    status text not null default 'active',  -- active | suspended | deactivated
    subscription_status text not null default 'trial',  -- trial | active | grace | suspended
    created_at timestamptz not null default now()
);

create table if not exists admins (
    id uuid primary key default gen_random_uuid(),
    gym_id uuid references gyms(id) on delete cascade,  -- null = developer (platform-level)
    email text unique not null,
    password_hash text not null,
    role text not null default 'gym_admin',  -- gym_admin | developer
    disabled boolean not null default false,
    created_at timestamptz not null default now()
);

-- Your existing `members` table already has gym_id per membership.py.
-- If it doesn't have a login `code` column yet, add it:
alter table members add column if not exists login_code text unique;
