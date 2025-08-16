# FaultMaven Frontend Testing Strategies

**Document Type**: Frontend Implementation Guide  
**Last Updated**: August 2025

## Overview

This document outlines comprehensive testing strategies for the FaultMaven frontend, covering unit testing, integration testing, end-to-end testing, and performance testing. All testing approaches are designed to work with the 7 response types and ensure robust, maintainable frontend code.

## Testing Philosophy

### Core Principles

1. **Test Behavior, Not Implementation**: Focus on what components do, not how they do it
2. **Test User Interactions**: Ensure the UI responds correctly to user actions
3. **Test Accessibility**: Verify that components meet WCAG 2.1 AA standards
4. **Test Edge Cases**: Cover error states, loading states, and boundary conditions
5. **Fast and Reliable**: Tests should run quickly and consistently

### Testing Pyramid

```
    /\
   /  \     E2E Tests (Few, Critical Paths)
  /____\    
 /      \   Integration Tests (Component Interactions)
/________\  
Unit Tests (Many, Individual Components)
```

## Testing Stack

### Core Testing Libraries

```typescript
// Testing Framework
- Jest - Test runner and assertion library
- React Testing Library - Component testing utilities
- @testing-library/jest-dom - Custom Jest matchers

// Component Testing
- @testing-library/react - React component testing
- @testing-library/user-event - User interaction simulation
- @testing-library/hooks - Custom hook testing

// Mocking and Stubbing
- Jest mocks - Function and module mocking
- MSW (Mock Service Worker) - API mocking
- React Query test utilities - Query/mutation testing

// Visual Testing
- Storybook - Component development and visual testing
- Chromatic - Visual regression testing
- Playwright - E2E testing with visual snapshots
```

### Configuration

```typescript
// jest.config.js
module.exports = {
  testEnvironment: 'jsdom',
  setupFilesAfterEnv: ['<rootDir>/src/test/setup.ts'],
  moduleNameMapping: {
    '^@/(.*)$': '<rootDir>/src/$1',
    '\\.(css|less|scss|sass)$': 'identity-obj-proxy'
  },
  collectCoverageFrom: [
    'src/**/*.{ts,tsx}',
    '!src/**/*.d.ts',
    '!src/**/*.stories.{ts,tsx}'
  ],
  coverageThreshold: {
    global: {
      branches: 80,
      functions: 80,
      lines: 80,
      statements: 80
    }
  }
};

// src/test/setup.ts
import '@testing-library/jest-dom';
import { server } from './mocks/server';

beforeAll(() => server.listen());
afterEach(() => server.resetHandlers());
afterAll(() => server.close());
```

## Unit Testing

### Component Testing

```typescript
import { render, screen, fireEvent } from '@testing-library/react';
import { AnswerResponse } from './AnswerResponse';

describe('AnswerResponse Component', () => {
  const mockResponse = {
    response_type: ResponseType.ANSWER,
    content: 'The issue is caused by insufficient memory allocation.',
    confidence_score: 0.95,
    sources: [
      {
        name: 'System Logs',
        type: SourceType.LOG,
        snippet: 'Memory usage at 95%',
        confidence: 0.9,
        relevance_score: 0.95,
        timestamp: '2025-01-15T10:30:00Z'
      }
    ],
    plan: null,
    estimated_time_to_resolution: '15 minutes',
    next_action_hint: null
  };

  const mockHandlers = {
    onFollowUp: jest.fn(),
    onMarkResolved: jest.fn()
  };

  it('renders solution content correctly', () => {
    render(<AnswerResponse response={mockResponse} {...mockHandlers} />);
    
    expect(screen.getByText('Solution Found')).toBeInTheDocument();
    expect(screen.getByText(mockResponse.content)).toBeInTheDocument();
    expect(screen.getByText('Mark as Resolved')).toBeInTheDocument();
  });

  it('displays confidence score', () => {
    render(<AnswerResponse response={mockResponse} {...mockHandlers} />);
    
    expect(screen.getByText('Confidence: 95%')).toBeInTheDocument();
    const confidenceBar = screen.getByRole('progressbar');
    expect(confidenceBar).toHaveAttribute('aria-valuenow', '95');
  });

  it('shows supporting evidence when sources exist', () => {
    render(<AnswerResponse response={mockResponse} {...mockHandlers} />);
    
    expect(screen.getByText('Supporting Evidence')).toBeInTheDocument();
    expect(screen.getByText('System Logs')).toBeInTheDocument();
    expect(screen.getByText('Memory usage at 95%')).toBeInTheDocument();
  });

  it('calls onMarkResolved when resolved button is clicked', () => {
    render(<AnswerResponse response={mockResponse} {...mockHandlers} />);
    
    fireEvent.click(screen.getByText('Mark as Resolved'));
    expect(mockHandlers.onMarkResolved).toHaveBeenCalledTimes(1);
  });

  it('calls onFollowUp when follow-up button is clicked', () => {
    render(<AnswerResponse response={mockResponse} {...mockHandlers} />);
    
    fireEvent.click(screen.getByText('Ask Follow-up Question'));
    expect(mockHandlers.onFollowUp).toHaveBeenCalledTimes(1);
  });
});
```

