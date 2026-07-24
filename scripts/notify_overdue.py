"""
Run once daily (Render Cron Job, GitHub Actions schedule, or plain crontab)
to WhatsApp every member currently in "Overdue" status a payment reminder.

    python -m scripts.notify_overdue

Dedup: members.last_overdue_notified_date is set after a successful send so
a retried/duplicate cron run the same day doesn't double-text anyone. Add
the column first (see schema.sql).
"""
from datetime import datetime, timezone

from app.db import supabase
from app.config import settings
from app.whatsapp import send_template
from app.main import compute_member_status


def run():
    today = datetime.now(timezone.utc).date()
    members = supabase.table("members").select("*").eq("status", "active").execute().data or []

    checked = 0
    sent = 0
    for m in members:
        checked += 1
        if compute_member_status(m, today)["current_month_payment_status"] != "Overdue":
            continue
        if m.get("last_overdue_notified_date") == today.isoformat():
            continue  # already pinged today
        if not m.get("phone"):
            continue

        result = send_template(
            m["phone"],
            settings.wa_template_payment_overdue,
            [m.get("name") or "there", m.get("membership_plan") or "Membership", m.get("monthly_fee") or "—"],
        )
        if result["ok"]:
            supabase.table("members").update(
                {"last_overdue_notified_date": today.isoformat()}
            ).eq("id", m["id"]).execute()
            sent += 1
        else:
            print(f"  send failed for member {m['id']}: {result['error']}")

    print(f"Overdue WhatsApp reminders: {sent} sent / {checked} members checked")


if __name__ == "__main__":
    run()
