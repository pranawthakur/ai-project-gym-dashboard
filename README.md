# Gym Admin Dashboard — standalone

This is intentionally isolated from `promptgen-backend`. Nothing here touches your
live app. It talks to the SAME Supabase project (same members table) but adds
two new tables (`gyms`, `admins`) and a `login_code` column on `members`.

## Setup

1. `cd admin-dashboard-backend`
2. `pip install -r requirements.txt`
3. Copy `.env.example` to `.env`, fill in your Supabase URL + service role key
   (same values as `promptgen-backend/.env`), and set a new `JWT_SECRET`.
4. Run `schema.sql` in the Supabase SQL editor (additive only, safe to run).
5. `python seed_first_admin.py` — creates one test gym + one gym admin login.
6. `uvicorn app.main:app --reload --port 8001`

## Test flow (do this before building any UI)

```bash
# 1. Login
curl -X POST http://localhost:8001/admin/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@testgym.com","password":"changeme123"}'
# -> copy the access_token from the response

# 2. Dashboard (use the token from step 1)
curl http://localhost:8001/admin/dashboard \
  -H "Authorization: Bearer <token>"

# 3. Add a member
curl -X POST http://localhost:8001/admin/members \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"name":"Test Member","phone":"9999999999"}'
# -> response includes the generated 8-digit login_code

# 4. List members
curl http://localhost:8001/admin/members \
  -H "Authorization: Bearer <token>"
```

If all four work, the gym admin side is proven. Next: dev dashboard (same
pattern — `role=developer`, `gym_id=null`, sees across all gyms), built as
another isolated folder the same way. Only after both are proven do we wire
either of them into `promptgen-backend`.

## Frontend

`frontend/admin-login.html` and `frontend/admin-dashboard.html` — plain HTML/CSS/JS,
same visual theme as the main app (dark violet, `#a855f7`→`#6366f1` gradient, Inter + Baloo 2).
No build step, no framework — open directly in a browser or serve with any static server.

Both files point at `BACKEND_URL = "http://localhost:8001"` at the top of their `<script>`
block — change that one line when you deploy the backend somewhere real.

Flow: `admin-login.html` → POST `/admin/login` → stores JWT in `localStorage` →
redirects to `admin-dashboard.html`, which reads stats from `/admin/dashboard`,
lists members from `/admin/members`, and adds members (generating the 8-digit code)
via POST `/admin/members`. A 401/403 from the backend bounces back to the login page.

To test: run the backend (`uvicorn app.main:app --reload --port 8001`), then open
`frontend/admin-login.html` in a browser and log in with the seeded test account.
