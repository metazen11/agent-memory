from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # PostgreSQL
    database_url: str = "postgresql://wfhub:@localhost:5433/agentic"

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

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
