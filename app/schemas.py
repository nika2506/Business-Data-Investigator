from pydantic import BaseModel, Field
from typing import Literal


class InvestigationRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    investigation_id: str | None = None
    mode: Literal["model", "offline"] = "model"