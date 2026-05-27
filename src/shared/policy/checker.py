from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.db.models import LedgerEntry, Policy


@dataclass
class PolicyContext:
    provider: str
    model: str
    max_tokens: Optional[int] = field(default=None)


class PolicyExceededError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class PolicyChecker:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def check(self, tenant_id: str, policy_context: PolicyContext) -> None:
        result = await self._db.execute(select(Policy).where(Policy.tenant_id == tenant_id))
        policy = result.scalar_one_or_none()
        if policy is None:
            return

        if policy.denied_models and policy_context.model in policy.denied_models:
            raise PolicyExceededError(f"model denied: {policy_context.model}")

        if policy.allowed_models and policy_context.model not in policy.allowed_models:
            raise PolicyExceededError(f"model not in allowlist: {policy_context.model}")

        if (policy.max_tokens_per_request is not None
                and policy_context.max_tokens is not None
                and policy_context.max_tokens > policy.max_tokens_per_request):
            raise PolicyExceededError(
                f"max_tokens {policy_context.max_tokens} exceeds limit {policy.max_tokens_per_request}"
            )

        if policy.budget_usd_monthly is not None:
            first_day = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            spent_result = await self._db.execute(
                select(func.sum(LedgerEntry.our_cost_usd)).where(
                    LedgerEntry.tenant_id == tenant_id,
                    LedgerEntry.created_at >= first_day,
                )
            )
            spent = spent_result.scalar() or 0.0
            if spent >= policy.budget_usd_monthly:
                raise PolicyExceededError("monthly budget exceeded")
