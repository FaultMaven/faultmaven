# FaultMaven Frontend API Integration Guide

**Document Type**: Frontend Implementation Guide  
**Last Updated**: August 2025

## Overview

This document defines the frontend requirements for integrating with the FaultMaven backend API, following a stateless frontend/stateful backend architecture.

**⚠️ Important**: The frontend must be a **stateless renderer** that relies on the backend as the single source of truth. All UI state comes from the `view_state` object returned by the backend.

## Architecture Requirements

### Core Principles

**Frontend Requirements:**
- **Stateless UI**: Frontend renders based on `view_state` objects from backend
- **Developer Login**: Establish user identity and obtain session_id
- **Case Management**: Display user's Cases, allow creation/switching
- **Distinct Data/Query Methods**: Separate endpoints for data submission vs questions
- **Markdown Rendering**: Render agent content as Markdown with rich formatting
- **Response-Type Rendering**: Use `response_type` enum for component decisions
- **Source Display**: Show evidence sources to build user trust

**Backend Contract:**
- **Single Source of Truth**: Backend manages all user and investigation data
- **Session vs Case Model**: Sessions (temporary connections) vs Cases (persistent investigations)
- **User-Owned Cases**: Cases belong to Users, not Sessions
- **Unified AgentResponse**: Single response format with explicit `response_type`
- **View State Management**: Complete UI state provided in `view_state` object

### Base Configuration

```typescript
// API configuration
export const API_CONFIG = {
  baseURL: process.env.VITE_API_BASE_URL || 'http://localhost:8000',
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json',
    'Accept': 'application/json'
  }
};

// API client setup with session token interceptor
export const faultMavenApi = axios.create(API_CONFIG);

// Request interceptor to add session token
faultMavenApi.interceptors.request.use((config) => {
  const sessionToken = localStorage.getItem('session_token');
  if (sessionToken) {
    config.headers.Authorization = `Bearer ${sessionToken}`;
  }
  return config;
});
```

## Core API Implementation

### 1. Session Management

#### Create Session
```typescript
interface SessionRequest {
  user_id: string;
  metadata?: Record<string, any>;
}

interface SessionResponse {
  session_id: string;
  session_token: string;
  expires_at: string;
}

// Create authenticated session and return session token
export const createSession = async (userId: string): Promise<SessionResponse> => {
  const response = await faultMavenApi.post('/api/v1/sessions', { 
    user_id: userId,
    metadata: {
      created_at: new Date().toISOString(),
      client: 'browser_extension'
    }
  });
  
  // Store session token for subsequent requests
  localStorage.setItem('session_token', response.data.session_token);
  localStorage.setItem('session_id', response.data.session_id);
  
  return response.data;
};
```

### 2. Developer Login (Required)

```typescript
interface DevLoginRequest {
  email: string;
}

interface AuthResponse {
  session_id: string;
  view_state: ViewState;
}

// Developer login implementation
export const developerLogin = async (email: string): Promise<AuthResponse> => {
  const response = await faultMavenApi.post('/api/v1/auth/dev-login', { email });
  
  // Extract user_id from response and create session
  const sessionResponse = await createSession(response.data.view_state.user.user_id);
  
  // Return complete view_state for immediate UI rendering
  return response.data;
};
```

### 3. Case Management

#### List Cases
```typescript
interface CaseListParams {
  status?: 'active' | 'resolved' | 'archived';
  priority?: 'low' | 'medium' | 'high' | 'urgent';
  limit?: number;
  offset?: number;
}

// Retrieve user's past cases
export const getCases = async (params?: CaseListParams): Promise<{cases: CaseWithMetadata[], total: number}> => {
  const response = await faultMavenApi.get('/api/v1/cases', { params });
  return response.data;
};
```

#### Create Case
```typescript
interface CreateCaseRequest {
  title: string;
  description?: string;
  priority?: 'low' | 'medium' | 'high' | 'urgent';
  tags?: string[];
}

// Create new, empty case and return case_id
export const createCase = async (request: CreateCaseRequest): Promise<AgentResponse> => {
  const response = await faultMavenApi.post('/api/v1/cases', request);
  return response.data;
};
```

### 4. Data Submission (Case-Specific)

```typescript
interface DataSubmissionRequest {
  case_id: string;
  content?: string;          // Raw text data
  file?: File;              // Uploaded file
  description?: string;     // Optional description
}

// Submit data to specific case
export const submitDataToCase = async (request: DataSubmissionRequest): Promise<AgentResponse> => {
  const formData = new FormData();
  
  if (request.content) {
    formData.append('content', request.content);
  }
  
  if (request.file) {
    formData.append('file', request.file);
  }
  
  if (request.description) {
    formData.append('description', request.description);
  }
  
  const response = await faultMavenApi.post(`/api/v1/cases/${request.case_id}/data`, formData, {
    headers: { 'Content-Type': 'multipart/form-data' }
  });
  
  return response.data;
};
```

