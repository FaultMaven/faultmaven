"""
Mock servers for external API integration testing.

This module provides mock implementations of external services that FaultMaven
integrates with, including LLM APIs and Web Search APIs.
"""

import asyncio
import random
import socket
import time
from typing import Any, Dict, List

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse


def get_free_port() -> int:
    """Get a free port for server binding."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        s.listen(1)
        port = s.getsockname()[1]
    return port


class MockLLMServer:
    """Mock server for LLM API providers (Fireworks, OpenRouter, Ollama)."""

    def __init__(self, port: int = None):
        self.port = port or get_free_port()
        self.app = FastAPI(title="Mock LLM Server")
        self.server = None
        self.server_task = None
        self.setup_routes()

        # Mock response templates
        self.response_templates = {
            "troubleshooting_response": """Based on the error logs provided, I can see this is a database connection timeout issue. Here's my analysis:

**Root Cause**: The connection pool is likely exhausted or the database server is experiencing high load.

**Immediate Actions**:
1. Check database server health and resource usage
2. Verify connection pool configuration
3. Look for connection leaks in the application code

**Recommendations**:
1. Increase connection pool size temporarily
2. Implement connection retry logic with exponential backoff
3. Add connection pool monitoring and alerting
4. Review long-running queries that might be holding connections

**Confidence**: 85% - This is a common pattern I've seen in similar systems.""",
            "hypothesis_response": """Based on the symptoms, I've identified several potential hypotheses:

**Hypothesis 1**: Database connection pool exhaustion
- Evidence: Connection timeout errors, retry attempts visible in logs
- Probability: High (80%)
- Next steps: Check pool size configuration and current usage

**Hypothesis 2**: Database server resource constraints
- Evidence: Consistent timeout pattern, no authentication errors
- Probability: Medium (60%)
- Next steps: Monitor database server CPU, memory, and disk I/O

**Hypothesis 3**: Network connectivity issues
- Evidence: Timeout rather than connection refused errors
- Probability: Medium (50%)
- Next steps: Test network connectivity between app and database servers""",
        }

    def setup_routes(self):
        """Setup mock API routes."""

        @self.app.post("/chat/completions")
        async def chat_completions(request: Dict[str, Any]):
            """Mock OpenAI-compatible chat completions endpoint."""
            messages = request.get("messages", [])
            model = request.get("model", "unknown")
            _ = request.get("max_tokens", 1000)  # Not used in mock

            # Simulate processing time
            await asyncio.sleep(0.1)

            # Generate response based on prompt content
            user_message = ""
            for msg in messages:
                if msg.get("role") == "user":
                    user_message = msg.get("content", "")
                    break

            # Choose appropriate response template
            if (
                "hypothesis" in user_message.lower()
                or "formulate" in user_message.lower()
            ):
                content = self.response_templates["hypothesis_response"]
            else:
                content = self.response_templates["troubleshooting_response"]

            # Add some variation to responses
            if random.random() < 0.3:  # 30% chance of adding context
                content += (
                    f"\n\n**Additional Context**: This analysis is "
                    f"based on the {model} model's understanding of similar issues."
                )

            return JSONResponse(
                {
                    "id": f"chatcmpl-{int(time.time())}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": content},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": len(user_message.split()),
                        "completion_tokens": len(content.split()),
                        "total_tokens": len(user_message.split())
                        + len(content.split()),
                    },
                }
            )

        @self.app.post("/api/generate")
        async def ollama_generate(request: Dict[str, Any]):
            """Mock Ollama generate endpoint."""
            prompt = request.get("prompt", "")
            model = request.get("model", "llama2")

            # Simulate processing time
            await asyncio.sleep(0.2)

            # Generate response
            if "hypothesis" in prompt.lower():
                response = self.response_templates["hypothesis_response"]
            else:
                response = self.response_templates["troubleshooting_response"]

            return JSONResponse(
                {
                    "model": model,
                    "created_at": f"{int(time.time())}",
                    "response": response,
                    "done": True,
                    "context": [1, 2, 3, 4, 5],
                    "total_duration": 200000000,
                    "load_duration": 100000000,
                    "prompt_eval_count": len(prompt.split()),
                    "prompt_eval_duration": 50000000,
                    "eval_count": len(response.split()),
                    "eval_duration": 150000000,
                }
            )

        @self.app.get("/health")
        async def health():
            """Health check endpoint."""
            return {"status": "healthy", "service": "mock_llm", "port": self.port}

    async def start(self):
        """Start the mock server."""
        try:
            config = uvicorn.Config(
                app=self.app, host="127.0.0.1", port=self.port, log_level="error"
            )
            self.server = uvicorn.Server(config)

            # Start server in a task to avoid blocking
            self.server_task = asyncio.create_task(self.server.serve())

            # Wait a bit for server to start
            await asyncio.sleep(0.5)

            # Verify server started
            await self._verify_server_started()

        except Exception as e:
            raise RuntimeError(f"Failed to start LLM server on port {self.port}: {e}")

    async def _verify_server_started(self):
        """Verify the server started successfully."""
        for attempt in range(15):  # Try for 7.5 seconds (increased from 5)
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    response = await client.get(f"http://127.0.0.1:{self.port}/health")
                    if response.status_code == 200:
                        return
            except (httpx.ReadTimeout, httpx.ConnectError):
                pass
            await asyncio.sleep(0.5)
        raise RuntimeError(
            f"LLM server failed to start on port {self.port} after 15 attempts"
        )

    async def stop(self):
        """Stop the mock server."""
        if not self.server:
            return

        try:
            # Signal server to stop
            self.server.should_exit = True

            # If there's a server task, wait for it to complete gracefully
            if self.server_task and not self.server_task.done():
                # Give the server a moment to shut down gracefully
                try:
                    await asyncio.wait_for(self.server_task, timeout=2.0)
                except asyncio.TimeoutError:
                    # If it doesn't stop gracefully, cancel it
                    self.server_task.cancel()
                    try:
                        await self.server_task
                    except asyncio.CancelledError:
                        pass
                except asyncio.CancelledError:
                    # Task was already cancelled
                    pass

        except Exception as e:
            # Log but don't fail shutdown
            print(f"Warning: Error during LLM server shutdown: {e}")
        finally:
            self.server = None
            self.server_task = None


