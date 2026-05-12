# Keys CLI

The `llm-service` CLI manages two things: encrypted provider keys (BYOK) and calling service registrations. No raw keys are ever written to files or environment variables.

---

## How keys are stored

When you store a key like `sk-abc123`, here's what actually happens:

```
sk-abc123
    ↓  JSON serialise
{"api_key": "sk-abc123"}
    ↓  Fernet encrypt (using master key from OS keyring)
gAAAAABq...  (encrypted blob)
    ↓  saved to tenant_keys table in Postgres
```

The master key that locks everything lives in your OS keyring (macOS Keychain on Mac, Secret Service on Linux). It never touches the database or any file. Production deployments swap the keyring for HashiCorp Vault or AWS Secrets Manager — the rest of the code stays the same.

---

## Keys commands

### `keys init`

Generates the master encryption key and saves it in the OS keyring. Run once on first setup.

```bash
uv run llm-service keys init
```

Running it again when a key already exists does nothing — it tells you one exists and exits.

---

### `keys set`

Encrypts and stores a provider key for a tenant. Creates the tenant row if it doesn't exist.

```bash
uv run llm-service keys set \
  --tenant <tenant-id> \
  --provider <provider> \
  --key-format <format> \
  --payload '<json>'
```

**Options:**

| Option | Required | What to put |
|--------|----------|-------------|
| `--tenant` | Yes | Tenant ID, e.g. `tenant_acme` |
| `--provider` | Yes | Provider name: `openai`, `anthropic`, `bedrock`, `hf_endpoint`, `hf_self_hosted` |
| `--key-format` | Yes | One of: `api_key`, `aws_credentials`, `endpoint_pair` |
| `--payload` | Yes | JSON string with the credentials |

**Payload shapes by format:**

`api_key` — for OpenAI, Anthropic, most providers:
```bash
--key-format api_key \
--payload '{"api_key": "sk-abc123"}'
```

`aws_credentials` — for Bedrock:
```bash
--key-format aws_credentials \
--payload '{"access_key_id": "AKIA...", "secret_access_key": "xyz...", "region": "us-east-1"}'
```

`endpoint_pair` — for HuggingFace Endpoints or self-hosted:
```bash
--key-format endpoint_pair \
--payload '{"endpoint_url": "https://your-endpoint.huggingface.cloud", "token": "hf_abc..."}'
```

Running `keys set` a second time for the same `(tenant, provider)` pair updates the existing key — it won't create a duplicate.

---

### `keys list`

Shows which providers have keys registered for a tenant. Values are masked.

```bash
uv run llm-service keys list --tenant tenant_dev
```

Example output:

```
  provider=openai  key_format=api_key  payload=<masked>
  provider=anthropic  key_format=api_key  payload=<masked>
```

---

## Services commands

### `services add`

Registers a calling service with a bearer token. The token is hashed before storing — save it somewhere safe, you can't retrieve it later.

```bash
uv run llm-service services add \
  --name <name> \
  --token <token> \
  --tenants <tenant1,tenant2,...>
```

**Options:**

| Option | Required | What to put |
|--------|----------|-------------|
| `--name` | Yes | A label for this service, e.g. `taxbot-backend` |
| `--token` | Yes | The bearer token this service will use in API requests |
| `--tenants` | Yes | Comma-separated list of tenant IDs this service can act on behalf of |

Example:

```bash
uv run llm-service services add \
  --name taxbot-backend \
  --token my-secret-token-abc \
  --tenants tenant_acme,tenant_globex
```

After running, use this token in API requests:

```
Authorization: Bearer my-secret-token-abc
```

---

### `services list`

Lists all registered calling services. Tokens are masked.

```bash
uv run llm-service services list
```

Example output:

```
  name=taxbot-backend  allowed_tenants=['tenant_acme', 'tenant_globex']  token=<masked>
  name=internal-tool  allowed_tenants=['tenant_dev']  token=<masked>
```

---

## Full command map

```
llm-service
├── keys
│   ├── init       Generate master encryption key (run once)
│   ├── set        Store a provider key for a tenant
│   └── list       List keys registered for a tenant
└── services
    ├── add        Register a calling service + token
    └── list       List all registered services
```

Get help on any command:

```bash
uv run llm-service --help
uv run llm-service keys --help
uv run llm-service keys set --help
uv run llm-service services --help
```