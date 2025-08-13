"""
Infrastructure test utilities for real component testing.

This module provides utilities and fixtures for testing infrastructure components
with real behavior instead of heavy mocking. Supports the minimal mocking
architecture that achieved 80%+ improvements in Phases 1-3.
"""

import asyncio
import pytest
import time
import json
import contextlib
from typing import Dict, List, Any, AsyncGenerator, Optional
from aiohttp import web, ClientSession
from aiohttp.test_utils import TestServer
from unittest.mock import patch, MagicMock

# Test server utilities
class MockHTTPServer:
    """Utility for creating test HTTP servers with controllable behavior."""
    
    def __init__(self, port: Optional[int] = None):
        self.port = port
        self.request_history = []
        self.response_config = {}
        self.failure_config = {}
        self.latency_config = {}
        
    async def create_server(self, routes: List[Dict] = None):
        """Create HTTP server with configurable routes."""
        app = web.Application()
        
        # Default catch-all handler
        async def default_handler(request):
            self.request_history.append({
                "method": request.method,
                "path": request.path_qs,
                "headers": dict(request.headers),
                "timestamp": time.time()
            })
            
            route_key = f"{request.method}:{request.path}"
            
            # Check for configured latency
            if route_key in self.latency_config:
                await asyncio.sleep(self.latency_config[route_key])
            
            # Check for configured failures
            if route_key in self.failure_config:
                failure_config = self.failure_config[route_key]
                if failure_config.get("count", 0) > 0:
                    failure_config["count"] -= 1
                    if failure_config["type"] == "timeout":
                        await asyncio.sleep(10)  # Simulate timeout
                    elif failure_config["type"] == "error":
                        raise web.HTTPInternalServerError(text=failure_config.get("message", "Server error"))
                    elif failure_config["type"] == "rate_limit":
                        raise web.HTTPTooManyRequests(text="Rate limit exceeded")
            
            # Return configured response or default
            if route_key in self.response_config:
                response_data = self.response_config[route_key]
                return web.json_response(response_data)
            else:
                return web.json_response({
                    "status": "ok",
                    "method": request.method,
                    "path": request.path,
                    "timestamp": time.time()
                })
        
        # Add routes
        if routes:
            for route_config in routes:
                method = route_config.get("method", "GET")
                path = route_config.get("path", "/")
                handler = route_config.get("handler", default_handler)
                
                if method.upper() == "GET":
                    app.router.add_get(path, handler)
                elif method.upper() == "POST":
                    app.router.add_post(path, handler)
                elif method.upper() == "PUT":
                    app.router.add_put(path, handler)
                elif method.upper() == "DELETE":
                    app.router.add_delete(path, handler)
        else:
            # Add catch-all routes
            app.router.add_route("*", "/{path:.*}", default_handler)
        
        server = TestServer(app, port=self.port)
        await server.start_server()
        return server
    
    def configure_response(self, method: str, path: str, response_data: Dict):
        """Configure response for specific route."""
        route_key = f"{method.upper()}:{path}"
        self.response_config[route_key] = response_data
    
    def configure_failure(self, method: str, path: str, failure_type: str, count: int = 1, message: str = None):
        """Configure failure behavior for specific route."""
        route_key = f"{method.upper()}:{path}"
        self.failure_config[route_key] = {
            "type": failure_type,
            "count": count,
            "message": message or f"{failure_type} error"
        }
    
    def configure_latency(self, method: str, path: str, delay_seconds: float):
        """Configure latency for specific route."""
        route_key = f"{method.upper()}:{path}"
        self.latency_config[route_key] = delay_seconds
    
    def get_request_history(self) -> List[Dict]:
        """Get history of requests made to server."""
        return self.request_history.copy()
    
    def clear_history(self):
        """Clear request history."""
        self.request_history.clear()


