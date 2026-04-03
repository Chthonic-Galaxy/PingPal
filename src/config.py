import os
import ssl
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import find_dotenv, load_dotenv
from pydantic import BaseModel, PostgresDsn, SecretStr, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv(find_dotenv())


def read_password():
    try:
        if Path("/run/secrets/pg_password").exists():
            with open("/run/secrets/pg_password", "r") as f:
                return f.read().strip()
        elif Path("./secrets/pg_password.txt").exists():
            with open("./secrets/pg_password.txt", "r") as f:
                return f.read().strip()
    except Exception:
        raise


class DBSettings(BaseModel):
    user: str
    password: str = os.getenv("DATABASE__PASSWORD") or read_password() or ""
    name: str
    host: str
    port: int

    echo: bool

    pool_size: int
    pool_pre_ping: bool

    @computed_field
    @property
    def url(self) -> str:
        encoded_user = quote_plus(self.user)

        encoded_password = quote_plus(self.password)

        return PostgresDsn.build(
            scheme="postgresql+asyncpg",
            username=encoded_user,
            password=encoded_password,
            host=self.host,
            port=self.port,
            path=self.name,
        ).encoded_string()


class NATSSettings(BaseModel):
    url: str

    kv_bucket: str
    kv_key_prefix: str
    kv_watch_pattern: str

    kv_del: str
    kv_purge: str

    metrics_subject: str
    agents_heartbeat_subject: str


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore",
    )

    # App
    log_level: str = "INFO"

    # NATS
    nats: NATSSettings

    # DB
    database: DBSettings

    # Agent Specific
    pingpal_region: str = "global"

    # Security
    # For Docker by default
    ## (TLS)
    tls_ca_cert: str = "/app/certs/ca.crt"
    tls_cert: str = (
        "/app/certs/client-core.crt"  # Must be redefine for Agent/s via ENV variables
    )
    tls_key: str = "/app/certs/client-core.key"

    app_api_key: SecretStr = "dev-secret-key"  # pyright: ignore[reportAssignmentType]

    @property
    def ssl_context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
        ctx.load_verify_locations(cafile=self.tls_ca_cert)
        ctx.load_cert_chain(certfile=self.tls_cert, keyfile=self.tls_key)

        return ctx


@lru_cache
def get_settings() -> Settings:
    return Settings()  # pyright: ignore[reportCallIssue]


settings = get_settings()
