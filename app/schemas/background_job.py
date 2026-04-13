from datetime import datetime

from pydantic import BaseModel, EmailStr


class BackgroundJobRead(BaseModel):
    id: int
    job_type: str
    status: str
    payload_json: dict | None = None
    result_json: dict | None = None
    error_message: str | None = None
    requested_by: EmailStr | None = None
    bug_id: int | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None

    model_config = {"from_attributes": True}
