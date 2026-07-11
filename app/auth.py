from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import jwt, JWTError

from app.config import settings

bearer_scheme = HTTPBearer()


def create_token(sub: str, role: str, gym_id: str | None = None) -> str:
    """
    role: 'gym_admin' | 'developer'
    gym_id: required for gym_admin, None for developer (platform-wide access)
    """
    payload = {
        "sub": sub,
        "role": role,
        "gym_id": gym_id,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def get_current_admin(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> dict:
    token = credentials.credentials
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired token: {e}",
        )
    return payload


def require_role(required_role: str):
    """
    Usage: Depends(require_role("gym_admin")) or Depends(require_role("developer"))
    Developer tokens (role=developer) are allowed everywhere gym_admin is required too,
    since a developer can act on behalf of any gym.
    """
    def checker(admin: dict = Depends(get_current_admin)) -> dict:
        if admin.get("role") == "developer":
            return admin
        if admin.get("role") != required_role:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires role '{required_role}'.",
            )
        return admin
    return checker
