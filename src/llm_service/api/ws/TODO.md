# llm_service/api/ws — WebSocket streaming handlers

## Files to create

- `chat.py` — WebSocket endpoint `WS /v1/ws/chat`:
  - Same pipeline as REST SSE but over a persistent WebSocket connection
  - Event envelope identical to SSE: `token`, `tool_use`, `usage`, `finish`, `error`
  - Handle mid-stream disconnects: cancel upstream call, write partial ledger row with `status=client_disconnect`
  - Session management: client can send follow-up turns on the same socket (context carried across turns)

## Why WebSocket vs SSE?

SSE is one-way server-push. WebSocket is bidirectional — needed for multi-turn sessions
where the client sends the next user message while the connection is still open.

## Phase

Phase 2
