# Component Interaction Patterns

This document details the interaction patterns between components in the FaultMaven system, showing how data flows through the architecture for different use cases.

## Agent Query Processing Flow

The core troubleshooting workflow demonstrates how the AI agent processes user queries through multiple layers.

```mermaid
sequenceDiagram
    participant Browser as Browser Extension
    participant Agent as Agent Router
    participant Service as Agent Service
    participant Core as AI Agent Core
    participant Tools as Agent Tools
    participant KB as Knowledge Base
    participant LLM as LLM Provider
    participant Tracer as Opik Tracer
    
    Browser->>Agent: POST /api/v1/troubleshoot
    note over Browser,Agent: User submits troubleshooting query
    
    Agent->>Service: process_query(query, session_id, context)
    note over Agent,Service: Route to service layer
    
    Service->>Tracer: start_trace("troubleshooting_session")
    Service->>Core: execute_reasoning(query, context)
    note over Service,Core: Begin AI reasoning workflow
    
    %% Phase 1: Define Blast Radius
    Core->>Core: phase_1_define_blast_radius()
    Core->>Tools: analyze_scope(query)
    Tools-->>Core: scope_analysis
    
    %% Phase 2: Establish Timeline
    Core->>Core: phase_2_establish_timeline()
    Core->>Tools: extract_temporal_context(query)
    Tools-->>Core: timeline_data
    
    %% Phase 3: Knowledge Search
    Core->>Tools: search_knowledge_base(query, context)
    Tools->>KB: semantic_search(query_embedding)
    KB-->>Tools: relevant_documents
    Tools-->>Core: knowledge_context
    
    %% Phase 4: Hypothesis Formation
    Core->>LLM: generate_hypothesis(query + context + knowledge)
    LLM-->>Core: hypothesis_list
    
    %% Phase 5: Solution Generation
    Core->>LLM: generate_solution(validated_hypothesis)
    LLM-->>Core: solution_recommendations
    
    Core-->>Service: investigation_result
    Service->>Tracer: end_trace(investigation_result)
    Service-->>Agent: troubleshooting_response
    Agent-->>Browser: JSON Response
    
    note over Browser,Agent: Complete troubleshooting response
```

## Data Ingestion Flow

Shows how uploaded files are processed through the data pipeline with classification and security.

```mermaid
sequenceDiagram
    participant User as User/Client
    participant API as Data Router
    participant Service as Data Service
    participant Classifier as Data Classifier
    participant Processor as Log Processor
    participant Sanitizer as Data Sanitizer
    participant Storage as Storage Backend
    participant Monitor as Health Monitor
    
    User->>API: POST /api/v1/data (multipart file upload)
    note over User,API: File upload with metadata
    
    API->>Service: ingest_data(file, metadata)
    Service->>Monitor: record_ingestion_start()
    
    %% File Classification
    Service->>Classifier: classify_file(file_content, metadata)
    Classifier->>Classifier: detect_file_type()
    Classifier->>Classifier: analyze_content_structure()
    Classifier-->>Service: classification_result
    
    note over Service: File type: logs, config, metrics, etc.
    
    %% Security Processing
    Service->>Sanitizer: sanitize_content(file_content)
    Sanitizer->>Sanitizer: detect_pii()
    Sanitizer->>Sanitizer: redact_sensitive_data()
    Sanitizer-->>Service: sanitized_content
    
    %% Content Processing
    alt Log File Processing
        Service->>Processor: process_logs(sanitized_content)
        Processor->>Processor: parse_log_entries()
        Processor->>Processor: extract_errors()
        Processor->>Processor: identify_patterns()
        Processor-->>Service: log_insights
    else Configuration File
        Service->>Processor: process_config(sanitized_content)
        Processor->>Processor: validate_syntax()
        Processor->>Processor: check_best_practices()
        Processor-->>Service: config_analysis
    else Metrics Data
        Service->>Processor: process_metrics(sanitized_content)
        Processor->>Processor: parse_time_series()
        Processor->>Processor: detect_anomalies()
        Processor-->>Service: metric_insights
    end
    
    %% Storage
    Service->>Storage: store_processed_data(insights, metadata)
    Storage-->>Service: storage_id
    
    Service->>Monitor: record_ingestion_complete(success=true)
    Service-->>API: ingestion_result
    API-->>User: Success Response with insights
    
    note over User,API: Processing complete with extracted insights
```

