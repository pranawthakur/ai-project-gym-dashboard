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

# Grace window (days) after expiry during which a membership is shown as
# "Overdue" rather than "Expired" — gives the admin a short window to chase
# payment before the member drops to fully expired.
OVERDUE_GRACE_DAYS = 15


def compute_member_status(member: dict, today) -> dict:
    """
    Derives the three status fields shown in the Member Management table
    and the payment panel, always from live data (expiry_date, status,
    admission_fee_paid) rather than a stored/stale flag.
    """
    expiry_raw = member.get("expiry_date")
    expiry = datetime.fromisoformat(expiry_raw).date() if expiry_raw else None

    admission_status = "Paid" if member.get("admission_fee_paid") else "Pending"

    if member.get("status") == "suspended":
        membership_status = "Suspended"
    elif expiry and expiry < today:
        membership_status = "Expired"
    else:
        membership_status = "Active"

    if not expiry:
        payment_status = "Pending"
    elif expiry < today:
        payment_status = "Overdue" if (today - expiry).days <= OVERDUE_GRACE_DAYS else "Expired"
    else:
        payment_status = "Paid"

    return {
        "admission_fee_status": admission_status,
        "current_month_payment_status": payment_status,
        "membership_status": membership_status,
    }


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
# Response is grouped into the four sections the admin dashboard renders:
# Membership Overview, Finance, AI Activity, Recent Activity. Each section
# is independently defensive — if a column/table isn't there yet, that
# section degrades to zeros instead of failing the whole endpoint.
@app.get("/admin/dashboard")
def dashboard(admin: dict = Depends(require_role("gym_admin"))):
    gym_id = admin["gym_id"]
    today = datetime.now(timezone.utc).date()
    month_start = today.replace(day=1)

    # ── Membership Overview ──
    try:
        all_members = supabase.table("members").select("*").eq("gym_id", gym_id).execute()
        member_rows = all_members.data or []
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"dashboard member query failed: {e}")

    total_members = len(member_rows)
    active_members = 0
    expired_members = 0
    new_members = 0
    pending_admission_fees = 0
    pending_membership_payments = 0

    for m in member_rows:
        statuses = compute_member_status(m, today)
        if statuses["membership_status"] == "Expired":
            expired_members += 1
        elif statuses["membership_status"] == "Active":
            active_members += 1
        if statuses["admission_fee_status"] == "Pending":
            pending_admission_fees += 1
        if statuses["current_month_payment_status"] in ("Pending", "Overdue"):
            pending_membership_payments += 1
        created_at = m.get("created_at")
        if created_at:
            try:
                created_date = datetime.fromisoformat(created_at.replace("Z", "+00:00")).date()
                if created_date >= month_start:
                    new_members += 1
            except Exception:
                pass

    # ── Finance ──
    try:
        todays_payments = supabase.table("payments").select("amount") \
            .eq("gym_id", gym_id).gte("created_at", today.isoformat()).execute()
        todays_revenue = sum(p["amount"] for p in todays_payments.data) if todays_payments.data else 0
    except Exception:
        todays_revenue = 0

    try:
        month_payments = supabase.table("payments").select("amount") \
            .eq("gym_id", gym_id).gte("created_at", month_start.isoformat()).execute()
        monthly_revenue = sum(p["amount"] for p in month_payments.data) if month_payments.data else 0
    except Exception:
        monthly_revenue = 0

    # ── AI Activity ──
    # This standalone admin dashboard doesn't own the AI workflow (that
    # lives in the separate member-facing app / ai-project-login repo), so
    # there's nothing real to query yet. Zeros here are a placeholder, not
    # a bug — wire this up once plan-generation/cache tables are shared.
    ai_activity = {
        "plans_generated": 0,
        "ai_credits_used": 0,
        "cache_hits": 0,
        "cache_misses": 0,
    }

    # ── Recent Activity ──
    try:
        recent_members = sorted(member_rows, key=lambda m: m.get("created_at") or "", reverse=True)[:5]
    except Exception:
        recent_members = []
    try:
        recent_payments_res = supabase.table("payments").select("*") \
            .eq("gym_id", gym_id).order("created_at", desc=True).limit(5).execute()
        recent_payments = recent_payments_res.data or []
    except Exception:
        recent_payments = []

    return {
        "gym_id": gym_id,
        "membership": {
            "total_members": total_members,
            "active_members": active_members,
            "expired_members": expired_members,
            "new_members": new_members,
        },
        "finance": {
            "todays_revenue": todays_revenue,
            "monthly_revenue": monthly_revenue,
            "pending_membership_payments": pending_membership_payments,
            "pending_admission_fees": pending_admission_fees,
        },
        "ai_activity": ai_activity,
        "recent_activity": {
            "recent_members": [
                {"id": m.get("id"), "name": m.get("name"), "created_at": m.get("created_at")}
                for m in recent_members
            ],
            "recent_payments": [
                {
                    "id": p.get("id"),
                    "member_id": p.get("member_id"),
                    "amount": p.get("amount"),
                    "payment_type": p.get("payment_type"),
                    "created_at": p.get("created_at"),
                }
                for p in recent_payments
            ],
            # not tracked yet — needs a login-log table
            "recent_logins": [],
        },
    }


