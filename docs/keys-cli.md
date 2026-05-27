# Keys & Secrets

The gateway uses BYOK (Bring Your Own Key) — every upstream LLM call uses the tenant's own API key, never a shared platform key. This page explains how those keys are stored and how to manage them.

---

## How keys are stored

When you store a key like `sk-ant-api03-...`, here's what actually happens:

```
sk-ant-api03-...
    ↓  JSON serialise into typed payload
{"api_key": "sk-ant-api03-..."}
    ↓  Fernet encrypt (using master key from OS keyring)
gAAAAABq...  (encrypted blob)
    ↓  saved to tenant_keys table in Postgres
```

The master key lives in your OS keyring (macOS Keychain on Mac, Secret Service on Linux). It never touches the database or any file. Production deployments swap the keyring for HashiCorp Vault or AWS Secrets Manager — the application code is identical.

---

## The setup script

`scripts/add_tenant_key.py` handles everything interactively: creating tenants, registering calling services, and storing encrypted provider keys.

```bash
uv run python scripts/add_tenant_key.py
```

It reads `DB_URL` from your `.env` and uses the master key already in your OS keyring (created during local setup).

---

## Key formats

Three formats are supported, selected automatically per provider:

| Format | Providers | Payload shape |
|--------|-----------|---------------|
| `api_key` | OpenAI, Anthropic, Groq, custom endpoints | `{"api_key": "sk-..."}` |
| `aws_credentials` | Bedrock | `{"access_key_id": "...", "secret_access_key": "...", "region": "us-east-1"}` |
| `endpoint_pair` | Self-hosted / HuggingFace endpoints | `{"endpoint_url": "https://...", "token": "hf_..."}` |

The format is inferred from the provider name. For any provider not in the built-in map, the script asks you to pick one.

---

## Adding an Anthropic key

```
Provider: anthropic
  API key: sk-ant-api03-...your-key...

✓ Key (anthropic / api_key) written.
```

---

## Adding a Bedrock key

```
Provider: bedrock
  AWS Access Key ID: <your-aws-access-key-id>
  AWS Secret Access Key: <your-aws-secret-access-key>
  AWS Region [us-east-1]: us-west-2
  AWS Session Token (press Enter to skip): [Enter]
  AWS Role Name (press Enter to skip): [Enter]
  S3 bucket name for batch inference (press Enter to skip): [Enter]
  IAM Role ARN for batch inference (press Enter to skip): [Enter]

✓ Key (bedrock / aws_credentials) written.
```

You can skip all the optional fields (session token, role name, S3 bucket, IAM role ARN) for regular chat and streaming. They are only needed for [Batch API](batch-api.md) requests.

---

## Adding an OpenAI key

```
Provider: openai
  API key: sk-proj-...your-key...

✓ Key (openai / api_key) written.
```

---

## Adding multiple providers for one tenant

Run the script once. After the first key is written, answer `y` to "Add another key for this tenant?" to add a second provider. The script loops until you answer `n`.

---

## Key rotation

Run the script again for the same tenant and provider. The upsert (`ON CONFLICT ON CONSTRAINT uq_tenant_provider DO UPDATE`) replaces the existing encrypted payload. The old key stops being used immediately on the next request.

---

## How keys are loaded at request time

The secret backend (`src/shared/secrets/postgres_encrypted.py`) runs this on every request:

1. Query `tenant_keys` for `(tenant_id, provider)`.
2. If no row → raise `MissingTenantKeyError` → handler returns `422 missing_tenant_key`.
3. Decrypt the payload with Fernet using the master key from the OS keyring.
4. Return `TenantKeyPayload(key_format, data)` to the transport adapter.

The key is decrypted fresh per call. It is never cached in memory between requests.

---

## One key per (tenant, provider)

The `tenant_keys` table has a unique constraint on `(tenant_id, provider)`. Each tenant has at most one key per provider at a time. Storing a new key for the same provider replaces the old one.

---

See [Policy Engine](policy.md) for how per-tenant usage limits are enforced on top of BYOK.