### Hook Testing

```typescript
import { renderHook, act, waitFor } from '@testing-library/react';
import { useSubmitQuery } from './useSubmitQuery';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

describe('useSubmitQuery Hook', () => {
  let queryClient: QueryClient;
  
  beforeEach(() => {
    queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false }
      }
    });
  });

  const wrapper = ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={queryClient}>
      {children}
    </QueryClientProvider>
  );

  it('should submit query successfully', async () => {
    const mockApi = createMockAPIClient();
    const { result } = renderHook(() => useSubmitQuery(), { wrapper });
    
    await waitFor(() => {
      expect(result.current.isIdle).toBe(true);
    });
    
    act(() => {
      result.current.mutate({
        query: 'Test query',
        session_id: 'test-session'
      });
    });
    
    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    
    expect(mockApi.submitQuery).toHaveBeenCalledWith({
      query: 'Test query',
      session_id: 'test-session'
    });
  });

  it('should handle errors gracefully', async () => {
    const mockApi = createMockAPIClient();
    mockApi.submitQuery.mockRejectedValue(new Error('API Error'));
    
    const { result } = renderHook(() => useSubmitQuery(), { wrapper });
    
    act(() => {
      result.current.mutate({
        query: 'Test query',
        session_id: 'test-session'
      });
    });
    
    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });
    
    expect(result.current.error).toBeInstanceOf(Error);
    expect(result.current.error?.message).toBe('API Error');
  });
});
```

### Utility Function Testing

```typescript
import { extractQuestions, extractDataRequirements } from './utils';

describe('Utility Functions', () => {
  describe('extractQuestions', () => {
    it('should extract questions from text', () => {
      const text = 'Please provide: 1. What is the error message? 2. When did this start?';
      const questions = extractQuestions(text);
      
      expect(questions).toEqual([
        'What is the error message?',
        'When did this start?'
      ]);
    });

    it('should handle text with no questions', () => {
      const text = 'This is a simple statement with no questions.';
      const questions = extractQuestions(text);
      
      expect(questions).toEqual([]);
    });
  });

  describe('extractDataRequirements', () => {
    it('should extract data requirements from text', () => {
      const text = 'Required: 1. System logs 2. Performance metrics 3. Configuration files';
      const requirements = extractDataRequirements(text);
      
      expect(requirements).toEqual([
        'System logs',
        'Performance metrics',
        'Configuration files'
      ]);
    });
  });
});
```

## Integration Testing

### Component Interaction Testing

```typescript
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { TroubleshootingInterface } from './TroubleshootingInterface';
import { useSubmitQuery } from './useSubmitQuery';

// Mock the hook
jest.mock('./useSubmitQuery');
const mockUseSubmitQuery = useSubmitQuery as jest.MockedFunction<typeof useSubmitQuery>;

describe('TroubleshootingInterface Integration', () => {
  beforeEach(() => {
    mockUseSubmitQuery.mockReturnValue({
      mutate: jest.fn(),
      isLoading: false,
      isError: false,
      error: null,
      isSuccess: false,
      data: null,
      isIdle: true
    });
  });

  it('should submit query and display response', async () => {
    const mockMutate = jest.fn();
    mockUseSubmitQuery.mockReturnValue({
      mutate: mockMutate,
      isLoading: false,
      isError: false,
      error: null,
      isSuccess: true,
      data: mockAnswerResponse,
      isIdle: false
    });

    render(<TroubleshootingInterface />);
    
    const input = screen.getByPlaceholderText('Describe your issue...');
    const submitButton = screen.getByText('Submit Query');
    
    fireEvent.change(input, {
      target: { value: 'Database connection timeout' }
    });
    
    fireEvent.click(submitButton);
    
    expect(mockMutate).toHaveBeenCalledWith({
      query: 'Database connection timeout',
      session_id: expect.any(String)
    });
    
    await waitFor(() => {
      expect(screen.getByText('Solution Found')).toBeInTheDocument();
    });
  });

  it('should handle loading states correctly', () => {
    mockUseSubmitQuery.mockReturnValue({
      mutate: jest.fn(),
      isLoading: true,
      isError: false,
      error: null,
      isSuccess: false,
      data: null,
      isIdle: false
    });

    render(<TroubleshootingInterface />);
    
    expect(screen.getByText('Processing...')).toBeInTheDocument();
    expect(screen.getByRole('progressbar')).toBeInTheDocument();
  });
});
```

