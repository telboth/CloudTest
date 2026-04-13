from datetime import datetime

from pydantic import BaseModel, EmailStr


class AIOptions(BaseModel):
    ai_provider: str | None = None
    ai_model: str | None = None


class EmbeddingOptions(BaseModel):
    embedding_provider: str | None = None
    embedding_model: str | None = None


class AttachmentRead(BaseModel):
    id: int
    filename: str
    content_type: str | None
    storage_path: str
    uploaded_by: EmailStr
    created_at: datetime

    model_config = {"from_attributes": True}


class CommentCreate(BaseModel):
    body: str


class CommentRead(BaseModel):
    id: int
    author_email: EmailStr
    author_role: str
    body: str
    created_at: datetime

    model_config = {"from_attributes": True}


class HistoryRead(BaseModel):
    id: int
    action: str
    details: str
    actor_email: EmailStr
    created_at: datetime

    model_config = {"from_attributes": True}


class BugBase(BaseModel):
    title: str
    description: str
    category: str = "software"
    severity: str | None = None
    reporting_date: datetime | None = None
    notify_emails: str | None = None
    environment: str | None = None
    repro_steps: str | None = None
    tags: str | None = None


class BugCreate(BugBase, AIOptions):
    reporter_id: EmailStr
    assignee_id: EmailStr | None = None


class BugDuplicateCheckRequest(BugBase):
    pass


class BugDuplicateCheckResponse(BaseModel):
    exact_duplicate_id: int | None = None
    possible_duplicate_ids: list[int] = []


class BugDuplicateCandidate(BaseModel):
    keep_bug_id: int
    keep_title: str
    keep_status: str
    delete_bug_id: int
    delete_title: str
    delete_status: str
    similarity_score: float
    recommendation_reason: str


class BugUpdate(AIOptions):
    title: str | None = None
    description: str | None = None
    category: str | None = None
    severity: str | None = None
    reporting_date: datetime | None = None
    status: str | None = None
    environment: str | None = None
    repro_steps: str | None = None
    tags: str | None = None
    notify_emails: str | None = None
    workaround: str | None = None
    resolution_summary: str | None = None
    reporter_satisfaction: str | None = None
    assignee_id: EmailStr | None = None
    closed_at: datetime | None = None


class BugChangesSinceLastView(BaseModel):
    count: int = 0
    summary: str | None = None
    last_change_at: datetime | None = None


class BugRead(BaseModel):
    id: int
    title: str
    description: str
    category: str
    severity: str
    reporting_date: datetime | None
    status: str
    environment: str | None
    repro_steps: str | None
    tags: str | None
    notify_emails: str | None
    reporter_satisfaction: str | None
    sentiment_label: str | None
    sentiment_summary: str | None
    sentiment_analyzed_at: datetime | None
    bug_summary: str | None
    bug_summary_updated_at: datetime | None
    workaround: str | None
    resolution_summary: str | None
    ado_work_item_id: int | None
    ado_work_item_url: str | None
    ado_sync_status: str | None
    ado_synced_at: datetime | None
    reporter_id: EmailStr
    assignee_id: EmailStr | None
    created_at: datetime
    updated_at: datetime | None
    closed_at: datetime | None
    attachments: list[AttachmentRead] = []
    comments: list[CommentRead] = []
    history: list[HistoryRead] = []
    changes_since_last_view: BugChangesSinceLastView | None = None

    model_config = {"from_attributes": True}


class BugAIDraftRequest(AIOptions):
    source_text: str
    assignable_emails: list[EmailStr] = []
    openai_api_key: str | None = None
    openai_model: str | None = None


class BugAIDraftResponse(BaseModel):
    title: str
    description: str
    repro_steps: str | None = None
    category: str = "software"
    severity: str = "medium"
    assignee_id: EmailStr | None = None
    notify_emails: str | None = None
    environment: str | None = None
    tags: str | None = None
    missing_info: list[str] = []
    assumptions: list[str] = []
    confidence: str = "medium"
    source: str
    detected_language: str | None = None
    debug_error: str | None = None


class BugDescriptionTypeaheadRequest(AIOptions, EmbeddingOptions):
    title: str | None = None
    description: str
    repro_steps: str | None = None
    category: str | None = None
    severity: str | None = None
    environment: str | None = None
    tags: str | None = None


class BugDescriptionTypeaheadResponse(BaseModel):
    suggestion: str
    source: str


class AITaskRequest(AIOptions, EmbeddingOptions):
    pass
