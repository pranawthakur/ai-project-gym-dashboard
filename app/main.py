import random
import string
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.cors import CORSMiddleware
from openpyxl import Workbook

from app.db import supabase
from app.security import verify_password, hash_password
from app.auth import create_token, require_role, get_current_admin
from app.config import settings

app = FastAPI(title="Gym Admin Dashboard (standalone)")

# wide open for local testing — tighten before this touches the real frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# Safety net: any unhandled exception anywhere still returns JSON with
# CORS headers, instead of a bare 500 that the browser reports as a
# CORS error (because Render's raw crash page skips CORSMiddleware).
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"detail": f"Unhandled server error: {exc}"},
    )

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


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
    try:
        result = supabase.table("admins").select("*").eq("email", body.email).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"admins lookup failed: {e}")
    if not result.data:
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    admin = result.data[0]

    if admin.get("disabled"):
        raise HTTPException(status_code=403, detail="This admin account is disabled.")

    if not verify_password(body.password, admin["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    token = create_token(sub=admin["id"], role=admin["role"], gym_id=admin.get("gym_id"))
    return {"access_token": token, "role": admin["role"], "gym_id": admin.get("gym_id")}


# ── DEV-ONLY signup — creates a gym + first admin in one step.
# NOTE: secret-header gate removed for now (demo/solo use only). Before
# any real user other than you can reach this URL, put a gate back here —
# right now anyone with the link can create unlimited gyms/admins.
class SignupRequest(BaseModel):
    gym_name: str
    admin_email: str
    admin_password: str


def generate_placeholder_slug() -> str:
    # No domain/URL system yet — this just satisfies the DB's NOT NULL
    # slug columns internally. Not shown to the user, not meant to be a
    # real URL. Revisit once real subdomain routing exists.
    return "gym-" + "".join(random.choices(string.ascii_lowercase + string.digits, k=8))


@app.post("/admin/signup")
def admin_signup(body: SignupRequest):
    placeholder_slug = generate_placeholder_slug()

    try:
        existing_email = supabase.table("admins").select("id").eq("email", body.admin_email).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"admins.email lookup failed: {e}")
    if existing_email.data:
        raise HTTPException(status_code=400, detail="That email is already registered.")

    try:
        gym = supabase.table("gyms").insert({
            "name": body.gym_name,
            "slug": placeholder_slug,
            # pre-existing column on the live table, NOT NULL, no default
            "signup_slug": placeholder_slug,
        }).execute()
        gym_id = gym.data[0]["id"]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"gyms insert failed: {e}")

    try:
        admin = supabase.table("admins").insert({
            "gym_id": gym_id,
            "email": body.admin_email,
            "password_hash": hash_password(body.admin_password),
            "role": "gym_admin",
        }).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"admins insert failed: {e}")

    token = create_token(sub=admin.data[0]["id"], role="gym_admin", gym_id=gym_id)
    return {"access_token": token, "role": "gym_admin", "gym_id": gym_id}


# ── Dashboard ────────────────────────────────────────────────────────────
@app.get("/admin/dashboard")
def dashboard(admin: dict = Depends(require_role("gym_admin"))):
    gym_id = admin["gym_id"]

    try:
        members = supabase.table("members").select("id", count="exact").eq("gym_id", gym_id).execute()
        active_members = supabase.table("members").select("id", count="exact") \
            .eq("gym_id", gym_id).eq("status", "active").execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"dashboard member counts failed: {e}")

    today = datetime.now(timezone.utc).date()
    week_out = today + timedelta(days=7)
    try:
        expiring = supabase.table("members").select("id", count="exact") \
            .eq("gym_id", gym_id).gte("expiry_date", today.isoformat()) \
            .lte("expiry_date", week_out.isoformat()).execute()
        expiring_count = expiring.count or 0
    except Exception:
        # expiry_date column may not exist yet if payments haven't been wired up
        expiring_count = 0

    try:
        todays_payments = supabase.table("payments").select("amount") \
            .eq("gym_id", gym_id).gte("created_at", today.isoformat()).execute()
        todays_revenue = sum(p["amount"] for p in todays_payments.data) if todays_payments.data else 0
    except Exception:
        # payments table may not exist yet
        todays_revenue = 0

    return {
        "gym_id": gym_id,
        "total_members": members.count or 0,
        "active_members": active_members.count or 0,
        "expiring_memberships": expiring_count,
        "todays_revenue": todays_revenue,
        # not tracked yet — need a login-log table to populate these for real
        "todays_logins": 0,
        "pending_renewals": 0,
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
    try:
        for _ in range(5):
            code = generate_login_code()
            existing = supabase.table("members").select("id").eq("login_code", code).execute()
            if not existing.data:
                break
        else:
            raise HTTPException(status_code=500, detail="Could not generate a unique code, try again.")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"login_code collision check failed: {e}")

    try:
        result = supabase.table("members").insert({
            "gym_id": gym_id,
            "name": body.name,
            "phone": body.phone,
            "email": body.email,
            "login_code": code,
            "status": "active",
        }).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"members insert failed: {e}")

    # TODO: trigger WhatsApp send here once messaging provider is wired up

    return {"member": result.data[0], "login_code": code}


# ── Member list with search/filter ──────────────────────────────────────
@app.get("/admin/members")
def list_members(
    admin: dict = Depends(require_role("gym_admin")),
    search: str | None = None,
    status: str | None = None,
):
    gym_id = admin["gym_id"]
    try:
        query = supabase.table("members").select("*").eq("gym_id", gym_id)
        if status:
            query = query.eq("status", status)
        result = query.execute()
        members = result.data or []
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"members list query failed: {e}")

    if search:
        s = search.lower()
        members = [
            m for m in members
            if s in (m.get("name") or "").lower()
            or s in (m.get("phone") or "")
            or s in (m.get("email") or "").lower()
        ]

    return {"members": members}