class MockWebSearchServer:
    """Mock server for Web Search API providers (Google Custom Search, Tavily)."""

    def __init__(self, port: int = None):
        self.port = port or get_free_port()
        self.app = FastAPI(title="Mock Web Search Server")
        self.server = None
        self.server_task = None
        self.setup_routes()

        # Mock search results database
        self.search_results = {
            "database connection": [
                {
                    "title": "Troubleshooting Database Connection Issues - Stack Overflow",
                    "link": "https://stackoverflow.com/questions/database-connection-timeout",
                    "snippet": "Common causes of database connection timeouts include connection pool exhaustion, network issues, and database server overload. Here are the most effective solutions...",
                },
                {
                    "title": "Connection Pool Management Best Practices - AWS Docs",
                    "link": "https://docs.aws.amazon.com/rds/latest/userguide/USER_ConnLimit.html",
                    "snippet": "To avoid connection pool exhaustion, configure your connection pool size based on your application's concurrent workload and database server capacity...",
                },
                {
                    "title": "Database Connection Troubleshooting Guide - MongoDB",
                    "link": "https://docs.mongodb.com/manual/faq/diagnostics/",
                    "snippet": "When experiencing connection timeouts, check network connectivity, authentication settings, and server resource utilization. Enable connection logging for detailed diagnostics...",
                },
            ],
            "connection timeout": [
                {
                    "title": "Fixing Connection Timeouts - PostgreSQL Wiki",
                    "link": "https://wiki.postgresql.org/wiki/Timeout_Issues",
                    "snippet": "Connection timeouts often indicate network issues, server overload, or misconfigured connection parameters. Check your connection string settings and server logs...",
                },
                {
                    "title": "MySQL Connection Timeout Solutions - MySQL Docs",
                    "link": "https://dev.mysql.com/doc/refman/8.0/en/server-system-variables.html#sysvar_connect_timeout",
                    "snippet": "The connect_timeout variable controls how long the server waits for a connection packet before timing out. Increase this value if you're experiencing frequent timeouts...",
                },
            ],
            "default": [
                {
                    "title": "General Troubleshooting Guide - Stack Overflow",
                    "link": "https://stackoverflow.com/questions/general-troubleshooting",
                    "snippet": "When facing technical issues, start with basic diagnostics: check logs, verify configurations, and isolate the problem area. Document your findings for future reference...",
                },
                {
                    "title": "Best Practices for System Monitoring - DevOps Guide",
                    "link": "https://devops.com/monitoring-best-practices/",
                    "snippet": "Implement comprehensive monitoring to catch issues early. Set up alerts for key metrics and maintain dashboards for real-time visibility into system health...",
                },
            ],
        }

    def setup_routes(self):
        """Setup mock API routes."""

        @self.app.get("/customsearch/v1")
        async def google_custom_search(
            key: str = "", cx: str = "", q: str = "", num: int = 10
        ):
            """Mock Google Custom Search API."""
            if not key or not cx:
                raise HTTPException(
                    status_code=400, detail="Missing required parameters"
                )

            # Simulate API delay
            await asyncio.sleep(0.1)

            # Find relevant results
            results = self._find_search_results(q, num)

            return JSONResponse(
                {
                    "kind": "customsearch#search",
                    "url": {
                        "type": "application/json",
                        "template": "https://www.googleapis.com/customsearch/v1?q={searchTerms}&num={count?}&start={startIndex?}&lr={language?}&safe={safe?}&cx={cx?}&sort={sort?}&filter={filter?}&gl={gl?}&cr={cr?}&googlehost={googleHost?}&c2coff={disableCnTwTranslation?}&hq={hq?}&hl={hl?}&siteSearch={siteSearch?}&siteSearchFilter={siteSearchFilter?}&exactTerms={exactTerms?}&excludeTerms={excludeTerms?}&linkSite={linkSite?}&orTerms={orTerms?}&relatedSite={relatedSite?}&dateRestrict={dateRestrict?}&lowRange={lowRange?}&highRange={highRange?}&searchType={searchType}&fileType={fileType?}&rights={rights?}&imgSize={imgSize?}&imgType={imgType?}&imgColorType={imgColorType?}&imgDominantColor={imgDominantColor?}&alt=json",
                    },
                    "queries": {
                        "request": [
                            {
                                "title": "Google Custom Search",
                                "totalResults": str(len(results)),
                                "searchTerms": q,
                                "count": len(results),
                                "startIndex": 1,
                                "inputEncoding": "utf8",
                                "outputEncoding": "utf8",
                                "safe": "off",
                                "cx": cx,
                            }
                        ]
                    },
                    "context": {"title": "FaultMaven Search"},
                    "searchInformation": {
                        "searchTime": 0.123456,
                        "formattedSearchTime": "0.12",
                        "totalResults": str(len(results)),
                        "formattedTotalResults": f"{len(results):,}",
                    },
                    "items": results,
                }
            )

        @self.app.post("/search")
        async def tavily_search(request: Dict[str, Any]):
            """Mock Tavily search API."""
            query = request.get("query", "")
            max_results = request.get("max_results", 5)

            # Simulate API delay
            await asyncio.sleep(0.1)

            # Find relevant results
            results = self._find_search_results(query, max_results)

            return JSONResponse(
                {
                    "query": query,
                    "follow_up_questions": [
                        "What are the most common causes of this issue?",
                        "How can I prevent this from happening again?",
                        "What monitoring should I implement?",
                    ],
                    "answer": "Based on the search results, this appears to be a database connection issue that can be resolved by checking connection pool configuration and database server health.",
                    "results": [
                        {
                            "title": result["title"],
                            "url": result["link"],
                            "content": result["snippet"],
                            "score": 0.95 - (i * 0.1),
                        }
                        for i, result in enumerate(results)
                    ],
                }
            )

        @self.app.get("/health")
        async def health():
            """Health check endpoint."""
            return {
                "status": "healthy",
                "service": "mock_web_search",
                "port": self.port,
            }

    def _find_search_results(
        self, query: str, max_results: int
    ) -> List[Dict[str, Any]]:
        """Find relevant search results based on query."""
        query_lower = query.lower()

        # Check for specific keywords
        if "database" in query_lower and "connection" in query_lower:
            results = self.search_results["database connection"]
        elif "connection" in query_lower and "timeout" in query_lower:
            results = self.search_results["connection timeout"]
        else:
            results = self.search_results["default"]

        # Limit results and add mock formatting
        limited_results = results[:max_results]

        # Format for Google Custom Search API
        formatted_results = []
        for i, result in enumerate(limited_results):
            formatted_results.append(
                {
                    "kind": "customsearch#result",
                    "title": result["title"],
                    "htmlTitle": result["title"],
                    "link": result["link"],
                    "displayLink": result["link"].split("//")[1].split("/")[0],
                    "snippet": result["snippet"],
                    "htmlSnippet": result["snippet"],
                    "cacheId": f"cache_{i}",
                    "formattedUrl": result["link"],
                    "htmlFormattedUrl": result["link"],
                }
            )

        return formatted_results

    async def start(self):
        """Start the mock server."""
        try:
            config = uvicorn.Config(
                app=self.app, host="127.0.0.1", port=self.port, log_level="error"
            )
            self.server = uvicorn.Server(config)

            # Start server in a task to avoid blocking
            self.server_task = asyncio.create_task(self.server.serve())

            # Wait a bit for server to start
            await asyncio.sleep(0.5)

            # Verify server started
            await self._verify_server_started()

        except Exception as e:
            raise RuntimeError(
                f"Failed to start Web Search server on port {self.port}: {e}"
            )

    async def _verify_server_started(self):
        """Verify the server started successfully."""
        for attempt in range(15):  # Try for 7.5 seconds (increased from 5)
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    response = await client.get(f"http://127.0.0.1:{self.port}/health")
                    if response.status_code == 200:
                        return
            except (httpx.ReadTimeout, httpx.ConnectError):
                pass
            await asyncio.sleep(0.5)
        raise RuntimeError(
            f"Web Search server failed to start on port {self.port} after 15 attempts"
        )

    async def stop(self):
        """Stop the mock server."""
        if not self.server:
            return

        try:
            # Signal server to stop
            self.server.should_exit = True

            # If there's a server task, wait for it to complete gracefully
            if self.server_task and not self.server_task.done():
                # Give the server a moment to shut down gracefully
                try:
                    await asyncio.wait_for(self.server_task, timeout=2.0)
                except asyncio.TimeoutError:
                    # If it doesn't stop gracefully, cancel it
                    self.server_task.cancel()
                    try:
                        await self.server_task
                    except asyncio.CancelledError:
                        pass
                except asyncio.CancelledError:
                    # Task was already cancelled
                    pass

        except Exception as e:
            # Log but don't fail shutdown
            print(f"Warning: Error during Web Search server shutdown: {e}")
        finally:
            self.server = None
            self.server_task = None


