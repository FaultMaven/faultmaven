# FaultMaven Performance Tuning Guide

## Executive Summary

This guide identifies the three most critical "tuning knots" in the FaultMaven codebase that have the highest impact on agent response quality, accuracy, and usefulness. These areas were identified through systematic analysis of the core agent logic, RAG pipeline, and LLM routing components.

---

## 🎯 Critical Tuning Area #1: Agent Decision-Making Thresholds

### **Files & Functions to Modify:**
- **Primary**: `faultmaven/core/agent/agent.py`
  - `_decide_if_user_update_is_needed()` (lines 777-813)
  - `_should_start_investigation()` (lines 766-775)
- **Secondary**: `faultmaven/services/session_service.py`
  - Constructor parameters (lines 32-46)

### **Why This is Critical:**
The agent's decision-making logic controls the entire troubleshooting flow. These thresholds determine when the agent escalates issues, seeks user input, or continues autonomously. **Poor calibration here leads to either overly passive agents that miss critical issues or overly aggressive agents that interrupt users constantly.**

### **Current Critical Parameters:**
```python
# Confidence threshold for seeking user input
confidence < 0.4  # Line 789 in agent.py

# Severity escalation thresholds  
severity in ["high", "critical"]  # Line 770 in agent.py

# Session confidence threshold
confidence_score > 0.7  # Line 318 in session_service.py

# Inactive session threshold
inactive_threshold_hours: int = 24  # Line 33 in session_service.py
```

### **Specific Tuning Experiments:**

#### **Experiment 1: Confidence Threshold Optimization**
```python
# In agent.py, line 789
# Current: if confidence < 0.4:
# Test range: 0.2 to 0.8 in 0.1 increments

confidence_thresholds = [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]
```
**Metrics to Track:**
- User interruption frequency
- Resolution success rate
- Time to resolution
- User satisfaction scores

#### **Experiment 2: Severity Escalation Tuning**
```python
# In agent.py, modify _should_start_investigation()
# Test different severity mappings:

# Conservative (current)
if severity in ["high", "critical"]: return "define_blast_radius"

# Moderate  
if severity in ["medium", "high", "critical"]: return "define_blast_radius"

# Aggressive
if severity in ["low", "medium", "high", "critical"]: return "define_blast_radius"
```

#### **Experiment 3: Session Success Threshold**
```python
# In session_service.py, line 318
# Current: confidence_score > 0.7
# Test: 0.5, 0.6, 0.7, 0.8, 0.9

success_thresholds = [0.5, 0.6, 0.7, 0.8, 0.9]
```

**Expected Impact:** 15-30% improvement in user experience and 20-40% reduction in false escalations.

---

## 🔍 Critical Tuning Area #2: RAG Pipeline Retrieval Parameters

### **Files & Functions to Modify:**
- **Primary**: `faultmaven/core/knowledge/ingestion.py`
  - `_split_content()` method (lines 309-348)
  - `search()` method (lines 350-402)
- **Secondary**: `faultmaven/tools/knowledge_base.py`
  - `_arun()` method (lines 63-100)

### **Why This is Critical:**
The RAG pipeline is the agent's memory and knowledge source. **Poor retrieval parameters directly impact the quality of information the agent uses to make decisions.** Chunk size affects context granularity, while search parameters determine relevance and coverage.

### **Current Critical Parameters:**
```python
# Document chunking parameters
chunk_size: int = 1000      # Line 310 in ingestion.py
overlap: int = 200          # Line 310 in ingestion.py

# Search result limits
n_results: int = 5          # Line 354 in ingestion.py, line 87 in knowledge_base.py

# Relevance scoring
relevance_score = 1 - distance  # Line 393-394 in ingestion.py
```

### **Specific Tuning Experiments:**