### 5. Query Submission (Case-Specific)

```typescript
interface QueryRequest {
  case_id: string;
  query: string;
  priority?: 'low' | 'medium' | 'high' | 'urgent';
}

// Submit questions to specific case
export const submitQueryToCase = async (request: QueryRequest): Promise<AgentResponse> => {
  const response = await faultMavenApi.post(`/api/v1/cases/${request.case_id}/query`, {
    query: request.query,
    priority: request.priority || 'medium'
  });
  
  return response.data;
};
```

## Stateless Frontend Implementation

### 4. Response Handling & View State

```typescript
interface AgentResponse {
  response_type: ResponseType;
  content: string;           // Markdown content for rendering
  view_state: ViewState;     // Complete UI state from backend
  sources?: Source[];        // Evidence sources for trust
  confidence_score?: number;
  plan?: PlanStep[];        // For PLAN_PROPOSAL responses
}

interface ViewState {
  session_id: string;
  user: User;
  active_case: Case | null;
  cases: Case[];
  conversation: {
    message_count: number;
    last_updated: string;
  };
}

enum ResponseType {
  ANSWER = 'ANSWER',
  PLAN_PROPOSAL = 'PLAN_PROPOSAL', 
  CLARIFICATION_REQUEST = 'CLARIFICATION_REQUEST',
  DATA_REQUEST = 'DATA_REQUEST',
  ERROR = 'ERROR'
}

// Stateless response handler - re-render entire UI from view_state
export const handleAgentResponse = (response: AgentResponse) => {
  // 1. Update entire UI state from view_state (not local state)
  updateUIFromViewState(response.view_state);
  
  // 2. Render content as Markdown
  const contentElement = renderMarkdown(response.content);
  
  // 3. Use response_type for component rendering decisions
  const componentType = getComponentForResponseType(response.response_type);
  
  // 4. Display sources for evidence and trust
  if (response.sources) {
    renderSources(response.sources);
  }
  
  // 5. Handle specific response types
  switch (response.response_type) {
    case ResponseType.ANSWER:
      renderTextBubble(contentElement);
      break;
    case ResponseType.PLAN_PROPOSAL:
      renderInteractiveChecklist(response.plan, contentElement);
      break;
    case ResponseType.CLARIFICATION_REQUEST:
      renderClarificationPrompt(contentElement);
      break;
    // ... other response types
  }
};
```

### 5. Case Management (User Interface Requirements)

```typescript
interface Case {
  id: string;
  title: string;
  status: 'active' | 'resolved' | 'archived';
  created_at: string;
  updated_at: string;
  user_id: string;  // Cases are owned by Users, not Sessions
}

// Display list of user's Cases (from view_state)
export const renderCaseList = (cases: Case[]) => {
  return (
    <div className="case-list">
      {cases.map(case => (
        <CaseItem 
          key={case.id}
          case={case}
          onClick={() => switchToCase(case.id)}
        />
      ))}
      <CreateCaseButton onClick={() => createNewCase()} />
    </div>
  );
};

// Create new Case
export const createNewCase = async (title: string): Promise<AgentResponse> => {
  const sessionId = localStorage.getItem('session_id');
  const response = await faultMavenApi.post('/api/v1/cases', {
    session_id: sessionId,
    title: title
  });
  
  // Backend returns updated view_state with new case
  return response.data;
};

// Switch between existing Cases
export const switchToCase = async (caseId: string): Promise<AgentResponse> => {
  const sessionId = localStorage.getItem('session_id');
  const response = await faultMavenApi.post(`/api/v1/cases/${caseId}/switch`, {
    session_id: sessionId
  });
  
  // Backend returns view_state with switched case context
  return response.data;
};

// Get all user cases with filtering
export const getUserCases = async (filters?: CaseFilters): Promise<CaseListResponse> => {
  const sessionId = localStorage.getItem('session_id');
  const params = new URLSearchParams({ session_id: sessionId });
  
  if (filters?.status) params.append('status', filters.status);
  if (filters?.priority) params.append('priority', filters.priority);
  if (filters?.search) params.append('search', filters.search);
  if (filters?.tags) filters.tags.forEach(tag => params.append('tags', tag));
  if (filters?.limit) params.append('limit', filters.limit.toString());
  if (filters?.offset) params.append('offset', filters.offset.toString());
  
  const response = await faultMavenApi.get(`/api/v1/cases?${params.toString()}`);
  return response.data;
};

// Get detailed case information
export const getCaseDetails = async (caseId: string): Promise<CaseWithMetadata> => {
  const sessionId = localStorage.getItem('session_id');
  const response = await faultMavenApi.get(`/api/v1/cases/${caseId}?session_id=${sessionId}`);
  return response.data;
};

// Update case metadata
export const updateCase = async (caseId: string, updates: CaseUpdateRequest): Promise<AgentResponse> => {
  const sessionId = localStorage.getItem('session_id');
  const response = await faultMavenApi.put(`/api/v1/cases/${caseId}`, {
    session_id: sessionId,
    ...updates
  });
  
  // Backend returns updated view_state
  return response.data;
};

// Delete case with confirmation
export const deleteCase = async (caseId: string, confirm: boolean = true): Promise<AgentResponse> => {
  const sessionId = localStorage.getItem('session_id');
  const response = await faultMavenApi.delete(`/api/v1/cases/${caseId}?session_id=${sessionId}&confirm=${confirm}`);
  
  // Backend returns updated view_state
  return response.data;
};

// Advanced case search
export const searchCases = async (searchRequest: CaseSearchRequest): Promise<CaseSearchResponse> => {
  const sessionId = localStorage.getItem('session_id');
  const response = await faultMavenApi.post('/api/v1/cases/search', {
    session_id: sessionId,
    ...searchRequest
  });
  
  return response.data;
};
```