# ── Add Member (generates the 8-digit login code) ──────────────────────────
class AddMemberRequest(BaseModel):
    name: str
    phone: str
    email: str | None = None
    membership_plan: str | None = None
    monthly_fee: float | None = None
    admission_fee_amount: float | None = None


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
        insert_fields = {
            "gym_id": gym_id,
            "name": body.name,
            "phone": body.phone,
            "email": body.email,
            "login_code": code,
            "status": "active",
        }
        if body.membership_plan:
            insert_fields["membership_plan"] = body.membership_plan
        if body.monthly_fee is not None:
            insert_fields["monthly_fee"] = body.monthly_fee
        if body.admission_fee_amount is not None:
            insert_fields["admission_fee_amount"] = body.admission_fee_amount

        result = supabase.table("members").insert(insert_fields).execute()
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

    today = datetime.now(timezone.utc).date()
    for m in members:
        m.update(compute_member_status(m, today))

    return {"members": members}


# ── Member detail (payment panel) ───────────────────────────────────────
@app.get("/admin/members/{member_id}")
def get_member_detail(member_id: str, admin: dict = Depends(require_role("gym_admin"))):
    gym_id = admin["gym_id"]
    try:
        result = supabase.table("members").select("*").eq("id", member_id).eq("gym_id", gym_id).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"member lookup failed: {e}")
    if not result.data:
        raise HTTPException(status_code=404, detail="Member not found in your gym.")
    member = result.data[0]

    today = datetime.now(timezone.utc).date()
    member.update(compute_member_status(member, today))

    try:
        history_res = supabase.table("payments").select("*") \
            .eq("member_id", member_id).order("created_at", desc=True).execute()
        history = history_res.data or []
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"payment history query failed: {e}")

    outstanding_balance = 0
    if member.get("admission_fee_amount") and not member.get("admission_fee_paid"):
        outstanding_balance += member["admission_fee_amount"]
    if member.get("current_month_payment_status") in ("Pending", "Overdue") and member.get("monthly_fee"):
        outstanding_balance += member["monthly_fee"]

    last_payment_date = history[0]["created_at"] if history else member.get("last_payment_date")

    return {
        "member": member,
        "payment_history": history,
        "outstanding_balance": outstanding_balance,
        "last_payment_date": last_payment_date,
        "next_due_date": member.get("expiry_date"),
    }


# ── Edit member ──────────────────────────────────────────────────────────
class EditMemberRequest(BaseModel):
    name: str | None = None
    phone: str | None = None
    email: str | None = None
    membership_plan: str | None = None
    monthly_fee: float | None = None
    admission_fee_amount: float | None = None


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


# ── Mark Admission Fee Paid ──────────────────────────────────────────────
class AdmissionFeePaidRequest(BaseModel):
    amount: float
    payment_method: str
    transaction_id: str | None = None
    notes: str | None = None


