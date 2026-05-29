from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://postgres:postgres@db:5432/frontier"

    analyst_version: str = "v1"

    backend_host: str = "0.0.0.0"
    backend_port: int = 8765

    cors_origins: str = ""

    purge_after_days: int = 180

    # GPU rental monitor — raw-snapshot archive dir (mounted volume) + optional source keys.
    # gpu_raw_dir is where each poll's raw response is gzipped for later reprocessing.
    # vast_api_key is REQUIRED for ToS-compliant Vast.ai collection (Vast bans anonymous
    # systematic retrieval); computeprices_api_key only raises the free rate limit.
    gpu_raw_dir: str = "/data/gpu_raw"
    vast_api_key: str = ""
    computeprices_api_key: str = ""

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