class MockRedisCluster:
    """Mock Redis cluster with realistic behavior for testing."""
    
    def __init__(self):
        self.data = {}
        self.expirations = {}
        self.call_count = 0
        self.operation_times = []
        
    async def get(self, key: str) -> Optional[str]:
        """Get value from Redis."""
        start_time = time.time()
        self.call_count += 1
        
        # Check if key is expired
        if key in self.expirations and time.time() > self.expirations[key]:
            del self.data[key]
            del self.expirations[key]
            result = None
        else:
            result = self.data.get(key)
        
        self.operation_times.append(time.time() - start_time)
        return result
    
    async def set(self, key: str, value: str, ex: Optional[int] = None) -> bool:
        """Set value in Redis."""
        start_time = time.time()
        self.call_count += 1
        
        self.data[key] = value
        
        if ex:
            self.expirations[key] = time.time() + ex
        elif key in self.expirations:
            # Remove expiration if no TTL set
            del self.expirations[key]
        
        self.operation_times.append(time.time() - start_time)
        return True
    
    async def delete(self, key: str) -> int:
        """Delete key from Redis."""
        start_time = time.time()
        self.call_count += 1
        
        if key in self.data:
            del self.data[key]
            if key in self.expirations:
                del self.expirations[key]
            result = 1
        else:
            result = 0
        
        self.operation_times.append(time.time() - start_time)
        return result
    
    async def exists(self, key: str) -> int:
        """Check if key exists in Redis."""
        start_time = time.time()
        self.call_count += 1
        
        # Check expiration
        if key in self.expirations and time.time() > self.expirations[key]:
            del self.data[key]
            del self.expirations[key]
            result = 0
        else:
            result = 1 if key in self.data else 0
        
        self.operation_times.append(time.time() - start_time)
        return result
    
    async def expire(self, key: str, seconds: int) -> bool:
        """Set expiration for key."""
        start_time = time.time()
        self.call_count += 1
        
        if key in self.data:
            self.expirations[key] = time.time() + seconds
            result = True
        else:
            result = False
        
        self.operation_times.append(time.time() - start_time)
        return result
    
    async def flushall(self):
        """Clear all data."""
        self.data.clear()
        self.expirations.clear()
    
    def get_stats(self) -> Dict:
        """Get performance statistics."""
        if not self.operation_times:
            return {"call_count": 0, "avg_time": 0, "max_time": 0}
        
        return {
            "call_count": self.call_count,
            "avg_time": sum(self.operation_times) / len(self.operation_times),
            "max_time": max(self.operation_times),
            "min_time": min(self.operation_times)
        }


class MockVectorDatabase:
    """Mock vector database with realistic similarity search."""
    
    def __init__(self):
        self.documents = {}
        self.embeddings = {}
        self.metadata = {}
        self.operation_count = 0
        
    def add_documents(self, docs: List[Dict]):
        """Add documents to vector database."""
        self.operation_count += 1
        
        for doc in docs:
            doc_id = doc["id"]
            content = doc["content"]
            meta = doc.get("metadata", {})
            
            self.documents[doc_id] = content
            self.metadata[doc_id] = meta
            
            # Simple text-based "embedding" for testing
            self.embeddings[doc_id] = self._generate_text_embedding(content)
    
    def search(self, query: str, k: int = 5) -> List[Dict]:
        """Search for similar documents."""
        self.operation_count += 1
        
        if not self.documents:
            return []
        
        query_embedding = self._generate_text_embedding(query)
        
        # Calculate similarities
        similarities = []
        for doc_id, doc_embedding in self.embeddings.items():
            similarity = self._calculate_similarity(query_embedding, doc_embedding)
            similarities.append((doc_id, similarity))
        
        # Sort by similarity and return top k
        similarities.sort(key=lambda x: x[1], reverse=True)
        top_results = similarities[:k]
        
        results = []
        for doc_id, similarity in top_results:
            results.append({
                "id": doc_id,
                "content": self.documents[doc_id],
                "metadata": self.metadata.get(doc_id, {}),
                "score": similarity
            })
        
        return results
    
    def delete_documents(self, doc_ids: List[str]):
        """Delete documents by IDs."""
        self.operation_count += 1
        
        for doc_id in doc_ids:
            self.documents.pop(doc_id, None)
            self.embeddings.pop(doc_id, None)
            self.metadata.pop(doc_id, None)
    
    def _generate_text_embedding(self, text: str) -> Dict[str, float]:
        """Generate simple text-based embedding for similarity testing."""
        words = text.lower().split()
        word_counts = {}
        
        for word in words:
            word_counts[word] = word_counts.get(word, 0) + 1
        
        # Normalize
        total_words = len(words)
        if total_words > 0:
            for word in word_counts:
                word_counts[word] = word_counts[word] / total_words
        
        return word_counts
    
    def _calculate_similarity(self, embedding1: Dict, embedding2: Dict) -> float:
        """Calculate cosine similarity between embeddings."""
        common_words = set(embedding1.keys()) & set(embedding2.keys())
        
        if not common_words:
            return 0.0
        
        dot_product = sum(embedding1[word] * embedding2[word] for word in common_words)
        
        norm1 = sum(val ** 2 for val in embedding1.values()) ** 0.5
        norm2 = sum(val ** 2 for val in embedding2.values()) ** 0.5
        
        if norm1 == 0 or norm2 == 0:
            return 0.0
        
        return dot_product / (norm1 * norm2)
    
    def get_document_count(self) -> int:
        """Get total document count."""
        return len(self.documents)
    
    def get_operation_count(self) -> int:
        """Get total operation count for performance testing."""
        return self.operation_count