### 6. Enhanced Case Management Interfaces

```typescript
interface CaseWithMetadata {
  id: string;
  title: string;
  description?: string;
  status: 'active' | 'resolved' | 'archived';
  priority: 'low' | 'medium' | 'high' | 'urgent';
  tags: string[];          // Max 5 tags
  user_id: string;         // Cases are owned by Users
  created_at: string;
  updated_at: string;
  conversation_count: number;
  last_activity: string;
  progress: {
    total_steps: number;
    completed_steps: number;
    percentage: number;
  };
  session_duration: number;  // For CaseStatusDisplay real-time tracking
  draft_message?: string;    // For draft management
}

interface CaseFilters {
  status?: 'active' | 'resolved' | 'archived';
  priority?: 'low' | 'medium' | 'high' | 'urgent';
  search?: string;           // Search in title/description  
  tags?: string[];
  limit?: number;            // For pagination
  offset?: number;
}

interface CaseListResponse {
  cases: CaseWithMetadata[];
  total: number;
  has_more: boolean;
}

interface CaseUpdateRequest {
  title?: string;
  description?: string;
  priority?: 'low' | 'medium' | 'high' | 'urgent';
  status?: 'active' | 'resolved' | 'archived';
  tags?: string[];  // Max 5 tags
}

interface CaseSearchRequest {
  query?: string;
  filters?: {
    status?: ('active' | 'resolved' | 'archived')[];
    priority?: ('low' | 'medium' | 'high' | 'urgent')[];
    tags?: string[];
    date_range?: {
      from: string;
      to: string;
    };
  };
  limit?: number;
}

interface CaseSearchResponse {
  results: (CaseWithMetadata & { relevance_score: number })[];
  total: number;
}
```

### 7. Component Integration for Case Management

