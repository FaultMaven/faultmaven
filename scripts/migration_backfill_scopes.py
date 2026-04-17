import asyncio
import logging
from faultmaven.container.app_container import AppContainer
from faultmaven.config.environment import init_environment

# Initialize environment FIRST to ensure settings load correctly
init_environment()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("migration")


async def run_migration():
    """Migrate existing documents to have scope=global"""
    # 1. Initialize DI Container
    container = AppContainer()
    container.wire(modules=[__name__])

    # 2. Get KnowledgeService
    try:
        knowledge_service = container.services.knowledge_service()
    except Exception as e:
        logger.error(f"Failed to get knowledge service: {e}")
        return

    # 3. List all documents
    logger.info("Fetching all documents...")
    all_docs = await knowledge_service.list_documents(limit=10000)
    docs = all_docs.get("documents", [])

    logger.info(f"Found {len(docs)} documents to process.")

    updates_made = 0

    for doc in docs:
        doc_id = doc.get("document_id")

        # Check current scope
        scope = doc.get("scope") or doc.get("metadata", {}).get("scope")
        if scope:
            continue

        logger.info(f"Updating document {doc_id} to global scope...")

        try:
            # We need to update the actual vector store and the redis backing store
            document = await knowledge_service.get_document(doc_id)
            if not document:
                continue

            # Default to global scope
            document["scope"] = "global"
            if "metadata" not in document:
                document["metadata"] = {}
            document["metadata"]["scope"] = "global"

            # Re-index the document
            from faultmaven.models.api import KnowledgeBaseDocument

            model = KnowledgeBaseDocument(
                document_id=document["document_id"],
                title=document["title"],
                content=document["content"],
                document_type=document.get("document_type"),
                tags=document.get("tags", []),
                source_url=document.get("source_url"),
                scope="global",
                created_at=document.get("created_at"),
                updated_at=document.get("updated_at"),
            )

            # It's protected method but we can call it for migration
            await knowledge_service._index_document_in_vector_store(model)

            # Update Redis store directly since there's no update_document method
            if knowledge_service._redis:
                import json

                raw = await knowledge_service._redis.hget(
                    knowledge_service._kb_doc_key.format(document_id=doc_id), "data"
                )
                if raw:
                    redis_doc = json.loads(raw)
                    redis_doc["scope"] = "global"
                    if "metadata" not in redis_doc:
                        redis_doc["metadata"] = {}
                    redis_doc["metadata"]["scope"] = "global"

                    await knowledge_service._redis.hset(
                        knowledge_service._kb_doc_key.format(document_id=doc_id),
                        "data",
                        json.dumps(redis_doc),
                    )
            else:
                knowledge_service._documents_store[doc_id] = document

            updates_made += 1

        except Exception as e:
            logger.error(f"Error updating doc {doc_id}: {e}")

    logger.info(f"Migration complete! Updated {updates_made} documents.")


if __name__ == "__main__":
    asyncio.run(run_migration())
