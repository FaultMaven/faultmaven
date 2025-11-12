"""
In-memory implementation of IVectorStore interface.

This module provides a RAM-based vector store for development and testing.
Uses simple cosine similarity for semantic search without external dependencies.
"""

from typing import List, Dict, Optional, Any
import asyncio
import math
from faultmaven.models.interfaces import IVectorStore


class InMemoryVectorStore(IVectorStore):
    """In-memory implementation of IVectorStore using Python dictionaries and simple cosine similarity"""

    def __init__(self):
        """Initialize in-memory vector store"""
        self._documents: Dict[str, Dict] = {}  # document_id -> {content, metadata, embedding}
        self._lock = asyncio.Lock()

    async def add_documents(self, documents: List[Dict]) -> None:
        """
        Add documents to the vector store.

        Args:
            documents: List of documents with 'id', 'content', and 'metadata' keys

        Note:
            In-memory store uses simple text matching instead of actual embeddings
            for development/testing purposes. For production, use ChromaDB.
        """
        async with self._lock:
            for doc in documents:
                doc_id = doc.get('id')
                if not doc_id:
                    continue

                # Store document with simple word-based "embedding" (word frequency vector)
                content = doc.get('content', '')
                metadata = doc.get('metadata', {})

                # Create simple word frequency vector for cosine similarity
                embedding = self._create_simple_embedding(content)

                self._documents[doc_id] = {
                    'id': doc_id,
                    'content': content,
                    'metadata': metadata,
                    'embedding': embedding
                }

    async def search(self, query: str, k: int = 5) -> List[Dict]:
        """
        Search for similar documents using simple cosine similarity.

        Args:
            query: Search query text
            k: Number of results to return

        Returns:
            List of similar documents with scores

        Note:
            Uses simple word-based similarity, not true semantic search.
            For production semantic search, use ChromaDB with proper embeddings.
        """
        async with self._lock:
            if not self._documents:
                return []

            # Create query embedding
            query_embedding = self._create_simple_embedding(query)

            # Calculate similarity scores for all documents
            scored_docs = []
            for doc_id, doc_data in self._documents.items():
                doc_embedding = doc_data['embedding']
                similarity = self._cosine_similarity(query_embedding, doc_embedding)

                scored_docs.append({
                    'id': doc_data['id'],
                    'content': doc_data['content'],
                    'metadata': doc_data.get('metadata', {}),
                    'score': similarity
                })

            # Sort by score (highest first) and return top k
            scored_docs.sort(key=lambda x: x['score'], reverse=True)
            return scored_docs[:k]

    async def delete_documents(self, ids: List[str]) -> None:
        """
        Delete documents from the vector store.

        Args:
            ids: List of document identifiers to delete
        """
        async with self._lock:
            for doc_id in ids:
                if doc_id in self._documents:
                    del self._documents[doc_id]

    def _create_simple_embedding(self, text: str) -> Dict[str, float]:
        """
        Create simple word frequency embedding for text.

        This is a placeholder for development/testing. Production should use
        proper embedding models like BGE-M3.

        Args:
            text: Text to create embedding from

        Returns:
            Dictionary mapping words to frequencies (normalized)
        """
        if not text:
            return {}

        # Convert to lowercase and split into words
        words = text.lower().split()

        # Count word frequencies
        word_freq: Dict[str, float] = {}
        for word in words:
            # Remove punctuation
            word = ''.join(c for c in word if c.isalnum())
            if word:
                word_freq[word] = word_freq.get(word, 0.0) + 1.0

        # Normalize frequencies
        if word_freq:
            max_freq = max(word_freq.values())
            word_freq = {word: freq / max_freq for word, freq in word_freq.items()}

        return word_freq

    def _cosine_similarity(self, vec1: Dict[str, float], vec2: Dict[str, float]) -> float:
        """
        Calculate cosine similarity between two word frequency vectors.

        Args:
            vec1: First vector (word -> frequency)
            vec2: Second vector (word -> frequency)

        Returns:
            Cosine similarity score between 0.0 and 1.0
        """
        if not vec1 or not vec2:
            return 0.0

        # Calculate dot product
        common_words = set(vec1.keys()) & set(vec2.keys())
        dot_product = sum(vec1[word] * vec2[word] for word in common_words)

        # Calculate magnitudes
        magnitude1 = math.sqrt(sum(v * v for v in vec1.values()))
        magnitude2 = math.sqrt(sum(v * v for v in vec2.values()))

        # Avoid division by zero
        if magnitude1 == 0.0 or magnitude2 == 0.0:
            return 0.0

        # Calculate cosine similarity
        similarity = dot_product / (magnitude1 * magnitude2)

        # Ensure result is in [0, 1] range
        return max(0.0, min(1.0, similarity))

    async def get_collection_info(self) -> Dict[str, Any]:
        """
        Get information about the in-memory collection.

        Returns:
            Dictionary with collection statistics
        """
        async with self._lock:
            return {
                'name': 'inmemory_collection',
                'document_count': len(self._documents),
                'type': 'in-memory',
                'embedding_type': 'simple_word_frequency'
            }
