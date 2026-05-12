#!/usr/bin/env python3
"""
Dev setup script — creates all fixtures needed for local testing in one shot.
Idempotent: safe to re-run.

Run (one-liner):
    DEV_TOKEN=dev-secret-token-abc123 DEV_PROVIDER=openai DEV_API_KEY=sk-your-real-key python scripts/dev_setup.py

Required env vars:
    DEV_TOKEN      Bearer token to use in Authorization header  (e.g. dev-secret-token-abc123)
    DEV_PROVIDER   LLM provider                                 (e.g. openai)
    DEV_API_KEY    Upstream API key to encrypt and store        (e.g. sk-... real key for LLM calls, any string for auth-only testing)

Optional env vars:
    DEV_KEY_FORMAT  api_key (default) | endpoint_pair
                    Use api_key for OpenAI and Anthropic.
                    aws_credentials (Bedrock) requires a structured payload — extend this script manually for that case.

Also requires a .env file with DB_URL, REDIS_URL, LOG_LEVEL, PRICING_STALENESS_DAYS, SECRET_BACKEND.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from pathlib import Path

_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

import keyring
from cryptography.fernet import Fernet
from sqlalchemy.dialects.postgresql import insert

from src.shared.db import AsyncSessionLocal
from src.shared.db.enums import KeyFormat
from src.shared.db.models import CallingService, CallingServiceTenant, Tenant
from src.shared.secrets.postgres_encrypted import PostgresEncryptedBackend

_KEYRING_SERVICE = "ai-service"
_KEYRING_KEY = "master-key"
_TENANT_ID = "tenant_dev"
_SERVICE_NAME = "dev-service"


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"ERROR: {name} is required but not set.", file=sys.stderr)
        sys.exit(1)
    return value


def _setup_master_key() -> None:
    if keyring.get_password(_KEYRING_SERVICE, _KEYRING_KEY):
        print("  master key   : already in OS keyring")
        return
    key = Fernet.generate_key().decode()
    keyring.set_password(_KEYRING_SERVICE, _KEYRING_KEY, key)
    print("  master key   : generated and stored in OS keyring")


async def _setup_db(token_hash: str, provider: str, key_format: KeyFormat, api_key: str) -> None:
    async with AsyncSessionLocal() as db:
        # upsert tenant
        await db.execute(
            insert(Tenant)
            .values(id=_TENANT_ID, name="Dev Tenant")
            .on_conflict_do_nothing(index_elements=["id"])
        )
        print(f"  tenant       : {_TENANT_ID!r}")

        # upsert calling service — DO UPDATE so RETURNING always fires (even on conflict)
        result = await db.execute(
            insert(CallingService)
            .values(name=_SERVICE_NAME, bearer_token_hash=token_hash)
            .on_conflict_do_update(
                index_elements=["bearer_token_hash"],
                set_={"name": _SERVICE_NAME},
            )
            .returning(CallingService.id)
        )
        service_id = result.scalar_one()
        print(f"  service      : {_SERVICE_NAME!r} (id={service_id})")

        # upsert bridge row granting the service access to the tenant
        await db.execute(
            insert(CallingServiceTenant)
            .values(calling_service_id=service_id, tenant_id=_TENANT_ID)
            .on_conflict_do_nothing()
        )
        print(f"  access grant : {_SERVICE_NAME!r} -> {_TENANT_ID!r}")

        await db.commit()

        # encrypt and store the BYOK key (set_key commits internally)
        backend = PostgresEncryptedBackend(db)
        await backend.set_key(_TENANT_ID, provider, key_format, {"api_key": api_key})
        print(f"  byok key     : tenant={_TENANT_ID!r} provider={provider!r} format={key_format.value!r}")


def main() -> None:
    dev_token = _require_env("DEV_TOKEN")
    dev_provider = _require_env("DEV_PROVIDER")
    dev_api_key = _require_env("DEV_API_KEY")
    dev_key_format_raw = os.environ.get("DEV_KEY_FORMAT", KeyFormat.API_KEY.value).strip()

    try:
        dev_key_format = KeyFormat(dev_key_format_raw)
    except ValueError:
        valid = [kf.value for kf in KeyFormat]
        print(f"ERROR: DEV_KEY_FORMAT={dev_key_format_raw!r} is not valid. Choose from: {valid}", file=sys.stderr)
        sys.exit(1)

    if dev_key_format == KeyFormat.AWS_CREDENTIALS:
        print(
            "ERROR: DEV_KEY_FORMAT=aws_credentials requires access_key_id, secret_access_key, and region.\n"
            "       Extend this script manually for Bedrock credentials.",
            file=sys.stderr,
        )
        sys.exit(1)

    token_hash = hashlib.sha256(dev_token.encode()).hexdigest()

    print("Setting up dev fixtures...")
    _setup_master_key()

    try:
        asyncio.run(_setup_db(token_hash, dev_provider, dev_key_format, dev_api_key))
    except Exception as exc:
        print(f"\nERROR: DB setup failed: {exc}", file=sys.stderr)
        print("Make sure Postgres is running and DB_URL is set correctly.", file=sys.stderr)
        sys.exit(1)

    print(f"\nAll done. Test the auth + key pipeline:\n")
    print(
        f"curl -X POST http://localhost:8000/v1/chat \\\n"
        f'  -H "Authorization: Bearer {dev_token}" \\\n'
        f'  -H "X-Tenant-Id: {_TENANT_ID}" \\\n'
        f'  -H "Content-Type: application/json" \\\n'
        f'  -d \'{{"provider":"{dev_provider}","model":"gpt-4o-mini",'
        f'"messages":[{{"role":"user","content":"What is 2+2?"}}]}}\''
    )


if __name__ == "__main__":
    main()