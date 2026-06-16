"""``llm-service`` command-line entry point.

Exposes BYOK key management non-interactively (the interactive equivalent is
``scripts/add_tenant_key.py``). Wired up as the ``llm-service`` console script
in pyproject.toml (``[project.scripts]``).

    uv run llm-service keys init
    uv run llm-service keys set --tenant=<id> --provider=<name> \
        --format=api_key --data='{"api_key": "sk-..."}'

This module is loaded by the console script as ``shared.secrets.cli`` (the
editable install puts ``src/`` on the path), but the rest of the codebase is
imported as ``src.shared.*`` and console scripts do not put the project root on
sys.path — so ensure it is present before importing anything under ``src``.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import click  # noqa: E402

_KEYRING_SERVICE = "ai-service"
_KEYRING_KEY = "master-key"

_VALID_FORMATS = ("api_key", "aws_credentials", "endpoint_pair")


@click.group()
def main() -> None:
    """ai-service administration CLI."""


@main.group()
def keys() -> None:
    """Manage encrypted BYOK tenant keys."""


@keys.command("init")
def keys_init() -> None:
    """Generate the Fernet master key and store it in the OS keyring (idempotent)."""
    import keyring
    from cryptography.fernet import Fernet

    existing = keyring.get_password(_KEYRING_SERVICE, _KEYRING_KEY)
    if existing:
        click.echo("Master key already present in OS keyring — nothing to do.")
        return
    new_key = Fernet.generate_key().decode()
    keyring.set_password(_KEYRING_SERVICE, _KEYRING_KEY, new_key)
    click.echo("✓ Master key generated and saved to OS keyring.")


@keys.command("set")
@click.option("--tenant", "tenant", required=True, help="Tenant ID the key belongs to.")
@click.option("--provider", required=True, help="Provider name, e.g. openrouter, anthropic, bedrock.")
@click.option(
    "--format", "key_format", required=True, type=click.Choice(_VALID_FORMATS),
    help="Shape of the credential payload.",
)
@click.option("--data", required=True, help="JSON object with the credential fields.")
def keys_set(tenant: str, provider: str, key_format: str, data: str) -> None:
    """Encrypt and upsert the key for (tenant, provider)."""
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"--data is not valid JSON: {exc}")
    if not isinstance(parsed, dict):
        raise click.ClickException("--data must be a JSON object, e.g. '{\"api_key\": \"sk-...\"}'")

    from src.shared.db import AsyncSessionLocal
    from src.shared.db.enums import KeyFormat
    from src.shared.secrets.postgres_encrypted import PostgresEncryptedBackend

    async def _run() -> None:
        async with AsyncSessionLocal() as session:
            backend = PostgresEncryptedBackend(session)
            await backend.set_key(tenant, provider, KeyFormat(key_format), parsed)

    try:
        asyncio.run(_run())
    except Exception as exc:  # surface a clean message instead of a traceback
        raise click.ClickException(str(exc))
    click.echo(f"✓ Key ({provider} / {key_format}) written for tenant {tenant!r}.")


if __name__ == "__main__":
    main()