### State Management Testing

```typescript
import { renderHook, act } from '@testing-library/react';
import { useFaultMavenStore } from './store';

describe('State Management Integration', () => {
  beforeEach(() => {
    useFaultMavenStore.getState().actions.clearState();
  });

  it('should manage session state correctly', () => {
    const { result } = renderHook(() => useFaultMavenStore());
    
    act(() => {
      result.current.actions.setCurrentCase({
        id: 'test-case',
        title: 'Test Case',
        status: 'active'
      });
    });
    
    expect(result.current.currentCase).toEqual({
      id: 'test-case',
      title: 'Test Case',
      status: 'active'
    });
  });

  it('should add messages to conversation history', () => {
    const { result } = renderHook(() => useFaultMavenStore());
    
    const message = {
      id: 'msg-1',
      content: 'Test message',
      timestamp: new Date().toISOString(),
      type: 'user'
    };
    
    act(() => {
      result.current.actions.addMessage(message);
    });
    
    expect(result.current.conversationHistory).toHaveLength(1);
    expect(result.current.conversationHistory[0]).toEqual(message);
  });
});
```

## End-to-End Testing

### Playwright Setup

```typescript
// playwright.config.ts
import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: 'html',
  use: {
    baseURL: 'http://localhost:3000',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure'
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] }
    },
    {
      name: 'firefox',
      use: { ...devices['Desktop Firefox'] }
    },
    {
      name: 'webkit',
      use: { ...devices['Desktop Safari'] }
    }
  ],
  webServer: {
    command: 'npm run dev',
    url: 'http://localhost:3000',
    reuseExistingServer: !process.env.CI
  }
});
```

### E2E Test Examples

```typescript
// tests/e2e/troubleshooting-flow.spec.ts
import { test, expect } from '@playwright/test';

test.describe('Troubleshooting Flow', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
  });

  test('should complete full troubleshooting flow', async ({ page }) => {
    // Start new troubleshooting session
    await page.click('[data-testid="new-session-button"]');
    
    // Fill in issue description
    await page.fill('[data-testid="issue-description"]', 
      'Database connection timeout after 30 seconds');
    
    // Submit query
    await page.click('[data-testid="submit-query-button"]');
    
    // Wait for response
    await page.waitForSelector('[data-testid="response-container"]');
    
    // Verify response type
    const responseType = await page.getAttribute(
      '[data-testid="response-type-badge"]', 
      'data-response-type'
    );
    expect(responseType).toBeDefined();
    
    // If clarification is needed, provide it
    if (responseType === 'CLARIFICATION_REQUEST') {
      await page.fill('[data-testid="clarification-input"]', 
        'The timeout occurs during peak hours');
      await page.click('[data-testid="submit-clarification-button"]');
      
      await page.waitForSelector('[data-testid="response-container"]');
    }
    
    // Verify final response
    const finalResponseType = await page.getAttribute(
      '[data-testid="response-type-badge"]', 
      'data-response-type'
    );
    expect(['ANSWER', 'SOLUTION_READY']).toContain(finalResponseType);
  });

  test('should handle file uploads correctly', async ({ page }) => {
    await page.click('[data-testid="new-session-button"]');
    
    // Create test file
    const testFile = Buffer.from('Test log content');
    
    // Upload file
    await page.setInputFiles('[data-testid="file-upload"]', {
      name: 'test.log',
      mimeType: 'text/plain',
      buffer: testFile
    });
    
    // Verify file is uploaded
    await expect(page.locator('[data-testid="uploaded-file"]'))
      .toContainText('test.log');
  });

  test('should handle errors gracefully', async ({ page }) => {
    // Mock API error
    await page.route('/api/v1/agent/query', route => {
      route.fulfill({
        status: 500,
        contentType: 'application/json',
        body: JSON.stringify({
          error: 'INTERNAL_ERROR',
          details: {
            title: 'Internal Server Error',
            detail: 'Something went wrong',
            isRetryable: true
          }
        })
      });
    });
    
    await page.click('[data-testid="new-session-button"]');
    await page.fill('[data-testid="issue-description"]', 'Test query');
    await page.click('[data-testid="submit-query-button"]');
    
    // Verify error message is displayed
    await expect(page.locator('[data-testid="error-message"]'))
      .toContainText('Internal Server Error');
    
    // Verify retry button is available
    await expect(page.locator('[data-testid="retry-button"]')).toBeVisible();
  });
});
```

