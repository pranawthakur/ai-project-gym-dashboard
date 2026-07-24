"""
Meta WhatsApp Cloud API client.

IMPORTANT — templates required:
Business-initiated messages (new-member code, payment confirmation, overdue
reminder) are NOT free-form text. Meta blocks that outside a 24h window the
*member* opens by messaging the gym first. You must pre-register and get
these 3 templates approved in Meta Business Manager > WhatsApp Manager
(utility category, ~few hours to a day to approve) before any of this works:

  member_welcome
    "Welcome to {{1}}! Your login code is {{2}}. Log in here: {{3}}"

  payment_confirmation
    "Hi {{1}}, we've received your payment of ₹{{2}} for {{3}}. Your
     membership is valid until {{4}}. View your invoice: {{5}}"

  payment_overdue
    "Hi {{1}}, your {{2}} membership payment of ₹{{3}} is overdue. Please
     renew at the earliest to avoid interruption to your access."

Template names/param counts here must match exactly what got approved —
a mismatch fails the send (visible in the returned response, not an
exception raised here).
"""
import logging
import re

import httpx

from app.config import settings

logger = logging.getLogger("whatsapp")

GRAPH_API_VERSION = "v22.0"


def _normalize_phone(phone: str | None) -> str | None:
    """
    Coerce a stored phone value (usually a bare 10-digit Indian number)
    into WhatsApp's expected format: country code + number, digits only,
    no '+', no leading 0. Returns None instead of raising on anything
    unusable — a bad number must never block member creation or payment
    recording.
    """
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    if not digits:
        return None
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 10:
        digits = settings.whatsapp_default_country_code + digits
    elif len(digits) == 11 and digits.startswith("0"):
        digits = settings.whatsapp_default_country_code + digits[1:]
    # else: assume it already includes a country code
    if len(digits) < 11 or len(digits) > 15:
        return None
    return digits


def send_template(phone: str | None, template_name: str, params: list[str], lang: str = "en") -> dict:
    """
    Fire a WhatsApp template message. Never raises — always returns a dict,
    so callers (add_member, mark_paid, etc.) can fire-and-forget without a
    try/except of their own and without WhatsApp being down ever breaking
    the actual gym-admin action.
    """
    if not settings.whatsapp_token or not settings.whatsapp_phone_number_id:
        logger.info("WhatsApp not configured — skipped send to %r (%s)", phone, template_name)
        return {"ok": False, "error": "whatsapp not configured"}

    to = _normalize_phone(phone)
    if not to:
        logger.warning("WhatsApp send skipped — unusable phone %r (%s)", phone, template_name)
        return {"ok": False, "error": "invalid phone"}

    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{settings.whatsapp_phone_number_id}/messages"
    components = [{"type": "body", "parameters": [{"type": "text", "text": str(p)} for p in params]}] if params else []
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {"name": template_name, "language": {"code": lang}, "components": components},
    }
    try:
        resp = httpx.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {settings.whatsapp_token}"},
            timeout=10,
        )
        data = resp.json()
        if resp.status_code >= 400:
            logger.error("WhatsApp send failed (%s) to %r: %s", resp.status_code, phone, data)
            return {"ok": False, "error": data}
        return {"ok": True, "response": data}
    except Exception as e:
        logger.error("WhatsApp send exception to %r: %s", phone, e)
        return {"ok": False, "error": str(e)}
