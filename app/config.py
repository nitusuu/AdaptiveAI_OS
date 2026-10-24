from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')
    adaptiveai_secret_key: str = 'development-only-change-before-deployment'
    adaptiveai_database_url: str = 'sqlite:///./data/adaptiveai.db'
    adaptiveai_storage_dir: str = './data'
    anthropic_api_key: str = ''
    adaptiveai_chat_model: str = 'claude-opus-5-5'
    # low | medium | high | xhigh | max - higher effort costs more tokens per call.
    adaptiveai_effort: str = 'low'
    # Local embedding model (fastembed); changing it re-indexes PDFs automatically at startup.
    adaptiveai_embed_model: str = 'BAAI/bge-small-en-v1.5'
    adaptiveai_max_upload_mb: int = 20
    adaptiveai_max_documents: int = 8
    adaptiveai_max_pages: int = 300
    adaptiveai_max_questions: int = 40
    adaptiveai_chunk_chars: int = 1200
    adaptiveai_chunk_overlap: int = 180
    adaptiveai_retrieval_top_k: int = 6
    adaptiveai_adapt_min_answers: int = 3
    adaptiveai_adapt_high: float = .85
    adaptiveai_adapt_low: float = .40

    @property
    def storage_path(self) -> Path:
        return Path(self.adaptiveai_storage_dir).resolve()


settings = Settings()
