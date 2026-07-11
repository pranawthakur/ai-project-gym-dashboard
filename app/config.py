from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    supabase_url: str
    supabase_service_role_key: str
    jwt_secret: str          # new secret, separate from Supabase's own — this app issues its own tokens
    jwt_expire_minutes: int = 60 * 24  # 1 day

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
