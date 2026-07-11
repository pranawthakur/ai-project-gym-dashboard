import random
import string

from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.cors import CORSMiddleware

from app.db import supabase
from app.security import verify_password
from app.auth import create_token, require_role, get_current_admin

app = FastAPI(title="Gym Admin Dashboard (standalone)")

# wide open for local testing — tighten before this touches the real frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

templates = Jinja2Templates(directory="app/templates")


@app.get("/health")
def health():
    return {"status": "ok"}


# ── Pages (plain HTML, JS calls the JSON API above via fetch) ──────────────
@app.get("/admin/login-page", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.get("/admin/dashboard-page", response_class=HTMLResponse)
def dashboard_page(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})


# ── Login ────────────────────────────────────────────────────────────────
class LoginRequest(BaseModel):
    email: str
    password: str


@app.post("/admin/login")
def admin_login(body: LoginRequest):
    result = supabase.table("admins").select("*").eq("email", body.email).execute()
    if not result.data:
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    admin = result.data[0]

    if admin.get("disabled"):
        raise HTTPException(status_code=403, detail="This admin account is disabled.")

    if not verify_password(body.password, admin["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    token = create_token(sub=admin["id"], role=admin["role"], gym_id=admin.get("gym_id"))
    return {"access_token": token, "role": admin["role"], "gym_id": admin.get("gym_id")}


# ── Dashboard (stub numbers for now — wire to real counts once members exist) ──
@app.get("/admin/dashboard")
def dashboard(admin: dict = Depends(require_role("gym_admin"))):
    gym_id = admin["gym_id"]

    members = supabase.table("members").select("id", count="exact").eq("gym_id", gym_id).execute()

    return {
        "gym_id": gym_id,
        "total_members": members.count or 0,
        # placeholders — fill in once payments/login-logs tables exist
        "todays_logins": 0,
        "active_members": 0,
        "expiring_memberships": 0,
        "pending_renewals": 0,
        "todays_revenue": 0,
    }


# ── Add Member (generates the 8-digit login code) ──────────────────────────
class AddMemberRequest(BaseModel):
    name: str
    phone: str
    email: str | None = None


def generate_login_code() -> str:
    return "".join(random.choices(string.digits, k=8))


@app.post("/admin/members")
def add_member(body: AddMemberRequest, admin: dict = Depends(require_role("gym_admin"))):
    gym_id = admin["gym_id"]

    # retry on the rare collision — login_code is unique
    for _ in range(5):
        code = generate_login_code()
        existing = supabase.table("members").select("id").eq("login_code", code).execute()
        if not existing.data:
            break
    else:
        raise HTTPException(status_code=500, detail="Could not generate a unique code, try again.")

    result = supabase.table("members").insert({
        "gym_id": gym_id,
        "name": body.name,
        "phone": body.phone,
        "email": body.email,
        "login_code": code,
        "status": "active",
    }).execute()

    # TODO: trigger WhatsApp send here once messaging provider is wired up

    return {"member": result.data[0], "login_code": code}


# ── Member list (search/filter comes later, this is the base list) ─────────
@app.get("/admin/members")
def list_members(admin: dict = Depends(require_role("gym_admin"))):
    gym_id = admin["gym_id"]
    result = supabase.table("members").select("*").eq("gym_id", gym_id).execute()
    return {"members": result.data}
