"""
Baseline Quality Test Suite

This module tests the quality of the knowledge base using benchmark queries
and a 4-dimension scoring system:
- Relevancy (40%)
- Completeness (25%)
- Actionability (20%)
- Structure (15%)

The baseline measurements establish a quality floor for continuous improvement.
"""

import pytest
import asyncio
from pathlib import Path
from typing import Dict, Any, List

from faultmaven.config.settings import get_settings
from faultmaven.core.knowledge.ingestion import KnowledgeIngester

from tests.quality.benchmark_queries import (
    get_all_benchmark_queries,
    get_queries_by_category,
    TOTAL_QUERIES,
    CATEGORIES
)
from tests.quality.quality_scorer import QualityScorer


@pytest.fixture(scope="module")
def knowledge_ingester():
    """Fixture to provide KnowledgeIngester instance"""
    try:
        settings = get_settings()
        return KnowledgeIngester(settings=settings)
    except Exception as e:
        pytest.skip(f"Cannot connect to ChromaDB: {e}")


@pytest.fixture(scope="module")
def quality_scorer():
    """Fixture to provide QualityScorer instance"""
    return QualityScorer()


@pytest.fixture(scope="module")
def runbook_contents():
    """Load all runbook file contents for structure analysis"""
    runbook_dir = Path("docs/runbooks")
    contents = {}

    for runbook_file in runbook_dir.rglob("*.md"):
        # Skip special files
        if runbook_file.name in ["README.md", "TEMPLATE.md", "CONTRIBUTING.md", "REVIEW_GUIDELINES.md"]:
            continue

        with open(runbook_file, 'r', encoding='utf-8') as f:
            content = f.read()

        # Extract document ID from YAML frontmatter
        if content.startswith('---'):
            parts = content.split('---', 2)
            if len(parts) >= 3:
                import yaml
                try:
                    metadata = yaml.safe_load(parts[1])
                    doc_id = metadata.get('id')
                    if doc_id:
                        contents[doc_id] = {
                            "content": content,
                            "metadata": metadata,
                            "file_path": str(runbook_file)
                        }
                except:
                    pass

    return contents


class TestKnowledgeBaseRelevancy:
    """Test relevancy dimension (40% weight)"""

    @pytest.mark.asyncio
    async def test_kubernetes_query_relevancy(self, knowledge_ingester):
        """Test Kubernetes-related query relevancy"""
        k8s_queries = get_queries_by_category("kubernetes")

        results = []
        for query_spec in k8s_queries:
            search_results = await knowledge_ingester.search(
                query=query_spec["query"],
                n_results=5
            )

            if query_spec.get("expected_runbook_id"):
                # Check if expected runbook is in results
                found = any(
                    r.get("metadata", {}).get("document_id") == query_spec["expected_runbook_id"]
                    for r in search_results
                )
                results.append(found)

        # At least 70% of queries should retrieve expected runbook
        relevancy_rate = sum(results) / len(results) if results else 0
        assert relevancy_rate >= 0.70, f"Kubernetes relevancy rate too low: {relevancy_rate:.2%}"

    @pytest.mark.asyncio
    async def test_redis_query_relevancy(self, knowledge_ingester):
        """Test Redis-related query relevancy"""
        redis_queries = get_queries_by_category("redis")

        results = []
        for query_spec in redis_queries:
            search_results = await knowledge_ingester.search(
                query=query_spec["query"],
                n_results=5
            )

            if query_spec.get("expected_runbook_id"):
                found = any(
                    r.get("metadata", {}).get("document_id") == query_spec["expected_runbook_id"]
                    for r in search_results
                )
                results.append(found)

        relevancy_rate = sum(results) / len(results) if results else 0
        assert relevancy_rate >= 0.70, f"Redis relevancy rate too low: {relevancy_rate:.2%}"

    @pytest.mark.asyncio
    async def test_overall_relevancy_score(self, knowledge_ingester, quality_scorer):
        """Test overall relevancy across all queries"""
        all_queries = get_all_benchmark_queries()
        scores = []

        for query_spec in all_queries[:10]:  # Sample first 10 queries
            if not query_spec.get("expected_runbook_id"):
                continue

            search_results = await knowledge_ingester.search(
                query=query_spec["query"],
                n_results=5
            )

            relevancy_score = quality_scorer.score_relevancy(
                query=query_spec["query"],
                search_results=search_results,
                expected_runbook_id=query_spec.get("expected_runbook_id")
            )
            scores.append(relevancy_score)

        avg_relevancy = sum(scores) / len(scores) if scores else 0
        # Average relevancy should be at least 60/100
        assert avg_relevancy >= 60.0, f"Average relevancy too low: {avg_relevancy:.2f}"


