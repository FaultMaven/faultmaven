# Platform-Specific Extractors

**Status:** Stages 1 and 2 implemented. Stages 3 and 4 are deferred (future enhancements) — see [Status](#status) at the bottom of this document and [Decision: Why Stage 3 is Deferred](#decision-why-stage-3-is-deferred).
**Priority:** Medium
**Category:** Data Ingestion
**Related:** Page Injection, Evidence Collection, [Data Preprocessing — Tier 3 Deep LLM Analysis](./data-preprocessing-design-specification.md#4-tier-3-deep-llm-analysis-renamed-from-tier-2)

---

## Overview

Platform-specific extractors intelligently parse and structure content from popular SRE/DevOps platforms (Datadog, GitHub, PagerDuty, Grafana, etc.) instead of treating all web pages as generic HTML blobs.

**Relationship to Four-Tier Model**: In the [Data Preprocessing](./data-preprocessing-design-specification.md) architecture, platform-specific extraction can operate at two levels:
- **Tier 1 (frontend)**: Client-side platform detection and structured data extraction before upload
- **Tier 3 (backend)**: Platform-aware deep analysis as a pluggable backend behind the `ITier2SearchService` interface. The `Tier2` token refers to the agent-tools tier system (Tier 2 = search, Tier 3 = interpreted analysis), not the preprocessing-tier system. Invoked via the `deep_analysis` agent tool.

This document primarily describes the Tier 1 (frontend) approach. For the Tier 3 service, see [Data Preprocessing §4](./data-preprocessing-design-specification.md#4-tier-3-deep-llm-analysis-renamed-from-tier-2).

---

## Current Implementation

### What We Have Now (Stage 1 — Implemented)

Stage 1 is the copilot extension's semantic DOM extraction (`htmlToStructuredText()`) paired with a backend pass-through branch (`page_capture_passthrough`) so page captures skip the `UnstructuredTextExtractor`. Features include error-first priority ordering, stat-panel detection (`tryStatValue`), label/value detection (`tryKeyValue`), ARIA-alert promotion, form-value extraction, and a `[captured_at: ISO timestamp]` preamble.

For the canonical enumeration of Stage 1 behaviour and its interaction with Tier 0+1, see [Data Preprocessing §2.4 — Pasted Text and Page Capture Processing](./data-preprocessing-design-specification.md#24-pasted-text-and-page-capture-processing).

### Stage 2 — Implemented: Query-Time Section Reranking

Stage 2 is backend query-time reranking of page-capture sections against the user's query, implemented in `_rerank_page_capture_sections()` (`faultmaven/core/investigation/prompts/context_builder.py`). When assembling Tier A evidence for an LLM call, page-capture structural indexes are split on `\n##` headings, each section scored by normalised keyword overlap against the user's query (stopwords excluded), and reassembled in descending relevance order. The `[captured_at: …]` preamble is pinned at position 0. Reranking runs **before** the per-item character cap so query-relevant sections survive truncation.

Stage 2 complements Stage 1 by surfacing the most relevant portion of a long page capture within the evidence budget, without changing the frontend extractor.

### What Remains (Limitations of Generic Extraction)
- ❌ No platform-aware parsing (Grafana panel types, Datadog monitor states)
- ❌ No structured query extraction (PromQL, Datadog metrics queries)
- ❌ No threshold/alert configuration extraction from platform-specific DOM
- ❌ No cross-panel correlation (which metrics share the same time range)

---

## Proposed Stage 3 Enhancement: Intelligent Platform Detection

The sections below describe Stage 3 (platform-aware parsing). Stages 1 and 2 are already implemented — see the [Status](#status) section for the current state.

When user injects a page, the Stage 3 system would:

**Step 1: Detect Platform**
```javascript
const url = "https://app.datadoghq.com/dashboard/abc-123"
const platform = detectPlatform(url)
// → "datadog"
```

**Step 2: Extract Structured Data**
```javascript
const extractor = getExtractor(platform)
const structuredData = await extractor.extract(pageContent, url)
```

**Step 3: Send Both Raw + Structured**
```json
{
  "data_type": "metrics",
  "source_url": "https://app.datadoghq.com/dashboard/abc-123",
  "platform": "datadog",
  "raw_html": "...",
  "structured_data": {
    "dashboard_id": "abc-123",
    "dashboard_name": "Production API Metrics",
    "time_range": "Last 1 hour",
    "widgets": [
      {
        "type": "timeseries",
        "metric": "avg:system.cpu.user{*}",
        "value": 87.3,
        "threshold": 80,
        "status": "alert"
      }
    ]
  }
}
```

---

## Supported Platforms

### Priority 1 (High Value)
- **Datadog** - Dashboards, monitors, logs
- **Grafana** - Dashboards, panels, queries
- **PagerDuty** - Incidents, alerts, timelines
- **GitHub** - Issues, PRs, discussions

### Priority 2 (Medium Value)
- **Splunk** - Search results, dashboards
- **New Relic** - APM traces, errors
- **Elastic** - Kibana dashboards, logs
- **Prometheus** - Metrics, alerts

### Priority 3 (Nice to Have)
- **Jira** - Tickets, workflows
- **Confluence** - Documentation
- **StatusPage** - Incident reports
- **Slack** - Thread archives

---

## Technical Architecture

### Frontend (Extension)

```typescript
// Platform detection
interface PlatformDetector {
  matches(url: string): boolean;
  name: string;
}

const detectors: PlatformDetector[] = [
  { name: 'datadog', matches: (url) => url.includes('datadoghq.com') },
  { name: 'grafana', matches: (url) => url.includes('grafana') },
  // ...
];

// Extraction interface
interface PlatformExtractor {
  extract(html: string, url: string): Promise<any>;
}

class DatadogExtractor implements PlatformExtractor {
  async extract(html: string, url: string) {
    // Parse Datadog-specific DOM structure
    // Extract widgets, metrics, thresholds, etc.
    return {
      dashboard_id: extractDashboardId(url),
      widgets: extractWidgets(html),
      // ...
    };
  }
}
```

### Backend (API)

```python
# Enhanced evidence processing
class EvidenceProcessor:
    def process_page_capture(self, data: PageCaptureData):
        # If structured_data provided, use it
        if data.structured_data:
            # Create rich embeddings from structured data
            embeddings = self.embed_structured(data.structured_data)

            # Store in vector DB with enhanced metadata
            self.store_with_platform_context(embeddings, data.platform)

        # Also process raw HTML as fallback
        self.process_raw_html(data.raw_html)
```

---

## Benefits

### For LLM Context
- ✅ **Richer context**: Structured data is easier for LLMs to reason about
- ✅ **Better retrieval**: Semantic search on structured fields
- ✅ **Precise queries**: "Show me all metrics above threshold" becomes trivial

### For Users
- ✅ **Smarter analysis**: "This dashboard shows CPU at 87%, threshold is 80%"
- ✅ **Platform-aware insights**: "Based on this Datadog monitor..."
- ✅ **Automatic correlation**: Link metrics to incidents automatically

### For System
- ✅ **Efficient storage**: Store structured data separately from HTML
- ✅ **Better indexing**: Search by metric name, threshold, etc.
- ✅ **Future integrations**: Easy to add platform-specific features

---

## Stage 3 Implementation Complexity (Future Work)

### Frontend Work (3-4 weeks)
- **Week 1**: Platform detection framework
- **Week 2**: Datadog + Grafana extractors
- **Week 3**: PagerDuty + GitHub extractors
- **Week 4**: Testing and refinement

### Backend Work (2-3 weeks)
- **Week 1**: Structured data schema design
- **Week 2**: Enhanced storage and retrieval
- **Week 3**: Platform-aware analysis

### Maintenance Cost
- **High**: Each platform requires custom parsing logic
- **Brittle**: Platform UI changes break extractors
- **Testing**: Requires mocking multiple platforms

---

## Decision: Why Stage 3 is Deferred

### Reasons to Defer
1. **Stages 1 and 2 cover most real use-cases**: Generic DOM extraction via `htmlToStructuredText()` + query-time section reranking already handles most dashboard patterns.
2. **High maintenance**: Platform UIs change frequently
3. **Backend-heavy**: Requires significant backend architecture
4. **Testing complexity**: Need mock environments for each platform
5. **Better alternatives**: Wait for platform APIs (webhooks, integrations)

### When to Implement Stage 3
- ✅ After MVP is validated with users
- ✅ When we see specific platform patterns in usage data
- ✅ If users explicitly request platform-specific features
- ✅ When we have dedicated frontend + backend resources

---

## Stage 4 (Deferred): Viewport Sync / Real-Time Capture

Stage 4 would add live-dashboard capture for platforms that auto-refresh (Grafana, Datadog live mode) so evidence reflects the state the user is actually looking at, not a stale snapshot. Options under consideration: periodic re-capture, `MutationObserver`-driven diffing, or an explicit "refresh capture" button in the copilot. Deferred for the same reasons as Stage 3 — Stages 1+2 cover the common case, and viewport sync adds meaningful frontend complexity without a validated user need.

---

## Alternative Approaches

### 1. Browser Extension Integrations
Instead of parsing HTML, integrate with platform APIs:
```javascript
// Datadog extension integration
const datadogClient = new DatadogAPI(userApiKey);
const dashboard = await datadogClient.getDashboard(dashboardId);
```

**Pros**: More reliable, structured by default
**Cons**: Requires user API keys, privacy concerns

### 2. Backend Webhooks
Let platforms push data to FaultMaven:
```
Datadog Monitor triggers → Webhook → FaultMaven
```

**Pros**: Real-time, structured, no parsing
**Cons**: Requires platform-side setup

### 3. AI-Powered Extraction
Use LLM to extract structure from any page:
```python
prompt = f"Extract metrics and thresholds from this HTML: {html}"
structured = llm.extract_structured_data(prompt)
```

**Pros**: Works for any platform, no custom extractors
**Cons**: Expensive, slower, less reliable

---

## References

- [Data Preprocessing Architecture](./data-preprocessing-design-specification.md) — Four-tier model including Tier 3 pluggable backends
- [Evidence Model](../investigation-engine/evidence-driven-investigation-framework.md#5-evidence-model) — Categories, source-type, and `evidence_source_invariant`
- [Data Classification Strategy](./data-classification-strategy.md) — Tier 0 data type classification

---

## Status

| Stage | State | Summary |
| --- | --- | --- |
| Stage 1 — Semantic DOM extraction | **Implemented** | `htmlToStructuredText()` with error-first ordering, stat/label/value detection, ARIA alert promotion, `[captured_at]` preamble. Backend pass-through via `page_capture_passthrough`. See §"What We Have Now". |
| Stage 2 — Query-time section reranking | **Implemented** | `_rerank_page_capture_sections()` in `context_builder.py`. Preamble pinned at position 0; runs before per-item char cap. See §"Stage 2 — Implemented". |
| Stage 3 — Platform-aware parsing | **Deferred** | Precision extractors per platform (Datadog, Grafana, PagerDuty, GitHub, …). See §"Proposed Stage 3 Enhancement" and §"Decision: Why Stage 3 is Deferred". |
| Stage 4 — Viewport sync / real-time capture | **Deferred** | Live-dashboard capture for auto-refreshing platforms. See §"Stage 4 (Deferred)". |
