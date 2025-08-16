# FaultMaven Frontend State Management

**Document Type**: Frontend Implementation Guide  
**Last Updated**: August 2025

## Overview

This document covers state management patterns and strategies for the FaultMaven frontend, focusing on Zustand for global state management and React Query for server state. The state management system is designed to work seamlessly with the 7 response types and provide a smooth user experience.

## State Management Architecture

### Design Principles

1. **Single Source of Truth**: Centralized state management with clear data flow
2. **Predictable State Changes**: Immutable updates with clear action patterns
3. **Performance Optimized**: Minimal re-renders and efficient state updates
4. **Developer Experience**: Type-safe state with clear interfaces
5. **Scalability**: Modular stores that can grow with the application

### State Categories

```typescript
// Global Application State (Zustand)
- User authentication and preferences
- Application settings and configuration
- Global UI state (modals, notifications, theme)

// Server State (React Query)
- API responses and caching
- Real-time data updates
- Background synchronization

// Local Component State (React useState/useReducer)
- Form inputs and validation
- Component-specific UI state
- Temporary user interactions
```

## Zustand Store Architecture

### Core Store Structure

```typescript
import { create } from 'zustand';
import { devtools, persist } from 'zustand/middleware';

interface FaultMavenState {
  // User and Authentication
  user: User | null;
  isAuthenticated: boolean;
  userPreferences: UserPreferences;
  
  // Application State
  theme: 'light' | 'dark';
  sidebarCollapsed: boolean;
  notifications: Notification[];
  
  // Troubleshooting State
  currentCase: Case | null;
  conversationHistory: Message[];
  activeResponseType: ResponseType | null;
  
  // UI State
  modals: ModalState;
  loadingStates: LoadingStates;
  errors: ErrorState;
  
  // Actions
  actions: {
    setUser: (user: User) => void;
    updatePreferences: (preferences: Partial<UserPreferences>) => void;
    setTheme: (theme: 'light' | 'dark') => void;
    addNotification: (notification: Notification) => void;
    setCurrentCase: (case: Case | null) => void;
    addMessage: (message: Message) => void;
    setActiveResponseType: (type: ResponseType | null) => void;
    openModal: (modal: ModalType, props?: any) => void;
    closeModal: (modal: ModalType) => void;
    setLoading: (key: string, loading: boolean) => void;
    setError: (key: string, error: Error | null) => void;
  };
}

export const useFaultMavenStore = create<FaultMavenState>()(
  devtools(
    persist(
      (set, get) => ({
        // Initial State
        user: null,
        isAuthenticated: false,
        userPreferences: {
          language: 'en',
          timezone: 'UTC',
          notifications: {
            email: true,
            push: false,
            inApp: true
          }
        },
        
        theme: 'light',
        sidebarCollapsed: false,
        notifications: [],
        
        currentCase: null,
        conversationHistory: [],
        activeResponseType: null,
        
        modals: {
          settings: { isOpen: false, props: {} },
          caseDetails: { isOpen: false, props: {} },
          escalation: { isOpen: false, props: {} }
        },
        
        loadingStates: {
          authentication: false,
          caseLoading: false,
          messageSending: false
        },
        
        errors: {
          authentication: null,
          caseLoading: null,
          messageSending: null
        },
        
        // Actions
        actions: {
          setUser: (user) => set({ user, isAuthenticated: !!user }),
          
          updatePreferences: (preferences) => set((state) => ({
            userPreferences: { ...state.userPreferences, ...preferences }
          })),
          
          setTheme: (theme) => set({ theme }),
          
          addNotification: (notification) => set((state) => ({
            notifications: [...state.notifications, notification]
          })),
          
          setCurrentCase: (case) => set({ currentCase: case }),
          
          addMessage: (message) => set((state) => ({
            conversationHistory: [...state.conversationHistory, message]
          })),
          
          setActiveResponseType: (type) => set({ activeResponseType: type }),
          
          openModal: (modalType, props = {}) => set((state) => ({
            modals: {
              ...state.modals,
              [modalType]: { isOpen: true, props }
            }
          })),
          
          closeModal: (modalType) => set((state) => ({
            modals: {
              ...state.modals,
              [modalType]: { isOpen: false, props: {} }
            }
          })),
          
          setLoading: (key, loading) => set((state) => ({
            loadingStates: {
              ...state.loadingStates,
              [key]: loading
            }
          })),
          
          setError: (key, error) => set((state) => ({
            errors: {
              ...state.errors,
              [key]: error
            }
          }))
        }
      }),
      {
        name: 'faultmaven-storage',
        partialize: (state) => ({
          user: state.user,
          userPreferences: state.userPreferences,
          theme: state.theme,
          sidebarCollapsed: state.sidebarCollapsed
        })
      }
    )
  )
);
```