class TestKnowledgeBaseCompleteness:
    """Test completeness dimension (25% weight)"""

    def test_required_sections_present(self, runbook_contents, quality_scorer):
        """Test that all runbooks have required sections"""
        scores = []

        for doc_id, doc_data in runbook_contents.items():
            completeness_score = quality_scorer.score_completeness(doc_data["content"])
            scores.append(completeness_score)

        avg_completeness = sum(scores) / len(scores) if scores else 0
        # Average completeness should be at least 70/100
        assert avg_completeness >= 70.0, f"Average completeness too low: {avg_completeness:.2f}"

    def test_all_runbooks_have_code_examples(self, runbook_contents):
        """Test that all runbooks include code examples"""
        runbooks_with_code = 0

        for doc_id, doc_data in runbook_contents.items():
            if "```" in doc_data["content"]:
                runbooks_with_code += 1

        code_coverage_rate = runbooks_with_code / len(runbook_contents) if runbook_contents else 0
        # At least 90% should have code examples
        assert code_coverage_rate >= 0.90, f"Code coverage too low: {code_coverage_rate:.2%}"


class TestKnowledgeBaseActionability:
    """Test actionability dimension (20% weight)"""

    def test_bash_commands_present(self, runbook_contents):
        """Test that runbooks include bash commands"""
        runbooks_with_bash = 0

        for doc_id, doc_data in runbook_contents.items():
            if "```bash" in doc_data["content"]:
                runbooks_with_bash += 1

        bash_coverage_rate = runbooks_with_bash / len(runbook_contents) if runbook_contents else 0
        # At least 80% should have bash commands
        assert bash_coverage_rate >= 0.80, f"Bash command coverage too low: {bash_coverage_rate:.2%}"

    def test_actionability_scores(self, runbook_contents, quality_scorer):
        """Test overall actionability scores"""
        scores = []

        for doc_id, doc_data in runbook_contents.items():
            actionability_score = quality_scorer.score_actionability(doc_data["content"])
            scores.append(actionability_score)

        avg_actionability = sum(scores) / len(scores) if scores else 0
        # Average actionability should be at least 60/100
        assert avg_actionability >= 60.0, f"Average actionability too low: {avg_actionability:.2f}"

    def test_multiple_solutions_provided(self, runbook_contents):
        """Test that runbooks provide multiple solution paths"""
        runbooks_with_multiple_solutions = 0

        for doc_id, doc_data in runbook_contents.items():
            import re
            solutions = len(re.findall(r'### Solution \d+', doc_data["content"]))
            if solutions >= 2:
                runbooks_with_multiple_solutions += 1

        multi_solution_rate = runbooks_with_multiple_solutions / len(runbook_contents) if runbook_contents else 0
        # At least 50% should have multiple solutions
        assert multi_solution_rate >= 0.50, f"Multiple solution rate too low: {multi_solution_rate:.2%}"


class TestKnowledgeBaseStructure:
    """Test structure dimension (15% weight)"""

    def test_yaml_frontmatter_present(self, runbook_contents):
        """Test that all runbooks have YAML frontmatter"""
        for doc_id, doc_data in runbook_contents.items():
            assert doc_data["content"].startswith('---'), f"Runbook {doc_id} missing YAML frontmatter"

    def test_required_metadata_fields(self, runbook_contents):
        """Test that all runbooks have required metadata"""
        required_fields = ["id", "title", "technology", "severity", "tags", "difficulty"]

        for doc_id, doc_data in runbook_contents.items():
            metadata = doc_data["metadata"]
            for field in required_fields:
                assert field in metadata, f"Runbook {doc_id} missing field: {field}"

    def test_structure_scores(self, runbook_contents, quality_scorer):
        """Test overall structure scores"""
        scores = []

        for doc_id, doc_data in runbook_contents.items():
            structure_score = quality_scorer.score_structure(
                doc_data["content"],
                doc_data["metadata"]
            )
            scores.append(structure_score)

        avg_structure = sum(scores) / len(scores) if scores else 0
        # Average structure should be at least 80/100
        assert avg_structure >= 80.0, f"Average structure score too low: {avg_structure:.2f}"