#### **Experiment 1: Chunk Size Optimization**
```python
# In ingestion.py, _split_content() method
chunk_size_experiments = [
    {"chunk_size": 500, "overlap": 100},   # Fine-grained
    {"chunk_size": 750, "overlap": 150},   # Balanced small
    {"chunk_size": 1000, "overlap": 200},  # Current
    {"chunk_size": 1500, "overlap": 300},  # Balanced large  
    {"chunk_size": 2000, "overlap": 400},  # Coarse-grained
]
```
**Test each configuration measuring:**
- Retrieval precision (relevant chunks / total chunks)
- Retrieval recall (relevant chunks found / total relevant chunks)
- Response coherence scores
- Query response time

#### **Experiment 2: Search Result Count Tuning**
```python
# In knowledge_base.py, line 87 and ingestion.py line 354
n_results_experiments = [3, 5, 7, 10, 15]
```
**A/B test with metrics:**
- Information coverage (% of relevant info retrieved)
- Response latency
- LLM context window utilization
- Answer completeness scores

#### **Experiment 3: Relevance Threshold Implementation**
```python
# Add relevance filtering in ingestion.py search() method
def search(self, query: str, n_results: int = 5, 
           relevance_threshold: float = 0.7):
    # ... existing code ...
    
    # Filter by relevance threshold
    filtered_results = [
        result for result in formatted_results 
        if result["relevance_score"] >= relevance_threshold
    ]
    
    return filtered_results[:n_results]
```
**Test thresholds:** 0.5, 0.6, 0.7, 0.8, 0.9

#### **Experiment 4: Context-Aware Search Enhancement**
```python
# In knowledge_base.py, enhance _expand_query_with_context()
def _expand_query_with_context(self, query: str, context: Dict[str, Any], 
                             expansion_weight: float = 0.3) -> str:
    # Current method + weight-based expansion
    # Test expansion_weight: 0.1, 0.2, 0.3, 0.4, 0.5
```

**Expected Impact:** 25-45% improvement in retrieval relevance and 15-25% faster resolution times.

---

## ⚡ Critical Tuning Area #3: LLM Routing Strategy & Confidence Scoring

### **Files & Functions to Modify:**
- **Primary**: `faultmaven/infrastructure/llm/router.py`
  - Constructor `confidence_threshold` parameter (line 27)
  - `route()` method (lines 54-63)
- **Secondary**: `faultmaven/infrastructure/llm/providers/registry.py`
  - `PROVIDER_SCHEMA` confidence scores (lines 39, 50, 61, 72, 83, 94, 105)
  - `route_request()` method (lines 228-294)

### **Why This is Critical:**
LLM routing determines which AI model handles each request and when to fall back to alternatives. **Poor routing leads to using weak models for complex tasks or expensive models for simple tasks, directly impacting both response quality and cost efficiency.**

### **Current Critical Parameters:**
```python
# Global confidence threshold
confidence_threshold: float = 0.8  # Line 27 in router.py

# Per-provider confidence scores
"fireworks": {"confidence_score": 0.9}    # Line 39
"openai": {"confidence_score": 0.85}      # Line 50  
"anthropic": {"confidence_score": 0.85}   # Line 105
"gemini": {"confidence_score": 0.8}       # Line 72
"local": {"confidence_score": 0.6}        # Line 61

# LLM generation parameters
max_tokens: int = 1000        # Line 59 in router.py
temperature: float = 0.7      # Line 60 in router.py
```

### **Specific Tuning Experiments:**

#### **Experiment 1: Global Confidence Threshold Optimization**
```python
# In router.py constructor
confidence_threshold_experiments = [0.6, 0.7, 0.75, 0.8, 0.85, 0.9]
```
**Measure for each threshold:**
- Provider utilization distribution
- Average response quality scores
- Cost per successful resolution
- Fallback frequency rates

#### **Experiment 2: Provider-Specific Confidence Tuning**
```python
# In registry.py, test different confidence score combinations
confidence_scenarios = [
    # Scenario 1: Conservative (favor high-quality providers)
    {"fireworks": 0.95, "openai": 0.9, "anthropic": 0.9, "gemini": 0.85},
    
    # Scenario 2: Balanced (current-ish)
    {"fireworks": 0.9, "openai": 0.85, "anthropic": 0.85, "gemini": 0.8},
    
    # Scenario 3: Aggressive (use cheaper providers more)
    {"fireworks": 0.8, "openai": 0.75, "anthropic": 0.75, "gemini": 0.7},
]
```

