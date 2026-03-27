from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, field_validator

from faultmaven.utils.serialization import to_json_compatible


class VectorMetadata(BaseModel):
    """Canonical metadata schema for vector documents sent to ChromaDB.

    Ensures consistent keys, value types, and ISO-8601 timestamps. Drops None
    and coerces non-primitive values to strings.
    """

    title: str | None = None
    document_type: str | None = None
    tags: list[str] = []
    source_url: str | None = None
    scope: str | None = None
    owner_id: str | None = None
    team_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @field_validator("tags", mode="before")
    @classmethod
    def _coerce_tags(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [t.strip() for t in v.split(",") if t.strip()]
        if isinstance(v, list):
            return [str(t) for t in v]
        return [str(v)]

    @field_validator(
        "title",
        "document_type",
        "source_url",
        "scope",
        "owner_id",
        "team_id",
        mode="before",
    )
    @classmethod
    def _coerce_str(cls, v: Any) -> str | None:
        if v is None:
            return None
        return str(v)

    def to_chroma_metadata(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if self.title:
            data["title"] = self.title
        if self.document_type:
            data["document_type"] = self.document_type
        if self.tags:
            data["tags"] = ",".join(self.tags)
        if self.source_url:
            data["source_url"] = self.source_url
        if self.scope:
            data["scope"] = self.scope
        if self.owner_id:
            data["owner_id"] = self.owner_id
        if self.team_id:
            data["team_id"] = self.team_id
        if self.created_at:
            data["created_at"] = to_json_compatible(self.created_at)
        if self.updated_at:
            data["updated_at"] = to_json_compatible(self.updated_at)
        return data
