"""Environment-based configuration. All secrets come from env / .env, never code."""
from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db: str = "terminator"
    jwt_secret: str = ""            # required in production
    jwt_ttl_minutes: int = 60 * 24
    secret_key: str = ""            # Fernet key for encrypting stored provider API keys
    cors_origins: str = "http://localhost:5173"
    data_dir: Path = Path("data")
    max_upload_mb: int = 25
    rate_limit_per_minute: int = 60
    auth_rate_limit_per_minute: int = 10
    allow_registration: bool = True

    # Optional bootstrap provider (users can also add providers via API/UI)
    default_provider_base_url: str = ""   # e.g. http://localhost:11434/v1 for Ollama
    default_provider_api_key: str = ""
    default_model: str = ""
    default_embedding_model: str = ""

    # Web search: SearxNG instance (JSON API) or Tavily key
    searxng_url: str = ""
    tavily_api_key: str = ""
    cloudflare_api_base: str = "https://api.cloudflare.com/client/v4"
    wikipedia_base: str = "https://en.wikipedia.org"
    # WhatsApp bridge from integrations/whatsapp-mcp (real sending/reading through a linked device)
    whatsapp_bridge_url: str = "http://127.0.0.1:8080"
    # Microsoft 365 (Teams chats, Outlook mail, calendar) via Microsoft Graph
    graph_base_url: str = "https://graph.microsoft.com/v1.0"
    ms_authority_base: str = "https://login.microsoftonline.com"
    whatsapp_store_dir: str = str(Path(__file__).resolve().parents[2] / "integrations/whatsapp-mcp/whatsapp-bridge/store")

    # System tool: comma-separated allow-list of read-only commands
    system_command_allowlist: str = "uptime,df,free,ps,uname,whoami,pm2,systemctl,journalctl,nginx,docker,ss,ls,cat,tail,head"
    sandbox_timeout_s: int = 20
    reminder_interval_s: float = 5.0
    # Desktop control (apps, volume, media, WhatsApp prefill, terminal). Auto-off without a graphical session.
    desktop_control: bool = True
    shell_timeout_s: int = 60
    # Keep local models (Ollama) loaded with a warm prompt cache after recent use; 0 disables.
    keep_warm_hours: float = 8.0
    turn_timeout_s: float = 1800.0
    env_autosave_minutes: float = 10.0  # snapshot open tabs/apps so "open previous environment" works (0 = off)     # safety stop so a hung turn never blocks a conversation forever
    keep_warm_interval_s: float = 240.0

    @property
    def is_prod(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    if s.is_prod and (len(s.jwt_secret) < 32 or not s.secret_key):
        raise RuntimeError("JWT_SECRET (>=32 chars) and SECRET_KEY must be set in production")
    if not s.jwt_secret:
        import secrets
        s.jwt_secret = secrets.token_urlsafe(48)  # dev only: sessions reset on restart
    if not s.secret_key:
        from cryptography.fernet import Fernet
        key_file = s.data_dir / ".dev_fernet_key"
        s.data_dir.mkdir(parents=True, exist_ok=True)
        if not key_file.exists():
            key_file.write_bytes(Fernet.generate_key())
            key_file.chmod(0o600)
        s.secret_key = key_file.read_text().strip()
    return s