## Knowledge Base Document Ingestion

Demonstrates how documents are processed and stored in the vector database for RAG operations.

```mermaid
sequenceDiagram
    participant User as User
    participant API as Knowledge Router
    participant Service as Knowledge Service
    participant Embedder as BGE-M3 Embedder
    participant Chunker as Document Chunker
    participant Sanitizer as Data Sanitizer
    participant VectorDB as ChromaDB
    participant Health as Health Monitor
    
    User->>API: POST /api/v1/knowledge/documents
    note over User,API: Upload document (PDF, MD, TXT)
    
    API->>Service: ingest_document(file, metadata, tags)
    Service->>Health: record_knowledge_ingestion_start()
    
    %% Content Extraction
    Service->>Service: extract_text_content(file)
    note over Service: Extract from PDF, parse Markdown, etc.
    
    %% Security Processing
    Service->>Sanitizer: sanitize_document(content)
    Sanitizer->>Sanitizer: detect_and_redact_pii()
    Sanitizer-->>Service: sanitized_content
    
    %% Document Chunking
    Service->>Chunker: chunk_document(sanitized_content)
    Chunker->>Chunker: split_by_semantic_boundaries()
    Chunker->>Chunker: ensure_chunk_overlap()
    Chunker->>Chunker: optimize_chunk_size()
    Chunker-->>Service: document_chunks[]
    
    %% Generate Embeddings
    loop For each chunk
        Service->>Embedder: generate_embedding(chunk_text)
        Embedder->>Embedder: tokenize_text()
        Embedder->>Embedder: compute_vector_embedding()
        Embedder-->>Service: embedding_vector[768]
    end
    
    %% Store in Vector Database
    Service->>VectorDB: store_embeddings(chunks, embeddings, metadata)
    VectorDB->>VectorDB: create_collection_if_needed()
    VectorDB->>VectorDB: insert_vectors_with_metadata()
    VectorDB->>VectorDB: build_search_index()
    VectorDB-->>Service: document_ids[]
    
    Service->>Health: record_knowledge_ingestion_complete(chunks_count)
    Service-->>API: ingestion_success(document_ids, chunks_count)
    API-->>User: Success Response
    
    note over User,API: Document ready for semantic search
```

## Session Management Lifecycle

Shows how user sessions are created, maintained, and cleaned up across the system.

```mermaid
sequenceDiagram
    participant Client as Client
    participant Session as Session Router
    participant Service as Session Service
    participant Redis as Redis Store
    participant Manager as Session Manager
    participant Cleanup as Cleanup Scheduler
    
    %% Session Creation
    Client->>Session: POST /api/v1/sessions
    Session->>Service: create_session(metadata)
    Service->>Redis: check_existing_sessions(user_id)
    Redis-->>Service: existing_sessions[]
    
    alt Too many active sessions
        Service->>Redis: cleanup_oldest_session(user_id)
        Service->>Manager: record_session_cleanup()
    end
    
    Service->>Service: generate_session_id()
    Service->>Redis: store_session(session_id, metadata, ttl)
    Redis-->>Service: storage_success
    Service->>Manager: track_new_session(session_id)
    Service-->>Session: session_created(session_id)
    Session-->>Client: Session Response
    
    %% Session Usage
    loop Active Session Usage
        Client->>Session: Use session for troubleshooting
        Session->>Service: update_session_activity(session_id)
        Service->>Redis: refresh_ttl(session_id)
        Service->>Manager: record_session_activity(session_id)
    end
    
    %% Background Cleanup Process
    par Background Cleanup
        Cleanup->>Manager: schedule_cleanup_task()
        
        loop Every 15 minutes
            Manager->>Redis: scan_expired_sessions()
            Redis-->>Manager: expired_session_ids[]
            
            loop For each expired session
                Manager->>Redis: delete_session(session_id)
                Manager->>Manager: update_cleanup_metrics()
            end
            
            Manager->>Manager: check_memory_usage()
            alt Memory usage > threshold
                Manager->>Redis: cleanup_idle_sessions()
            end
        end
    end
    
    %% Explicit Session Termination
    Client->>Session: DELETE /api/v1/sessions/{session_id}
    Session->>Service: terminate_session(session_id)
    Service->>Redis: delete_session(session_id)
    Service->>Manager: record_session_termination(session_id)
    Service-->>Session: termination_success
    Session-->>Client: Success Response
```

