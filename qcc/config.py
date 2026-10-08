from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = Path.home() / ".config" / "quality-command-center" / ".env"
load_dotenv(Path(os.getenv("QCC_ENV_FILE", str(DEFAULT_ENV_FILE))).expanduser(), override=False)


def _placeholder(value: str | None) -> bool:
    if not value:
        return True
    upper = value.upper()
    return "CHANGE_ME" in upper


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


@dataclass(frozen=True)
class Settings:
    app_env: str
    log_level: str
    release_sha: str
    database_url: str | None
    direct_database_url: str | None
    database_ssl_root_cert_file: str | None
    supabase_url: str | None
    supabase_secret_key: str | None
    ingest_bucket: str
    export_bucket: str
    public_origin: str
    allowed_hosts: tuple[str, ...]
    netlify_site_id: str | None
    netlify_jws_secret: str | None
    max_upload_bytes: int
    max_rows: int
    max_cols: int
    max_json_bytes: int
    signed_url_ttl_s: int

    @property
    def is_production_like(self) -> bool:
        return self.app_env in {"staging", "production"}

    @property
    def uses_postgres(self) -> bool:
        return bool(self.database_url and self.database_url.startswith("postgresql"))

    def validate_runtime(self) -> None:
        if self.is_production_like and not self.uses_postgres:
            raise RuntimeError("DATABASE_URL must be a resolved PostgreSQL URL in staging/production")
        if self.is_production_like:
            required = {
                "SUPABASE_URL": self.supabase_url,
                "SUPABASE_SECRET_KEY": self.supabase_secret_key,
                "NETLIFY_SITE_ID": self.netlify_site_id,
                "NETLIFY_JWS_SECRET": self.netlify_jws_secret,
            }
            missing = [name for name, value in required.items() if _placeholder(value)]
            if missing:
                raise RuntimeError(f"Unresolved production settings: {', '.join(missing)}")


def load_settings() -> Settings:
    raw_database_url = os.getenv("DATABASE_URL")
    raw_direct_url = os.getenv("DIRECT_DATABASE_URL")
    database_url = None if _placeholder(raw_database_url) else raw_database_url
    direct_url = None if _placeholder(raw_direct_url) else raw_direct_url
    allowed_hosts = tuple(
        value.strip() for value in os.getenv("ALLOWED_HOSTS", "127.0.0.1,localhost").split(",") if value.strip()
    )
    settings = Settings(
        app_env=os.getenv("APP_ENV", "development").strip().lower(),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
        release_sha=os.getenv("RELEASE_SHA", os.getenv("RENDER_GIT_COMMIT", "local")),
        database_url=database_url,
        direct_database_url=direct_url,
        database_ssl_root_cert_file=os.getenv("DATABASE_SSL_ROOT_CERT_FILE") or None,
        supabase_url=os.getenv("SUPABASE_URL") or None,
        supabase_secret_key=os.getenv("SUPABASE_SECRET_KEY") or None,
        ingest_bucket=os.getenv("INGEST_BUCKET", "qcc-ingest"),
        export_bucket=os.getenv("EXPORT_BUCKET", "qcc-export-temp"),
        public_origin=os.getenv("PUBLIC_ORIGIN", "http://127.0.0.1:8765").rstrip("/"),
        allowed_hosts=allowed_hosts,
        netlify_site_id=os.getenv("NETLIFY_SITE_ID") or None,
        netlify_jws_secret=os.getenv("NETLIFY_JWS_SECRET") or None,
        max_upload_bytes=_int("MAX_UPLOAD_BYTES", 25 * 1024 * 1024),
        max_rows=_int("MAX_ROWS", 200_000),
        max_cols=_int("MAX_COLS", 250),
        max_json_bytes=_int("MAX_JSON_BYTES", 1024 * 1024),
        signed_url_ttl_s=_int("SIGNED_URL_TTL_S", 60),
    )
    settings.validate_runtime()
    return settings


settings = load_settings()
