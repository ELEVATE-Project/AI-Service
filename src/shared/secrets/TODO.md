# shared/secrets — BYOK key storage

Used by both `llm_service` (to load provider API keys) and `voice` (to load Bhashini/Sarvam/Google keys).
The same backend instance serves both modules.

## Files to create

- `base.py` — abstract `SecretBackend`:
  ```python
  async def get_key(tenant_id: str, provider: str) -> str: ...
  async def set_key(tenant_id: str, provider: str, key: str) -> None: ...
  async def delete_key(tenant_id: str, provider: str) -> None: ...
  async def list_keys(tenant_id: str) -> list[str]: ...  # returns masked values
  ```

- `postgres.py` — `PostgresEncryptedBackend` (default for all environments):
  - Keys stored AES-256-GCM encrypted in `shared.db` → `tenant_keys` table
  - **Dev**: master key from OS keyring via `keyring` lib (macOS Keychain / Linux Secret Service) — no key ever touches a file
  - **Prod**: master key injected from cloud KMS (Vault or AWS KMS) via `config.py`

- `vault.py` — `VaultBackend` stub (HashiCorp Vault, for on-prem govt deployments)

- `aws_secrets.py` — `AWSSecretsManagerBackend` stub (for AWS-hosted deployments)

- `cli.py` — `keys` CLI (exposed as `uv run llm-service keys <cmd>`):
  - `init` — generate dev master key, write to OS keyring
  - `set --tenant=<t> --provider=<p> --key=<k>` — encrypt + store in dev DB
  - `list --tenant=<t>` — print masked key list

## Hard invariants

- Missing key → `422 missing_tenant_key`
- Key present but upstream rejects it → `502 tenant_key_rejected` (surface upstream error verbatim)
- **Never** fall back to a Gritworks-default key

## Note on voice providers

For Bhashini/Sarvam: the open question (per `docs/architecture.md`) is whether these are per-tenant BYOK
or shared Gritworks-negotiated keys. Resolve before Phase 4. This backend supports both models.

## Phase

Phase 1
