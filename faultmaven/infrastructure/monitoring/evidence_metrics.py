"""Evidence Processing Metrics and Alerts

This module defines metrics and alert rules for evidence creation failure
handling, retry infrastructure, and storage cleanup operations.

Design Reference:
- docs/architecture/data-processing/EVIDENCE-CREATION-FAILURE-MODES.md
- Task 7.6: Monitoring & Alerts

Key Metrics:
- Failure rates: upload_failures, llm_timeouts, llm_errors, db_insert_failures
- Retry metrics: retry_attempts, retry_successes, retry_permanent_failures
- Category fallback: category_fallback (LLM schema drift detection)
- Storage cleanup: orphaned_files_cleaned

Alert Rules:
- High LLM timeout rate (>5%)
- Permanent retry failures (requires intervention)
- High orphaned file rate (>10 files per cleanup)
- Category fallback rate (schema/prompt drift)

Integration:
- Uses existing metrics_collector from infrastructure/monitoring/
- Alerts via alerting service
- Grafana dashboard definitions (YAML)
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# =============================================================================
# Metric Definitions
# =============================================================================

EVIDENCE_METRICS = {
    # Failure counters (increment on error)
    "evidence.upload_failures": {
        "type": "counter",
        "description": "Number of file upload failures during evidence creation",
        "labels": ["case_id", "error_type"],
        "alert_threshold": None,  # No alert - clean failure before state change
    },
    "evidence.llm_timeouts": {
        "type": "counter",
        "description": "Number of LLM analysis timeouts (triggers async retry)",
        "labels": ["case_id", "provider"],
        "alert_threshold": "rate > 5%",  # Alert if >5% of LLM calls timeout
    },
    "evidence.llm_errors": {
        "type": "counter",
        "description": "Number of LLM analysis errors (non-timeout failures)",
        "labels": ["case_id", "provider", "error_type"],
        "alert_threshold": "rate > 10%",  # Alert if >10% of LLM calls error
    },
    "evidence.db_insert_failures": {
        "type": "counter",
        "description": "Number of database insert failures after successful LLM analysis",
        "labels": ["case_id", "error_type"],
        "alert_threshold": "count > 0",  # Alert on any DB failure (preserves LLM work)
    },
    # Retry counters
    "evidence.llm_retry_attempts": {
        "type": "counter",
        "description": "Number of LLM analysis retry attempts",
        "labels": ["retry_number"],
        "alert_threshold": None,
    },
    "evidence.llm_retry_successes": {
        "type": "counter",
        "description": "Number of successful LLM retries",
        "labels": ["retry_number"],
        "alert_threshold": None,
    },
    "evidence.llm_retry_permanent_failures": {
        "type": "counter",
        "description": "Number of permanent LLM failures after max retries",
        "labels": ["case_id"],
        "alert_threshold": "count > 0",  # Alert on any permanent failure
    },
    "evidence.db_retry_attempts": {
        "type": "counter",
        "description": "Number of database insert retry attempts",
        "labels": ["retry_number"],
        "alert_threshold": None,
    },
    "evidence.db_retry_successes": {
        "type": "counter",
        "description": "Number of successful database insert retries",
        "labels": ["retry_number"],
        "alert_threshold": None,
    },
    "evidence.db_retry_permanent_failures": {
        "type": "counter",
        "description": "Number of permanent DB insert failures after max retries",
        "labels": ["case_id"],
        "alert_threshold": "count > 0",  # Critical alert - requires manual intervention
    },
    # Category fallback (LLM schema drift detection)
    "evidence.category_fallback": {
        "type": "counter",
        "description": "Number of times LLM returned invalid category (fallback applied)",
        "labels": ["category_attempted"],
        "alert_threshold": "count > 10",  # Alert if >10 fallbacks (prompt drift)
    },
    # Storage cleanup
    "evidence.orphaned_files_found": {
        "type": "gauge",
        "description": "Number of orphaned files found during cleanup scan",
        "labels": [],
        "alert_threshold": "value > 50",  # Alert if >50 orphaned files
    },
    "evidence.orphaned_files_cleaned": {
        "type": "gauge",
        "description": "Number of orphaned files deleted during cleanup",
        "labels": [],
        "alert_threshold": None,
    },
    "evidence.orphaned_files_failed": {
        "type": "counter",
        "description": "Number of orphaned file deletion failures",
        "labels": ["error_type"],
        "alert_threshold": "count > 5",  # Alert if >5 deletion failures
    },
    # Success metrics (for rate calculations)
    "evidence.created_success": {
        "type": "counter",
        "description": "Number of successfully created evidence records",
        "labels": ["category", "source_type"],
        "alert_threshold": None,
    },
    "evidence.llm_analysis_success": {
        "type": "counter",
        "description": "Number of successful LLM analysis calls",
        "labels": ["provider"],
        "alert_threshold": None,
    },
}


# =============================================================================
# Alert Rule Definitions (Prometheus/Grafana)
# =============================================================================

ALERT_RULES_YAML = """
# Evidence Processing Alert Rules
# Generated from: faultmaven/infrastructure/monitoring/evidence_metrics.py

