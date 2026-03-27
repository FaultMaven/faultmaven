"""
ExternalTier2Client — HTTP call to a cloud Tier 2 microservice.

Supports lazy file staging: raw files are uploaded to the backend only
when the first deep query arrives, then cached for subsequent queries.

Backends: Gemini file search, OpenAI assistants, or custom service.

Design Reference:
    docs/architecture/data-processing/data-preprocessing-design-specification.md Section 6.3
"""

import logging
import time
from typing import Any

from faultmaven.core.preprocessing.models import (
    AnalysisContext,
    DeepAnalysisResult,
    UnifiedDataType,
)
from faultmaven.core.preprocessing.tier2.interface import ITier2AnalysisService

logger = logging.getLogger(__name__)


class ExternalTier2Client(ITier2AnalysisService):
    """Calls an external Tier 2 microservice via HTTP."""

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        storage_service: Any | None = None,
        timeout_seconds: int = 30,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.storage_service = storage_service
        self.timeout_seconds = timeout_seconds
        self._staged_files: dict[str, str] = {}

    async def analyze(
        self,
        file_ref: str,
        query: str,
        context: AnalysisContext,
        data_type: UnifiedDataType,
    ) -> DeepAnalysisResult:
        start_time = time.time()

        # Lazy staging: upload file to backend if not already staged
        staged_file_id = await self._ensure_staged(file_ref)

        headers: dict[str, str] = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            import httpx

            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
                    f"{self.base_url}/analyze",
                    json={
                        "file_id": staged_file_id,
                        "query": query,
                        "context": context.model_dump(),
                        "data_type": data_type.value,
                    },
                    headers=headers,
                )
                response.raise_for_status()
                result_data = response.json()

            processing_time_ms = int((time.time() - start_time) * 1000)
            result = DeepAnalysisResult(**result_data)
            result.processing_time_ms = processing_time_ms
            result.backend_used = "external"
            return result

        except ImportError:
            logger.error("httpx not installed — cannot use ExternalTier2Client")
            return DeepAnalysisResult(
                answer="External Tier 2 unavailable: httpx not installed",
                backend_used="external",
                processing_time_ms=int((time.time() - start_time) * 1000),
            )
        except Exception as e:
            logger.error(f"External Tier 2 analysis failed: {e}")
            return DeepAnalysisResult(
                answer=f"External analysis failed: {e}",
                backend_used="external",
                processing_time_ms=int((time.time() - start_time) * 1000),
            )

    async def is_available(self) -> bool:
        try:
            import httpx

            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(f"{self.base_url}/health")
                return response.status_code == 200
        except Exception:
            return False

    async def _ensure_staged(self, file_ref: str) -> str:
        """Lazy staging: upload raw file to Tier 2 backend on first use."""
        if file_ref in self._staged_files:
            return self._staged_files[file_ref]

        if self.storage_service is None:
            # No storage service — pass file_ref directly
            return file_ref

        try:
            raw_content = await self.storage_service.retrieve(file_ref)
            staged_id = await self._upload_to_backend(raw_content, file_ref)
            self._staged_files[file_ref] = staged_id
            return staged_id
        except Exception as e:
            logger.warning(f"File staging failed for {file_ref}: {e}")
            return file_ref

    async def _upload_to_backend(self, raw_content: bytes, file_ref: str) -> str:
        """Upload a file to the external backend for analysis."""
        import httpx

        headers: dict[str, str] = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                f"{self.base_url}/stage",
                files={"file": (file_ref, raw_content)},
                headers=headers,
            )
            response.raise_for_status()
            return response.json().get("file_id", file_ref)