## Error Handling and Recovery Flow

Demonstrates how errors are detected, contextualized, and recovered across the system.

```mermaid
sequenceDiagram
    participant Client as Client
    participant API as API Layer
    participant Service as Service Layer
    participant ErrorCtx as Error Context
    participant Recovery as Error Recovery
    participant Health as Health Monitor
    participant Logging as Logging System
    participant Alert as Alert Manager
    
    Client->>API: Request
    API->>Service: Process Request
    Service->>Service: ❌ Error Occurs
    
    %% Error Context Building
    Service->>ErrorCtx: add_layer_error("service", error_details)
    ErrorCtx->>ErrorCtx: analyze_error_pattern()
    ErrorCtx->>ErrorCtx: calculate_severity_score()
    ErrorCtx->>ErrorCtx: detect_error_correlations()
    
    %% Error Escalation Decision
    ErrorCtx->>ErrorCtx: should_escalate()
    
    alt Auto-Recovery Possible
        ErrorCtx->>Recovery: attempt_recovery(error_context)
        
        alt Circuit Breaker Recovery
            Recovery->>Recovery: check_circuit_breaker_state()
            Recovery->>Recovery: attempt_fallback_service()
            Recovery-->>ErrorCtx: recovery_result(success=true)
        else Retry Recovery
            Recovery->>Recovery: calculate_retry_backoff()
            Recovery->>Service: retry_operation(with_backoff)
            Service-->>Recovery: retry_result
            Recovery-->>ErrorCtx: recovery_result
        else Resource Recovery
            Recovery->>Recovery: free_resources()
            Recovery->>Recovery: reset_connections()
            Recovery-->>ErrorCtx: recovery_result(partial_success)
        end
        
        alt Recovery Successful
            ErrorCtx->>Logging: log_recovery_success(error_context)
            ErrorCtx-->>Service: continue_processing()
            Service-->>API: Success Response
        else Recovery Failed
            ErrorCtx->>ErrorCtx: escalate_error()
        end
    else Manual Intervention Required
        ErrorCtx->>ErrorCtx: escalate_error()
    end
    
    %% Error Escalation
    alt Error Escalated
        ErrorCtx->>Health: record_component_degradation()
        ErrorCtx->>Alert: trigger_alert(error_severity, context)
        
        Alert->>Alert: evaluate_alert_rules()
        Alert->>Alert: check_notification_cooldown()
        
        alt Critical Error
            Alert->>Alert: send_immediate_notification()
        else Non-Critical Error
            Alert->>Alert: batch_notification()
        end
        
        ErrorCtx->>Logging: log_escalated_error(full_context)
        ErrorCtx-->>Service: error_escalated
        Service-->>API: Error Response
    end
    
    API->>API: format_error_response()
    API->>Logging: log_request_error(correlation_id)
    API-->>Client: Error Response with correlation_id
    
    %% Health Impact Assessment
    Health->>Health: assess_system_health_impact()
    Health->>Health: update_component_health_status()
    Health->>Health: check_sla_violations()
    
    note over Client,Alert: Error handled with full context and monitoring
```

## LLM Provider Failover Flow

Shows how the system handles LLM provider failures with automatic failover and caching.

