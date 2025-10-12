# Testing the Investigation Framework

**Version**: 1.0  
**Last Updated**: 2025-10-11  
**Status**: Testing Guide  
**Source**: Created from investigation framework testing patterns

---

## Overview

This guide provides comprehensive testing strategies for FaultMaven's investigation framework implementation, including unit tests, integration tests, and performance benchmarks.

---

## Table of Contents

1. [Unit Testing Strategy](#unit-testing-strategy)
2. [Integration Testing](#integration-testing)
3. [Performance Testing](#performance-testing)
4. [Test Fixtures and Helpers](#test-fixtures-and-helpers)

---

## Unit Testing Strategy

### Testing Phase Transitions

```python
class TestPhaseTransitions:
    """Unit tests for phase transition logic"""
    
    def test_phase_transition_validation(self):
        """Test phase transition validation"""
        engine = PhaseTransitionEngine()
        state = InvestigationState(
            investigation_id="test-002",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
            current_phase=0
        )
        
        # Phase 0 → Phase 1 without meeting criteria
        can_transition, reason = engine.can_transition(state, 1)
        assert not can_transition
        assert "completion criteria not met" in reason
        
        # Add required fields
        state.problem_statement = "API errors"
        state.urgency_level = UrgencyLevel.HIGH
        
        can_transition, reason = engine.can_transition(state, 1)
        assert can_transition
    
    def test_phase_completion_criteria(self):
        """Test completion criteria for each phase"""
        engine = PhaseTransitionEngine()
        
        # Phase 0 (Intake) criteria
        state_p0 = InvestigationState(current_phase=0)
        assert not engine.check_completion_criteria(state_p0, 0)
        
        state_p0.problem_statement = "API failing"
        state_p0.urgency_level = UrgencyLevel.HIGH
        assert engine.check_completion_criteria(state_p0, 0)
        
        # Phase 1 (Blast Radius) criteria
        state_p1 = InvestigationState(current_phase=1)
        state_p1.anomaly_frame = AnomalyFrame(
            statement="API errors",
            affected_components=["api-service"],
            confidence=0.7
        )
        state_p1.evidence_items = {"ev-001": EvidenceItem(...), "ev-002": EvidenceItem(...)}
        assert engine.check_completion_criteria(state_p1, 1)
```

### Testing OODA Step Advancement

```python
class TestOODAController:
    """Unit tests for OODA loop control"""
    
    def test_ooda_step_advancement(self):
        """Test OODA step advancement logic"""
        controller = OODAController()
        state = InvestigationState(
            investigation_id="test-003",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
            current_phase=4,
            current_ooda_step=OODAStep.OBSERVE,
            current_ooda_iteration=1
        )
        
        # Start iteration
        state.ooda_iterations.append(
            OODAIteration(
                iteration_number=1,
                started_at=datetime.utcnow()
            )
        )
        
        # Complete OBSERVE step
        step_result = {
            "evidence_collected": ["ev-001", "ev-002"]
        }
        
        updated_state = controller.advance_ooda_step(state, step_result)
        
        assert updated_state.current_ooda_step == OODAStep.ORIENT
        assert updated_state.ooda_iterations[0].observe_completed
    
    def test_ooda_iteration_completion(self):
        """Test completing full OODA iteration"""
        controller = OODAController()
        state = InvestigationState(current_phase=4)
        
        # Execute full cycle
        for step in [OODAStep.OBSERVE, OODAStep.ORIENT, OODAStep.DECIDE, OODAStep.ACT]:
            state.current_ooda_step = step
            result = {"step_complete": True}
            state = controller.advance_ooda_step(state, result)
        
        # Should start new iteration
        assert state.current_ooda_iteration == 2
        assert state.current_ooda_step == OODAStep.OBSERVE
```

### Testing Engagement Mode Switching

```python
class TestEngagementModes:
    """Unit tests for engagement mode transitions"""
    
    def test_consultant_mode_problem_detection(self):
        """Test that consultant mode detects problem signals"""
        controller = EngagementController()
        
        # Strong problem signal
        user_input = "Production API is down with 500 errors!"
        signal = controller.detect_problem_signal(user_input)
        
        assert signal.strength == "strong"
        assert signal.should_offer_investigation == True
    
    def test_mode_transition_consent(self):
        """Test mode transition requires user consent"""
        state = InvestigationState(
            engagement_mode="consultant",
            current_phase=0
        )
        
        # Detect problem signal
        signal = ProblemSignal(strength="strong", keywords=["down", "errors"])
        
        # Should stay in consultant until consent
        assert state.engagement_mode == "consultant"
        
        # User consents
        state = transition_to_investigator(state, user_consent=True)
        
        assert state.engagement_mode == "investigator"
        assert state.current_phase == 1
```

### Testing Hypothesis Management

```python
class TestHypothesisManager:
    """Unit tests for hypothesis lifecycle"""
    
    def test_hypothesis_anchoring_detection(self):
        """Test detection of hypothesis anchoring"""
        detector = AnchoringDetector()
        
        hypotheses = [
            Hypothesis(category="deployment", status="tested"),
            Hypothesis(category="deployment", status="tested"),
            Hypothesis(category="deployment", status="tested"),
            Hypothesis(category="deployment", status="testing"),
        ]
        
        warning = detector.detect_anchoring(hypotheses, [])
        
        assert warning is not None
        assert "deployment" in warning
        assert "4 times" in warning
    
    def test_confidence_decay(self):
        """Test hypothesis confidence decay"""
        hyp = Hypothesis(
            hypothesis_id="hyp-001",
            statement="Database pool exhausted",
            likelihood=0.85,
            initial_likelihood=0.85,
            created_at_turn=1
        )
        
        # No progress for 3 turns
        hyp.iterations_without_progress = 3
        hyp.apply_confidence_decay(current_turn=4)
        
        # Confidence should decay: 0.85 * (0.85^3) = ~0.52
        assert hyp.likelihood < 0.60
        assert hyp.likelihood > 0.50
    
    def test_forced_alternatives(self):
        """Test forcing alternative hypothesis categories"""
        detector = AnchoringDetector()
        tested_categories = ["deployment", "infrastructure"]
        
        alternatives = detector.force_alternatives(tested_categories)
        
        assert len(alternatives) >= 3
        assert "deployment" not in alternatives
        assert "infrastructure" not in alternatives
```

### Testing Evidence Tracking

```python
class TestEvidenceTracker:
    """Unit tests for evidence management"""
    
    def test_evidence_coverage_calculation(self):
        """Test evidence coverage score calculation"""
        tracker = EvidenceTracker()
        state = InvestigationState(
            investigation_id="test-005",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )
        
        # No evidence
        assert tracker.calculate_coverage_score(state) == 0.0
        
        # Add symptoms evidence
        state.evidence_items["ev-001"] = EvidenceItem(
            evidence_id="ev-001",
            label="Error logs",
            category="symptoms",
            content="500 errors in logs"
        )
        
        # Add scope evidence
        state.evidence_items["ev-002"] = EvidenceItem(
            evidence_id="ev-002",
            label="Affected regions",
            category="scope",
            content="EU and US regions"
        )
        
        # Add timeline evidence
        state.evidence_items["ev-003"] = EvidenceItem(
            evidence_id="ev-003",
            label="Started time",
            category="timeline",
            content="14:20 UTC"
        )
        
        coverage = tracker.calculate_coverage_score(state)
        assert coverage >= 1.0  # All core categories covered
    
    def test_evidence_request_prioritization(self):
        """Test prioritization of evidence requests"""
        tracker = EvidenceTracker()
        state = InvestigationState()
        
        requests = [
            {"label": "Symptoms", "category": "symptoms"},
            {"label": "Config", "category": "configuration"},
            {"label": "Metrics", "category": "metrics"}
        ]
        
        prioritized = tracker.prioritize_evidence_requests(state, requests)
        
        # Symptoms should be highest priority (core category)
        assert prioritized[0]["category"] == "symptoms"
```

---

## Integration Testing

### End-to-End Active Incident Flow

```python
class TestEndToEndFlows:
    """Integration tests for complete investigation flows"""
    
    async def test_active_incident_flow(self):
        """Test complete active incident flow"""
        agent = FaultMavenAgent()
        
        # 1. User reports problem (Phase 0)
        response1 = await agent.process_message(
            "Production API is returning 500 errors!",
            investigation_id=None
        )
        
        investigation_id = response1["investigation_id"]
        
        # Should be in consultant mode, offering to switch
        assert "troubleshoot" in response1["message"].lower() or "investigate" in response1["message"].lower()
        
        # 2. User confirms wanting help (Phase 0 → Phase 1)
        response2 = await agent.process_message(
            "Yes, please help me troubleshoot",
            investigation_id=investigation_id
        )
        
        # Should transition to investigator mode, Phase 1
        assert response2["current_phase"] == 1
        assert response2["engagement_mode"] == "investigator"
        assert "evidence" in response2["message"].lower()
        
        # 3. User provides evidence (Phase 1)
        response3 = await agent.process_message(
            "The errors started at 14:20 UTC. Logs show TimeoutException connecting to database.",
            investigation_id=investigation_id
        )
        
        # Should be analyzing evidence, may advance to Phase 2 or 3
        assert response3["current_phase"] in [1, 2, 3]
        
        # 4. User provides more evidence (Phase 2-3)
        response4 = await agent.process_message(
            "We deployed v2.3.1 at 14:18 UTC, about 2 minutes before errors started.",
            investigation_id=investigation_id
        )
        
        # Should generate deployment-related hypothesis
        assert response4["hypotheses"] or response4["current_phase"] >= 3
        
        # 5. Test hypothesis (Phase 4)
        response5 = await agent.process_message(
            "Checked deployment logs, new version has database connection leak",
            investigation_id=investigation_id
        )
        
        # Should identify root cause
        assert response5["current_phase"] in [4, 5]
    
    async def test_post_mortem_flow(self):
        """Test post-mortem investigation flow"""
        agent = FaultMavenAgent()
        
        # 1. User asks about past incident
        response1 = await agent.process_message(
            "Can you help me analyze the database timeout incident from last Tuesday?",
            investigation_id=None
        )
        
        investigation_id = response1["investigation_id"]
        
        # Should offer post-mortem mode
        assert "post" in response1["message"].lower() or "analyze" in response1["message"].lower()
        
        # 2. User provides context
        response2 = await agent.process_message(
            "Yes. The incident lasted from 14:20 to 15:30 UTC. "
            "Database queries were timing out. We mitigated by rolling back deployment.",
            investigation_id=investigation_id
        )
        
        # Should be in Phase 1, framing problem
        assert response2["current_phase"] >= 1
        assert response2["investigation_strategy"] == "post_mortem"
```

### Testing State Persistence

```python
class TestStatePersistence:
    """Integration tests for state management"""
    
    async def test_state_save_and_load(self):
        """Test investigation state persists correctly"""
        manager = StateManager()
        
        # Create investigation with rich state
        state = InvestigationState(
            investigation_id="test-persist-001",
            current_phase=4,
            engagement_mode="investigator",
            anomaly_frame=AnomalyFrame(
                statement="API timeouts",
                confidence=0.8
            ),
            hypotheses=[
                Hypothesis(statement="Pool exhausted", likelihood=0.75)
            ]
        )
        
        # Save state
        await manager.save_state(state)
        
        # Load state
        loaded = await manager.load_state("test-persist-001")
        
        # Verify state preserved
        assert loaded.current_phase == 4
        assert loaded.engagement_mode == "investigator"
        assert loaded.anomaly_frame.statement == "API timeouts"
        assert len(loaded.hypotheses) == 1
        assert loaded.hypotheses[0].likelihood == 0.75
```

---

## Performance Testing

### State Persistence Performance

```python
class TestPerformance:
    """Performance and scalability tests"""
    
    async def test_state_persistence_performance(self):
        """Test state save/load performance with large state"""
        manager = StateManager(persistence=RedisPersistence())
        
        # Create large state
        state = InvestigationState(
            investigation_id="perf-test-001",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )
        
        # Add 100 evidence items
        for i in range(100):
            state.evidence_items[f"ev-{i:03d}"] = EvidenceItem(
                evidence_id=f"ev-{i:03d}",
                label=f"Evidence {i}",
                description="Performance test evidence",
                category="symptoms",
                content="Large content " * 100,  # ~1KB per item
                source="test",
                collected_at=datetime.utcnow()
            )
        
        # Measure save time
        start = time.time()
        await manager.save_state(state)
        save_time = time.time() - start
        
        assert save_time < 0.5  # Should save in <500ms
        
        # Measure load time
        start = time.time()
        loaded_state = await manager.load_state("perf-test-001")
        load_time = time.time() - start
        
        assert load_time < 0.3  # Should load in <300ms
        assert len(loaded_state.evidence_items) == 100
    
    async def test_memory_compression_performance(self):
        """Test memory compression with many OODA iterations"""
        memory = HierarchicalMemory()
        state = InvestigationState()
        
        # Simulate 20 OODA iterations
        for i in range(20):
            iteration = OODAIteration(
                iteration_number=i + 1,
                started_at=datetime.utcnow(),
                steps_completed=[OODAStep.OBSERVE, OODAStep.ORIENT, OODAStep.DECIDE, OODAStep.ACT],
                new_evidence_collected=3,
                hypotheses_generated=2
            )
            state.ooda_iterations.append(iteration)
        
        # Measure compression time
        start = time.time()
        await memory.compress(state, current_turn=20)
        compression_time = time.time() - start
        
        assert compression_time < 0.2  # Should compress in <200ms
        
        # Verify token reduction
        uncompressed_tokens = estimate_tokens(state.to_json())
        compressed_tokens = estimate_tokens(memory.to_prompt_context())
        
        reduction = (uncompressed_tokens - compressed_tokens) / uncompressed_tokens
        assert reduction > 0.60  # Should achieve >60% reduction
        assert compressed_tokens < 1800  # Should stay under 1800 tokens
```

### OODA Iteration Performance

```python
class TestOODAPerformance:
    """Performance tests for OODA execution"""
    
    async def test_ooda_iteration_latency(self):
        """Test OODA iteration processing time"""
        controller = OODAController()
        state = InvestigationState(
            current_phase=4,
            current_ooda_step=OODAStep.OBSERVE
        )
        
        # Measure iteration advancement time
        start = time.time()
        for _ in range(10):
            step_result = {"evidence_analyzed": ["ev-001"]}
            state = controller.advance_ooda_step(state, step_result)
        latency = (time.time() - start) / 10
        
        assert latency < 0.050  # Should process in <50ms per iteration
```

---

## Test Fixtures and Helpers

### Common Test States

```python
@pytest.fixture
def basic_investigation_state():
    """Basic investigation state for testing"""
    return InvestigationState(
        investigation_id="test-001",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
        current_phase=1,
        engagement_mode="investigator"
    )

@pytest.fixture
def state_with_anomaly_frame():
    """Investigation state with anomaly frame"""
    return InvestigationState(
        investigation_id="test-002",
        current_phase=2,
        anomaly_frame=AnomalyFrame(
            statement="API returning 500 errors",
            affected_components=["api-service", "database"],
            affected_scope="EU region users",
            severity="high",
            confidence=0.75
        )
    )

@pytest.fixture
def state_with_hypotheses():
    """Investigation state with multiple hypotheses"""
    return InvestigationState(
        investigation_id="test-003",
        current_phase=4,
        hypotheses=[
            Hypothesis(
                hypothesis_id="hyp-001",
                statement="Database connection pool exhausted",
                category="infrastructure",
                likelihood=0.85
            ),
            Hypothesis(
                hypothesis_id="hyp-002",
                statement="Memory leak in new feature",
                category="code",
                likelihood=0.60
            )
        ]
    )
```

### Test Helpers

```python
class TestHelpers:
    """Helper functions for testing"""
    
    @staticmethod
    def create_evidence_item(category: str = "symptoms", **kwargs) -> EvidenceItem:
        """Create evidence item for testing"""
        defaults = {
            "evidence_id": f"ev-{uuid.uuid4().hex[:6]}",
            "label": "Test evidence",
            "description": "Test description",
            "category": category,
            "content": "Test content",
            "source": "test",
            "collected_at": datetime.utcnow()
        }
        defaults.update(kwargs)
        return EvidenceItem(**defaults)
    
    @staticmethod
    def create_hypothesis(likelihood: float = 0.75, **kwargs) -> Hypothesis:
        """Create hypothesis for testing"""
        defaults = {
            "hypothesis_id": f"hyp-{uuid.uuid4().hex[:6]}",
            "statement": "Test hypothesis",
            "category": "code",
            "likelihood": likelihood,
            "initial_likelihood": likelihood,
            "status": "pending",
            "created_at_turn": 1
        }
        defaults.update(kwargs)
        return Hypothesis(**defaults)
    
    @staticmethod
    async def advance_to_phase(state: InvestigationState, target_phase: int) -> InvestigationState:
        """Helper to advance state to specific phase"""
        engine = PhaseTransitionEngine()
        
        while state.current_phase < target_phase:
            # Set minimal completion criteria
            if state.current_phase == 0:
                state.problem_statement = "Test problem"
                state.urgency_level = UrgencyLevel.HIGH
            elif state.current_phase == 1:
                state.anomaly_frame = AnomalyFrame(
                    statement="Test anomaly",
                    confidence=0.7
                )
                state.evidence_items = {
                    "ev-001": TestHelpers.create_evidence_item(),
                    "ev-002": TestHelpers.create_evidence_item()
                }
            
            # Transition
            next_phase = state.current_phase + 1
            state = engine.transition(state, next_phase)
        
        return state
```

---

## Test Coverage Targets

| Component | Target Coverage | Priority |
|-----------|----------------|----------|
| Phase Transitions | 95%+ | High |
| OODA Controller | 90%+ | High |
| Hypothesis Manager | 90%+ | High |
| Evidence Tracker | 85%+ | Medium |
| Memory Manager | 80%+ | Medium |
| Engagement Modes | 95%+ | High |
| State Persistence | 95%+ | High |

---

## Running Tests

```bash
# Run all tests
pytest tests/

# Run unit tests only
pytest tests/unit/

# Run integration tests only
pytest tests/integration/

# Run performance tests
pytest tests/performance/ -v

# Run with coverage
pytest --cov=faultmaven --cov-report=html tests/

# Run specific test file
pytest tests/unit/test_phase_transitions.py -v
```

---

**END OF DOCUMENT**