@app.post("/admin/members/{member_id}/admission-fee/pay")
def pay_admission_fee(member_id: str, body: AdmissionFeePaidRequest, admin: dict = Depends(require_role("gym_admin"))):
    gym_id = admin["gym_id"]
    try:
        member = supabase.table("members").select("id").eq("id", member_id).eq("gym_id", gym_id).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"member lookup failed: {e}")
    if not member.data:
        raise HTTPException(status_code=404, detail="Member not found in your gym.")

    try:
        payment = supabase.table("payments").insert({
            "gym_id": gym_id,
            "member_id": member_id,
            "amount": body.amount,
            "plan": "Admission Fee",
            "months": 0,
            "payment_method": body.payment_method,
            "transaction_id": body.transaction_id,
            "payment_type": "admission",
            "notes": body.notes,
        }).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"admission fee payment insert failed: {e}")

    try:
        updated = supabase.table("members").update({
            "admission_fee_paid": True,
            "admission_fee_amount": body.amount,
        }).eq("id", member_id).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"member admission_fee_paid update failed: {e}")

    return {"payment": payment.data[0], "member": updated.data[0]}


# ── Mark Monthly Payment Paid ────────────────────────────────────────────
class MonthlyPaymentRequest(BaseModel):
    amount: float
    months: int = 1
    plan: str | None = None
    payment_method: str
    transaction_id: str | None = None
    notes: str | None = None


@app.post("/admin/members/{member_id}/monthly-payment/pay")
def pay_monthly_membership(member_id: str, body: MonthlyPaymentRequest, admin: dict = Depends(require_role("gym_admin"))):
    gym_id = admin["gym_id"]
    try:
        result = supabase.table("members").select("*").eq("id", member_id).eq("gym_id", gym_id).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"member lookup failed: {e}")
    if not result.data:
        raise HTTPException(status_code=404, detail="Member not found in your gym.")
    member = result.data[0]

    today = datetime.now(timezone.utc).date()
    current_expiry = datetime.fromisoformat(member["expiry_date"]).date() if member.get("expiry_date") else None
    # Renewing extends from the later of today or the current expiry, so
    # paying early doesn't lose the remaining paid-for days.
    base_date = current_expiry if current_expiry and current_expiry > today else today
    new_expiry = (base_date + timedelta(days=30 * body.months)).isoformat()

    try:
        payment = supabase.table("payments").insert({
            "gym_id": gym_id,
            "member_id": member_id,
            "amount": body.amount,
            "plan": body.plan or member.get("membership_plan") or "Membership",
            "months": body.months,
            "payment_method": body.payment_method,
            "transaction_id": body.transaction_id,
            "payment_type": "monthly",
            "notes": body.notes,
        }).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"monthly payment insert failed: {e}")

    update_fields = {"expiry_date": new_expiry, "last_payment_date": today.isoformat()}
    if body.plan:
        update_fields["membership_plan"] = body.plan
    if body.months:
        update_fields["monthly_fee"] = body.amount / body.months

    try:
        updated = supabase.table("members").update(update_fields).eq("id", member_id).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"member expiry update failed: {e}")

    return {"payment": payment.data[0], "member": updated.data[0], "new_expiry_date": new_expiry}


# ── Extend Membership (no new payment — admin override / goodwill extension) ──
class ExtendMembershipRequest(BaseModel):
    months: int = 1
    notes: str | None = None


@app.post("/admin/members/{member_id}/extend")
def extend_membership(member_id: str, body: ExtendMembershipRequest, admin: dict = Depends(require_role("gym_admin"))):
    gym_id = admin["gym_id"]
    try:
        result = supabase.table("members").select("*").eq("id", member_id).eq("gym_id", gym_id).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"member lookup failed: {e}")
    if not result.data:
        raise HTTPException(status_code=404, detail="Member not found in your gym.")
    member = result.data[0]

    today = datetime.now(timezone.utc).date()
    current_expiry = datetime.fromisoformat(member["expiry_date"]).date() if member.get("expiry_date") else today
    base_date = current_expiry if current_expiry > today else today
    new_expiry = (base_date + timedelta(days=30 * body.months)).isoformat()

    try:
        updated = supabase.table("members").update({"expiry_date": new_expiry}).eq("id", member_id).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"membership extend failed: {e}")

    return {"member": updated.data[0], "new_expiry_date": new_expiry}


