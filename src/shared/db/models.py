from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import Boolean, CheckConstraint, DateTime, Enum, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func

from src.shared.db.enums import BatchJobStatus, Feature, KeyFormat, LedgerStatus, Transport


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    """A tenant whose resources (keys, policies, ledger rows) are isolated from all other tenants."""

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(
        String, primary_key=True,
        comment="Slug-style identifier, e.g. tenant_acme. Used as FK in every other table.",
    )
    name: Mapped[str] = mapped_column(
        String, nullable=False,
        comment="Human-readable display name.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
        comment="Row creation timestamp (UTC).",
    )


class CallingService(Base):
    """A trusted backend application or service authorized to call llm-service APIs."""

    __tablename__ = "calling_services"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
        comment="UUID primary key.",
    )
    name: Mapped[str] = mapped_column(
        String, nullable=False,
        comment="Service name, e.g. taxbot-inc.",
    )
    # raw token is never stored — only SHA-256 hash; prevents credential leakage if DB is compromised
    bearer_token_hash: Mapped[str] = mapped_column(
        String, nullable=False, unique=True,
        comment="SHA-256 of the bearer token. Raw token is never persisted.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
        comment="Row creation timestamp (UTC).",
    )


class CallingServiceTenant(Base):
    """Grants a CallingService permission to perform requests on behalf of a Tenant."""

    __tablename__ = "calling_service_tenants"

    calling_service_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calling_services.id", ondelete="CASCADE"), primary_key=True,
        comment="The calling service being granted access. Cascade-deletes this row when the service is removed.",
    )
    tenant_id: Mapped[str] = mapped_column(
        String, ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True,
        comment="The tenant this service is permitted to act on behalf of. Cascade-deletes this row when the tenant is removed.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
        comment="When this access grant was created (UTC).",
    )


class TenantKey(Base):
    """Encrypted BYOK credential for one (tenant, provider) pair. One row per combination."""

    __tablename__ = "tenant_keys"
    # one key per (tenant, provider) — enforced here so the secrets layer can do a simple point lookup
    __table_args__ = (UniqueConstraint("tenant_id", "provider", name="uq_tenant_provider"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
        comment="UUID primary key.",
    )
    tenant_id: Mapped[str] = mapped_column(
        String, ForeignKey("tenants.id"), nullable=False,
        comment="FK to tenants.id.",
    )
    provider: Mapped[str] = mapped_column(
        String, nullable=False,
        comment="Provider identifier, e.g. openai, anthropic, bedrock. Not an enum — new providers must not require a migration.",
    )
    key_format: Mapped[KeyFormat] = mapped_column(
        Enum(KeyFormat, native_enum=False, values_callable=lambda o: [e.value for e in o]),
        nullable=False,
        comment="Shape of the decrypted JSON payload: api_key | aws_credentials | endpoint_pair.",
    )
    encrypted_payload: Mapped[str] = mapped_column(
        Text, nullable=False,
        comment="Fernet-encrypted JSON. Shape is determined by key_format.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
        comment="Row creation timestamp (UTC).",
    )


class LedgerEntry(Base):
    """One row per upstream LLM call. Stores both our computed cost and the raw provider usage for audit."""

    __tablename__ = "ledger_entries"
    __table_args__ = (
        CheckConstraint(
            "(status != 'success') OR (provider_reported_usage IS NOT NULL)",
            name="ck_ledger_success_has_provider_usage",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
        comment="UUID primary key.",
    )
    # unique so INSERT ... ON CONFLICT (request_id) DO NOTHING gives idempotent ledger writes
    request_id: Mapped[str] = mapped_column(
        String, nullable=False, unique=True,
        comment="Client-supplied or gateway-generated idempotency key.",
    )
    tenant_id: Mapped[str] = mapped_column(
        String, ForeignKey("tenants.id"), nullable=False,
        comment="FK to tenants.id.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
        comment="Row creation timestamp (UTC).",
    )
    provider: Mapped[str] = mapped_column(
        String, nullable=False,
        comment="LLM provider that handled the call, e.g. openai, anthropic, bedrock.",
    )
    model: Mapped[str] = mapped_column(
        String, nullable=False,
        comment="Canonical model ID as sent in the request.",
    )
    transport: Mapped[Transport] = mapped_column(
        Enum(Transport, native_enum=False, values_callable=lambda o: [e.value for e in o]),
        nullable=False,
        comment="Whether the call was routed through the LiteLLM SDK or a direct adapter.",
    )
    region: Mapped[Optional[str]] = mapped_column(
        String, nullable=True,
        comment="Cloud region the upstream call was routed to, e.g. us-east-1.",
    )
    feature: Mapped[Feature] = mapped_column(
        Enum(Feature, native_enum=False, values_callable=lambda o: [e.value for e in o]),
        nullable=False,
        comment="Request type executed: chat | stream | embed | tool.",
    )
    tokens_in: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
        comment="Prompt tokens reported by the provider.",
    )
    tokens_out: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
        comment="Completion tokens reported by the provider.",
    )
    input_tokens_cache_write: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
        comment="Tokens written to provider-side prompt cache, billed at cache write rate.",
    )
    input_tokens_cache_read: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
        comment="Tokens served from provider-side prompt cache, billed at cache read rate.",
    )
    upstream_prompt_cache_hit: Mapped[Optional[bool]] = mapped_column(
        Boolean, nullable=True,
        comment="True if any input tokens were served from the provider-side prompt cache.",
    )
    our_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(precision=18, scale=10), nullable=False,
        comment="Cost computed from pricing/models.yaml at request time.",
    )
    pricing_version: Mapped[int] = mapped_column(
        Integer, nullable=False,
        comment="pricing_version from models.yaml used to compute our_cost_usd. Allows historical cost re-computation.",
    )
    provider_reported_usage: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB, nullable=True,
        comment="Raw usage block from the provider response, stored verbatim for billing audit.",
    )
    provider_reported_cost_usd: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(precision=18, scale=10), nullable=True,
        comment="Cost figure reported by the provider where available, e.g. Bedrock.",
    )
    latency_ms: Mapped[int] = mapped_column(
        Integer, nullable=False,
        comment="Total wall-clock time in ms from request receipt to response sent.",
    )
    time_to_first_token_ms: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
        comment="Time in ms to the first streaming token. Null for non-streaming requests.",
    )
    status: Mapped[LedgerStatus] = mapped_column(
        Enum(LedgerStatus, native_enum=False, values_callable=lambda o: [e.value for e in o]),
        nullable=False,
        comment="Outcome of the request: success | error | partial_response.",
    )
    error_code: Mapped[Optional[str]] = mapped_column(
        String, nullable=True,
        comment="Structured error code when status is error or partial_response.",
    )
    our_cache_hit: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
        comment="True if the response was served from our exact-match Redis cache.",
    )
    batched: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
        comment="True if the request was submitted via the OpenAI/Anthropic batch API.",
    )
    guardrail_flags: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB, nullable=True,
        comment="Presidio/Llama-Guard events: which rules fired and what was redacted.",
    )


