from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    app_name: str = "Bug Ticket System"
    secret_key: str = "change-me-for-real-use"
    access_token_expire_minutes: int = 720
    database_url: str = "postgresql+psycopg://bugapp:bugapp@localhost:5432/bug_ticket_system"
    api_base_url: str = "http://localhost:8010"
    storage_dir: str = "storage"
    default_admin_email: str = "admin@example.com"
    default_admin_password: str = "admin123"
    ai_provider: str = "openai"
    ai_model: str = "gpt-4o-mini"
    embedding_provider: str = "openai"
    embedding_model: str = "text-embedding-3-small"
    local_embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_lock_enabled: bool = True
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5:7b"
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"
    openai_embedding_model: str = "text-embedding-3-small"
    entra_tenant_id: str | None = None
    entra_client_id: str | None = None
    entra_client_secret: str | None = None
    entra_redirect_uri: str = "http://localhost:8010/auth/entra/callback"
    streamlit_reporter_url: str = "http://localhost:8501"
    streamlit_assignee_url: str = "http://localhost:8502"
    streamlit_admin_url: str = "http://localhost:8503"
    entra_admin_emails: str = ""
    entra_assignee_emails: str = ""
    azure_devops_org: str = ""
    azure_devops_project: str = ""
    azure_devops_pat: str | None = None
    azure_devops_dashboard_url: str = "https://dev.azure.com/xlentoslo/XlentRAG/_workitems/recentlyupdated/"

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
    def entra_enabled(self) -> bool:
        return bool(self.entra_tenant_id and self.entra_client_id and self.entra_client_secret)

    @property
    def entra_admin_email_list(self) -> set[str]:
        return {
            value.strip().casefold()
            for value in self.entra_admin_emails.split(",")
            if value.strip()
        }

    @property
    def entra_assignee_email_list(self) -> set[str]:
        return {
            value.strip().casefold()
            for value in self.entra_assignee_emails.split(",")
            if value.strip()
        }

    def streamlit_url_for_app(self, app_name: str) -> str:
        mapping = {
            "reporter": self.streamlit_reporter_url,
            "assignee": self.streamlit_assignee_url,
            "admin": self.streamlit_admin_url,
        }
        return mapping.get(app_name, self.streamlit_reporter_url)


settings = Settings()