# ── Generate Invoice ─────────────────────────────────────────────────────
# No PDF library is wired into this project yet, so this returns a clean,
# printable HTML invoice (the browser's own "Print to PDF" covers the PDF
# need without adding a new dependency). Defaults to the most recent
# payment; pass ?payment_id= to invoice a specific one.
@app.get("/admin/members/{member_id}/invoice", response_class=HTMLResponse)
def generate_invoice(member_id: str, payment_id: str | None = None, admin: dict = Depends(require_role("gym_admin"))):
    gym_id = admin["gym_id"]
    try:
        member_res = supabase.table("members").select("*").eq("id", member_id).eq("gym_id", gym_id).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"member lookup failed: {e}")
    if not member_res.data:
        raise HTTPException(status_code=404, detail="Member not found in your gym.")
    member = member_res.data[0]

    try:
        query = supabase.table("payments").select("*").eq("member_id", member_id)
        if payment_id:
            query = query.eq("id", payment_id)
        else:
            query = query.order("created_at", desc=True).limit(1)
        payment_res = query.execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"payment lookup failed: {e}")
    if not payment_res.data:
        raise HTTPException(status_code=404, detail="No payment found to invoice.")
    payment = payment_res.data[0]

    html = f"""
    <!DOCTYPE html>
    <html><head><meta charset="UTF-8"><title>Invoice</title>
    <style>
      body {{ font-family: Arial, sans-serif; color: #111; max-width: 640px; margin: 40px auto; }}
      h1 {{ font-size: 20px; margin-bottom: 4px; }}
      table {{ width: 100%; border-collapse: collapse; margin-top: 20px; }}
      td, th {{ padding: 8px; border-bottom: 1px solid #ddd; text-align: left; font-size: 14px; }}
      .total {{ font-weight: bold; font-size: 16px; }}
    </style></head>
    <body>
      <h1>Payment Invoice</h1>
      <div>Member: {member.get('name', '')}</div>
      <div>Phone: {member.get('phone', '')}</div>
      <div>Membership Plan: {member.get('membership_plan') or '-'}</div>
      <table>
        <tr><th>Date</th><th>Type</th><th>Method</th><th>Transaction ID</th><th>Amount</th></tr>
        <tr>
          <td>{payment.get('created_at', '')[:10]}</td>
          <td>{'Admission' if payment.get('payment_type') == 'admission' else 'Monthly'}</td>
          <td>{payment.get('payment_method', '')}</td>
          <td>{payment.get('transaction_id') or '-'}</td>
          <td>{payment.get('amount', 0)}</td>
        </tr>
      </table>
      <p class="total">Total Paid: {payment.get('amount', 0)}</p>
    </body></html>
    """
    return HTMLResponse(content=html)


# ── Growth Analytics ─────────────────────────────────────────────────────
# Monthly new-members and revenue series for the last `months` calendar
# months (default 6), oldest first — feeds the Gym Growth chart on the
# dashboard. Computed from members/payments already loaded elsewhere in
# this file, so it degrades to zeros per month rather than failing if a
# table is briefly unreachable.
@app.get("/admin/analytics/growth")
def growth_analytics(months: int = 6, admin: dict = Depends(require_role("gym_admin"))):
    gym_id = admin["gym_id"]
    months = max(1, min(months, 24))

    today = datetime.now(timezone.utc).date()
    # Build the list of month buckets, oldest first, e.g. ["2026-02", ..., "2026-07"]
    buckets = []
    y, m = today.year, today.month
    for _ in range(months):
        buckets.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    buckets.reverse()
    bucket_start = buckets[0] + "-01"

    try:
        members_res = supabase.table("members").select("created_at") \
            .eq("gym_id", gym_id).gte("created_at", bucket_start).execute()
        member_rows = members_res.data or []
    except Exception:
        member_rows = []

    try:
        payments_res = supabase.table("payments").select("amount,created_at") \
            .eq("gym_id", gym_id).gte("created_at", bucket_start).execute()
        payment_rows = payments_res.data or []
    except Exception:
        payment_rows = []

    new_members = {b: 0 for b in buckets}
    revenue = {b: 0 for b in buckets}

    for row in member_rows:
        created_at = row.get("created_at")
        if not created_at:
            continue
        key = created_at[:7]
        if key in new_members:
            new_members[key] += 1

    for row in payment_rows:
        created_at = row.get("created_at")
        if not created_at:
            continue
        key = created_at[:7]
        if key in revenue:
            revenue[key] += row.get("amount") or 0

    return {
        "months": buckets,
        "new_members": [new_members[b] for b in buckets],
        "revenue": [revenue[b] for b in buckets],
    }


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
