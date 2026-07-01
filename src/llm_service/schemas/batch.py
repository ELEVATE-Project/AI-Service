from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from src.llm_service.schemas.chat import ChatResponse
from src.shared.db.enums import BatchJobStatus


class BatchAcceptedResponse(BaseModel):
    job_id: str
    request_id: str
    status: str


class BatchJobStatusResponse(BaseModel):
    id: str
    request_id: str
    status: BatchJobStatus
    provider: str
    model: str
    created_at: datetime
    completed_at: Optional[datetime] = None
    result: Optional[ChatResponse] = None
    error_code: Optional[str] = None
