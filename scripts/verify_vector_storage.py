#!/usr/bin/env python3
"""
Verify that evidence documents are stored in ChromaDB vector store.

Usage:
    python scripts/verify_vector_storage.py <case_id>

Example:
    python scripts/verify_vector_storage.py case_e073cea20fd1
"""

import asyncio
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from faultmaven.infrastructure.persistence.case_vector_store import CaseVectorStore


async def verify_case_evidence(case_id: str):
    """Verify evidence documents are stored for a case."""

    print(f"\n🔍 Checking vector storage for case: {case_id}")
    print("=" * 60)

    try:
        # Initialize vector store
        vector_store = CaseVectorStore()

        # Get document count
        doc_count = await vector_store.get_case_document_count(case_id)

        print(f"\n📊 Document Count: {doc_count}")

        if doc_count == 0:
            print("\n⚠️  No documents found in vector storage.")
            print("   This could mean:")
            print("   - No evidence uploaded yet")
            print("   - Background vectorization still in progress")
            print("   - Vectorization failed (check logs)")
            return

        print(f"\n✅ Found {doc_count} document(s) in vector storage!")

        # Test search
        print("\n🔎 Testing search functionality...")
        test_query = "what is this about?"
        results = await vector_store.search(
            case_id=case_id,
            query=test_query,
            k=3
        )

        if results:
            print(f"\n✅ Search works! Found {len(results)} result(s)")
            print(f"\nTop result preview:")
            print(f"  ID: {results[0]['id']}")
            print(f"  Similarity: {results[0]['score']:.3f}")
            print(f"  Content preview: {results[0]['content'][:200]}...")
        else:
            print("\n⚠️  Search returned no results")

    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/verify_vector_storage.py <case_id>")
        print("\nExample:")
        print("  python scripts/verify_vector_storage.py case_e073cea20fd1")
        sys.exit(1)

    case_id = sys.argv[1]
    asyncio.run(verify_case_evidence(case_id))


if __name__ == "__main__":
    main()
