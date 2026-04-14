from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    app_name: str = "Bug Ticket System"
    secret_key: str = "change-me-for-real-use"
    database_url: str = f"sqlite:///{(ROOT_DIR / 'bug_tracker_cloud.db').as_posix()}"
    storage_dir: str = "storage"
    default_admin_email: str = "admin@example.com"
    default_admin_password: str = "admin123"
    ai_provider: str = "openai"
    ai_model: str = "gpt-4o-mini"
    embedding_provider: str = "openai"
    embedding_model: str = "text-embedding-3-small"
    local_embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_lock_enabled: bool = True
    sqlite_vec_enabled: bool = True
    sqlite_vec_dimensions: int = 1536
    sqlite_vec_embedding_provider: str = "openai"
    sqlite_vec_embedding_model: str = "text-embedding-3-small"
    sqlite_vec_table_name: str = "bug_search_vec"
    sqlite_fts_table_name: str = "bug_search_fts"
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5:7b"
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"
    openai_embedding_model: str = "text-embedding-3-small"

    model_config = SettingsConfigDict(env_file=ROOT_DIR / ".env", env_file_encoding="utf-8")

    @property
    def attachment_dir(self) -> Path:
        root = Path(self.storage_dir)
        root.mkdir(parents=True, exist_ok=True)
        attachment_root = root / "attachments"
        attachment_root.mkdir(parents=True, exist_ok=True)
        return attachment_root

    @property
    def database_backend(self) -> str:
        value = (self.database_url or "").strip().casefold()
        if value.startswith("postgresql") or value.startswith("postgres"):
            return "postgresql"
        if value.startswith("sqlite"):
            return "sqlite"
        return "other"

    @property
    def database_is_sqlite(self) -> bool:
        return self.database_backend == "sqlite"

    @property
    def database_is_postgresql(self) -> bool:
        return self.database_backend == "postgresql"

    @property
    def sqlite_vec_lock_active(self) -> bool:
        return self.database_is_sqlite and bool(self.sqlite_vec_enabled)


settings = Settings()
