from __future__ import annotations
from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[3]
ENV_FILE = ROOT_DIR / ".env"
DEFAULT_DB = ROOT_DIR / "migration_factory.db"

class Settings(BaseSettings):
    app_name: str = "Oracle to Databricks AI Migration Factory"
    environment: str = "DEV"
    database_url: str = f"sqlite:///{DEFAULT_DB.as_posix()}"
    jwt_secret: str = "change-me-in-production-minimum-32-characters"
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 1440
    allowed_origins: str = "http://localhost:5173,http://localhost:5174"
    bootstrap_admin_username: str | None = None
    bootstrap_admin_password: str | None = None

    # Source Database Config (Oracle primary, MySQL, PostgreSQL & SQL Server compatibility)
    source_type: str = "ORACLE"
    oracle_host: str | None = "localhost"
    oracle_port: int = 1521
    oracle_service_name: str | None = None
    oracle_sid: str | None = None
    oracle_username: str | None = "system"
    oracle_password: str | None = None
    oracle_schema: str | None = None

    mysql_host: str | None = "localhost"
    mysql_port: int = 3306
    mysql_database: str | None = None
    mysql_username: str | None = "root"
    mysql_password: str | None = None
    mysql_ssl_mode: str | None = "DISABLED"

    postgres_host: str | None = None
    postgres_port: int = 5432
    postgres_database: str | None = None
    postgres_username: str | None = None
    postgres_password: str | None = None
    postgres_schema: str | None = "public"
    postgres_sslmode: str = "prefer"

    sqlserver_host: str | None = None
    sqlserver_database: str | None = None
    sqlserver_driver: str = "ODBC Driver 18 for SQL Server"
    sqlserver_username: str | None = None
    sqlserver_password: str | None = None

    databricks_host: str | None = None
    databricks_http_path: str | None = None
    databricks_token: str | None = None
    dev_catalog: str = "migration_dev"
    test_catalog: str = "migration_test"
    uat_catalog: str = "migration_uat"
    prod_catalog: str = "migration_prod"
    control_catalog: str = "migration_control"
    control_schema: str = "migration_control"

    llm_enabled: bool = False
    llm_provider: str = "OLLAMA"
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    llm_timeout_seconds: int = 120
    llm_max_attempts: int = 3
    llm_num_ctx: int = 8192
    llm_num_predict: int = 4096
    llm_max_prompt_chars: int = 160000
    ollama_keep_alive: str = "5m"
    deployment_policy: str = "FAIL_ON_SCHEMA_DRIFT"
    dev_schema_policy: str = "REPLACE_CHANGED"
    batch_size: int = 10000
    parallelism: int = 4
    max_rows: int | None = None
    load_mode: str = "FULL_LOAD"

    model_config = SettingsConfigDict(env_file=str(ENV_FILE), env_file_encoding="utf-8", case_sensitive=False, extra="ignore")

    @property
    def origins(self) -> list[str]:
        return [x.strip() for x in self.allowed_origins.split(",") if x.strip()]

    def validate_production_security(self) -> None:
        if not 1 <= self.llm_max_attempts <= 5:
            raise RuntimeError("LLM_MAX_ATTEMPTS must be between 1 and 5")
        if not 2048 <= self.llm_num_ctx <= 131072:
            raise RuntimeError("LLM_NUM_CTX must be between 2048 and 131072")
        if not 128 <= self.llm_num_predict <= 32768:
            raise RuntimeError("LLM_NUM_PREDICT must be between 128 and 32768")
        if not 10000 <= self.llm_max_prompt_chars <= 1000000:
            raise RuntimeError("LLM_MAX_PROMPT_CHARS must be between 10000 and 1000000")
        if self.environment.upper() == "PROD":
            weak = {"change-me", "secret", "changeme", "change-me-in-production-minimum-32-characters"}
            if len(self.jwt_secret) < 32 or self.jwt_secret.lower() in weak:
                raise RuntimeError("Production requires a strong JWT_SECRET of at least 32 characters")

@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.validate_production_security()
    return s