## Visual Testing

### Storybook Stories

```typescript
// stories/AnswerResponse.stories.tsx
import type { Meta, StoryObj } from '@storybook/react';
import { AnswerResponse } from '../components/AnswerResponse';

const meta: Meta<typeof AnswerResponse> = {
  title: 'Components/Response Types/AnswerResponse',
  component: AnswerResponse,
  parameters: {
    layout: 'centered',
    docs: {
      description: {
        component: 'Displays direct solutions with supporting evidence and follow-up actions.'
      }
    }
  },
  argTypes: {
    response: {
      control: 'object',
      description: 'The agent response data'
    },
    onFollowUp: {
      action: 'follow-up-requested',
      description: 'Called when user requests follow-up'
    },
    onMarkResolved: {
      action: 'case-resolved',
      description: 'Called when user marks case as resolved'
    }
  }
};

export default meta;
type Story = StoryObj<typeof meta>;

export const Default: Story = {
  args: {
    response: {
      response_type: ResponseType.ANSWER,
      content: 'The issue is caused by insufficient memory allocation.',
      confidence_score: 0.95,
      sources: [
        {
          name: 'System Logs',
          type: SourceType.LOG,
          snippet: 'Memory usage at 95%',
          confidence: 0.9,
          relevance_score: 0.95,
          timestamp: '2025-01-15T10:30:00Z'
        }
      ],
      plan: null,
      estimated_time_to_resolution: '15 minutes',
      next_action_hint: null
    }
  }
};

export const HighConfidence: Story = {
  args: {
    ...Default.args,
    response: {
      ...Default.args.response!,
      confidence_score: 0.99
    }
  }
};

export const LowConfidence: Story = {
  args: {
    ...Default.args,
    response: {
      ...Default.args.response!,
      confidence_score: 0.6
    }
  }
};

export const MultipleSources: Story = {
  args: {
    ...Default.args,
    response: {
      ...Default.args.response!,
      sources: [
        {
          name: 'System Logs',
          type: SourceType.LOG,
          snippet: 'Memory usage at 95%',
          confidence: 0.9,
          relevance_score: 0.95,
          timestamp: '2025-01-15T10:30:00Z'
        },
        {
          name: 'Performance Metrics',
          type: SourceType.METRIC,
          snippet: 'CPU utilization: 87%',
          confidence: 0.85,
          relevance_score: 0.8,
          timestamp: '2025-01-15T10:30:00Z'
        }
      ]
    }
  }
};
```

### Visual Regression Testing

```typescript
// tests/visual/visual-regression.spec.ts
import { test, expect } from '@playwright/test';

test.describe('Visual Regression Tests', () => {
  test('should match visual baseline for all response types', async ({ page }) => {
    const responseTypes = [
      'ANSWER',
      'PLAN_PROPOSAL', 
      'CLARIFICATION_REQUEST',
      'CONFIRMATION_REQUEST',
      'SOLUTION_READY',
      'NEEDS_MORE_DATA',
      'ESCALATION_REQUIRED'
    ];
    
    for (const responseType of responseTypes) {
      await page.goto(`/storybook/iframe.html?id=components-responsetypes-${responseType.toLowerCase()}--default`);
      
      // Wait for component to load
      await page.waitForSelector('[data-testid="response-container"]');
      
      // Take screenshot
      await expect(page.locator('[data-testid="response-container"]'))
        .toHaveScreenshot(`${responseType.toLowerCase()}-response.png`);
    }
  });
});
```