#### **Experiment 3: Dynamic Temperature Based on Task Complexity**
```python
# Add to router.py route() method
def _calculate_dynamic_temperature(self, prompt: str, base_temp: float = 0.7):
    """Adjust temperature based on task complexity"""
    complexity_indicators = [
        "troubleshoot", "debug", "analyze", "investigate", 
        "complex", "multiple", "various", "several"
    ]
    
    complexity_score = sum(1 for indicator in complexity_indicators 
                          if indicator in prompt.lower())
    
    # Higher complexity = lower temperature (more focused)
    if complexity_score >= 3:
        return base_temp * 0.8  # More focused for complex tasks
    elif complexity_score >= 1:
        return base_temp        # Normal temperature
    else:
        return base_temp * 1.2  # More creative for simple tasks

# Test base temperatures: 0.5, 0.6, 0.7, 0.8, 0.9
```

#### **Experiment 4: Token Allocation Optimization**
```python
# In router.py, implement dynamic token allocation
def _calculate_max_tokens(self, prompt: str, phase: str = None):
    """Dynamic token allocation based on phase and prompt complexity"""
    
    base_tokens = 1000
    
    phase_multipliers = {
        "define_blast_radius": 0.8,      # Concise assessment
        "establish_timeline": 1.0,       # Standard detail  
        "formulate_hypothesis": 1.2,     # More reasoning needed
        "validate_hypothesis": 1.4,      # Detailed analysis
        "propose_solution": 1.6,         # Comprehensive solutions
    }
    
    multiplier = phase_multipliers.get(phase, 1.0)
    return int(base_tokens * multiplier)

# Test base_tokens: 800, 1000, 1200, 1500
```

#### **Experiment 5: Circuit Breaker Tuning**
```python
# In router.py constructor, lines 33-34
circuit_breaker_experiments = [
    {"threshold": 2, "timeout": 15},   # Aggressive
    {"threshold": 3, "timeout": 30},   # Current
    {"threshold": 5, "timeout": 60},   # Conservative
]
```

**Expected Impact:** 30-50% cost reduction with maintained or improved quality, 20-35% faster response times.

---

## 🧪 Implementation Strategy

### **Phase 1: Baseline Measurement (Week 1)**
1. Implement comprehensive metrics collection for all three areas
2. Establish baseline performance across all key metrics
3. Create A/B testing framework for controlled experiments

### **Phase 2: Sequential Optimization (Weeks 2-4)**
1. **Week 2**: Agent decision thresholds (highest impact, lowest risk)
2. **Week 3**: RAG pipeline parameters (medium impact, medium risk)  
3. **Week 4**: LLM routing optimization (high impact, higher complexity)

### **Phase 3: Integration & Fine-tuning (Week 5)**
1. Test optimal combinations of parameters
2. Validate performance improvements
3. Document final recommendations

### **Key Success Metrics:**
- **Quality**: User satisfaction scores, resolution accuracy
- **Efficiency**: Time to resolution, cost per query
- **Reliability**: Success rate, fallback frequency
- **User Experience**: Interruption frequency, response relevance

### **Risk Mitigation:**
- Always test with 10% traffic split initially
- Maintain rollback capability for all changes
- Monitor error rates and user feedback closely
- Have baseline configurations ready for immediate revert

---

## 📊 Expected Overall Impact

**Conservative Estimates:**
- 20-30% improvement in response quality
- 15-25% reduction in resolution time
- 25-40% cost optimization
- 30-50% reduction in false escalations

**Optimistic Estimates:**
- 40-60% improvement in response quality
- 30-45% reduction in resolution time  
- 40-60% cost optimization
- 50-70% reduction in false escalations

The combination of these three tuning areas addresses the full pipeline from decision-making through knowledge retrieval to response generation, creating a multiplicative effect on overall system performance.
