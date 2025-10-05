"""
Quality Benchmark Queries for Knowledge Base Testing

This module defines a comprehensive set of benchmark queries to test the quality
of the knowledge base retrieval system across multiple dimensions:
- Relevancy: Ability to find correct runbooks
- Completeness: Coverage of all major troubleshooting scenarios
- Actionability: Retrieval of concrete solutions
- Structure: Proper formatting and organization
"""

from typing import List, Dict, Any


# Benchmark queries organized by category
KUBERNETES_QUERIES = [
    {
        "query": "pod keeps crashing and restarting",
        "expected_runbook_id": "k8s-pod-crashloopbackoff",
        "category": "kubernetes",
        "difficulty": "intermediate"
    },
    {
        "query": "kubernetes pod out of memory",
        "expected_runbook_id": "k8s-pod-oomkilled",
        "category": "kubernetes",
        "difficulty": "intermediate"
    },
    {
        "query": "cannot pull container image",
        "expected_runbook_id": "k8s-pod-imagepullbackoff",
        "category": "kubernetes",
        "difficulty": "beginner"
    },
    {
        "query": "kubernetes node not ready status",
        "expected_runbook_id": "k8s-node-not-ready",
        "category": "kubernetes",
        "difficulty": "intermediate"
    },
    {
        "query": "pod memory limit exceeded",
        "expected_runbook_id": "k8s-pod-oomkilled",
        "category": "kubernetes",
        "difficulty": "intermediate"
    },
]

REDIS_QUERIES = [
    {
        "query": "cannot connect to redis server",
        "expected_runbook_id": "redis-connection-refused",
        "category": "redis",
        "difficulty": "beginner"
    },
    {
        "query": "redis memory full",
        "expected_runbook_id": "redis-out-of-memory",
        "category": "redis",
        "difficulty": "intermediate"
    },
    {
        "query": "redis connection refused error",
        "expected_runbook_id": "redis-connection-refused",
        "category": "redis",
        "difficulty": "beginner"
    },
]

POSTGRESQL_QUERIES = [
    {
        "query": "postgres connection pool exhausted",
        "expected_runbook_id": "postgres-connection-pool-exhausted",
        "category": "postgresql",
        "difficulty": "intermediate"
    },
    {
        "query": "database queries running slow",
        "expected_runbook_id": "postgres-slow-queries",
        "category": "postgresql",
        "difficulty": "intermediate"
    },
    {
        "query": "postgresql performance degradation",
        "expected_runbook_id": "postgres-slow-queries",
        "category": "postgresql",
        "difficulty": "intermediate"
    },
]

NETWORKING_QUERIES = [
    {
        "query": "dns resolution failing",
        "expected_runbook_id": "network-dns-resolution-failure",
        "category": "networking",
        "difficulty": "beginner"
    },
    {
        "query": "connection timeout error",
        "expected_runbook_id": "network-connection-timeout",
        "category": "networking",
        "difficulty": "beginner"
    },
    {
        "query": "cannot resolve domain name",
        "expected_runbook_id": "network-dns-resolution-failure",
        "category": "networking",
        "difficulty": "beginner"
    },
]

# Edge case queries to test robustness
EDGE_CASE_QUERIES = [
    {
        "query": "pod",
        "expected_runbook_id": None,  # Too vague
        "category": "kubernetes",
        "difficulty": "edge_case",
        "notes": "Tests handling of overly vague queries"
    },
    {
        "query": "application crashing repeatedly with memory errors",
        "expected_runbook_id": "k8s-pod-oomkilled",
        "category": "kubernetes",
        "difficulty": "edge_case",
        "notes": "Tests natural language understanding"
    },
    {
        "query": "service unreachable firewall blocking",
        "expected_runbook_id": "network-connection-timeout",
        "category": "networking",
        "difficulty": "edge_case",
        "notes": "Tests multi-symptom queries"
    },
]


def get_all_benchmark_queries() -> List[Dict[str, Any]]:
    """
    Get all benchmark queries combined.

    Returns:
        List of all benchmark query dictionaries
    """
    return (
        KUBERNETES_QUERIES +
        REDIS_QUERIES +
        POSTGRESQL_QUERIES +
        NETWORKING_QUERIES +
        EDGE_CASE_QUERIES
    )


def get_queries_by_category(category: str) -> List[Dict[str, Any]]:
    """
    Get benchmark queries filtered by category.

    Args:
        category: Category to filter by (kubernetes, redis, postgresql, networking)

    Returns:
        List of benchmark queries for the specified category
    """
    all_queries = get_all_benchmark_queries()
    return [q for q in all_queries if q["category"] == category]


def get_queries_by_difficulty(difficulty: str) -> List[Dict[str, Any]]:
    """
    Get benchmark queries filtered by difficulty.

    Args:
        difficulty: Difficulty level (beginner, intermediate, edge_case)

    Returns:
        List of benchmark queries for the specified difficulty
    """
    all_queries = get_all_benchmark_queries()
    return [q for q in all_queries if q["difficulty"] == difficulty]


# Summary statistics
TOTAL_QUERIES = len(get_all_benchmark_queries())
CATEGORIES = {
    "kubernetes": len(KUBERNETES_QUERIES),
    "redis": len(REDIS_QUERIES),
    "postgresql": len(POSTGRESQL_QUERIES),
    "networking": len(NETWORKING_QUERIES),
    "edge_cases": len(EDGE_CASE_QUERIES)
}

if __name__ == "__main__":
    print("=== Knowledge Base Benchmark Queries ===")
    print(f"\nTotal queries: {TOTAL_QUERIES}")
    print("\nBreakdown by category:")
    for category, count in CATEGORIES.items():
        print(f"  {category}: {count} queries")

    print("\n\nSample queries:")
    for query in get_all_benchmark_queries()[:5]:
        print(f"\n  Query: '{query['query']}'")
        print(f"  Expected: {query['expected_runbook_id']}")
        print(f"  Category: {query['category']} | Difficulty: {query['difficulty']}")
