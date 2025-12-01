# FaultMaven Claude Code Subagents

This directory contains specialized subagent prompts (slash commands) for FaultMaven development tasks.

## Core Principles

**All subagents must follow [agent-principles.md](agent-principles.md):**

1. **Root Cause Resolution** — Fix the underlying issue, never bandaid symptoms
2. **Concise Commits** — Human author, no AI mention, imperative mood
3. **Verify First** — Read code and run tests before suggesting changes
4. **Minimal Changes** — Do exactly what's needed, nothing more
5. **Preserve Patterns** — Follow existing codebase conventions
6. **Security by Default** — Never introduce vulnerabilities
7. **Test Changes** — Run tests, add tests for new functionality
8. **Communicate Clearly** — Be direct, admit unknowns
9. **Respect Human Authority** — Present options, human decides
10. **Leave It Better** — Every change should improve quality

## Available Subagents

| Command | Subagent Role | Use Case |
|---------|---------------|----------|
| `/microservice-architect` | Microservice Architect | Design APIs, service boundaries, data models |
| `/test-engineer` | Test Engineer | Write unit tests, integration tests, mocks |
| `/security-auditor` | Security Auditor | Audit code for vulnerabilities, auth issues |
| `/llm-specialist` | LLM Integration Specialist | Work with OpenAI, Anthropic, prompt engineering |
| `/vectordb-expert` | Vector DB Expert | ChromaDB, embeddings, RAG optimization |
| `/api-docs-writer` | API Documentation Writer | OpenAPI specs, endpoint documentation |
| `/devops-engineer` | DevOps Engineer | Docker, CI/CD, deployment |
| `/code-reviewer` | Code Reviewer | Review PRs, code quality, best practices |

## How to Use

Invoke any subagent by typing its slash command followed by your request:

```
/test-engineer Write unit tests for the auth service login endpoint

/security-auditor Audit the JWT validation code in api-gateway

/llm-specialist Add support for Groq as an LLM provider

/vectordb-expert Optimize the knowledge base search for better relevance

/code-reviewer Review this PR for the new case status feature
```

## Subagent Details

### `/microservice-architect`
**Best for:**
- Designing new API endpoints
- Planning new microservices
- Refactoring service boundaries
- Database schema design

**Example prompts:**
- "Design an endpoint for bulk case updates"
- "Plan a notification service for case status changes"
- "How should we structure the webhook delivery system?"

---

### `/test-engineer`
**Best for:**
- Writing pytest unit tests
- Creating integration tests
- Setting up test fixtures and mocks
- Improving test coverage

**Example prompts:**
- "Write tests for the Case Service CRUD operations"
- "Create mocks for the LLM provider in Agent Service tests"
- "Add edge case tests for the document chunking function"

---

### `/security-auditor`
**Best for:**
- Security code reviews
- Identifying OWASP vulnerabilities
- Auth/authz audits
- Secrets management review

**Example prompts:**
- "Audit the auth service for security vulnerabilities"
- "Review the file upload endpoint for path traversal risks"
- "Check the JWT implementation for common weaknesses"

---

### `/llm-specialist`
**Best for:**
- Adding new LLM providers
- Optimizing prompts
- Implementing streaming
- Cost optimization strategies

**Example prompts:**
- "Add support for Google Gemini as a provider"
- "Optimize the troubleshooting system prompt"
- "Implement token counting and context window management"

---

### `/vectordb-expert`
**Best for:**
- ChromaDB configuration
- Embedding model selection
- Search relevance tuning
- RAG pipeline optimization

**Example prompts:**
- "Improve chunking strategy for markdown documents"
- "Implement hybrid search (vector + keyword)"
- "Add metadata filtering to knowledge base search"

---

### `/api-docs-writer`
**Best for:**
- Writing endpoint documentation
- Adding Pydantic field descriptions
- Creating API examples
- Documenting error responses

**Example prompts:**
- "Document the new bulk upload endpoint"
- "Add examples to the API.md for all case endpoints"
- "Update Pydantic models with proper Field descriptions"

---

### `/devops-engineer`
**Best for:**
- Docker and Docker Compose issues
- CI/CD pipeline setup
- Deployment configuration
- Infrastructure troubleshooting

**Example prompts:**
- "Add health checks to all service Dockerfiles"
- "Create a GitHub Actions workflow for running tests"
- "Configure Nginx reverse proxy for production"

---

### `/code-reviewer`
**Best for:**
- PR reviews
- Code quality feedback
- Best practices enforcement
- Refactoring suggestions

**Example prompts:**
- "Review the changes in the knowledge service"
- "Check this new endpoint for FastAPI best practices"
- "Review error handling in the agent service"

## Creating New Subagents

To add a new subagent:

1. Create a new `.md` file in `.claude/commands/`
2. Follow the existing format:
   - Role definition
   - Expertise areas
   - FaultMaven-specific context
   - Task guidelines
   - Code examples
   - End with `$ARGUMENTS`

3. The filename becomes the command: `my-agent.md` → `/my-agent`

## Tips for Effective Use

1. **Be specific**: Include file names, service names, or PR numbers
2. **Provide context**: Mention what you've already tried
3. **Chain subagents**: Use output from one as input to another
4. **Iterate**: Ask follow-up questions for refinement

## Example Workflow

```
# 1. Design the feature
/microservice-architect Design an endpoint for exporting case history as PDF

# 2. Implement and get security review
/security-auditor Review the new PDF export endpoint for vulnerabilities

# 3. Write tests
/test-engineer Write tests for the PDF export functionality

# 4. Document the API
/api-docs-writer Document the PDF export endpoint with examples

# 5. Get code review before merging
/code-reviewer Review all changes for the PDF export feature
```
