from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.llm_service.providers import registry
from src.shared.config import settings
from src.shared.db.enums import BatchJobStatus
from src.shared.db.models import BatchJob
from src.shared.secrets.backend import MissingTenantKeyError
from src.shared.secrets.postgres_encrypted import PostgresEncryptedBackend

BATCH_ELIGIBLE_PROVIDERS: frozenset[str] = frozenset({"openai", "azure", "anthropic", "bedrock", "vertex_ai"})


async def batch_submit(ctx: dict) -> None:
    """Cron task: submit all pending batch jobs to their provider's batch API."""
    async with AsyncSession(ctx["db_engine"], expire_on_commit=False) as db:
        rows = await db.execute(select(BatchJob).where(BatchJob.status == BatchJobStatus.PENDING))
        pending_jobs = list(rows.scalars().all())

        groups: dict[tuple[str, str], list[BatchJob]] = {}
        for job in pending_jobs:
            groups.setdefault((job.tenant_id, job.provider), []).append(job)

        max_attempts = max(1, settings.batch_max_submit_attempts)
        for (tenant_id, provider), jobs in groups.items():
            try:
                key = await PostgresEncryptedBackend(db).get_key(tenant_id, provider)
                transport = registry.resolve(provider, jobs[0].model, "batch")
                await transport.batch_submit(jobs, key)
                await db.commit()
            except MissingTenantKeyError:
                for job in jobs:
                    job.status = BatchJobStatus.FAILED
                    job.error_code = "missing_tenant_key"
                await db.commit()
            except Exception:
                for job in jobs:
                    job.submit_attempts += 1
                    if job.submit_attempts >= max_attempts:
                        job.status = BatchJobStatus.FAILED
                        job.error_code = "max_submit_attempts_exceeded"
                await db.commit()


async def batch_poll(ctx: dict) -> None:
    """Cron task: check in-flight batch jobs and write results when complete."""
    async with AsyncSession(ctx["db_engine"], expire_on_commit=False) as db:
        rows = await db.execute(select(BatchJob).where(BatchJob.status == BatchJobStatus.SUBMITTED))
        submitted_jobs = list(rows.scalars().all())

        batches: dict[tuple[str, str, str], list[BatchJob]] = {}
        for job in submitted_jobs:
            batches.setdefault((job.tenant_id, job.provider, job.upstream_batch_id), []).append(job)

        for (tenant_id, provider, upstream_batch_id), jobs in batches.items():
            try:
                key = await PostgresEncryptedBackend(db).get_key(tenant_id, provider)
                transport = registry.resolve(provider, jobs[0].model, "batch")
                await transport.batch_poll(upstream_batch_id, jobs, key)
                await db.commit()
            except MissingTenantKeyError:
                for job in jobs:
                    job.status = BatchJobStatus.FAILED
                    job.error_code = "missing_tenant_key"
                await db.commit()
            except Exception as error:
                print(f"batch_poll: failed for ({tenant_id}, {provider}, {upstream_batch_id}): {error}")