```mermaid
sequenceDiagram
    participant Agent as Agent Service
    participant Router as LLM Router
    participant Cache as LLM Cache
    participant Primary as Primary LLM (OpenAI)
    participant Secondary as Secondary LLM (Anthropic)
    participant Tertiary as Tertiary LLM (Fireworks)
    participant Monitor as Circuit Breaker
    
    Agent->>Router: generate_response(prompt, config)
    Router->>Cache: check_semantic_cache(prompt_embedding)
    
    alt Cache Hit
        Cache-->>Router: cached_response
        Router-->>Agent: cached_response
        note over Agent,Router: Fast response from cache
    else Cache Miss
        Router->>Monitor: check_provider_health(primary)
        
        alt Primary Provider Healthy
            Router->>Primary: generate_response(prompt)
            
            alt Primary Success
                Primary-->>Router: response
                Router->>Cache: store_response(prompt, response)
                Router-->>Agent: response
            else Primary Failure
                Primary-->>Router: error (timeout/rate_limit/service_error)
                Router->>Monitor: record_failure(primary)
                Monitor->>Monitor: update_circuit_breaker_state()
            end
        end
        
        alt Primary Failed or Circuit Open
            Router->>Monitor: check_provider_health(secondary)
            
            alt Secondary Available
                Router->>Secondary: generate_response(adapted_prompt)
                
                alt Secondary Success
                    Secondary-->>Router: response
                    Router->>Cache: store_response(prompt, response)
                    Router-->>Agent: response
                else Secondary Failure
                    Secondary-->>Router: error
                    Router->>Monitor: record_failure(secondary)
                end
            end
        end
        
        alt Primary and Secondary Failed
            Router->>Monitor: check_provider_health(tertiary)
            
            alt Tertiary Available
                Router->>Tertiary: generate_response(adapted_prompt)
                
                alt Tertiary Success
                    Tertiary-->>Router: response
                    Router->>Cache: store_response(prompt, response)
                    Router-->>Agent: response
                else All Providers Failed
                    Tertiary-->>Router: error
                    Router->>Monitor: record_failure(tertiary)
                    Router-->>Agent: LLMProviderError("All providers unavailable")
                end
            end
        end
    end
    
    %% Background Health Recovery
    par Circuit Breaker Recovery
        loop Every 30 seconds
            Monitor->>Monitor: check_recovery_conditions()
            
            alt Circuit Breaker Half-Open
                Monitor->>Primary: health_check()
                
                alt Health Check Success
                    Monitor->>Monitor: close_circuit_breaker(primary)
                else Health Check Failed
                    Monitor->>Monitor: keep_circuit_open(primary)
                end
            end
        end
    end
    
    note over Agent,Monitor: Automatic failover with health monitoring
```

## Performance Monitoring Flow

Demonstrates how performance metrics are collected and monitored across all system components.

```mermaid
sequenceDiagram
    participant Request as Incoming Request
    participant Middleware as Performance Middleware
    participant Service as Service Layer
    participant Collector as Metrics Collector
    participant APM as APM Integration
    participant Alerting as Alert Manager
    participant Dashboard as Monitoring Dashboard
    
    Request->>Middleware: HTTP Request
    Middleware->>Middleware: start_request_timer()
    Middleware->>Middleware: record_request_start_metrics()
    
    Middleware->>Service: Forward Request
    Service->>Collector: record_service_start(service_name)
    
    %% Service Processing with Metrics
    Service->>Service: process_business_logic()
    Service->>Collector: record_operation_metric(operation, duration)
    
    %% Database/External Service Calls
    Service->>Service: external_service_call()
    Service->>Collector: record_external_call(service, response_time, success)
    
    Service-->>Middleware: Service Response
    Service->>Collector: record_service_complete(service_name, success)
    
    Middleware->>Middleware: calculate_total_response_time()
    Middleware->>Middleware: record_response_metrics()
    Middleware->>Collector: record_request_complete(status_code, response_time)
    
    %% Metrics Processing
    Collector->>Collector: aggregate_metrics()
    Collector->>Collector: calculate_percentiles()
    Collector->>Collector: update_sliding_windows()
    
    %% APM Integration
    par APM Export
        loop Every 10 seconds
            Collector->>APM: export_metrics_batch()
            APM->>APM: format_prometheus_metrics()
            APM->>APM: send_to_external_monitoring()
        end
    end
    
    %% Real-time Alerting
    par Alert Processing
        Collector->>Alerting: check_alert_thresholds(latest_metrics)
        
        alt Threshold Exceeded
            Alerting->>Alerting: evaluate_alert_rules()
            Alerting->>Alerting: check_alert_cooldown()
            
            alt Alert Triggered
                Alerting->>Alerting: create_alert(metric, threshold, current_value)
                Alerting->>Alerting: send_notification()
                Alerting->>Dashboard: update_alert_status()
            end
        else Threshold Normal
            Alerting->>Alerting: check_alert_resolution()
            
            alt Alert Resolved
                Alerting->>Alerting: resolve_alert()
                Alerting->>Dashboard: update_alert_status(resolved)
            end
        end
    end
    
    %% Dashboard Updates
    par Dashboard Updates
        loop Every 5 seconds
            Dashboard->>Collector: get_real_time_metrics()
            Collector-->>Dashboard: latest_metrics
            Dashboard->>Dashboard: update_charts_and_graphs()
            Dashboard->>APM: get_historical_data()
            APM-->>Dashboard: time_series_data
        end
    end
    
    Middleware-->>Request: HTTP Response
    
    note over Request,Dashboard: Complete request lifecycle with monitoring
```