# Fixtures for common infrastructure testing
@pytest.fixture
async def http_test_server():
    """Fixture providing HTTP test server."""
    server_util = TestHTTPServer()
    server = await server_util.create_server()
    
    yield server, server_util
    
    await server.close()


@pytest.fixture
async def redis_test_cluster():
    """Fixture providing Redis test cluster."""
    redis_cluster = MockRedisCluster()
    yield redis_cluster


@pytest.fixture
async def vector_test_database():
    """Fixture providing vector database for testing."""
    vector_db = MockVectorDatabase()
    yield vector_db


@pytest.fixture
async def integrated_test_infrastructure(http_test_server, redis_test_cluster, vector_test_database):
    """Fixture providing complete test infrastructure."""
    server, server_util = http_test_server
    redis_cluster = redis_test_cluster
    vector_db = vector_test_database
    
    yield {
        "http_server": server,
        "server_util": server_util,
        "redis": redis_cluster,
        "vector_db": vector_db
    }


class PerformanceBenchmark:
    """Utility for performance benchmarking in tests."""
    
    def __init__(self, operation_name: str):
        self.operation_name = operation_name
        self.start_time = None
        self.end_time = None
        self.measurements = []
    
    def start(self):
        """Start timing measurement."""
        self.start_time = time.time()
    
    def stop(self):
        """Stop timing measurement."""
        if self.start_time is None:
            raise ValueError("Benchmark not started")
        
        self.end_time = time.time()
        duration = self.end_time - self.start_time
        self.measurements.append(duration)
        return duration
    
    def get_duration(self) -> float:
        """Get last measurement duration."""
        if not self.measurements:
            raise ValueError("No measurements taken")
        return self.measurements[-1]
    
    def get_average_duration(self) -> float:
        """Get average duration across all measurements."""
        if not self.measurements:
            raise ValueError("No measurements taken")
        return sum(self.measurements) / len(self.measurements)
    
    def get_statistics(self) -> Dict:
        """Get comprehensive statistics."""
        if not self.measurements:
            return {"count": 0}
        
        return {
            "operation": self.operation_name,
            "count": len(self.measurements),
            "total_time": sum(self.measurements),
            "average_time": sum(self.measurements) / len(self.measurements),
            "min_time": min(self.measurements),
            "max_time": max(self.measurements),
            "median_time": sorted(self.measurements)[len(self.measurements) // 2]
        }
    
    @contextlib.contextmanager
    def measure(self):
        """Context manager for timing operations."""
        self.start()
        try:
            yield self
        finally:
            self.stop()


class ConcurrencyTester:
    """Utility for testing concurrent operations."""
    
    @staticmethod
    async def run_concurrent_operations(operations: List, max_concurrency: int = None):
        """Run operations concurrently with optional concurrency limit."""
        if max_concurrency and len(operations) > max_concurrency:
            # Run in batches
            results = []
            for i in range(0, len(operations), max_concurrency):
                batch = operations[i:i + max_concurrency]
                batch_results = await asyncio.gather(*batch)
                results.extend(batch_results)
            return results
        else:
            return await asyncio.gather(*operations)
    
    @staticmethod
    async def measure_concurrent_performance(operation_factory, count: int, max_concurrency: int = None):
        """Measure performance of concurrent operations."""
        start_time = time.time()
        
        operations = [operation_factory(i) for i in range(count)]
        results = await ConcurrencyTester.run_concurrent_operations(operations, max_concurrency)
        
        total_time = time.time() - start_time
        
        return {
            "operation_count": count,
            "total_time": total_time,
            "operations_per_second": count / total_time if total_time > 0 else 0,
            "average_time_per_operation": total_time / count if count > 0 else 0,
            "results": results
        }


# Helper functions for common test scenarios
def create_sample_log_data(count: int = 100) -> List[str]:
    """Create sample log data for testing."""
    log_levels = ["INFO", "WARN", "ERROR", "DEBUG"]
    log_messages = [
        "User authentication successful",
        "Database connection established", 
        "API request processed",
        "Cache hit for key",
        "Background job completed",
        "Configuration loaded",
        "Session created",
        "Data validation passed",
        "File upload completed",
        "Email notification sent"
    ]
    
    logs = []
    for i in range(count):
        level = log_levels[i % len(log_levels)]
        message = log_messages[i % len(log_messages)]
        timestamp = f"2025-01-15 10:{i%60:02d}:{i%60:02d}"
        
        log_entry = f"{timestamp} [{level}] {message} {i}"
        logs.append(log_entry)
    
    return logs


def create_sample_documents(count: int = 50, categories: List[str] = None) -> List[Dict]:
    """Create sample documents for vector database testing."""
    if categories is None:
        categories = ["error", "solution", "configuration", "performance", "security"]
    
    documents = []
    
    for i in range(count):
        category = categories[i % len(categories)]
        
        doc = {
            "id": f"doc-{i:03d}",
            "content": f"This is a {category} document number {i} with technical content about troubleshooting {category} issues in distributed systems.",
            "metadata": {
                "category": category,
                "doc_number": i,
                "created_at": "2025-01-15T10:00:00Z",
                "priority": "high" if i % 3 == 0 else "medium",
                "tags": [category, "troubleshooting", f"doc-{i}"]
            }
        }
        
        documents.append(doc)
    
    return documents


def create_sample_metrics_data() -> Dict[str, List]:
    """Create sample metrics data for testing."""
    import random
    
    return {
        "response_times": [random.uniform(10, 200) for _ in range(100)],
        "error_counts": [random.randint(0, 5) for _ in range(24)],  # 24 hours
        "memory_usage": [random.uniform(60, 95) for _ in range(60)],  # 60 minutes
        "cpu_usage": [random.uniform(20, 80) for _ in range(60)],
        "request_counts": [random.randint(100, 1000) for _ in range(24)]
    }


def assert_performance_within_bounds(duration: float, min_time: float, max_time: float, operation: str = "operation"):
    """Assert that operation performance is within expected bounds."""
    assert min_time <= duration <= max_time, f"{operation} took {duration:.3f}s, expected between {min_time:.3f}s and {max_time:.3f}s"


def assert_concurrent_performance(results: Dict, min_ops_per_second: float, operation: str = "operations"):
    """Assert that concurrent performance meets minimum requirements."""
    ops_per_second = results["operations_per_second"]
    assert ops_per_second >= min_ops_per_second, f"{operation} achieved {ops_per_second:.1f} ops/sec, expected at least {min_ops_per_second} ops/sec"