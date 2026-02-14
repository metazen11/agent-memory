from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # PostgreSQL — component-based (install.js generates these)
    postgres_user: str = "agentmem"
    postgres_password: str = ""
    postgres_host: str = "localhost"
    postgres_port: int = 5433
    postgres_db: str = "agent_memory"

    # Full URL override (takes precedence over components above)
    database_url: str = ""

    # Embeddings (sentence-transformers, in-process)
    embedding_model: str = "nomic-ai/nomic-embed-text-v1.5"

    # Observation LLM (local GGUF via llama-cpp-python)
    observation_llm_model: str = ""  # path to .gguf file

    # Anthropic API (optional fallback for observation LLM)
    anthropic_api_key: str = ""

    # Server
    host: str = "0.0.0.0"
    port: int = 3377

    # Queue worker
    queue_poll_interval: int = 5
    queue_max_retries: int = 3

    @property
    def effective_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        pw = f":{self.postgres_password}" if self.postgres_password else ""
        return f"postgresql://{self.postgres_user}{pw}@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