```typescript
// CaseSelector integration with search and filtering
export const useCaseSelector = () => {
  const [cases, setCases] = useState<CaseWithMetadata[]>([]);
  const [loading, setLoading] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  
  // Debounced search for CaseSelector dropdown
  const debouncedSearch = useCallback(
    debounce(async (query: string) => {
      if (!query) {
        const response = await getUserCases();
        setCases(response.cases);
      } else {
        const response = await searchCases({
          query,
          limit: 20
        });
        setCases(response.results);
      }
    }, 300),
    []
  );
  
  useEffect(() => {
    debouncedSearch(searchQuery);
  }, [searchQuery, debouncedSearch]);
  
  const handleCaseSelect = async (caseId: string) => {
    setLoading(true);
    try {
      const response = await switchToCase(caseId);
      // Update entire UI from returned view_state
      updateUIFromViewState(response.view_state);
    } finally {
      setLoading(false);
    }
  };
  
  return {
    cases,
    loading,
    searchQuery,
    setSearchQuery,
    onCaseSelect: handleCaseSelect
  };
};

// CaseManagementPanel integration with validation
export const useCaseManagement = () => {
  const [validationErrors, setValidationErrors] = useState<Record<string, string>>({});
  
  const validateCaseData = (data: CaseUpdateRequest): boolean => {
    const errors: Record<string, string> = {};
    
    if (data.title && data.title.length > 200) {
      errors.title = 'Title must be 200 characters or less';
    }
    
    if (data.description && data.description.length > 1000) {
      errors.description = 'Description must be 1000 characters or less';
    }
    
    if (data.tags && data.tags.length > 5) {
      errors.tags = 'Maximum 5 tags allowed';
    }
    
    setValidationErrors(errors);
    return Object.keys(errors).length === 0;
  };
  
  const createCaseWithValidation = async (data: { title: string; description?: string; tags?: string[]; priority?: string }): Promise<AgentResponse | null> => {
    if (!validateCaseData(data)) return null;
    
    const response = await createNewCase(data.title);
    
    // Update additional metadata if needed
    if (data.description || data.tags || data.priority) {
      await updateCase(response.view_state.active_case?.id!, {
        description: data.description,
        tags: data.tags,
        priority: data.priority as any
      });
    }
    
    return response;
  };
  
  return {
    validationErrors,
    validateCaseData,
    createCaseWithValidation,
    updateCase,
    deleteCase
  };
};

// CaseStatusDisplay integration with real-time updates
export const useCaseStatusDisplay = (caseId: string) => {
  const [caseDetails, setCaseDetails] = useState<CaseWithMetadata | null>(null);
  const [sessionDuration, setSessionDuration] = useState(0);
  
  // Real-time session duration tracking
  useEffect(() => {
    const interval = setInterval(() => {
      setSessionDuration(prev => prev + 1);
    }, 1000);
    
    return () => clearInterval(interval);
  }, []);
  
  // Load case details for progress tracking
  useEffect(() => {
    if (caseId) {
      getCaseDetails(caseId).then(setCaseDetails);
    }
  }, [caseId]);
  
  const formatSessionDuration = (seconds: number): string => {
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const secs = seconds % 60;
    
    if (hours > 0) {
      return `${hours}h ${minutes}m ${secs}s`;
    } else if (minutes > 0) {
      return `${minutes}m ${secs}s`;
    } else {
      return `${secs}s`;
    }
  };
  
  return {
    caseDetails,
    sessionDuration: formatSessionDuration(sessionDuration),
    progressPercentage: caseDetails?.progress?.percentage || 0
  };
};
```

## UI Rendering Requirements

### 8. Markdown Content Rendering

```typescript
import ReactMarkdown from 'react-markdown';
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter';

// Render agent content as Markdown with rich formatting
export const renderMarkdown = (content: string) => {
  return (
    <ReactMarkdown
      components={{
        // Code blocks
        code({ node, inline, className, children, ...props }) {
          const match = /language-(\w+)/.exec(className || '');
          return !inline && match ? (
            <SyntaxHighlighter
              style={prism}
              language={match[1]}
              PreTag="div"
              {...props}
            >
              {String(children).replace(/\n$/, '')}
            </SyntaxHighlighter>
          ) : (
            <code className={className} {...props}>
              {children}
            </code>
          );
        },
        // Tables
        table: ({ children }) => (
          <table className="table-auto border-collapse border border-gray-300">
            {children}
          </table>
        ),
        // Lists
        ul: ({ children }) => (
          <ul className="list-disc ml-6 space-y-1">
            {children}
          </ul>
        ),
        ol: ({ children }) => (
          <ol className="list-decimal ml-6 space-y-1">
            {children}
          </ol>
        ),
      }}
    >
      {content}
    </ReactMarkdown>
  );
};
```

### 7. Source Display for Trust

```typescript
interface Source {
  type: string;
  content: string;
  confidence?: number;
}

// Display evidence sources to build user trust
export const renderSources = (sources: Source[]) => {
  return (
    <div className="sources-section">
      <h4>Evidence Sources:</h4>
      {sources.map((source, index) => (
        <div key={index} className="source-item">
          <span className="source-type">{source.type}</span>
          <span className="source-content">{source.content}</span>
          {source.confidence && (
            <span className="confidence">
              ({Math.round(source.confidence * 100)}% confidence)
            </span>
          )}
        </div>
      ))}
    </div>
  );
};
```

### 8. Response Type Component Rendering

```typescript
// Use response_type for different UI components
export const getComponentForResponseType = (responseType: ResponseType) => {
  switch (responseType) {
    case ResponseType.ANSWER:
      return 'TextBubble';  // Simple text display
      
    case ResponseType.PLAN_PROPOSAL:
      return 'InteractiveChecklist';  // Interactive plan steps
      
    case ResponseType.CLARIFICATION_REQUEST:
      return 'ClarificationPrompt';  // Input form for clarification
      
    case ResponseType.DATA_REQUEST:
      return 'DataUploadPrompt';  // File/data upload interface
      
    case ResponseType.ERROR:
      return 'ErrorDisplay';  // Error message with retry options
      
    default:
      return 'TextBubble';
  }
};
```