## Health Monitoring and SLA Tracking

Shows how component health is continuously monitored and SLA compliance is tracked.

```mermaid
sequenceDiagram
    participant Scheduler as Health Scheduler
    participant Monitor as Component Monitor
    participant Redis as Redis
    participant ChromaDB as ChromaDB
    participant Presidio as Presidio Service
    participant LLM as LLM Providers
    participant SLA as SLA Tracker
    participant Alert as Alert Manager
    
    %% Continuous Health Monitoring
    loop Every 30 seconds
        Scheduler->>Monitor: trigger_health_check_cycle()
        
        %% Check Each Component
        par Component Health Checks
            Monitor->>Redis: health_check()
            Redis-->>Monitor: health_result(response_time, status)
            
            Monitor->>ChromaDB: health_check()
            ChromaDB-->>Monitor: health_result(response_time, status)
            
            Monitor->>Presidio: health_check()
            Presidio-->>Monitor: health_result(response_time, status)
            
            Monitor->>LLM: health_check_all_providers()
            LLM-->>Monitor: provider_health_results[]
        end
        
        %% Process Health Results
        Monitor->>Monitor: aggregate_health_results()
        Monitor->>Monitor: calculate_overall_system_health()
        
        %% SLA Tracking
        Monitor->>SLA: record_health_measurements(component_results)
        SLA->>SLA: update_availability_metrics()
        SLA->>SLA: calculate_response_time_percentiles()
        SLA->>SLA: check_sla_violations()
        
        alt SLA Violation Detected
            SLA->>SLA: record_sla_breach(component, metric, threshold)
            SLA->>Alert: trigger_sla_violation_alert()
            
            Alert->>Alert: evaluate_alert_severity()
            Alert->>Alert: check_escalation_rules()
            Alert->>Alert: send_sla_notification()
        end
        
        %% Component-Specific Actions
        loop For each component
            alt Component Degraded
                Monitor->>Monitor: record_degradation_event()
                Monitor->>Monitor: check_dependency_impact()
                
                alt Critical Dependency
                    Monitor->>Alert: trigger_critical_component_alert()
                end
            else Component Recovered
                Monitor->>Monitor: record_recovery_event()
                Monitor->>SLA: update_recovery_metrics()
            end
        end
        
        %% Health Status Publication
        Monitor->>Monitor: publish_health_status()
        Monitor->>SLA: publish_sla_metrics()
    end
    
    %% SLA Reporting
    par SLA Reporting
        loop Every Hour
            SLA->>SLA: generate_hourly_sla_report()
            SLA->>SLA: calculate_trending_metrics()
            SLA->>SLA: identify_performance_patterns()
        end
        
        loop Every Day
            SLA->>SLA: generate_daily_sla_summary()
            SLA->>SLA: calculate_availability_percentage()
            SLA->>SLA: generate_performance_recommendations()
        end
    end
    
    note over Scheduler,Alert: Continuous health monitoring with SLA compliance tracking
```

## Inter-Component Communication Patterns

### Synchronous Communication
- **HTTP REST APIs**: Client-server communication with request/response pattern
- **Direct Method Calls**: In-process communication between layers via dependency injection
- **Database Queries**: Synchronous data retrieval with connection pooling

### Asynchronous Communication
- **Background Processing**: File processing and data ingestion in separate tasks
- **Event-Driven Updates**: Health monitoring and metrics collection
- **Batch Operations**: Session cleanup and maintenance operations

### Error Propagation
- **Context Preservation**: Error context flows through all layers with correlation IDs
- **Graceful Degradation**: System continues operating with reduced functionality during failures
- **Circuit Breaker Pattern**: Automatic failover and recovery for external services

### Performance Optimization
- **Caching Layers**: Multiple levels of caching for improved response times
- **Connection Pooling**: Efficient resource utilization for external services
- **Lazy Loading**: On-demand initialization of expensive resources

These interaction patterns demonstrate how FaultMaven maintains high availability, performance, and reliability through sophisticated error handling, monitoring, and recovery mechanisms.