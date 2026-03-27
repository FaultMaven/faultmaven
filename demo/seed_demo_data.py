#!/usr/bin/env python3
"""
Demo Data Seeder for FaultMaven

This script pre-loads FaultMaven with sample data to demonstrate the system:
- Runbooks uploaded to the Knowledge Base
- A sample incident case with evidence

Usage:
    python -m demo.seed_demo_data

Or with the demo Docker profile:
    docker compose --profile demo up
"""

import asyncio
import os
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))


async def seed_knowledge_base():
    """Upload sample runbooks to the Knowledge Base."""
    from faultmaven.container import container

    print("\n📚 Seeding Knowledge Base with sample runbooks...")

    demo_dir = Path(__file__).parent
    runbooks_dir = demo_dir / "runbooks"

    if not runbooks_dir.exists():
        print(f"   ❌ Runbooks directory not found: {runbooks_dir}")
        return False

    try:
        await container.initialize()
        knowledge_service = container.get_knowledge_service()

        runbook_files = list(runbooks_dir.glob("*.md"))
        if not runbook_files:
            print("   ⚠️  No runbook files found")
            return False

        for runbook_path in runbook_files:
            print(f"   📄 Uploading: {runbook_path.name}")
            content = runbook_path.read_text()

            # Upload to global KB (admin-managed)
            await knowledge_service.add_document(
                content=content,
                metadata={
                    "filename": runbook_path.name,
                    "source": "demo_seed",
                    "type": "runbook",
                },
                kb_type="global",
            )
            print(f"   ✅ Uploaded: {runbook_path.name}")

        print(f"\n   ✅ Uploaded {len(runbook_files)} runbook(s) to Knowledge Base")
        return True

    except Exception as e:
        print(f"   ❌ Failed to seed Knowledge Base: {e}")
        return False


async def create_demo_case():
    """Create a sample incident case with pre-loaded evidence."""
    from faultmaven.container import container

    print("\n🔧 Creating demo incident case...")

    demo_dir = Path(__file__).parent
    evidence_dir = demo_dir / "evidence"

    try:
        await container.initialize()
        case_service = container.get_case_service()

        # Create the demo case
        case = await case_service.create_case(
            title="Payment Service OOMKilled - Production Incident",
            description="""
The payment-service pods in production are repeatedly crashing with OOMKilled status.
This started happening after a recent deployment. The batch reconciliation job
appears to be triggering the crashes every 5 minutes.

Impact: Some payment transactions are failing during pod restarts.
            """.strip(),
            user_id="demo-user",
            metadata={
                "severity": "high",
                "environment": "production",
                "service": "payment-service",
                "demo": True,
            },
        )

        print(f"   ✅ Created case: {case.id}")

        # Add evidence files
        evidence_files = [
            ("pod-logs.txt", "application_logs"),
            ("deployment.yaml", "configuration"),
            ("kubectl-describe-pod.txt", "diagnostic_output"),
        ]

        evidence_service = container.get_evidence_service()

        for filename, evidence_type in evidence_files:
            filepath = evidence_dir / filename
            if filepath.exists():
                content = filepath.read_text()
                await evidence_service.add_evidence(
                    case_id=case.id,
                    content=content,
                    evidence_type=evidence_type,
                    metadata={
                        "filename": filename,
                        "source": "demo_seed",
                    },
                )
                print(f"   📎 Attached evidence: {filename}")

        print(f"\n   ✅ Demo case created with {len(evidence_files)} evidence files")
        print(f"   📋 Case ID: {case.id}")

        return case.id

    except Exception as e:
        print(f"   ❌ Failed to create demo case: {e}")
        import traceback

        traceback.print_exc()
        return None


async def main():
    """Main entry point for demo seeding."""
    print("=" * 60)
    print("🎭 FaultMaven Demo Data Seeder")
    print("=" * 60)

    # Check if we're in demo mode
    demo_mode = os.getenv("FAULTMAVEN_DEMO_MODE", "false").lower() == "true"
    if demo_mode:
        print("\n🎯 Running in DEMO MODE")

    # Seed the Knowledge Base
    kb_success = await seed_knowledge_base()

    # Create demo case
    case_id = await create_demo_case()

    print("\n" + "=" * 60)
    if kb_success and case_id:
        print("✅ Demo data seeded successfully!")
        print("")
        print("Next steps:")
        print("  1. Open the Dashboard at http://localhost:3333")
        print("  2. View the demo case in Case History")
        print("  3. Try asking FaultMaven: 'Why is the payment service crashing?'")
        print("")
        print(
            "The AI will analyze the evidence and suggest increasing the memory limit."
        )
    else:
        print("⚠️  Demo seeding completed with some errors")
        print("    Check the output above for details")

    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