# ── Edit member ──────────────────────────────────────────────────────────
class EditMemberRequest(BaseModel):
    name: str | None = None
    phone: str | None = None
    email: str | None = None


@app.patch("/admin/members/{member_id}")
def edit_member(member_id: str, body: EditMemberRequest, admin: dict = Depends(require_role("gym_admin"))):
    gym_id = admin["gym_id"]
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update.")

    try:
        result = supabase.table("members").update(updates).eq("id", member_id).eq("gym_id", gym_id).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"member update failed: {e}")
    if not result.data:
        raise HTTPException(status_code=404, detail="Member not found in your gym.")
    return {"member": result.data[0]}


# ── Suspend / reactivate member ─────────────────────────────────────────
@app.post("/admin/members/{member_id}/suspend")
def suspend_member(member_id: str, admin: dict = Depends(require_role("gym_admin"))):
    gym_id = admin["gym_id"]
    try:
        result = supabase.table("members").update({"status": "suspended"}) \
            .eq("id", member_id).eq("gym_id", gym_id).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"suspend failed: {e}")
    if not result.data:
        raise HTTPException(status_code=404, detail="Member not found in your gym.")
    return {"member": result.data[0]}


@app.post("/admin/members/{member_id}/reactivate")
def reactivate_member(member_id: str, admin: dict = Depends(require_role("gym_admin"))):
    gym_id = admin["gym_id"]
    try:
        result = supabase.table("members").update({"status": "active"}) \
            .eq("id", member_id).eq("gym_id", gym_id).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"reactivate failed: {e}")
    if not result.data:
        raise HTTPException(status_code=404, detail="Member not found in your gym.")
    return {"member": result.data[0]}


# ── Delete member ────────────────────────────────────────────────────────
@app.delete("/admin/members/{member_id}")
def delete_member(member_id: str, admin: dict = Depends(require_role("gym_admin"))):
    gym_id = admin["gym_id"]
    try:
        result = supabase.table("members").delete().eq("id", member_id).eq("gym_id", gym_id).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"delete failed: {e}")
    if not result.data:
        raise HTTPException(status_code=404, detail="Member not found in your gym.")
    return {"deleted": True, "member_id": member_id}


# ── Regenerate login code ───────────────────────────────────────────────
@app.post("/admin/members/{member_id}/regenerate-code")
def regenerate_code(member_id: str, admin: dict = Depends(require_role("gym_admin"))):
    gym_id = admin["gym_id"]

    try:
        existing_member = supabase.table("members").select("id").eq("id", member_id).eq("gym_id", gym_id).execute()
        if not existing_member.data:
            raise HTTPException(status_code=404, detail="Member not found in your gym.")

        for _ in range(5):
            code = generate_login_code()
            clash = supabase.table("members").select("id").eq("login_code", code).execute()
            if not clash.data:
                break
        else:
            raise HTTPException(status_code=500, detail="Could not generate a unique code, try again.")

        result = supabase.table("members").update({"login_code": code}).eq("id", member_id).execute()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"regenerate code failed: {e}")

    # TODO: send new code via WhatsApp once messaging provider is wired up
    return {"member": result.data[0], "login_code": code}


# ── Membership Payment ──────────────────────────────────────────────────
class MarkPaidRequest(BaseModel):
    amount: float
    plan: str
    months: int
    payment_method: str
    transaction_id: str | None = None


@app.post("/admin/members/{member_id}/payments")
def mark_paid(member_id: str, body: MarkPaidRequest, admin: dict = Depends(require_role("gym_admin"))):
    gym_id = admin["gym_id"]

    try:
        member = supabase.table("members").select("id").eq("id", member_id).eq("gym_id", gym_id).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"member lookup failed: {e}")
    if not member.data:
        raise HTTPException(status_code=404, detail="Member not found in your gym.")

    expiry_date = (datetime.now(timezone.utc) + timedelta(days=30 * body.months)).date().isoformat()

    try:
        payment = supabase.table("payments").insert({
            "gym_id": gym_id,
            "member_id": member_id,
            "amount": body.amount,
            "plan": body.plan,
            "months": body.months,
            "payment_method": body.payment_method,
            "transaction_id": body.transaction_id,
        }).execute()
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"payments insert failed (does the 'payments' table exist yet? see schema.sql): {e}"
        )

    try:
        supabase.table("members").update({"expiry_date": expiry_date}).eq("id", member_id).execute()
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"member expiry_date update failed (does members.expiry_date exist yet?): {e}"
        )

    # TODO: generate invoice PDF (ReportLab/WeasyPrint) and send via WhatsApp
    # once those providers are wired up — flagged in flowchart, not built yet.
    return {"payment": payment.data[0], "new_expiry_date": expiry_date}


# ── Export members to Excel ─────────────────────────────────────────────
@app.get("/admin/export/members")
def export_members(admin: dict = Depends(require_role("gym_admin"))):
    gym_id = admin["gym_id"]
    try:
        result = supabase.table("members").select("*").eq("gym_id", gym_id).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"export query failed: {e}")
    members = result.data or []

    wb = Workbook()
    ws = wb.active
    ws.title = "Members"

    headers = ["Name", "Phone", "Email", "Login Code", "Status", "Expiry Date"]
    ws.append(headers)

    for m in members:
        ws.append([
            m.get("name", ""),
            m.get("phone", ""),
            m.get("email", ""),
            m.get("login_code", ""),
            m.get("status", ""),
            m.get("expiry_date", ""),
        ])

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=members_export.xlsx"},
    )