### Store Composition Pattern

```typescript
// Split stores by domain for better organization
interface TroubleshootingStore {
  currentCase: Case | null;
  conversationHistory: Message[];
  activeResponseType: ResponseType | null;
  caseStatus: CaseStatus;
  
  actions: {
    setCurrentCase: (case: Case | null) => void;
    addMessage: (message: Message) => void;
    updateCaseStatus: (status: CaseStatus) => void;
    clearConversation: () => void;
  };
}

export const useTroubleshootingStore = create<TroubleshootingStore>()(
  devtools(
    (set, get) => ({
      currentCase: null,
      conversationHistory: [],
      activeResponseType: null,
      caseStatus: 'idle',
      
      actions: {
        setCurrentCase: (case) => set({ currentCase: case }),
        
        addMessage: (message) => set((state) => ({
          conversationHistory: [...state.conversationHistory, message]
        })),
        
        updateCaseStatus: (status) => set({ caseStatus: status }),
        
        clearConversation: () => set({
          conversationHistory: [],
          activeResponseType: null,
          caseStatus: 'idle'
        })
      }
    })
  )
);

// UI Store for global UI state
interface UIStore {
  theme: 'light' | 'dark';
  sidebarCollapsed: boolean;
  modals: ModalState;
  notifications: Notification[];
  
  actions: {
    setTheme: (theme: 'light' | 'dark') => void;
    toggleSidebar: () => void;
    openModal: (modal: ModalType, props?: any) => void;
    closeModal: (modal: ModalType) => void;
    addNotification: (notification: Notification) => void;
    removeNotification: (id: string) => void;
  };
}

export const useUIStore = create<UIStore>()(
  devtools(
    persist(
      (set, get) => ({
        theme: 'light',
        sidebarCollapsed: false,
        modals: {
          settings: { isOpen: false, props: {} },
          caseDetails: { isOpen: false, props: {} },
          escalation: { isOpen: false, props: {} }
        },
        notifications: [],
        
        actions: {
          setTheme: (theme) => set({ theme }),
          
          toggleSidebar: () => set((state) => ({
            sidebarCollapsed: !state.sidebarCollapsed
          })),
          
          openModal: (modalType, props = {}) => set((state) => ({
            modals: {
              ...state.modals,
              [modalType]: { isOpen: true, props }
            }
          })),
          
          closeModal: (modalType) => set((state) => ({
            modals: {
              ...state.modals,
              [modalType]: { isOpen: false, props: {} }
            }
          })),
          
          addNotification: (notification) => set((state) => ({
            notifications: [...state.notifications, notification]
          })),
          
          removeNotification: (id) => set((state) => ({
            notifications: state.notifications.filter(n => n.id !== id)
          }))
        }
      }),
      {
        name: 'faultmaven-ui-storage',
        partialize: (state) => ({
          theme: state.theme,
          sidebarCollapsed: state.sidebarCollapsed
        })
      }
    )
  )
);
```

## React Query Integration

### Server State Management

