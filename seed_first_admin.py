"""
Run once to create your first gym + gym admin login, so you have
something to log in with before any 'create gym' UI exists.

Usage:
    python seed_first_admin.py
"""
from app.db import supabase
from app.security import hash_password

GYM_NAME = "Test Gym"
GYM_SLUG = "testgym"
ADMIN_EMAIL = "admin@testgym.com"
ADMIN_PASSWORD = "changeme123"  # change after first login, this is just for local testing

gym = supabase.table("gyms").insert({
    "name": GYM_NAME,
    "slug": GYM_SLUG,
}).execute()

gym_id = gym.data[0]["id"]

admin = supabase.table("admins").insert({
    "gym_id": gym_id,
    "email": ADMIN_EMAIL,
    "password_hash": hash_password(ADMIN_PASSWORD),
    "role": "gym_admin",
}).execute()

print(f"Created gym: {GYM_NAME} ({gym_id})")
print(f"Created admin login: {ADMIN_EMAIL} / {ADMIN_PASSWORD}")
