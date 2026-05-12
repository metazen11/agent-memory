from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # PostgreSQL — component-based (install.js generates these)
    postgres_user: str = "agentmem"
    postgres_password: str = ""
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "agent_memory"

    # Full URL override (takes precedence over components above)
    database_url: str = ""

    # Embeddings (sentence-transformers, in-process)
    embedding_model: str = "nomic-ai/nomic-embed-text-v1.5"
    embedding_trust_remote_code: bool = False

    # Observation LLM (local GGUF via llama-cpp-python)
    observation_llm_model: str = ""  # path to .gguf file

    # Anthropic API (optional fallback for observation LLM)
    anthropic_api_key: str = ""

    # Server
    host: str = "127.0.0.1"
    port: int = 3377

    # Security
    allow_trust_auth: bool = False
    cors_origins: str = "http://localhost:3377,http://127.0.0.1:3377"
    require_auth: bool = False
    trusted_agents: str = "anvil,claude,codex,gemini,python-httpx"  # comma-separated, "*" = trust all localhost

    # Redaction
    redact_secrets: bool = True
    redact_pii: bool = False

    # Rate limiting
    rate_limit_enabled: bool = True
    rate_limit_writes_per_min: int = 100
    rate_limit_reads_per_min: int = 500

    # Audit logging
    audit_log_level: str = "writes_only"  # writes_only | all | off
    audit_retention_days: int = 30

    # Queue worker
    queue_poll_interval: int = 5
    queue_max_retries: int = 3

    @property
    def effective_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        pw = f":{self.postgres_password}" if self.postgres_password else ""
        return f"postgresql://{self.postgres_user}{pw}@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


settings = Settings()
