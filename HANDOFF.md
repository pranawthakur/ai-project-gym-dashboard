# Handoff: Gym Admin Dashboard (standalone) — AI Gym Trainer SaaS

## Who this is for
Another AI assistant or developer picking up this work mid-stream. Read this
fully before touching code — several decisions here were made deliberately
(isolation from the main app, dev-only signup route, Vercel-specific path
handling) and undoing them without knowing why will cause regressions.

## Project owner context
Pranaw (19), solo-ish founder of a B2B AI fitness SaaS for Indian gyms.
Two collaborators: one on frontend, one on sales/business side. Pranaw wants
brutally direct, no-fluff technical answers — no motivational padding, no
hedging, just the actual fix and why. He is hands-on in the code himself but
is not a professional backend developer — explain deployment/env concepts
plainly, don't assume prior DevOps knowledge, but also don't over-explain
things he's already done successfully.

## The bigger picture
There are TWO separate codebases now:

1. **`ai-project-login`** (existing, live, on GitHub at
   `pranawthakur/ai-project-login`) — the actual product. FastAPI backend
   (`promptgen-backend/`) deployed on **Render**, Vercel hosts only the
   static frontend. This handles the MEMBER-facing flow: 8-digit-code
   questionnaire → AI-generated workout/diet plan (Gemini). This repo is
   NOT part of the current work — do not modify it. It's mentioned here only
   for context on tech stack conventions to stay consistent with.