```typescript
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { faultMavenApi } from '../api';

// Query Keys
export const queryKeys = {
  cases: ['cases'] as const,
  case: (id: string) => ['cases', id] as const,
  conversation: (caseId: string) => ['conversation', caseId] as const,
  user: ['user'] as const,
  notifications: ['notifications'] as const
};

// Case Management Queries
export const useCases = () => {
  return useQuery({
    queryKey: queryKeys.cases,
    queryFn: () => faultMavenApi.getCases(),
    staleTime: 5 * 60 * 1000, // 5 minutes
    gcTime: 10 * 60 * 1000 // 10 minutes
  });
};

export const useCase = (caseId: string) => {
  return useQuery({
    queryKey: queryKeys.case(caseId),
    queryFn: () => faultMavenApi.getCase(caseId),
    enabled: !!caseId,
    staleTime: 2 * 60 * 1000, // 2 minutes
    gcTime: 5 * 60 * 1000 // 5 minutes
  });
};

export const useConversation = (caseId: string) => {
  return useQuery({
    queryKey: queryKeys.conversation(caseId),
    queryFn: () => faultMavenApi.getConversation(caseId),
    enabled: !!caseId,
    staleTime: 1 * 60 * 1000, // 1 minute
    gcTime: 2 * 60 * 1000 // 2 minutes
  });
};

// Mutations
export const useSubmitQuery = () => {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: (query: SubmitQueryRequest) => faultMavenApi.submitQuery(query),
    onSuccess: (response, variables) => {
      // Update conversation cache
      queryClient.setQueryData(
        queryKeys.conversation(variables.caseId),
        (oldData: Conversation | undefined) => {
          if (!oldData) return oldData;
          return {
            ...oldData,
            messages: [...oldData.messages, response]
          };
        }
      );
      
      // Update case status if needed
      if (response.response_type === ResponseType.SOLUTION_READY) {
        queryClient.setQueryData(
          queryKeys.case(variables.caseId),
          (oldData: Case | undefined) => {
            if (!oldData) return oldData;
            return {
              ...oldData,
              status: 'solution_ready',
              lastUpdated: new Date().toISOString()
            };
          }
        );
      }
    },
    onError: (error) => {
      // Handle error (show notification, update UI state)
      console.error('Failed to submit query:', error);
    }
  });
};

export const useMarkCaseResolved = () => {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: (caseId: string) => faultMavenApi.markCaseResolved(caseId),
    onSuccess: (_, caseId) => {
      // Update case status
      queryClient.setQueryData(
        queryKeys.case(caseId),
        (oldData: Case | undefined) => {
          if (!oldData) return oldData;
          return {
            ...oldData,
            status: 'resolved',
            resolvedAt: new Date().toISOString()
          };
        }
      );
      
      // Remove from cases list or update status
      queryClient.setQueryData(
        queryKeys.cases,
        (oldData: Case[] | undefined) => {
          if (!oldData) return oldData;
          return oldData.map(case => 
            case.id === caseId 
              ? { ...case, status: 'resolved', resolvedAt: new Date().toISOString() }
              : case
          );
        }
      );
    }
  });
};
```

### Real-time Updates with WebSocket

```typescript
import { useEffect } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { queryKeys } from './queries';

export const useWebSocketUpdates = (caseId: string) => {
  const queryClient = useQueryClient();
  
  useEffect(() => {
    const ws = new WebSocket(`ws://localhost:8000/ws/case/${caseId}`);
    
    ws.onmessage = (event) => {
      const update = JSON.parse(event.data);
      
      switch (update.type) {
        case 'message_received':
          // Update conversation cache
          queryClient.setQueryData(
            queryKeys.conversation(caseId),
            (oldData: Conversation | undefined) => {
              if (!oldData) return oldData;
              return {
                ...oldData,
                messages: [...oldData.messages, update.message]
              };
            }
          );
          break;
          
        case 'case_status_updated':
          // Update case cache
          queryClient.setQueryData(
            queryKeys.case(caseId),
            (oldData: Case | undefined) => {
              if (!oldData) return oldData;
              return {
                ...oldData,
                status: update.status,
                lastUpdated: update.timestamp
              };
            }
          );
          break;
          
        case 'escalation_created':
          // Handle escalation updates
          queryClient.invalidateQueries({
            queryKey: queryKeys.case(caseId)
          });
          break;
      }
    };
    
    return () => ws.close();
  }, [caseId, queryClient]);
};
```

## State Management Patterns

### 1. Optimistic Updates

```typescript
export const useOptimisticCaseUpdate = () => {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: (update: CaseUpdate) => faultMavenApi.updateCase(update),
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
    },
    onSettled: (_, __, update) => {
      // Always refetch after error or success
      queryClient.invalidateQueries({
        queryKey: queryKeys.case(update.caseId)
      });
    }
  });
};
```

### 2. State Synchronization

```typescript
// Sync Zustand state with React Query
export const useSyncCaseState = (caseId: string) => {
  const { data: caseData } = useCase(caseId);
  const { setCurrentCase } = useTroubleshootingStore();
  
  useEffect(() => {
    if (caseData) {
      setCurrentCase(caseData);
    }
  }, [caseData, setCurrentCase]);
};

