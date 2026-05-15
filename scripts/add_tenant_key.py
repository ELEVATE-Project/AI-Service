#!/usr/bin/env python3
"""
scripts/add_tenant_key.py — interactively add tenants, calling service access, and provider keys.

Run: uv run python scripts/add_tenant_key.py

Prompts for everything. No flags needed.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import sys
import uuid
from pathlib import Path

import asyncpg
import keyring
from cryptography.fernet import Fernet

_KEYRING_SERVICE = "ai-service"
_KEYRING_KEY = "master-key"

# Key format inferred per provider. Anything not listed prompts the user to choose.
_PROVIDER_FORMAT: dict[str, str] = {
    "openai":          "api_key",
    "anthropic":       "api_key",
    "groq":            "api_key",
    "bedrock":         "aws_credentials",
    "custom_endpoint": "endpoint_pair",
}


# ── low-level helpers ─────────────────────────────────────────────────────────

def _load_env(path: str = ".env") -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env


def _get_fernet() -> Fernet:
    existing = keyring.get_password(_KEYRING_SERVICE, _KEYRING_KEY)
    if existing:
        return Fernet(existing.encode())
    new_key = Fernet.generate_key().decode()
    keyring.set_password(_KEYRING_SERVICE, _KEYRING_KEY, new_key)
    print("  ✓ Master key generated and saved to OS keyring.")
    return Fernet(new_key.encode())


def _asyncpg_dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://")


# ── prompt helpers ────────────────────────────────────────────────────────────

def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or default


def _ask_yn(prompt: str) -> bool:
    while True:
        raw = input(f"{prompt} (y/n): ").strip().lower()
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False


# ── key data collection ───────────────────────────────────────────────────────

def _collect_key_data(provider: str) -> tuple[str, dict[str, str]]:
    key_format = _PROVIDER_FORMAT.get(provider)

    if key_format is None:
        print("  Key format options: api_key | aws_credentials | endpoint_pair")
        key_format = _ask("  Key format")
        if key_format not in ("api_key", "aws_credentials", "endpoint_pair"):
            print(f"  Unknown format {key_format!r}. Defaulting to api_key.")
            key_format = "api_key"

    if key_format == "api_key":
        data: dict[str, str] = {"api_key": _ask("  API key")}
        if provider == "custom_endpoint":
            data["api_base"] = _ask("  API base URL")
        return key_format, data

    if key_format == "aws_credentials":
        return key_format, {
            "access_key_id":     _ask("  AWS Access Key ID"),
            "secret_access_key": _ask("  AWS Secret Access Key"),
            "region":            _ask("  AWS Region", default="us-east-1"),
        }

    if key_format == "endpoint_pair":
        return key_format, {
            "endpoint_url": _ask("  Endpoint URL"),
            "token":        _ask("  Token (enter 'none' if no auth required)"),
        }

    raise ValueError(f"Unhandled key format: {key_format}")


# ── calling service flow ──────────────────────────────────────────────────────

async def _handle_calling_services(conn: asyncpg.Connection, tenant_id: str) -> None:
    grant = _ask_yn(f"\nGrant a calling service access to '{tenant_id}'?")
    while grant:
        print()
        service_name = _ask("  Service name (press Enter to create new)", default="")

        if service_name:
            existing = await conn.fetchrow(
                "SELECT id FROM calling_services WHERE name = $1", service_name,
            )
        else:
            existing = None

        if existing:
            service_id: uuid.UUID = existing["id"]
            print(f"  → Existing service found (id={service_id})")
        else:
            if not service_name:
                service_name = _ask("  New service name")
            raw_token = f"svc_{secrets.token_hex(24)}"
            token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
            service_id = uuid.uuid4()
            await conn.execute(
                "INSERT INTO calling_services (id, name, bearer_token_hash) VALUES ($1, $2, $3)",
                service_id, service_name, token_hash,
            )
            print(f"\n  !! Save this token now — it will not be shown again:")
            print(f"     {raw_token}\n")

        await conn.execute(
            """
            INSERT INTO calling_service_tenants (calling_service_id, tenant_id)
            VALUES ($1, $2)
            ON CONFLICT (calling_service_id, tenant_id) DO NOTHING
            """,
            service_id, tenant_id,
        )
        print(f"  ✓ Access granted: {service_name} → {tenant_id}")

        grant = _ask_yn(f"\nGrant another calling service access to '{tenant_id}'?")


# ── main flow ─────────────────────────────────────────────────────────────────

async def _run(db_url: str) -> None:
    fernet = _get_fernet()
    conn = await asyncpg.connect(_asyncpg_dsn(db_url))

    try:
        while True:
            print()
            tenant_id = _ask("Tenant ID")
            tenant_name = _ask("Tenant display name", default=tenant_id)

            await conn.execute(
                """
                INSERT INTO tenants (id, name) VALUES ($1, $2)
                ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name
                """,
                tenant_id, tenant_name,
            )
            print(f"  ✓ Tenant '{tenant_id}' ready.")

            await _handle_calling_services(conn, tenant_id)

            add_key = _ask_yn(f"\nAdd a provider key for '{tenant_id}'?")
            while add_key:
                print()
                provider = _ask("  Provider (openai / anthropic / bedrock / groq / custom_endpoint)")
                key_format, data = _collect_key_data(provider)
                encrypted = fernet.encrypt(json.dumps(data).encode()).decode()
                await conn.execute(
                    """
                    INSERT INTO tenant_keys (id, tenant_id, provider, key_format, encrypted_payload)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT ON CONSTRAINT uq_tenant_provider
                    DO UPDATE SET key_format = EXCLUDED.key_format,
                                  encrypted_payload = EXCLUDED.encrypted_payload
                    """,
                    uuid.uuid4(), tenant_id, provider, key_format, encrypted,
                )
                print(f"  ✓ Key ({provider} / {key_format}) written.")
                add_key = _ask_yn(f"\nAdd another key for '{tenant_id}'?")

            if not _ask_yn("\nAdd another tenant?"):
                break
    finally:
        await conn.close()

    print("\nDone.")


def main() -> None:
    env = _load_env()
    db_url = env.get("DB_URL") or os.environ.get("DB_URL", "")
    if not db_url:
        raise SystemExit("DB_URL not found in .env")

    try:
        asyncio.run(_run(db_url))
    except KeyboardInterrupt:
        print("\nAborted.")
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        print("Make sure Postgres is running and DB_URL is set in .env.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()