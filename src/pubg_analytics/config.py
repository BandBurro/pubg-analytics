"""Runtime configuration, loaded from environment or .env."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PUBG_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    api_key: str = Field(default="", description="PUBG developer API key")
    shard: str = Field(default="steam")
    data_dir: Path = Field(default=Path("./data"))

    # The 10 req/min ceiling applies to /samples, /players and /seasons.
    # /matches and telemetry asset downloads are exempt, so they get a
    # plain concurrency cap instead of a rate limiter.
    rpm: int = Field(default=10)
    concurrency: int = Field(default=8)

    @property
    def base_url(self) -> str:
        return f"https://api.pubg.com/shards/{self.shard}"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def bronze_dir(self) -> Path:
        return self.data_dir / "bronze"

    @property
    def ledger_path(self) -> Path:
        return self.data_dir / "ledger.sqlite"

    def require_key(self) -> str:
        if not self.api_key:
            raise SystemExit(
                "PUBG_API_KEY is not set.\n"
                "  1. Register a free key at https://developer.pubg.com (personal email)\n"
                "  2. cp .env.example .env  and paste the key in"
            )
        return self.api_key


settings = Settings()