class BatchJob(Base):
    """One row per async batch request. Tracks lifecycle from pending → submitted → complete/failed."""

    __tablename__ = "batch_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
        comment="UUID primary key. Used as custom_id in the upstream batch API request.",
    )
    request_id: Mapped[str] = mapped_column(
        String, nullable=False, unique=True,
        comment="Gateway-level request ID — same idempotency key as ledger_entries.request_id.",
    )
    tenant_id: Mapped[str] = mapped_column(
        String, ForeignKey("tenants.id"), nullable=False,
        comment="FK to tenants.id. Used to reload the BYOK key at submission time.",
    )
    provider: Mapped[str] = mapped_column(
        String, nullable=False,
        comment="Provider that will handle the batch, e.g. openai, anthropic.",
    )
    model: Mapped[str] = mapped_column(
        String, nullable=False,
        comment="Canonical model ID as sent in the original request.",
    )
    normalised_request: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False,
        comment="NormalisedLLMRequest serialised as JSONB. Reconstructed at submission time.",
    )
    upstream_batch_id: Mapped[Optional[str]] = mapped_column(
        String, nullable=True,
        comment="Provider batch job ID, e.g. OpenAI batch_xxx or Anthropic msgbatch_xxx.",
    )
    upstream_file_id: Mapped[Optional[str]] = mapped_column(
        String, nullable=True,
        comment="OpenAI input file ID (file_xxx). Null for Anthropic.",
    )
    status: Mapped[BatchJobStatus] = mapped_column(
        Enum(BatchJobStatus, native_enum=False, values_callable=lambda o: [e.value for e in o]),
        nullable=False, default=BatchJobStatus.PENDING,
        comment="Lifecycle state: pending → submitted → complete | failed.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
        comment="Row creation timestamp (UTC).",
    )
    submitted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="When the job was submitted to the provider batch API (UTC).",
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="When the result was retrieved from the provider (UTC).",
    )
    result: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB, nullable=True,
        comment="ChatResponse serialised as JSONB. Populated when status=complete.",
    )
    submit_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
        comment="Number of times batch_submit has tried (and failed) to send this job to the provider.",
    )
    error_code: Mapped[Optional[str]] = mapped_column(
        String, nullable=True,
        comment="Structured error code when status=failed.",
    )


class Policy(Base):
    """Per-tenant usage controls. One row per tenant; all limits are optional (null = no limit)."""

    __tablename__ = "policies"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
        comment="UUID primary key.",
    )
    tenant_id: Mapped[str] = mapped_column(
        String, ForeignKey("tenants.id"), nullable=False, unique=True,
        comment="FK to tenants.id. One policy row per tenant.",
    )
    max_tokens_per_request: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
        comment="Hard cap on max_tokens per request. Requests over this are rejected with 429.",
    )
    rate_limit_rpm: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
        comment="Maximum requests per minute. Null means no rate limit.",
    )
    budget_usd_monthly: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(precision=12, scale=2), nullable=True,
        comment="Monthly cost ceiling in USD. Null means no budget limit.",
    )
    allowed_models: Mapped[Optional[list[str]]] = mapped_column(
        ARRAY(String), nullable=True,
        comment="Whitelist of model IDs this tenant may use. Null means all models are allowed.",
    )
    denied_models: Mapped[Optional[list[str]]] = mapped_column(
        ARRAY(String), nullable=True,
        comment="Blacklist of model IDs this tenant may not use. Null means none are denied.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
        comment="Row creation timestamp (UTC).",
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True,
        comment="Last modification timestamp (UTC). Null until the first update.",
    )