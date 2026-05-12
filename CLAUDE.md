# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`llm-service` is Gritworks' multi-provider LLM gateway. It abstracts LLM
integration (and a sibling voice provider module) for every other
Gritworks service, behind REST + WebSocket APIs (MCP planned). Hybrid
LiteLLM (in-process Python SDK) + direct provider adapters. Strict BYOK
with no Gritworks-default fallback.

The repo currently contains the architectural plan only — no
implementation has landed yet. Read `docs/architecture.md` first; it is
the source of truth for design decisions, response shapes, ledger schema,
and phasing.

## Where to start

- **`docs/architecture.md`** — full design: integration strategy, BYOK,
  pricing/ledger, prompt caching, response envelope, repo layout,
  phased delivery, verification.

## Hard invariants (do not violate without explicit discussion)

- **BYOK only.** No Gritworks-default upstream key fallback, ever.
  Missing or invalid tenant key must produce a clear hard error, not a
  silent fallback charge to Gritworks accounts.
- **Dual cost recording.** Every upstream call writes a ledger row that
  contains both our YAML-computed cost (with `pricing_version`) and the
  raw provider-reported usage block. Both must be present before the
  request is considered complete.
- **One place to add a provider.** New LLM providers = one adapter file in
  `src/llm_service/providers/` + one entry in
  `src/llm_service/providers/registry.py`. New voice providers = one adapter
  file in `src/voice/providers/` + one registry entry. Don't fan out provider
  logic across the codebase.
- **No keys in `.env` or any committed file.** Local dev uses the
  `keys` CLI which writes encrypted entries to dev Postgres with the
  master key in the OS keyring. Production swaps the secret backend
  (Vault, AWS Secrets Manager) via config.
- **Pricing YAML stays fresh.** CI fails the build if any model in
  `pricing/models.yaml` is older than the configured staleness window.
  Changes bump `pricing_version`.
- **Response envelope is non-negotiable.** Both streaming and
  non-streaming responses surface the full ledger-grade metadata
  (tokens, cost, latency, cache, guardrails, policy). Don't strip
  fields to "simplify" — callers depend on them.

## Tech stack at a glance

FastAPI · uvicorn/uvloop · Pydantic v2 · async SQLAlchemy 2.x + asyncpg ·
Alembic · Arq (Redis) · Redis cache · Langfuse self-hosted · structlog ·
uv · pytest + pytest-asyncio · vcrpy · LiteLLM (in-process SDK) ·
Presidio + Llama-Guard guardrails.

## Implementation status

The repository is a fresh init. No source code, dependency manifest, or
tests exist yet. Build/test/run commands will be added to this file as
phase 1 of `docs/architecture.md` lands. Until then: this file is a map,
not a manual.