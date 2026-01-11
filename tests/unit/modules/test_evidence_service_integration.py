"""Integration tests for EvidenceService using CaseRepository.

Tests that EvidenceService correctly uses ICaseRepository for persistence.
Run with: python3 tests/unit/modules/test_evidence_service_integration.py
"""

import asyncio
import sys
from uuid import uuid4
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, '/home/swhouse/product/faultmaven')

from faultmaven.modules.case.infrastructure.case_repository import InMemoryCaseRepository
from faultmaven.modules.evidence.domain.services.evidence_service import EvidenceService
from faultmaven.modules.evidence.domain.models import EvidenceListFilter


class MockStorage:
    """Mock storage adapter for testing."""
    async def store_file(self, file, namespace="evidence"):
        return f"evidence/test/{file.filename}"
    
    async def delete_file(self, path):
        return True
    
    async def get_download_url(self, path):
        return f"http://localhost:8000/api/v1/evidence/file/{path}"


class MockUploadFile:
    """Mock FastAPI UploadFile."""
    def __init__(self, filename="test.log", content_type="text/plain", size=1024):
        self.filename = filename
        self.content_type = content_type
        self.size = size
    
    async def read(self):
        return b"test content"


async def test_evidence_service_uses_case_repository():
    """Test that EvidenceService correctly uses CaseRepository."""
    print("Test: EvidenceService uses CaseRepository")
    
    # Create dependencies
    storage = MockStorage()
    case_repo = InMemoryCaseRepository()
    service = EvidenceService(storage_provider=storage, case_repository=case_repo)
    
    user_id = uuid4()
    file = MockUploadFile()
    
    # Upload evidence
    evidence = await service.upload_evidence(
        file=file,
        uploaded_by=user_id,
        description="Test evidence",
        tags=["test"],
    )
    
    assert evidence is not None, "Evidence should be created"
    assert evidence.original_filename == file.filename, "Should have correct filename"
    assert "test" in evidence.tags, "Should have tags"
    print("  ✓ Upload evidence via CaseRepository")
    
    # Get evidence (non-existent)
    from uuid import UUID
    retrieved = await service.get_evidence(uuid4())  # Wrong ID
    assert retrieved is None, "Should return None for non-existent evidence"
    
    # Get evidence (correct ID)
    evidence_uuid = UUID(evidence.evidence_id)
    retrieved_actual = await service.get_evidence(evidence_uuid)
    assert retrieved_actual is not None, "Should retrieve evidence by ID"
    assert retrieved_actual.evidence_id == evidence.evidence_id, "Should return same evidence"
    print("  ✓ Get evidence via CaseRepository")
    
    # List evidence
    filters = EvidenceListFilter(limit=10, offset=0)
    evidence_list, total = await service.list_evidence(filters)
    assert total >= 1, "Should have at least 1 evidence"
    print("  ✓ List evidence via CaseRepository")
    
    # Delete evidence
    deleted = await service.delete_evidence(evidence_uuid)
    assert deleted is True, "Should delete successfully"
    
    # Verify deleted
    retrieved_after = await service.get_evidence(evidence_uuid)
    assert retrieved_after is None, "Should not find deleted evidence"
    print("  ✓ Delete evidence via CaseRepository")
    
    print("  ✅ EvidenceService integration test passed!\n")


async def run_service_tests():
    """Run service integration tests."""
    print("=" * 60)
    print("EvidenceService Integration Tests")
    print("=" * 60)
    print()
    
    try:
        await test_evidence_service_uses_case_repository()
        print("=" * 60)
        print("✅ ALL SERVICE TESTS PASSED!")
        print("=" * 60)
        return True
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = asyncio.run(run_service_tests())
    sys.exit(0 if success else 1)
