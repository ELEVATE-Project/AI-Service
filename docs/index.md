# LLM Service

A multi-provider LLM gateway. It sits between your application and LLM providers (OpenAI, Anthropic, Bedrock, and others), handling auth, key management, cost tracking, and streaming — so your application never needs to touch any provider SDK directly.

---

## What it does

- **Brings your own keys** — every tenant supplies their own provider API keys. No shared keys, no surprise bills.
- **One API for all providers** — same request shape whether you're calling OpenAI, Anthropic, or a self-hosted model.
- **Full cost tracking** — every call writes a ledger row with token counts and cost in USD.
- **Streaming** — SSE streaming with per-token events and a final metadata block.
- **Auth** — bearer token auth with per-tenant access control. Bad tokens stop at the door.

---

## Quick links

- [Local Setup](local-setup.md) — get it running on your machine in 8 steps
- [Usage Guide](usage.md) — curl examples and sample responses
- [Auth & Tenants](auth-and-tenants.md) — how auth works and what a tenant is
- [Keys & Secrets CLI](keys-cli.md) — how to store and manage provider keys