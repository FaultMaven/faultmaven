"""Shared builders for the container provider tests.

``make_chroma_settings`` is THE settings builder for ChromaDB client-path
tests. It pins every field the dispatch and auth code reads, explicitly and
per call — ambient env (a developer's exported ``CHROMADB_URL``,
``CHROMADB_AUTH_TOKEN`` or ``SKIP_SERVICE_CHECKS``) must not decide which
branch a test exercises. It lives here because the per-file copies had
already drifted on which fields they pinned; a new must-pin field gets added
in exactly one place.
"""

from pydantic import SecretStr

from faultmaven.config.settings import DeploymentMode, FaultMavenSettings


def make_chroma_settings(
    *,
    cloud: bool = False,
    chromadb_url: str = "",
    chromadb_host: str = "localhost",
    vector_storage_type: str = "chromadb",
    auth_token: str | None = None,
    api_key: str | None = None,
    skip_service_checks: bool = False,
) -> FaultMavenSettings:
    settings = FaultMavenSettings(_env_file=None)
    settings.deployment_mode = (
        DeploymentMode.CLOUD if cloud else DeploymentMode.STANDALONE
    )
    settings.database.chromadb_url = chromadb_url
    settings.database.chromadb_host = chromadb_host
    settings.database.vector_storage_type = vector_storage_type
    settings.database.chromadb_auth_token = (
        SecretStr(auth_token) if auth_token else None
    )
    settings.database.chromadb_api_key = SecretStr(api_key) if api_key else None
    settings.server.skip_service_checks = skip_service_checks
    return settings