2. **`admin-dashboard-backend`** (new, THIS is what's being built) — a
   fully standalone repo/backend for the **Gym Admin** and (later)
   **Developer** tiers described in a system architecture flowchart the
   user provided (`final_flow_chart.txt`, attached earlier in conversation
   history — ask the user to re-share if not present, but a summary is
   below in "Target architecture"). Deliberately built in complete isolation
   from `ai-project-login` — different repo, different `.env`, different
   port locally — but talks to the **same Supabase project** (shared DB).

## Why the isolation
The user asked to build the gym admin dashboard, test it fully standalone,
THEN combine with the main app later — specifically so the live member-facing
product never breaks while this is being built and debugged. Do not merge
this into `promptgen-backend` unless the user explicitly asks for that step.

## Target architecture (from the flowchart)
Three login tiers, each gated by JWT role:
- **Developer** (platform owner) — create/suspend gyms, manage gym admins,
  Instamojo B2B billing, platform-wide analytics, AI cost/usage analytics,
  per-gym data export, direct AI engine testing.
- **Gym Admin** — per-gym dashboard (member counts, revenue), Add Member
  (generates 8-digit login code, sends via WhatsApp), Member Management
  (search/edit/suspend/regenerate code), Membership Payment (mark paid →
  invoice PDF → WhatsApp), Excel export.
- **Member** — already exists in `ai-project-login`, logs in with 8-digit
  code, fills questionnaire, gets AI-generated plan. NOT part of this repo.

Background automation (not yet built anywhere): WhatsApp expiry reminders,
daily backups, cache/AI cost tracking, Instamojo webhook → gym suspension on
failed payment.

**Build order being followed:** Gym Admin dashboard first (current focus),
proven working standalone → Developer dashboard next, same isolated pattern
→ only then wire both into the real app.

## Current state of `admin-dashboard-backend`

### Structure
```
admin-dashboard-backend/
├── api/
│   └── index.py          # Vercel entrypoint, imports app.main:app
├── app/
│   ├── main.py            # All routes
│   ├── auth.py             # JWT issuer (HS256) + require_role() guard
│   ├── config.py           # pydantic Settings, reads .env
│   ├── db.py                # Supabase client (service role key)
│   ├── security.py           # bcrypt password hash/verify
│   └── templates/
│       ├── login.html         # Gym admin login + dev-only signup toggle
│       └── dashboard.html      # Stats + Add Member + Member table
├── schema.sql              # gyms, admins tables + login_code column on members
├── seed_first_admin.py      # One-time script: creates first gym+admin
├── requirements.txt
├── vercel.json               # Routes all requests to api/index.py
├── .env.example
├── .gitignore                # excludes .env, __pycache__, venv
└── README.md
```

### Auth design
- Custom JWT issuer, **HS256**, separate `JWT_SECRET` — deliberately NOT
  using Supabase Auth at all for this tier (Supabase Auth is only used by
  the member-facing app, and even there `auth.py` in the main repo has a
  known bug: it verifies with ES256 assuming Supabase issues ES256 tokens,
  but Supabase actually uses HS256 — this bug is UNRELATED to this repo and
  lives in `promptgen-backend/app/auth.py`, not fixed yet, not this repo's
  problem to fix unless asked).
- `create_token(sub, role, gym_id)` in `app/auth.py` — role is
  `"gym_admin"` or `"developer"`. Developer tokens bypass gym_id scoping
  everywhere (`require_role()` lets a developer through any gym_admin-gated
  route).
- Passwords hashed with `bcrypt` directly (not passlib, to avoid version
  churn).

### Database (Supabase, shared with main project)
New tables (see `schema.sql`, additive only, already run by the user):
- `gyms` (id, name, slug, status, subscription_status, created_at)
- `admins` (id, gym_id nullable, email, password_hash, role, disabled,
  created_at) — gym_id null = developer account
- `members.login_code` — new column added to the EXISTING members table
  from the main project (unique 8-digit code)

### Routes implemented in `app/main.py`
- `GET /health`
- `GET /admin/login-page` — serves login.html
- `GET /admin/dashboard-page` — serves dashboard.html
- `POST /admin/login` — `{email, password}` → JWT
- `POST /admin/signup` — **DEV-ONLY, NO AUTH GATE** — creates a gym + first
  admin in one step, returns a JWT immediately. This exists purely so the
  user can self-serve test accounts without running `seed_first_admin.py`
  each time. **MUST be deleted or gated before any real user touches this
  app** — currently anyone with the URL can create an unlimited number of
  gyms/admins. Flagged in a code comment above the route too.
- `GET /admin/dashboard` — role-gated (`require_role("gym_admin")`), returns
  member count (real, queried) + payment/login-log stats (currently
  hardcoded zeros, TODO once those tables exist)
- `POST /admin/members` — add member, generates unique 8-digit `login_code`
  with collision retry, saves to `members` table scoped to `admin["gym_id"]`.
  TODO comment marks where WhatsApp send should be triggered once a
  messaging provider is wired up.
- `GET /admin/members` — lists members for the logged-in admin's gym.

### Frontend
Plain HTML + vanilla JS (no build step, no framework) in
`app/templates/`, styled to match the EXISTING main app's theme exactly —
dark purple/indigo (`#a855f7` → `#6366f1` gradients), 'Baloo 2' + 'Inter'
fonts, "GymCoach Studio" branding, glassmorphism cards. This was copied from
`ai-project-login/dashbord.html`'s CSS variables to keep visual consistency
across the two separate codebases.

`API_BASE` is **hardcoded** at the top of the `<script>` block in both
`login.html` and `dashboard.html` as `http://localhost:8001`. **This must be
manually updated to the deployed URL** any time this moves from local to
Vercel (or elsewhere) — it is not environment-aware. This is a known rough
edge, worth fixing properly (e.g. relative URLs, since frontend and backend
are served from the same FastAPI app) before this goes further.

Login page has a collapsible "Dev only — create a test gym + admin" link
that reveals a signup form hitting `POST /admin/signup`.

## What's WORKING right now
Confirmed by the user via screenshot: running locally with
`uvicorn app.main:app --reload --port 8001`, the login page renders
correctly, styled correctly, and the local dev flow up through login page
render is functioning. Local `.env` is filled with real Supabase credentials
(same project as `ai-project-login`).

Not yet explicitly confirmed by the user: whether login itself succeeds,
whether `seed_first_admin.py` completed successfully, whether Add
Member/dashboard data loads. **Do not assume these work — verify with the
user or ask for a fresh test before building further on top.**

## What's BROKEN right now (active issue)
**Vercel deployment crashes with 500 `FUNCTION_INVOCATION_FAILED`.**

Sequence so far:
1. First deploy attempt crashed before any `vercel.json`/`api/index.py`
   existed at all (expected — Vercel had no idea how to run a plain
   `uvicorn` app). Fixed by adding:
   - `api/index.py` — imports `app.main:app`, appends project root to
     `sys.path` so `from app.main import app` resolves correctly in
     Vercel's function environment.
   - `vercel.json` — routes all paths to `api/index.py` via `@vercel/python`.
   - Changed `Jinja2Templates(directory="app/templates")` (relative,
     fragile) to an absolute path via
     `Path(__file__).parent / "templates"` in `main.py`, since Vercel's
     serverless functions don't guarantee working directory.
2. After that fix, `localhost:8001/...` continued working correctly
   (confirmed by screenshot).
3. On Vercel, hitting the bare domain root `/` correctly returns FastAPI's
   own `{"detail":"Not Found"}` — this is EXPECTED, there's no `GET /` route
   defined, not a bug.
4. Hitting `/admin/login-page` on the deployed Vercel URL currently returns
   **500 `FUNCTION_INVOCATION_FAILED`** (screenshot provided, Vercel error
   ID format `bom1::xxxx-...`). This is a real crash, not a routing issue.

**Most likely cause (not yet confirmed):** Environment variables not set in
Vercel's dashboard. `.env` is git-ignored by design and never reaches
Vercel — env vars must be manually added under Project Settings →
Environment Variables (`SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`,
`JWT_SECRET`). If missing, `pydantic_settings.BaseSettings` in
`app/config.py` throws a validation error at import time, which crashes the
serverless function immediately — matches the symptom exactly.

**Next diagnostic step (told to user, not yet done):** Have the user open
Vercel dashboard → Logs tab (or the "check the logs" link on the error page
itself), reproduce the crash, and paste the actual Python traceback. Without
that traceback, further fixes are guesses. Prime suspects in order of
likelihood:
1. Missing/unset environment variables in Vercel dashboard (most likely).
2. Stale deployment — env vars were added AFTER an earlier failed deploy,
   and the user hasn't manually triggered a redeploy since (Vercel does not
   retroactively apply new env vars to existing deployments).
