import ssl
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus

from pydantic import BaseModel, PostgresDsn, SecretStr, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DBSettings(BaseModel):
    user: str = "pingpal"
    password: str = "pingpal"
    name: str = "pingpal"
    host: str = "127.0.0.1"
    port: int = 5432

    echo: bool = False

    pool_size: int = 0
    pool_pre_ping: bool = False

    @computed_field
    @property
    def url(self) -> str:
        encoded_user = quote_plus(self.user)

        password = self.password
        try:
            if Path("/run/secrets/pg_password").exists():
                with open("/run/secrets/pg_password", "r") as f:
                    password = f.read().strip()
            elif Path("./secrets/pg_password.txt").exists():
                with open("./secrets/pg_password.txt", "r") as f:
                    password = f.read().strip()
        except Exception:
            raise

        encoded_password = quote_plus(password)

        return PostgresDsn.build(
            scheme="postgresql+asyncpg",
            username=encoded_user,
            password=encoded_password,
            host=self.host,
            port=self.port,
            path=self.name,
        ).encoded_string()


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
    nats_url: str = "nats://127.0.0.1:4222"

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