groups:
  - name: evidence_processing
    interval: 30s
    rules:

      # LLM Timeout Rate
      - alert: HighEvidenceLLMTimeoutRate
        expr: |
          rate(evidence_llm_timeouts[5m])
          /
          (rate(evidence_llm_analysis_success[5m]) + rate(evidence_llm_timeouts[5m]) + rate(evidence_llm_errors[5m]))
          > 0.05
        for: 5m
        labels:
          severity: warning
          team: ai_platform
        annotations:
          summary: "High LLM timeout rate for evidence analysis"
          description: "LLM timeouts exceed 5% of evidence analysis attempts over the last 5 minutes. This may indicate LLM provider performance issues or timeout threshold too low."
          dashboard: "evidence-processing"

      # LLM Error Rate
      - alert: HighEvidenceLLMErrorRate
        expr: |
          rate(evidence_llm_errors[5m])
          /
          (rate(evidence_llm_analysis_success[5m]) + rate(evidence_llm_timeouts[5m]) + rate(evidence_llm_errors[5m]))
          > 0.10
        for: 5m
        labels:
          severity: warning
          team: ai_platform
        annotations:
          summary: "High LLM error rate for evidence analysis"
          description: "LLM errors exceed 10% of evidence analysis attempts. Check LLM provider status and error logs."
          dashboard: "evidence-processing"

      # DB Insert Failures
      - alert: EvidenceDBInsertFailures
        expr: increase(evidence_db_insert_failures[5m]) > 0
        labels:
          severity: high
          team: platform
        annotations:
          summary: "Evidence database insert failures detected"
          description: "Database insert failed after successful LLM analysis. LLM work is preserved via retry queue but database may have issues."
          dashboard: "evidence-processing"
          runbook: "Check database health, connection pool, disk space. LLM results are queued for retry."

      # Permanent LLM Retry Failures
      - alert: EvidenceLLMRetryPermanentFailures
        expr: increase(evidence_llm_retry_permanent_failures[1h]) > 0
        labels:
          severity: critical
          team: ai_platform
        annotations:
          summary: "Evidence LLM analysis failed permanently after retries"
          description: "LLM analysis failed after max retries. REJECTED evidence was created. Check LLM provider status and retry configuration."
          dashboard: "evidence-processing"
          runbook: "Review REJECTED evidence records. Check if LLM provider has prolonged outage."

      # Permanent DB Retry Failures
      - alert: EvidenceDBRetryPermanentFailures
        expr: increase(evidence_db_retry_permanent_failures[1h]) > 0
        labels:
          severity: critical
          team: platform
          page: true  # Page on-call
        annotations:
          summary: "Evidence DB insert failed permanently after retries"
          description: "Database insert failed after max retries. Manual intervention required. LLM analysis is lost."
          dashboard: "evidence-processing"
          runbook: "URGENT: Check database health. Review logs for case_id and content_ref. May need manual evidence creation from logs."

      # Category Fallback (Schema Drift)
      - alert: EvidenceCategoryFallback
        expr: increase(evidence_category_fallback[1h]) > 10
        labels:
          severity: warning
          team: ai_platform
        annotations:
          summary: "LLM returning invalid evidence categories"
          description: "More than 10 category fallbacks in the last hour. Indicates LLM prompt drift or schema mismatch."
          dashboard: "evidence-processing"
          runbook: "Review LLM prompts and response validation. Check if model has changed. Update prompts if needed."

      # High Orphaned File Rate
      - alert: HighOrphanedFileRate
        expr: evidence_orphaned_files_found > 50
        labels:
          severity: warning
          team: platform
        annotations:
          summary: "High number of orphaned files in storage"
          description: "More than 50 orphaned files found during cleanup. May indicate systematic failures in evidence processing."
          dashboard: "evidence-processing"
          runbook: "Check LLM timeout rate, DB insert failure rate. Review retry job logs."

      # Storage Cleanup Failures
      - alert: StorageCleanupFailures
        expr: increase(evidence_orphaned_files_failed[1d]) > 5
        labels:
          severity: warning
          team: platform
        annotations:
          summary: "Storage cleanup failing to delete orphaned files"
          description: "More than 5 deletion failures in the last day. Storage backend may have issues."
          dashboard: "evidence-processing"
          runbook: "Check storage backend health (S3/filesystem). Review cleanup job logs."

      # Retry Success Rate (tracking recovery)
      - alert: LowLLMRetrySuccessRate
        expr: |
          rate(evidence_llm_retry_successes[1h])
          /
          (rate(evidence_llm_retry_attempts[1h]) + 0.001)
          < 0.5
        for: 1h
        labels:
          severity: warning
          team: ai_platform
        annotations:
          summary: "Low LLM retry success rate"
          description: "Less than 50% of LLM retries are succeeding. May indicate persistent LLM provider issues."
          dashboard: "evidence-processing"
          runbook: "Check LLM provider status. Consider switching to fallback provider."

      # DB Retry Success Rate
      - alert: LowDBRetrySuccessRate
        expr: |
          rate(evidence_db_retry_successes[1h])
          /
          (rate(evidence_db_retry_attempts[1h]) + 0.001)
          < 0.5
        for: 1h
        labels:
          severity: high
          team: platform
        annotations:
          summary: "Low database retry success rate"
          description: "Less than 50% of DB retries are succeeding. Database may be unhealthy."
          dashboard: "evidence-processing"
          runbook: "Check database health, connection pool, disk space. May need to scale database."