## Complete Integration Example

```typescript
// Main application integration
export class FaultMavenApp {
  private sessionId: string | null = null;
  
  async initialize() {
    // 1. Developer login to establish identity
    const email = await promptForEmail();
    const authResponse = await developerLogin(email);
    
    this.sessionId = authResponse.session_id;
    
    // 2. Render initial UI from view_state
    this.renderFromViewState(authResponse.view_state);
  }
  
  async submitData(content: string, file?: File) {
    if (!this.sessionId) throw new Error('Not authenticated');
    
    const activeCaseId = this.getActiveCaseId();
    const response = await submitData({
      session_id: this.sessionId,
      case_id: activeCaseId,
      content,
      file
    });
    
    // Re-render UI from updated view_state
    this.handleResponse(response);
  }
  
  async askQuestion(query: string) {
    if (!this.sessionId) throw new Error('Not authenticated');
    
    const activeCaseId = this.getActiveCaseId();
    const response = await submitQuery({
      session_id: this.sessionId,
      case_id: activeCaseId,
      query
    });
    
    // Re-render UI from updated view_state
    this.handleResponse(response);
  }
  
  private handleResponse(response: AgentResponse) {
    // Stateless rendering from view_state
    handleAgentResponse(response);
  }
  
  private renderFromViewState(viewState: ViewState) {
    // Update entire UI from backend state
    updateUIFromViewState(viewState);
  }
}
```

## Summary

This integration guide ensures the frontend meets all requirements:

**✅ User and Case Management:**
- Developer login establishes user identity and session_id
- UI displays list of user's Cases from view_state
- Users can create new Cases and switch between existing ones

**✅ Interaction Model:**
- Distinct methods for data submission (/data) vs questions (/query)
- Frontend is stateless renderer using view_state objects
- All UI state comes from backend, not local frontend state

**✅ Response Rendering:**
- Agent content rendered as Markdown with rich formatting
- response_type enum drives component rendering decisions
- Sources displayed for evidence and user trust
- Interactive components for different response types (plans, clarifications, etc.)
// Submit troubleshooting query
export const submitQuery = async (request: SubmitQueryRequest): Promise<AgentResponse> => {
  const response = await faultMavenApi.post('/api/v1/cases/{case_id}/queries', request);
  return response.data;
};

// Query with file uploads
export const submitQueryWithFiles = async (
  request: SubmitQueryRequest,
  files: File[]
): Promise<AgentResponse> => {
  const formData = new FormData();
  formData.append('request', JSON.stringify(request));
  
  files.forEach((file, index) => {
    formData.append(`file_${index}`, file);
  });
  
  const response = await faultMavenApi.post('/api/v1/cases/{case_id}/queries', formData, {
    headers: {
      'Content-Type': 'multipart/form-data'
    }
  });
  
  return response.data;
};
```

### 2. Session Management

```typescript
// Create new session
export const createSession = async (metadata: SessionMetadata): Promise<Session> => {
  const response = await faultMavenApi.post('/api/v1/sessions', metadata);
  return response.data;
};

// Get session details
export const getSession = async (sessionId: string): Promise<Session> => {
  const response = await faultMavenApi.get(`/api/v1/sessions/${sessionId}`);
  return response.data;
};

// Update session
export const updateSession = async (
  sessionId: string,
  updates: Partial<Session>
): Promise<Session> => {
  const response = await faultMavenApi.patch(`/api/v1/sessions/${sessionId}`, updates);
  return response.data;
};
```

### 3. Case Management

```typescript
// Get user cases
export const getUserCases = async (filters?: CaseFilters): Promise<Case[]> => {
  const params = new URLSearchParams();
  if (filters) {
    Object.entries(filters).forEach(([key, value]) => {
      if (value !== undefined) {
        params.append(key, String(value));
      }
    });
  }
  
  const response = await faultMavenApi.get(`/api/v1/cases?${params.toString()}`);
  return response.data;
};

// Mark case as resolved
export const markCaseResolved = async (caseId: string): Promise<Case> => {
  const response = await faultMavenApi.patch(`/api/v1/cases/${caseId}/resolve`);
  return response.data;
};
```

## React Query Integration

### Query Hooks

```typescript
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';

// Query keys
export const queryKeys = {
  sessions: ['sessions'] as const,
  session: (id: string) => ['sessions', id] as const,
  cases: ['cases'] as const,
  case: (id: string) => ['cases', id] as const,
  conversation: (sessionId: string) => ['conversation', sessionId] as const
};

// Session queries
export const useSessions = (filters?: CaseFilters) => {
  return useQuery({
    queryKey: [...queryKeys.sessions, filters],
    queryFn: () => getUserCases(filters),
    staleTime: 5 * 60 * 1000, // 5 minutes
    gcTime: 10 * 60 * 1000 // 10 minutes
  });
};