3. `bcrypt` or another dependency failing to build in Vercel's Python
   runtime (less likely but possible — Vercel's `@vercel/python` runtime
   has had issues with native-extension packages historically; if env vars
   turn out fine, check this next).
4. `vercel.json` build config issue — `@vercel/python` may need
   `requirements.txt` in a specific location relative to `api/index.py`
   depending on Vercel's current runtime version; if all else fails, check
   Vercel's current official FastAPI deployment docs, since this project's
   `vercel.json` was written from general knowledge, not verified against
   Vercel's latest docs.

## Known deliberate rough edges (not bugs, just not-yet-done)
- `dashboard` stats for logins/active/expiring/pending/revenue are hardcoded
  zeros — real queries need payment and login-log tables that don't exist
  yet.
- No WhatsApp integration yet (flagged with TODO comment in `add_member`).
- No search/filter/edit/suspend on Member Management — only add + list.
- No Instamojo billing, no PDF invoices, no analytics — all later phases
  per the flowchart, not started.
- `API_BASE` hardcoded per-environment in frontend JS, not dynamic.
- `/admin/signup` has zero access control — dev convenience only.

## Immediate next action
Get the Vercel Function Logs traceback from the user for the current 500
crash, fix the root cause (check env vars first), confirm
`/admin/login-page` and a full login→dashboard→add-member flow work on the
deployed Vercel URL, THEN move to Developer dashboard (same standalone
pattern, `role="developer"`, `gym_id=null`, sees across all gyms) before any
merge into `ai-project-login`.