// Sync conversation state
export const useSyncConversationState = (caseId: string) => {
  const { data: conversationData } = useConversation(caseId);
  const { addMessage, clearConversation } = useTroubleshootingStore();
  
  useEffect(() => {
    if (conversationData) {
      // Clear existing conversation and add new messages
      clearConversation();
      conversationData.messages.forEach(message => {
        addMessage(message);
      });
    }
  }, [conversationData, addMessage, clearConversation]);
};
```

### 3. Computed State

```typescript
// Computed selectors for derived state
export const useCaseProgress = (caseId: string) => {
  const { data: caseData } = useCase(caseId);
  const { data: conversationData } = useConversation(caseId);
  
  return useMemo(() => {
    if (!caseData || !conversationData) return 0;
    
    // Calculate progress based on conversation length and case status
    const baseProgress = Math.min(conversationData.messages.length / 10, 0.8);
    
    switch (caseData.status) {
      case 'opened':
        return baseProgress * 0.3;
      case 'in_progress':
        return baseProgress * 0.6;
      case 'solution_ready':
        return 0.9;
      case 'resolved':
        return 1.0;
      default:
        return baseProgress;
    }
  }, [caseData, conversationData]);
};

export const useResponseTypeStats = (caseId: string) => {
  const { data: conversationData } = useConversation(caseId);
  
  return useMemo(() => {
    if (!conversationData) return {};
    
    return conversationData.messages.reduce((stats, message) => {
      const type = message.response_type;
      stats[type] = (stats[type] || 0) + 1;
      return stats;
    }, {} as Record<ResponseType, number>);
  }, [conversationData]);
};
```

## Performance Optimizations

### 1. Selective State Updates

```typescript
// Only update specific parts of state
export const useUpdateCaseStatus = () => {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: (update: { caseId: string; status: CaseStatus }) => 
      faultMavenApi.updateCaseStatus(update),
    onSuccess: (_, { caseId, status }) => {
      // Update only the status field
      queryClient.setQueryData(
        queryKeys.case(caseId),
        (oldData: Case | undefined) => {
          if (!oldData) return oldData;
          return { ...oldData, status };
        }
      );
    }
  });
};
```

### 2. Debounced Updates

```typescript
import { useDebouncedCallback } from 'use-debounce';

export const useDebouncedSearch = () => {
  const [searchTerm, setSearchTerm] = useState('');
  const queryClient = useQueryClient();
  
  const debouncedSearch = useDebouncedCallback((term: string) => {
    setSearchTerm(term);
    // Trigger search query
    queryClient.prefetchQuery({
      queryKey: [...queryKeys.cases, 'search', term],
      queryFn: () => faultMavenApi.searchCases(term)
    });
  }, 300);
  
  return { searchTerm, debouncedSearch };
};
```

### 3. State Persistence

```typescript
// Persist important state to localStorage
export const usePersistentState = <T>(
  key: string,
  initialValue: T
): [T, (value: T) => void] => {
  const [storedValue, setStoredValue] = useState<T>(() => {
    try {
      const item = window.localStorage.getItem(key);
      return item ? JSON.parse(item) : initialValue;
    } catch (error) {
      console.error(`Error reading localStorage key "${key}":`, error);
      return initialValue;
    }
  });
  
  const setValue = (value: T) => {
    try {
      setStoredValue(value);
      window.localStorage.setItem(key, JSON.stringify(value));
    } catch (error) {
      console.error(`Error setting localStorage key "${key}":`, error);
    }
  };
  
  return [storedValue, setValue];
};
```

## Error Handling and Recovery

### 1. Error Boundaries

```typescript
class StateErrorBoundary extends React.Component<
  { children: React.ReactNode },
  { hasError: boolean; error?: Error }