export const useSession = (sessionId: string) => {
  return useQuery({
    queryKey: queryKeys.session(sessionId),
    queryFn: () => getSession(sessionId),
    enabled: !!sessionId,
    staleTime: 2 * 60 * 1000, // 2 minutes
    gcTime: 5 * 60 * 1000 // 5 minutes
  });
};

// Mutations
export const useSubmitQuery = () => {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: submitQuery,
    onSuccess: (response, variables) => {
      // Update conversation cache
      queryClient.setQueryData(
        queryKeys.conversation(variables.session_id),
        (oldData: Conversation | undefined) => {
          if (!oldData) return oldData;
          return {
            ...oldData,
            messages: [...oldData.messages, response]
          };
        }
      );
      
      // Update session status if needed
      if (response.response_type === ResponseType.SOLUTION_READY) {
        queryClient.setQueryData(
          queryKeys.session(variables.session_id),
          (oldData: Session | undefined) => {
            if (!oldData) return oldData;
            return {
              ...oldData,
              status: 'solution_ready',
              lastUpdated: new Date().toISOString()
            };
          }
        );
      }
    }
  });
};
```

## Real-time Communication

### WebSocket Integration

```typescript
export const useWebSocketConnection = (sessionId: string) => {
  const queryClient = useQueryClient();
  
  useEffect(() => {
    const ws = new WebSocket(`ws://localhost:8000/ws/session/${sessionId}`);
    
    ws.onopen = () => {
      console.log('WebSocket connected');
    };
    
    ws.onmessage = (event) => {
      const update = JSON.parse(event.data);
      
      switch (update.type) {
        case 'message_received':
          // Update conversation cache
          queryClient.setQueryData(
            queryKeys.conversation(sessionId),
            (oldData: Conversation | undefined) => {
              if (!oldData) return oldData;
              return {
                ...oldData,
                messages: [...oldData.messages, update.message]
              };
            }
          );
          break;
          
        case 'session_status_updated':
          // Update session cache
          queryClient.setQueryData(
            queryKeys.session(sessionId),
            (oldData: Session | undefined) => {
              if (!oldData) return oldData;
              return {
                ...oldData,
                status: update.status,
                lastUpdated: update.timestamp
              };
            }
          );
          break;
      }
    };
    
    ws.onerror = (error) => {
      console.error('WebSocket error:', error);
    };
    
    ws.onclose = () => {
      console.log('WebSocket disconnected');
    };
    
    return () => {
      ws.close();
    };
  }, [sessionId, queryClient]);
};
```

### Server-Sent Events

```typescript
export const useServerSentEvents = (sessionId: string) => {
  const queryClient = useQueryClient();
  
  useEffect(() => {
    const eventSource = new EventSource(`/api/v1/sessions/${sessionId}/events`);
    
    eventSource.onmessage = (event) => {
      const update = JSON.parse(event.data);
      // Handle real-time updates
      handleRealtimeUpdate(update, queryClient, sessionId);
    };
    
    eventSource.onerror = (error) => {
      console.error('SSE error:', error);
      eventSource.close();
    };
    
    return () => {
      eventSource.close();
    };
  }, [sessionId, queryClient]);
};
```

## Error Handling

### API Error Types

```typescript
export interface APIError {
  error: string;
  details: {
    title: string;
    detail: string;
    isRetryable: boolean;
    additionalInfo?: Record<string, any>;
  };
  isExpected: boolean;
  timestamp: string;  // UTC ISO 8601 format: YYYY-MM-DDTHH:mm:ss.sssZ
}

export class FaultMavenAPIError extends Error {
  constructor(
    public apiError: APIError,
    public statusCode: number
  ) {
    super(apiError.details.title);
    this.name = 'FaultMavenAPIError';
  }
  
  get isRetryable(): boolean {
    return this.apiError.details.isRetryable;
  }
  
