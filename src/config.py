from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # App
    log_level: str = "INFO"

    # NATS
    nats_url: str = "nats://127.0.0.1:4222"

    # DB
    database_url: str | None = None
    pingpal_db_user: str = "pingpal"
    pingpal_db_password: str = "pingpal"
    pingpal_db_host: str = "127.0.0.1"
    pingpal_db_port: int = 5432
    pingpal_db_name: str = "pingpal"

    db_echo: bool = False

    # Agent Specific
    pingpal_region: str = "global"

    @property
    def db_url(self) -> str:
        if self.database_url:
            return self.database_url

        password = self.pingpal_db_password
        try:
            if Path("/run/secrets/pg_password").exists():
                with open("/run/secrets/pg_password", "r") as f:
                    password = f.read().strip()
            elif Path("./secrets/pg_password.txt").exists():
                with open("./secrets/pg_password.txt", "r") as f:
                    password = f.read().strip()
        except Exception:
            raise

        return f"postgresql+asyncpg://{self.pingpal_db_user}:{password}@{self.pingpal_db_host}:{self.pingpal_db_port}/{self.pingpal_db_name}"


settings = Settings()
