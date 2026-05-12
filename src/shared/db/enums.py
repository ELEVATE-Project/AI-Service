from __future__ import annotations

from enum import Enum


class KeyFormat(str, Enum):
    """Shape of the encrypted payload stored in tenant_keys.encrypted_payload."""

    API_KEY = "api_key"
    AWS_CREDENTIALS = "aws_credentials"
    ENDPOINT_PAIR = "endpoint_pair"


class Transport(str, Enum):
    """Which integration layer handled the upstream LLM call."""

    LITELLM = "litellm"
    DIRECT = "direct"


class Feature(str, Enum):
    """Request type executed against the upstream provider."""

    CHAT = "chat"
    STREAM = "stream"
    EMBED = "embed"
    TOOL = "tool"


class LedgerStatus(str, Enum):
    """Outcome of a ledger-recorded upstream call."""

    SUCCESS = "success"
    ERROR = "error"
    PARTIAL_RESPONSE = "partial_response"


class SecretBackendType(str, Enum):
    """Which secret store implementation to use for the entire deployment."""

    POSTGRES = "postgres"
    VAULT = "vault"
    AWS = "aws"