  get userMessage(): string {
    return this.apiError.details.detail;
  }
}
```

### Error Handling in Components

```typescript
export const useErrorHandler = () => {
  const { addNotification } = useUIStore();
  
  const handleError = useCallback((error: unknown) => {
    if (error instanceof FaultMavenAPIError) {
      addNotification({
        id: generateId(),
        type: 'error',
        title: 'API Error',
        message: error.userMessage,
        isRetryable: error.isRetryable
      });
    } else if (error instanceof Error) {
      addNotification({
        id: generateId(),
        type: 'error',
        title: 'Error',
        message: error.message
      });
    } else {
      addNotification({
        id: generateId(),
        type: 'error',
        title: 'Unknown Error',
        message: 'An unexpected error occurred'
      });
    }
  }, [addNotification]);
  
  return { handleError };
};
```

## File Upload Handling

### File Upload with Progress

```typescript
export const uploadFile = async (
  file: File,
  onProgress?: (progress: number) => void
): Promise<UploadedFile> => {
  const formData = new FormData();
  formData.append('file', file);
  
  const response = await faultMavenApi.post('/api/v1/files/upload', formData, {
    headers: {
      'Content-Type': 'multipart/form-data'
    },
    onUploadProgress: (progressEvent) => {
      if (onProgress && progressEvent.total) {
        const progress = Math.round(
          (progressEvent.loaded * 100) / progressEvent.total
        );
        onProgress(progress);
      }
    }
  });
  
  return response.data;
};

// File upload hook
export const useFileUpload = () => {
  const [uploadProgress, setUploadProgress] = useState<Record<string, number>>({});
  
  const uploadFileWithProgress = useCallback(async (file: File) => {
    const fileId = generateId();
    setUploadProgress(prev => ({ ...prev, [fileId]: 0 }));
    
    try {
      const result = await uploadFile(file, (progress) => {
        setUploadProgress(prev => ({ ...prev, [fileId]: progress }));
      });
      
      setUploadProgress(prev => {
        const { [fileId]: _, ...rest } = prev;
        return rest;
      });
      
      return result;
    } catch (error) {
      setUploadProgress(prev => {
        const { [fileId]: _, ...rest } = prev;
        return rest;
      });
      throw error;
    }
  }, []);
  
  return { uploadFileWithProgress, uploadProgress };
};
```

## Testing API Integration

### Mock API for Testing

```typescript
// Mock API responses for testing
export const mockAPIResponses = {
  submitQuery: {
    success: {
      response_type: "ANSWER",  // UPPERCASE as per API spec
      content: 'Mock response content',  // Plain text, not JSON
      confidence_score: 0.95,
      sources: [],
      plan: null,
      estimated_time_to_resolution: '5 minutes',
      next_action_hint: null,
      timestamp: new Date().toISOString()  // UTC with Z suffix
    },
    error: {
      error: 'MOCK_ERROR',
      details: {
        title: 'Mock Error',
        detail: 'This is a mock error for testing',
        isRetryable: false,
        additionalInfo: {}
      },
      isExpected: true
    }
  }
};

// Mock API client for testing
export const createMockAPIClient = () => ({
  submitQuery: jest.fn().mockResolvedValue(mockAPIResponses.submitQuery.success),
  createSession: jest.fn().mockResolvedValue({ id: 'mock-session-id' }),
  getSession: jest.fn().mockResolvedValue({ id: 'mock-session-id', status: 'active' })
});
```

### Testing API Hooks

```typescript
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

