from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    supabase_url: str
    supabase_service_role_key: str
    jwt_secret: str          # new secret, separate from Supabase's own — this app issues its own tokens
    jwt_expire_minutes: int = 60 * 24  # 1 day
    # Gate on /admin/signup so it's not wide open on the internet.
    # SET A REAL VALUE IN RENDER ENV VARS. This default is dev-only.
    signup_secret: str = "changeme-dev-signup"
    # Base URL of the member-facing login app (ai-project-login repo's
    # Vercel deployment). Used to build the full ?gym=<slug> link shown
    # after Add Member, since a bare login code alone resolves to the
    # wrong gym on the backend (see ai-project-login/app/gym_scope.py).
    # Ugly Vercel URL for now — swap for the real domain later, nothing
    # else needs to change.
    member_frontend_url: str = ""
    # This backend's own public URL (Render deploy URL), used to build the
    # /admin/members/{id}/invoice?token=... link sent over WhatsApp — the
    # invoice route lives here, not on the member-frontend app.
    public_backend_url: str = ""

    # ── WhatsApp (Meta Cloud API) ──────────────────────────────────────
    # Leave whatsapp_token/whatsapp_phone_number_id blank to no-op sends
    # (app.whatsapp.send_template just logs and returns — nothing breaks).
    whatsapp_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_default_country_code: str = "91"
    # Names of the pre-approved Meta message templates — see app/whatsapp.py
    # header comment for the exact body text to submit for approval.
    wa_template_new_member: str = "member_welcome"
    wa_template_payment_confirmation: str = "payment_confirmation"
    wa_template_payment_overdue: str = "payment_overdue"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
