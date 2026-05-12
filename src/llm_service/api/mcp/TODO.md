# llm_service/api/mcp — MCP transport (Phase 5 — placeholder)

Do not implement until Phase 5.

## When Phase 5 starts

- `server.py` — expose `llm_service` itself as an MCP server (tools: chat, embed)
- `client.py` — `MCPTransport` consumer: call upstream MCP servers as providers

## Why no core changes are needed

`llm_service/normaliser.py` already accepts `tools[]` as first-class fields.
When MCP lands: add `MCPTransport` to `providers/registry.py` + build this module.
Nothing else in the pipeline changes.

## Phase

Phase 5