describe('API Integration Hooks', () => {
  let queryClient: QueryClient;
  
  beforeEach(() => {
    queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false }
      }
    });
  });
  
  it('should submit query successfully', async () => {
    const mockApi = createMockAPIClient();
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={queryClient}>
        {children}
      </QueryClientProvider>
    );
    
    const { result } = renderHook(() => useSubmitQuery(), { wrapper });
    
    await waitFor(() => {
      expect(result.current.isIdle).toBe(true);
    });
    
    result.current.mutate({
      query: 'Test query',
      session_id: 'test-session'
    });
    
    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    
    expect(mockApi.submitQuery).toHaveBeenCalledWith({
      query: 'Test query',
      session_id: 'test-session'
    });
  });
});
```

## Performance Optimization

### Request Deduplication

```typescript
// Deduplicate identical requests
export const useDeduplicatedQuery = <T>(
  queryKey: readonly unknown[],
  queryFn: () => Promise<T>,
  options?: UseQueryOptions<T>
) => {
  return useQuery({
    queryKey,
    queryFn,
    staleTime: 5 * 60 * 1000, // 5 minutes
    gcTime: 10 * 60 * 1000, // 10 minutes
    ...options
  });
};
```

### Optimistic Updates

```typescript
export const useOptimisticCaseUpdate = () => {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: updateCase,
    onMutate: async (update) => {
      // Cancel outgoing refetches
      await queryClient.cancelQueries({ queryKey: queryKeys.case(update.caseId) });
      
      // Snapshot previous value
      const previousCase = queryClient.getQueryData(queryKeys.case(update.caseId));
      
      // Optimistically update
      queryClient.setQueryData(
        queryKeys.case(update.caseId),
        (oldData: Case | undefined) => {
          if (!oldData) return oldData;
          return { ...oldData, ...update.changes };
        }
      );
      
      return { previousCase };
    },
    onError: (err, update, context) => {
      // Rollback on error
      if (context?.previousCase) {
        queryClient.setQueryData(
          queryKeys.case(update.caseId),
          context.previousCase
        );
      }
    }
  });
};
```

## Best Practices

### 1. Error Boundaries
- Implement error boundaries for API-related errors
- Provide user-friendly error messages
- Include retry mechanisms for transient failures

### 2. Loading States
- Show loading indicators during API calls
- Implement skeleton screens for better UX
- Use optimistic updates when possible

### 3. Caching Strategy
- Implement appropriate stale times for different data types
- Use background refetching for real-time updates
- Implement proper cache invalidation

### 4. Security
- Validate all API responses
- Implement proper authentication handling
- Sanitize user inputs before sending to API

## API Reference

### Available Endpoints

The following endpoints are available according to the OpenAPI specification:

**⚠️ API Compliance Requirements:**
- All `content` fields must contain plain text, never JSON objects or Python dict strings
- All `response_type` values must be UPPERCASE (e.g., "ANSWER", "PLAN_PROPOSAL")
- All `timestamp` fields must use UTC timezone in ISO 8601 format: `YYYY-MM-DDTHH:mm:ss.sssZ`

**🔄 Architecture Model:**
- **Sessions**: Top-level conversation containers (session_id)
- **Cases**: Individual troubleshooting cases within sessions (case_id)
- **Relationship**: Each session can contain multiple cases, but most sessions have one primary case
- **Implementation**: Frontend uses session_id for navigation, case_id for troubleshooting context

#### Agent Operations
- `POST /api/v1/cases/{case_id}/queries` - Submit troubleshooting query (returns `AgentResponse`)

#### Case Management
- `GET /api/v1/cases/{case_id}` - Get case details
- `GET /api/v1/sessions/{session_id}/cases` - List cases for a session

#### Session Management
- `POST /api/v1/sessions` - Create new session
- `GET /api/v1/sessions` - List sessions
- `GET /api/v1/sessions/{session_id}` - Get session details
- `DELETE /api/v1/sessions/{session_id}` - Delete session
- `POST /api/v1/sessions/{session_id}/heartbeat` - Session heartbeat
- `GET /api/v1/sessions/{session_id}/stats` - Get session statistics
- `POST /api/v1/sessions/{session_id}/cleanup` - Cleanup session
- `GET /api/v1/sessions/{session_id}/recovery-info` - Get recovery info
- `POST /api/v1/sessions/{session_id}/restore` - Restore session

#### Case Management
- `GET /api/v1/cases` - List cases
- `PATCH /api/v1/cases/{case_id}/resolve` - Resolve case

#### File Management
- `POST /api/v1/files/upload` - Upload file

#### Real-time Communication
- `GET /ws/session/{session_id}` - WebSocket connection
- `GET /api/v1/sessions/{session_id}/events` - Server-sent events

#### Data Operations
- `POST /api/v1/data/upload` - Upload data
- `POST /api/v1/data/batch-upload` - Batch upload data
- `GET /api/v1/data/sessions/{session_id}` - Get session data

#### Knowledge Base
- `POST /api/v1/knowledge/documents` - Upload document
- `GET /api/v1/knowledge/documents` - List documents
- `GET /api/v1/knowledge/search` - Search documents

### Response Types

The API uses the new v3.1.0 schema with 7 response types:

1. **ANSWER** - Direct answer to the query
2. **PLAN_PROPOSAL** - Step-by-step troubleshooting plan
3. **CLARIFICATION_REQUEST** - Asks for more information
4. **CONFIRMATION_REQUEST** - Asks for user confirmation
5. **SOLUTION_READY** - Solution is ready to implement
6. **NEEDS_MORE_DATA** - Requires additional data uploads
7. **ESCALATION_REQUIRED** - Issue escalated to human support

**Critical Implementation Notes:**
- All ResponseType enum values are UPPERCASE strings
- Backend MUST NOT send Python dict string representations in `content` field
- Frontend MUST handle case normalization defensively for backward compatibility
- All timestamp fields MUST use UTC timezone with explicit 'Z' suffix

## Conclusion

This guide provides comprehensive coverage of frontend API integration patterns for FaultMaven. **Always refer to the OpenAPI specification** (`docs/api/openapi.json` or `docs/api/openapi.yaml`) for the definitive API contract.

For additional guidance, refer to:
- [Frontend Component Library](./copilot-components.md)
- [State Management Patterns](./copilot-state.md)
- [System Requirements](../FAULTMAVEN_SYSTEM_REQUIREMENTS.md)

---

*Document Version: 1.0*  
*Last Updated: August 2025*  
*Author: FaultMaven Frontend Team*
