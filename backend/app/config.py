from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://postgres:postgres@db:5432/frontier"

    analyst_version: str = "v1"

    backend_host: str = "0.0.0.0"
    backend_port: int = 8765

    cors_origins: str = ""

    purge_after_days: int = 180

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
