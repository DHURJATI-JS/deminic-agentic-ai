"""Central configuration loaded from .env (see .env.example)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent

SECTIONS = ("personal", "business", "coding")
DEVELOPER_EMAIL = "dhurjatisharma2010@gmail.com"
DEVELOPER_GITHUB = "https://github.com/dhurjati-js"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    app_host: str = "0.0.0.0"
    app_port: int = 8700
    enable_lan_https: bool = True
    https_port: int = 8743
    app_base_url: str = "http://127.0.0.1:8700"
    cookie_secure: bool = False
    debug: bool = False

    secret_key: str
    encryption_key: str

    database_url: str
    redis_url: str
    clamd_host: str = "127.0.0.1"
    clamd_port: int = 3311

    # Your OpenAI-compatible AI gateway - set DEFAULT_GATEWAY_URL / DEFAULT_GATEWAY_KEY in .env
    default_gateway_url: str = "http://localhost:8000/v1"
    default_gateway_key: str = ""
    default_local_model: str = "gemma"
    default_coder_model: str = "qwen"

    ollama_url: str = "http://127.0.0.1:11435/v1"
    fallback_chat_model: str = "gemma3:4b"
    fallback_coder_model: str = "qwen2.5-coder:3b"
    embed_model: str = "all-minilm"

    system_smtp_host: str = ""
    system_smtp_port: int = 587
    system_smtp_username: str = ""
    system_smtp_password: str = ""
    system_smtp_from: str = ""

    rate_limit_default: str = "600/minute"
    rate_limit_auth: str = "20/minute"  # register / forgot / reset, per IP
    rate_limit_login: str = "120/minute"  # per IP; plus a per-account failure throttle
    rate_limit_chat: str = "60/minute"
    rate_limit_storage: str = "memory://"

    # Timeouts / limits
    llm_timeout_s: float = 120.0
    generation_timeout_s: float = 420.0
    max_upload_mb: int = 25

    @property
    def data_dir(self) -> Path:
        d = ROOT / "data"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def workspaces_dir(self) -> Path:
        d = self.data_dir / "workspaces"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def uploads_dir(self) -> Path:
        d = self.data_dir / "uploads"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def reports_dir(self) -> Path:
        d = self.data_dir / "locust_reports"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def tls_dir(self) -> Path:
        d = self.data_dir / "tls"
        d.mkdir(parents=True, exist_ok=True)
        return d


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
