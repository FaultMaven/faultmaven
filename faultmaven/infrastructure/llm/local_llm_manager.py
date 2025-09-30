"""
Local LLM Service Manager

This module handles the automatic startup and management of local LLM services
when FaultMaven is configured to use local LLM providers.
"""

import asyncio
import logging
import os
import subprocess
import time
from typing import Optional, Dict, Any

import aiohttp
from faultmaven.infrastructure.logging.config import get_logger

logger = get_logger(__name__)


class LocalLLMServiceManager:
    """Manages local LLM service lifecycle"""

    def __init__(self, base_url: str = "http://localhost:8080", script_path: Optional[str] = None):
        self.base_url = base_url
        self.script_path = script_path or self._get_default_script_path()
        self.max_startup_wait = 120  # 2 minutes max wait for service to start
        self.health_check_interval = 5  # Check health every 5 seconds

    def _get_default_script_path(self) -> str:
        """Get the default path to the local LLM service management script"""
        # Assume script is in the scripts directory relative to FaultMaven root
        current_dir = os.path.dirname(os.path.abspath(__file__))
        faultmaven_root = os.path.dirname(os.path.dirname(os.path.dirname(current_dir)))
        return os.path.join(faultmaven_root, "scripts", "local_llm_service.sh")

    async def is_service_running(self) -> bool:
        """Check if the local LLM service is running and responding"""
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5.0)) as session:
                # Try different health endpoints that llama.cpp server might have
                health_endpoints = ["/health", "/v1/models", "/"]

                for endpoint in health_endpoints:
                    try:
                        async with session.get(f"{self.base_url}{endpoint}") as response:
                            if response.status == 200:
                                logger.debug(f"Local LLM service health check passed on {endpoint}")
                                return True
                    except Exception as e:
                        logger.debug(f"Health check failed on {endpoint}: {e}")
                        continue

                return False

        except Exception as e:
            logger.debug(f"Service health check failed: {e}")
            return False

    def _run_script_command(self, command: str, model_name: Optional[str] = None) -> subprocess.CompletedProcess:
        """Run a command using the local LLM service script"""
        if not os.path.exists(self.script_path):
            raise FileNotFoundError(f"Local LLM service script not found: {self.script_path}")

        if not os.access(self.script_path, os.X_OK):
            raise PermissionError(f"Local LLM service script is not executable: {self.script_path}")

        cmd = [self.script_path, command]
        if model_name:
            cmd.append(model_name)

        logger.info(f"Running command: {' '.join(cmd)}")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120  # 120 second timeout for script commands (container startup can be slow)
        )

        if result.returncode != 0:
            logger.error(f"Script command failed: {result.stderr}")
            raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)

        return result

    async def start_service(self, model_name: str) -> bool:
        """Start the local LLM service with the specified model"""
        try:
            # Check if already running
            if await self.is_service_running():
                logger.info("Local LLM service is already running")
                return True

            logger.info(f"Starting local LLM service with model: {model_name}")

            # Start the service using the script
            result = self._run_script_command("start", model_name)
            logger.info(f"Service start script output: {result.stdout}")

            # Wait for the service to become healthy
            start_time = time.time()
            while time.time() - start_time < self.max_startup_wait:
                if await self.is_service_running():
                    logger.info("Local LLM service started successfully")
                    return True

                logger.debug("Waiting for local LLM service to start...")
                await asyncio.sleep(self.health_check_interval)

            logger.error("Local LLM service failed to start within timeout period")
            return False

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to start local LLM service: {e.stderr}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error starting local LLM service: {e}")
            return False

    def stop_service(self) -> bool:
        """Stop the local LLM service"""
        try:
            logger.info("Stopping local LLM service")
            result = self._run_script_command("stop")
            logger.info(f"Service stop script output: {result.stdout}")
            return True

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to stop local LLM service: {e.stderr}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error stopping local LLM service: {e}")
            return False

    def get_service_status(self) -> Dict[str, Any]:
        """Get the current status of the local LLM service"""
        try:
            result = self._run_script_command("status")
            return {
                "script_available": True,
                "script_output": result.stdout,
                "script_path": self.script_path
            }

        except subprocess.CalledProcessError as e:
            return {
                "script_available": True,
                "script_error": e.stderr,
                "script_path": self.script_path,
                "exit_code": e.returncode
            }
        except FileNotFoundError:
            return {
                "script_available": False,
                "error": f"Script not found: {self.script_path}"
            }
        except Exception as e:
            return {
                "script_available": False,
                "error": str(e)
            }

    def check_and_fix_service(self) -> bool:
        """Check model consistency and restart with correct model if needed"""
        try:
            logger.info("Checking and fixing local LLM service model consistency...")
            result = self._run_script_command("check")
            logger.info(f"Service check completed: {result.stdout}")

            # Verify the fix actually worked by checking if warnings are gone
            status_result = self._run_script_command("status")
            if "[WARNING] Model mismatch detected" in status_result.stdout:
                logger.error("❌ Model mismatch still exists after fix attempt")
                return False
            else:
                logger.info("✅ Model consistency verified after fix")
                return True

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to check/fix service: {e.stderr}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error checking service: {e}")
            return False

    async def ensure_service_running(self, model_name: str) -> bool:
        """Ensure the local LLM service is running with the correct model"""
        # First, check if a service is running
        if await self.is_service_running():
            # Use the script's built-in check command to verify and fix model consistency
            logger.info("Local LLM service detected, verifying model consistency...")
            if self.check_and_fix_service():
                logger.info("✅ Local LLM service is running with correct model")
                return True
            else:
                logger.warning("⚠️ Failed to ensure correct model is running")
                return False
        else:
            # No service running, start with the specified model
            logger.info("Local LLM service is not running, starting with correct model")
            return await self.start_service(model_name)

    async def health_check(self) -> Dict[str, Any]:
        """Comprehensive health check of the local LLM service"""
        health_info = {
            "service_running": False,
            "base_url": self.base_url,
            "script_path": self.script_path,
            "timestamp": time.time()
        }

        # Check if service is running
        health_info["service_running"] = await self.is_service_running()

        # Get script status
        script_status = self.get_service_status()
        health_info.update(script_status)

        # Try to get service info if running
        if health_info["service_running"]:
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5.0)) as session:
                    async with session.get(f"{self.base_url}/v1/models") as response:
                        if response.status == 200:
                            models_info = await response.json()
                            health_info["models"] = models_info
            except Exception as e:
                health_info["model_info_error"] = str(e)

        return health_info


# Global instance for easy access
_local_llm_manager: Optional[LocalLLMServiceManager] = None


def get_local_llm_manager(base_url: str = "http://localhost:8080") -> LocalLLMServiceManager:
    """Get the global LocalLLMServiceManager instance"""
    global _local_llm_manager
    if _local_llm_manager is None:
        _local_llm_manager = LocalLLMServiceManager(base_url)
    return _local_llm_manager


async def check_and_start_local_llm_service(
    provider_name: str,
    base_url: str,
    model_name: str
) -> bool:
    """
    Check if local LLM service is needed and start it if necessary.

    Args:
        provider_name: The LLM provider name (should be "local")
        base_url: The base URL for the local service
        model_name: The model name to use

    Returns:
        bool: True if service is running or successfully started, False otherwise
    """
    if provider_name != "local":
        return True  # Not a local provider, no action needed

    manager = get_local_llm_manager(base_url)

    try:
        # Ensure service is running with the correct model (includes verification and auto-restart)
        success = await manager.ensure_service_running(model_name)

        return success

    except Exception as e:
        logger.error(f"Error checking/starting local LLM service: {e}")
        return False