"""


# =============================================================================
# Grafana Dashboard Definition
# =============================================================================

GRAFANA_DASHBOARD_JSON = {
    "dashboard": {
        "title": "Evidence Processing",
        "uid": "evidence-processing",
        "tags": ["evidence", "processing", "failures"],
        "timezone": "UTC",
        "panels": [
            {
                "title": "Evidence Creation Success Rate",
                "type": "graph",
                "targets": [
                    {
                        "expr": "rate(evidence_created_success[5m])",
                        "legendFormat": "Success Rate",
                    },
                ],
            },
            {
                "title": "LLM Failure Rates",
                "type": "graph",
                "targets": [
                    {
                        "expr": "rate(evidence_llm_timeouts[5m])",
                        "legendFormat": "Timeouts",
                    },
                    {
                        "expr": "rate(evidence_llm_errors[5m])",
                        "legendFormat": "Errors",
                    },
                ],
            },
            {
                "title": "DB Insert Failures",
                "type": "graph",
                "targets": [
                    {
                        "expr": "rate(evidence_db_insert_failures[5m])",
                        "legendFormat": "DB Failures",
                    },
                ],
            },
            {
                "title": "Retry Attempts & Success",
                "type": "graph",
                "targets": [
                    {
                        "expr": "rate(evidence_llm_retry_attempts[5m])",
                        "legendFormat": "LLM Retry Attempts",
                    },
                    {
                        "expr": "rate(evidence_llm_retry_successes[5m])",
                        "legendFormat": "LLM Retry Success",
                    },
                    {
                        "expr": "rate(evidence_db_retry_attempts[5m])",
                        "legendFormat": "DB Retry Attempts",
                    },
                    {
                        "expr": "rate(evidence_db_retry_successes[5m])",
                        "legendFormat": "DB Retry Success",
                    },
                ],
            },
            {
                "title": "Permanent Failures (Critical)",
                "type": "stat",
                "targets": [
                    {
                        "expr": "increase(evidence_llm_retry_permanent_failures[1h])",
                        "legendFormat": "LLM Permanent Failures",
                    },
                    {
                        "expr": "increase(evidence_db_retry_permanent_failures[1h])",
                        "legendFormat": "DB Permanent Failures",
                    },
                ],
            },
            {
                "title": "Category Fallback (Schema Drift)",
                "type": "graph",
                "targets": [
                    {
                        "expr": "rate(evidence_category_fallback[5m])",
                        "legendFormat": "Fallback Rate",
                    },
                ],
            },
            {
                "title": "Orphaned Files",
                "type": "graph",
                "targets": [
                    {
                        "expr": "evidence_orphaned_files_found",
                        "legendFormat": "Files Found",
                    },
                    {
                        "expr": "evidence_orphaned_files_cleaned",
                        "legendFormat": "Files Cleaned",
                    },
                ],
            },
            {
                "title": "Retry Success Rate",
                "type": "gauge",
                "targets": [
                    {
                        "expr": "rate(evidence_llm_retry_successes[1h]) / rate(evidence_llm_retry_attempts[1h])",
                        "legendFormat": "LLM Retry Success %",
                    },
                    {
                        "expr": "rate(evidence_db_retry_successes[1h]) / rate(evidence_db_retry_attempts[1h])",
                        "legendFormat": "DB Retry Success %",
                    },
                ],
            },
        ],
    }
}


# =============================================================================
# Helper Functions for Metrics Recording
# =============================================================================


async def record_upload_failure(
    metrics_collector: Any, case_id: str, error_type: str
) -> None:
    """Record file upload failure metric."""
    try:
        await metrics_collector.increment(
            "evidence.upload_failures",
            labels={"case_id": case_id, "error_type": error_type},
        )
    except Exception as e:
        logger.warning(f"Failed to record upload failure metric: {e}")


async def record_llm_timeout(
    metrics_collector: Any, case_id: str, provider: str
) -> None:
    """Record LLM timeout metric."""
    try:
        await metrics_collector.increment(
            "evidence.llm_timeouts", labels={"case_id": case_id, "provider": provider}
        )
    except Exception as e:
        logger.warning(f"Failed to record LLM timeout metric: {e}")


async def record_llm_error(
    metrics_collector: Any, case_id: str, provider: str, error_type: str
) -> None:
    """Record LLM error metric."""
    try:
        await metrics_collector.increment(
            "evidence.llm_errors",
            labels={"case_id": case_id, "provider": provider, "error_type": error_type},
        )
    except Exception as e:
        logger.warning(f"Failed to record LLM error metric: {e}")


async def record_db_insert_failure(
    metrics_collector: Any, case_id: str, error_type: str
) -> None:
    """Record database insert failure metric."""
    try:
        await metrics_collector.increment(
            "evidence.db_insert_failures",
            labels={"case_id": case_id, "error_type": error_type},
        )
    except Exception as e:
        logger.warning(f"Failed to record DB insert failure metric: {e}")


async def record_evidence_success(
    metrics_collector: Any, category: str, source_type: str
) -> None:
    """Record successful evidence creation metric."""
    try:
        await metrics_collector.increment(
            "evidence.created_success",
            labels={"category": category, "source_type": source_type},
        )
    except Exception as e:
        logger.warning(f"Failed to record evidence success metric: {e}")


async def record_llm_success(metrics_collector: Any, provider: str) -> None:
    """Record successful LLM analysis metric."""
    try:
        await metrics_collector.increment(
            "evidence.llm_analysis_success", labels={"provider": provider}
        )
    except Exception as e:
        logger.warning(f"Failed to record LLM success metric: {e}")


# =============================================================================
# Export Alert Rules and Dashboard
# =============================================================================


def export_alert_rules(output_path: str = "evidence_alerts.yml") -> None:
    """Export alert rules to YAML file for Prometheus."""
    with open(output_path, "w") as f:
        f.write(ALERT_RULES_YAML)
    logger.info(f"Alert rules exported to {output_path}")


def export_grafana_dashboard(output_path: str = "evidence_dashboard.json") -> None:
    """Export Grafana dashboard definition to JSON."""
    import json

    with open(output_path, "w") as f:
        json.dump(GRAFANA_DASHBOARD_JSON, f, indent=2)
    logger.info(f"Grafana dashboard exported to {output_path}")


# =============================================================================
# CLI for Exporting Configs
# =============================================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "export":
        export_alert_rules()
        export_grafana_dashboard()
        print("Exported alert rules and dashboard definitions")
    else:
        print(
            "Usage: python -m faultmaven.infrastructure.monitoring.evidence_metrics export"
        )