class MockServerManager:
    """Manages lifecycle of all mock servers."""

    def __init__(self):
        self.llm_server = MockLLMServer()
        self.web_search_server = MockWebSearchServer()
        self.servers = [self.llm_server, self.web_search_server]
        self.started = False

    async def start_all(self):
        """Start all mock servers."""
        if self.started:
            return

        try:
            # Start servers concurrently
            start_tasks = [server.start() for server in self.servers]
            await asyncio.gather(*start_tasks)

            self.started = True

        except Exception as e:
            # Clean up on failure
            await self.stop_all()
            raise RuntimeError(f"Failed to start mock servers: {e}")

    async def stop_all(self):
        """Stop all mock servers."""
        if not self.started:
            return

        try:
            # Stop servers one by one to avoid racing conditions
            for server in self.servers:
                try:
                    await server.stop()
                except Exception as e:
                    print(f"Warning: Error stopping server: {e}")

            # Give a small grace period for any remaining cleanup
            await asyncio.sleep(0.1)

        except Exception as e:
            print(f"Warning: Error during mock server cleanup: {e}")
        finally:
            self.started = False

    def get_llm_base_url(self) -> str:
        """Get base URL for mock LLM server."""
        return f"http://127.0.0.1:{self.llm_server.port}"

    def get_web_search_base_url(self) -> str:
        """Get base URL for mock web search server."""
        return f"http://127.0.0.1:{self.web_search_server.port}"

    def get_ports(self) -> Dict[str, int]:
        """Get port information for all servers."""
        return {
            "llm": self.llm_server.port,
            "web_search": self.web_search_server.port,
        }


# Global instance for use in tests
mock_server_manager = MockServerManager()