> {
  constructor(props: { children: React.ReactNode }) {
    super(props);
    this.state = { hasError: false };
  }
  
  static getDerivedStateFromError(error: Error) {
    return { hasError: true, error };
  }
  
  componentDidCatch(error: Error, errorInfo: React.ErrorInfo) {
    console.error('State Error Boundary caught an error:', error, errorInfo);
    
    // Log to error reporting service
    // Reset problematic state
    // Show user-friendly error message
  }
  
  render() {
    if (this.state.hasError) {
      return (
        <div className="error-boundary">
          <h2>Something went wrong with the application state.</h2>
          <button onClick={() => window.location.reload()}>
            Reload Application
          </button>
        </div>
      );
    }
    
    return this.props.children;
  }
}
```

### 2. State Recovery

```typescript
export const useStateRecovery = () => {
  const queryClient = useQueryClient();
  
  const recoverState = useCallback(async () => {
    try {
      // Clear all queries
      await queryClient.clear();
      
      // Re-fetch essential data
      await Promise.all([
        queryClient.prefetchQuery({
          queryKey: queryKeys.user,
          queryFn: () => faultMavenApi.getCurrentUser()
        }),
        queryClient.prefetchQuery({
          queryKey: queryKeys.cases,
          queryFn: () => faultMavenApi.getCases()
        })
      ]);
      
      // Reset Zustand stores to initial state
      useFaultMavenStore.getState().actions.setCurrentCase(null);
      useUIStore.getState().actions.closeModal('settings');
      
    } catch (error) {
      console.error('Failed to recover state:', error);
      // Show error message to user
    }
  }, [queryClient]);
  
  return { recoverState };
};
```

## Testing State Management

### 1. Store Testing

```typescript
import { renderHook, act } from '@testing-library/react';
import { useFaultMavenStore } from '../stores/faultMavenStore';

describe('FaultMaven Store', () => {
  beforeEach(() => {
    // Reset store to initial state
    act(() => {
      useFaultMavenStore.setState({
        user: null,
        isAuthenticated: false,
        currentCase: null,
        conversationHistory: []
      });
    });
  });
  
  it('should set user and authentication state', () => {
    const { result } = renderHook(() => useFaultMavenStore());
    
    act(() => {
      result.current.actions.setUser({ id: '1', name: 'Test User' });
    });
    
    expect(result.current.user).toEqual({ id: '1', name: 'Test User' });
    expect(result.current.isAuthenticated).toBe(true);
  });
  
  it('should add messages to conversation history', () => {
    const { result } = renderHook(() => useFaultMavenStore());
    const message = { id: '1', content: 'Test message', type: 'user' };
    
    act(() => {
      result.current.actions.addMessage(message);
    });
    
    expect(result.current.conversationHistory).toHaveLength(1);
    expect(result.current.conversationHistory[0]).toEqual(message);
  });
});
```

### 2. Query Testing

```typescript
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderHook, waitFor } from '@testing-library/react';
import { useCase } from '../hooks/queries';

const createTestQueryClient = () => new QueryClient({
  defaultOptions: {
    queries: { retry: false },
    mutations: { retry: false }
  }
});

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <QueryClientProvider client={createTestQueryClient()}>
    {children}
  </QueryClientProvider>
);

describe('Case Queries', () => {
  it('should fetch case data', async () => {
    const mockCase = { id: '1', title: 'Test Case', status: 'open' };
    
    // Mock API response
    jest.spyOn(faultMavenApi, 'getCase').mockResolvedValue(mockCase);
    
    const { result } = renderHook(() => useCase('1'), { wrapper });
    
    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    
    expect(result.current.data).toEqual(mockCase);
  });
});
```

## Best Practices

### 1. State Organization
- **Keep stores focused**: Split by domain (UI, authentication, troubleshooting)
- **Minimize cross-store dependencies**: Use composition over inheritance
- **Clear naming conventions**: Use descriptive names for actions and state

### 2. Performance
- **Selective updates**: Only update changed parts of state
- **Debounce user inputs**: Prevent excessive API calls
- **Optimistic updates**: Improve perceived performance
- **Efficient caching**: Use React Query's caching strategies

### 3. Error Handling
- **Graceful degradation**: Handle errors without breaking the UI
- **User feedback**: Show clear error messages
- **Recovery mechanisms**: Provide ways to recover from errors
- **Error boundaries**: Catch and handle unexpected errors

### 4. Testing
- **Test stores in isolation**: Mock dependencies and test store logic
- **Test queries separately**: Use React Query's testing utilities
- **Test error scenarios**: Ensure error handling works correctly
- **Integration testing**: Test state interactions between components

## Conclusion

This state management system provides a robust foundation for the FaultMaven frontend, ensuring predictable state updates, optimal performance, and excellent developer experience.

For additional guidance, refer to:
- [Component Library](../frontend/component-library.md)
- [API Integration Guide](../frontend/api-integration.md)
- [Frontend Testing Strategies](../frontend/testing-strategies.md)
- [System Requirements](../FAULTMAVEN_SYSTEM_REQUIREMENTS.md) - Frontend Design section

---

*Document Version: 1.0*  
*Last Updated: August 2025*  
*Author: FaultMaven Frontend Team*