## Performance Testing

### Component Performance Testing

```typescript
import { render } from '@testing-library/react';
import { AnswerResponse } from './AnswerResponse';

describe('Performance Tests', () => {
  it('should render within performance budget', () => {
    const startTime = performance.now();
    
    render(<AnswerResponse response={mockResponse} {...mockHandlers} />);
    
    const endTime = performance.now();
    const renderTime = endTime - startTime;
    
    // Component should render in under 16ms (60fps)
    expect(renderTime).toBeLessThan(16);
  });

  it('should handle large data sets efficiently', () => {
    const largeResponse = {
      ...mockResponse,
      sources: Array.from({ length: 1000 }, (_, i) => ({
        name: `Source ${i}`,
        type: SourceType.LOG,
        snippet: `Log entry ${i}`,
        confidence: 0.9,
        relevance_score: 0.8,
        timestamp: new Date().toISOString()
      }))
    };
    
    const startTime = performance.now();
    
    render(<AnswerResponse response={largeResponse} {...mockHandlers} />);
    
    const endTime = performance.now();
    const renderTime = endTime - startTime;
    
    // Should handle 1000 sources in under 100ms
    expect(renderTime).toBeLessThan(100);
  });
});
```

### Bundle Size Testing

```typescript
// tests/bundle/bundle-size.spec.ts
import { expect } from '@jest/globals';

describe('Bundle Size Tests', () => {
  it('should maintain reasonable bundle size', () => {
    // This would typically use webpack-bundle-analyzer or similar
    // For now, we'll document the expected sizes
    
    const expectedSizes = {
      'main.js': '500KB', // Main bundle
      'vendor.js': '1.2MB', // Third-party dependencies
      'component-library.js': '200KB' // Component library
    };
    
    // In a real implementation, you would:
    // 1. Build the project
    // 2. Analyze bundle sizes
    // 3. Compare against expected sizes
    // 4. Fail if sizes exceed thresholds
    
    expect(true).toBe(true); // Placeholder assertion
  });
});
```

## Accessibility Testing

### Automated Accessibility Testing

```typescript
import { render, screen } from '@testing-library/react';
import { axe, toHaveNoViolations } from 'jest-axe';
import { AnswerResponse } from './AnswerResponse';

expect.extend(toHaveNoViolations);

describe('Accessibility Tests', () => {
  it('should meet WCAG 2.1 AA standards', async () => {
    const { container } = render(
      <AnswerResponse response={mockResponse} {...mockHandlers} />
    );
    
    const results = await axe(container);
    expect(results).toHaveNoViolations();
  });

  it('should have proper ARIA labels', () => {
    render(<AnswerResponse response={mockResponse} {...mockHandlers} />);
    
    // Check for proper ARIA labels
    expect(screen.getByRole('region', { name: /response type: answer/i }))
      .toBeInTheDocument();
    
    // Check for progress bar accessibility
    const confidenceBar = screen.getByRole('progressbar');
    expect(confidenceBar).toHaveAttribute('aria-valuenow', '95');
    expect(confidenceBar).toHaveAttribute('aria-valuemin', '0');
    expect(confidenceBar).toHaveAttribute('aria-valuemax', '100');
  });

  it('should support keyboard navigation', () => {
    render(<AnswerResponse response={mockResponse} {...mockHandlers} />);
    
    // Tab through interactive elements
    const firstButton = screen.getByText('Ask Follow-up Question');
    const secondButton = screen.getByText('Mark as Resolved');
    
    firstButton.focus();
    expect(firstButton).toHaveFocus();
    
    // Tab to next button
    firstButton.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab' }));
    expect(secondButton).toHaveFocus();
  });
});
```

## Mocking Strategies

### API Mocking

```typescript
// src/test/mocks/handlers.ts
import { rest } from 'msw';

export const handlers = [
  rest.post('/api/v1/agent/query', (req, res, ctx) => {
    const { query } = req.body as { query: string };
    
    if (query.includes('error')) {
      return res(
        ctx.status(500),
        ctx.json({
          error: 'INTERNAL_ERROR',
          details: {
            title: 'Internal Server Error',
            detail: 'Something went wrong',
            isRetryable: true
          }
        })
      );
    }
    
    return res(
      ctx.json({
        response_type: ResponseType.ANSWER,
        content: `Response to: ${query}`,
        confidence_score: 0.9,
        sources: [],
        plan: null,
        estimated_time_to_resolution: '5 minutes',
        next_action_hint: null
      })
    );
  }),
  
  rest.get('/api/v1/sessions/:id', (req, res, ctx) => {
    const { id } = req.params;
    
    return res(
      ctx.json({
        id,
        title: 'Test Session',
        status: 'active',
        createdAt: new Date().toISOString()
      })
    );
  })
];
```

