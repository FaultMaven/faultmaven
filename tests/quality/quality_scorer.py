"""
Quality Scoring System for Knowledge Base Evaluation

4-Dimension Scoring Framework:
1. Relevancy (40%) - Keyword matching, runbook retrieval accuracy
2. Completeness (25%) - Required sections present, comprehensive content
3. Actionability (20%) - Concrete steps, commands, solutions provided
4. Structure (15%) - Format compliance, metadata quality

Total possible score: 100 points
"""

import re
from typing import List, Dict, Any, Optional


class QualityScorer:
    """Evaluates knowledge base quality across 4 dimensions"""

    # Scoring weights
    WEIGHTS = {
        "relevancy": 0.40,
        "completeness": 0.25,
        "actionability": 0.20,
        "structure": 0.15
    }

    # Required sections for completeness check
    REQUIRED_SECTIONS = [
        "Quick Reference Card",
        "Diagnostic Steps",
        "Solutions",
        "Prevention",
        "Related Issues"
    ]

    def score_relevancy(
        self,
        query: str,
        search_results: List[Dict[str, Any]],
        expected_runbook_id: Optional[str] = None
    ) -> float:
        """
        Score relevancy (0-100 points).

        Criteria:
        - Top result matches expected runbook: 100 points
        - Expected runbook in top 3: 75 points
        - Expected runbook in top 5: 50 points
        - Keyword overlap in top result: up to 50 points

        Args:
            query: Search query
            search_results: List of search results with metadata
            expected_runbook_id: Expected runbook identifier

        Returns:
            Relevancy score (0-100)
        """
        if not search_results:
            return 0.0

        score = 0.0

        # Check if expected runbook is retrieved
        if expected_runbook_id:
            for i, result in enumerate(search_results[:5]):
                result_id = result.get("metadata", {}).get("document_id", "")
                if result_id == expected_runbook_id:
                    if i == 0:
                        score = 100.0  # Perfect match
                    elif i < 3:
                        score = 75.0   # In top 3
                    else:
                        score = 50.0   # In top 5
                    break
        else:
            # No expected result - score based on keyword overlap
            query_keywords = set(query.lower().split())
            top_result = search_results[0]
            result_text = top_result.get("document", "").lower()

            matching_keywords = sum(1 for kw in query_keywords if kw in result_text)
            keyword_overlap = matching_keywords / len(query_keywords) if query_keywords else 0
            score = keyword_overlap * 50.0

        return score

    def score_completeness(self, document_content: str) -> float:
        """
        Score completeness (0-100 points).

        Criteria:
        - All 5 required sections present: 100 points
        - Each missing section: -20 points
        - Additional content quality bonus: up to 20 points

        Args:
            document_content: Full document content

        Returns:
            Completeness score (0-100)
        """
        score = 0.0
        sections_found = 0

        # Check for required sections
        for section in self.REQUIRED_SECTIONS:
            if f"## {section}" in document_content or f"# {section}" in document_content:
                sections_found += 1

        # Base score from sections (80 points max)
        section_score = (sections_found / len(self.REQUIRED_SECTIONS)) * 80.0
        score += section_score

        # Content quality bonus (20 points max)
        # Check for code blocks (indicates actionable content)
        code_blocks = len(re.findall(r'```[\s\S]*?```', document_content))
        quality_bonus = min(code_blocks * 5, 20)  # Up to 4 code blocks = 20 points
        score += quality_bonus

        return min(score, 100.0)

    def score_actionability(self, document_content: str) -> float:
        """
        Score actionability (0-100 points).

        Criteria:
        - Commands/scripts present: 40 points
        - Step-by-step procedures: 30 points
        - Multiple solution paths: 20 points
        - Time estimates provided: 10 points

        Args:
            document_content: Full document content

        Returns:
            Actionability score (0-100)
        """
        score = 0.0

        # Check for commands/scripts (40 points)
        bash_blocks = len(re.findall(r'```bash[\s\S]*?```', document_content))
        if bash_blocks > 0:
            score += 40.0

        # Check for step-by-step procedures (30 points)
        step_patterns = [
            r'### Step \d+',
            r'## Step \d+',
            r'\d+\.\s+[A-Z]',  # Numbered lists
        ]
        has_steps = any(re.search(pattern, document_content) for pattern in step_patterns)
        if has_steps:
            score += 30.0

        # Check for multiple solutions (20 points)
        solution_patterns = [
            r'### Solution \d+',
            r'## Solution \d+',
        ]
        solutions = sum(len(re.findall(pattern, document_content)) for pattern in solution_patterns)
        if solutions >= 2:
            score += 20.0
        elif solutions == 1:
            score += 10.0

        # Check for time estimates (10 points)
        time_estimate_patterns = [
            r'\d+-\d+ minutes',
            r'⏱️.*\d+',
            r'Estimated.*time',
        ]
        has_time_estimate = any(re.search(pattern, document_content, re.IGNORECASE) for pattern in time_estimate_patterns)
        if has_time_estimate:
            score += 10.0

        return min(score, 100.0)

    def score_structure(self, document_content: str, metadata: Dict[str, Any]) -> float:
        """
        Score structure (0-100 points).

        Criteria:
        - Valid YAML frontmatter: 40 points
        - Required metadata fields: 30 points
        - Proper markdown formatting: 20 points
        - Consistent heading hierarchy: 10 points

        Args:
            document_content: Full document content
            metadata: Document metadata dictionary

        Returns:
            Structure score (0-100)
        """
        score = 0.0

        # Check for YAML frontmatter (40 points)
        if document_content.startswith('---'):
            score += 40.0

        # Check required metadata fields (30 points)
        required_fields = ["id", "title", "technology", "severity", "tags", "difficulty"]
        fields_present = sum(1 for field in required_fields if field in metadata)
        metadata_score = (fields_present / len(required_fields)) * 30.0
        score += metadata_score

        # Check markdown formatting (20 points)
        has_headers = bool(re.search(r'^#{1,3}\s+', document_content, re.MULTILINE))
        has_lists = bool(re.search(r'^\s*[-*+]\s+', document_content, re.MULTILINE))
        has_code_blocks = bool(re.search(r'```', document_content))

        formatting_score = sum([has_headers * 7, has_lists * 7, has_code_blocks * 6])
        score += formatting_score

        # Check heading hierarchy (10 points)
        headings = re.findall(r'^(#{1,6})\s+', document_content, re.MULTILINE)
        if headings and headings[0] == '#':  # Starts with H1
            score += 10.0

        return min(score, 100.0)

    def calculate_overall_score(
        self,
        query: str,
        search_results: List[Dict[str, Any]],
        document_content: str,
        metadata: Dict[str, Any],
        expected_runbook_id: Optional[str] = None
    ) -> Dict[str, float]:
        """
        Calculate overall quality score across all dimensions.

        Args:
            query: Search query
            search_results: Search results list
            document_content: Full document content
            metadata: Document metadata
            expected_runbook_id: Expected runbook ID for relevancy check

        Returns:
            Dictionary with dimension scores and overall score
        """
        relevancy = self.score_relevancy(query, search_results, expected_runbook_id)
        completeness = self.score_completeness(document_content)
        actionability = self.score_actionability(document_content)
        structure = self.score_structure(document_content, metadata)

        # Calculate weighted overall score
        overall = (
            relevancy * self.WEIGHTS["relevancy"] +
            completeness * self.WEIGHTS["completeness"] +
            actionability * self.WEIGHTS["actionability"] +
            structure * self.WEIGHTS["structure"]
        )

        return {
            "relevancy": round(relevancy, 2),
            "completeness": round(completeness, 2),
            "actionability": round(actionability, 2),
            "structure": round(structure, 2),
            "overall": round(overall, 2),
            "weights": self.WEIGHTS
        }

    def grade_score(self, score: float) -> str:
        """
        Convert numeric score to letter grade.

        Args:
            score: Numeric score (0-100)

        Returns:
            Letter grade (A+, A, B+, B, C, D, F)
        """
        if score >= 95:
            return "A+"
        elif score >= 90:
            return "A"
        elif score >= 85:
            return "B+"
        elif score >= 80:
            return "B"
        elif score >= 70:
            return "C"
        elif score >= 60:
            return "D"
        else:
            return "F"


if __name__ == "__main__":
    # Example usage
    scorer = QualityScorer()

    # Test sample document
    sample_doc = """---
id: test-runbook
title: Test Runbook
technology: kubernetes
severity: high
tags: [test, example]
difficulty: intermediate
---

# Test Runbook

## Quick Reference Card

Some content here.

## Diagnostic Steps

### Step 1: Check the issue

```bash
kubectl get pods
```

## Solutions

### Solution 1: Fix it

Some solution steps.

### Solution 2: Alternative fix

Another approach.

## Prevention

Best practices.

## Related Issues

- Related issue 1
"""

    sample_metadata = {
        "id": "test-runbook",
        "title": "Test Runbook",
        "technology": "kubernetes"
    }

    print("=== Quality Scoring System ===\n")
    print("Completeness:", scorer.score_completeness(sample_doc))
    print("Actionability:", scorer.score_actionability(sample_doc))
    print("Structure:", scorer.score_structure(sample_doc, sample_metadata))