class TestOverallQuality:
    """Test overall quality metrics"""

    @pytest.mark.asyncio
    async def test_baseline_overall_quality(self, knowledge_ingester, runbook_contents, quality_scorer):
        """Calculate and test baseline overall quality score"""
        all_queries = get_all_benchmark_queries()
        overall_scores = []

        # Sample queries for testing
        for query_spec in all_queries[:5]:  # Test with first 5 queries
            if not query_spec.get("expected_runbook_id"):
                continue

            # Search
            search_results = await knowledge_ingester.search(
                query=query_spec["query"],
                n_results=5
            )

            # Get runbook content
            expected_id = query_spec["expected_runbook_id"]
            if expected_id not in runbook_contents:
                continue

            doc_data = runbook_contents[expected_id]

            # Calculate overall score
            scores = quality_scorer.calculate_overall_score(
                query=query_spec["query"],
                search_results=search_results,
                document_content=doc_data["content"],
                metadata=doc_data["metadata"],
                expected_runbook_id=expected_id
            )

            overall_scores.append(scores["overall"])

        if overall_scores:
            avg_overall = sum(overall_scores) / len(overall_scores)
            # Overall score should be at least 65/100
            assert avg_overall >= 65.0, f"Baseline quality too low: {avg_overall:.2f}"

    def test_knowledge_base_collection_stats(self, knowledge_ingester):
        """Verify knowledge base has sufficient content"""
        stats = knowledge_ingester.get_collection_stats()

        # Should have at least 100 chunks (from 10 runbooks)
        assert stats["total_chunks"] >= 100, f"Not enough chunks: {stats['total_chunks']}"

        # Should have runbook document type
        assert "runbook" in stats["document_types"], "Missing runbook document type"

        # Should have diverse tags
        assert len(stats["top_tags"]) >= 5, f"Not enough tag diversity: {len(stats['top_tags'])}"


@pytest.mark.asyncio
async def test_generate_baseline_report(knowledge_ingester, runbook_contents, quality_scorer):
    """Generate comprehensive baseline quality report"""
    print("\n" + "=" * 60)
    print("KNOWLEDGE BASE BASELINE QUALITY REPORT")
    print("=" * 60)

    # Collection stats
    stats = knowledge_ingester.get_collection_stats()
    print(f"\n📊 Collection Statistics:")
    print(f"  Total chunks: {stats['total_chunks']}")
    print(f"  Document types: {stats['document_types']}")
    print(f"  Top 5 tags: {dict(list(stats['top_tags'].items())[:5])}")

    # Dimension scores
    print(f"\n📈 Quality Scores by Dimension:")

    dimension_scores = {
        "relevancy": [],
        "completeness": [],
        "actionability": [],
        "structure": []
    }

    # Calculate scores for all runbooks
    for doc_id, doc_data in runbook_contents.items():
        dimension_scores["completeness"].append(
            quality_scorer.score_completeness(doc_data["content"])
        )
        dimension_scores["actionability"].append(
            quality_scorer.score_actionability(doc_data["content"])
        )
        dimension_scores["structure"].append(
            quality_scorer.score_structure(doc_data["content"], doc_data["metadata"])
        )

    # Test relevancy with sample queries
    all_queries = get_all_benchmark_queries()
    for query_spec in all_queries[:10]:
        if not query_spec.get("expected_runbook_id"):
            continue

        search_results = await knowledge_ingester.search(
            query=query_spec["query"],
            n_results=5
        )

        relevancy_score = quality_scorer.score_relevancy(
            query=query_spec["query"],
            search_results=search_results,
            expected_runbook_id=query_spec.get("expected_runbook_id")
        )
        dimension_scores["relevancy"].append(relevancy_score)

    # Print averages
    for dimension, scores in dimension_scores.items():
        if scores:
            avg = sum(scores) / len(scores)
            grade = quality_scorer.grade_score(avg)
            weight = quality_scorer.WEIGHTS.get(dimension, 0) * 100
            print(f"  {dimension.capitalize():15} {avg:6.2f}/100  (Grade: {grade})  [Weight: {weight:.0f}%]")

    # Overall score
    overall_score = sum(
        sum(scores) / len(scores) * quality_scorer.WEIGHTS[dimension]
        for dimension, scores in dimension_scores.items()
        if scores
    )
    overall_grade = quality_scorer.grade_score(overall_score)

    print(f"\n🎯 Overall Quality Score: {overall_score:.2f}/100  (Grade: {overall_grade})")

    print("\n" + "=" * 60)
    print("Baseline quality measurements complete!")
    print("=" * 60 + "\n")