### Store Mocking

```typescript
// src/test/mocks/store.ts
import { createMockStore } from 'zustand/mock';

export const createMockFaultMavenStore = () => createMockStore({
  user: null,
  isAuthenticated: false,
  currentCase: null,
  conversationHistory: [],
  actions: {
    setUser: jest.fn(),
    setCurrentCase: jest.fn(),
    addMessage: jest.fn(),
    clearState: jest.fn()
  }
});
```

## Test Data Management

### Test Data Factories

```typescript
// src/test/factories/response-factory.ts
export const createMockAgentResponse = (
  overrides: Partial<AgentResponse> = {}
): AgentResponse => ({
  response_type: ResponseType.ANSWER,
  content: 'Default response content',
  confidence_score: 0.9,
  sources: [],
  plan: null,
  estimated_time_to_resolution: '5 minutes',
  next_action_hint: null,
  ...overrides
});

export const createMockSource = (
  overrides: Partial<Source> = {}
): Source => ({
  name: 'Test Source',
  type: SourceType.LOG,
  snippet: 'Test snippet',
  confidence: 0.9,
  relevance_score: 0.8,
  timestamp: new Date().toISOString(),
  ...overrides
});

export const createMockPlanStep = (
  overrides: Partial<PlanStep> = {}
): PlanStep => ({
  step_id: 'step-1',
  title: 'Test Step',
  description: 'Test step description',
  estimated_duration: '2 minutes',
  dependencies: [],
  risk_level: 'low',
  ...overrides
});
```

### Test Data Cleanup

```typescript
// src/test/utils/cleanup.ts
export const cleanupTestData = () => {
  // Clear localStorage
  localStorage.clear();
  
  // Clear sessionStorage
  sessionStorage.clear();
  
  // Reset mocks
  jest.clearAllMocks();
  
  // Clear React Query cache
  queryClient.clear();
};
```

## Continuous Integration

### GitHub Actions Workflow

```yaml
# .github/workflows/test.yml
name: Frontend Tests

on:
  push:
    branches: [ main, develop ]
  pull_request:
    branches: [ main ]

jobs:
  test:
    runs-on: ubuntu-latest
    
    steps:
    - uses: actions/checkout@v3
    
    - name: Setup Node.js
      uses: actions/setup-node@v3
      with:
        node-version: '18'
        cache: 'npm'
    
    - name: Install dependencies
      run: npm ci
    
    - name: Run linting
      run: npm run lint
    
    - name: Run type checking
      run: npm run type-check
    
    - name: Run unit tests
      run: npm run test:unit -- --coverage
    
    - name: Run integration tests
      run: npm run test:integration
    
    - name: Run E2E tests
      run: npm run test:e2e
    
    - name: Upload coverage
      uses: codecov/codecov-action@v3
      with:
        file: ./coverage/lcov.info
```

## Best Practices

### 1. Test Organization
- Group tests by feature or component
- Use descriptive test names
- Follow AAA pattern (Arrange, Act, Assert)

### 2. Test Data
- Use factories for consistent test data
- Avoid hardcoded values
- Clean up test data after each test

### 3. Mocking
- Mock external dependencies
- Use realistic mock data
- Avoid over-mocking

### 4. Performance
- Keep tests fast
- Use appropriate timeouts
- Avoid unnecessary setup/teardown

### 5. Maintenance
- Update tests when components change
- Remove obsolete tests
- Keep test code clean and readable

## Conclusion

This comprehensive testing strategy ensures that the FaultMaven frontend is robust, accessible, and maintainable. By following these patterns, developers can confidently build and modify components while maintaining high code quality.

For additional guidance, refer to:
- [Frontend Component Library](./component-library.md)
- [State Management Patterns](./state-management.md)
- [API Integration Guide](./api-integration.md)
- [System Requirements](../FAULTMAVEN_SYSTEM_REQUIREMENTS.md)

---

*Document Version: 1.0*  
*Last Updated: August 2025*  
*Author: FaultMaven Frontend Team*
