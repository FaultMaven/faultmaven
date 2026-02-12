# Platform-Specific Extractors

**Status:** 🔮 Future Enhancement
**Priority:** Medium
**Category:** Data Ingestion
**Related:** Page Injection, Evidence Collection, [Data Preprocessing v3.0 — Tier 2](./data-preprocessing-design-specification.md#6-tier-2-deep-analysis-service)

---

## Overview

Platform-specific extractors intelligently parse and structure content from popular SRE/DevOps platforms (Datadog, GitHub, PagerDuty, Grafana, etc.) instead of treating all web pages as generic HTML blobs.

**Relationship to Three-Tier Model**: In the [Data Preprocessing v3.0](./data-preprocessing-design-specification.md) architecture, platform-specific extraction can operate at two levels:
- **Tier 1 (frontend)**: Client-side platform detection and structured data extraction before upload
- **Tier 2 (backend)**: Platform-aware deep analysis as a pluggable `ITier2AnalysisService` backend

This document primarily describes the Tier 1 (frontend) approach. For Tier 2 integration, see the [Tier 2 Service Interface](./data-preprocessing-design-specification.md#62-service-interface-contract).

---

## Current Implementation

### What We Have Now
When a user clicks "Inject current page":
1. ✅ Capture entire HTML content
2. ✅ Store page URL as metadata
3. ✅ Send raw HTML to backend for analysis
4. ✅ Backend processes it as unstructured text

### Limitations
- ❌ All pages treated as generic HTML
- ❌ Important structured data buried in HTML
- ❌ No platform-aware parsing
- ❌ Suboptimal LLM context usage

---

## Proposed Enhancement

### Intelligent Platform Detection

When user injects a page, the system would:

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
  "source_type": "page_capture",
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

## Implementation Complexity

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

## Decision: Why Future Enhancement?

### Reasons to Defer
1. **Not MVP-critical**: Generic HTML extraction works for v1
2. **High maintenance**: Platform UIs change frequently
3. **Backend-heavy**: Requires significant backend architecture
4. **Testing complexity**: Need mock environments for each platform
5. **Better alternatives**: Wait for platform APIs (webhooks, integrations)

### When to Implement
- ✅ After MVP is validated with users
- ✅ When we see specific platform patterns in usage data
- ✅ If users explicitly request platform-specific features
- ✅ When we have dedicated frontend + backend resources

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

- [Data Preprocessing Architecture v3.0](./data-preprocessing-design-specification.md) - Three-tier model including Tier 2 pluggable backends
- [Evidence Classification Design](./evidence-classification-design.md) - Evidence classification and submission types
- [Data Classification Strategy v1.2](./data-classification-strategy.md) - Tier 0 data type classification

---

## Status

**Current:** Generic HTML capture only (Tier 1 structural extraction handles HTML as UNSTRUCTURED_TEXT)
**Next Steps:** Monitor usage patterns, validate need with users. Consider implementing as a Tier 2 backend for platform-aware deep analysis.
**Target:** Q2 2026 (post-MVP)
