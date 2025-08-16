# FaultMaven Frontend API Integration Guide

**Document Type**: Frontend Implementation Guide  
**Last Updated**: August 2025

## Overview

This document provides comprehensive guidance for integrating the FaultMaven frontend with the backend API. It covers authentication, API patterns, error handling, and real-time communication.

**⚠️ Important**: This document should be used in conjunction with the **OpenAPI specification** (`docs/api/openapi.json` or `docs/api/openapi.yaml`) which is the authoritative source for:
- Available endpoints and their exact paths
- Request/response schemas and data types
- Authentication requirements
- Error response formats

## API Architecture

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

// API client setup
export const faultMavenApi = axios.create(API_CONFIG);
```

### Authentication & Headers

```typescript
// Request interceptor for authentication
faultMavenApi.interceptors.request.use((config) => {
  const token = localStorage.getItem('auth_token');
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  
  // Add session context
  const sessionId = useFaultMavenStore.getState().currentSession?.id;
  if (sessionId) {
    config.headers['X-Session-ID'] = sessionId;
  }
  
  return config;
});

// Response interceptor for error handling
faultMavenApi.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      // Handle authentication errors
      useFaultMavenStore.getState().actions.logout();
    }
    return Promise.reject(error);
  }
);
```

## Core API Endpoints

### 1. Agent Query Endpoint

```typescript
// Submit troubleshooting query
export const submitQuery = async (request: SubmitQueryRequest): Promise<AgentResponse> => {
  const response = await faultMavenApi.post('/api/v1/agent/query', request);
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
  
  const response = await faultMavenApi.post('/api/v1/agent/query', formData, {
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
- `POST /api/v1/agent/query` - Submit troubleshooting query (returns `AgentResponse`)

#### Case Management
- `GET /api/v1/agent/cases/{case_id}` - Get case details
- `GET /api/v1/agent/sessions/{session_id}/cases` - List cases for a session

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
