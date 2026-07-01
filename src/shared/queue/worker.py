from __future__ import annotations

import logging

import litellm
from arq import cron
from arq.connections import RedisSettings
from sqlalchemy.ext.asyncio import create_async_engine

from src.shared.config import settings
from src.shared.queue.tasks import batch_poll, batch_submit


class _SuppressBatchOutputFileNone(logging.Filter):
    """Drop the LiteLLM LoggingWorker noise for in-progress batch polls.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return "Output file id is None" not in record.getMessage()


async def startup(ctx: dict) -> None:
    ctx["db_engine"] = create_async_engine(settings.db_url)
    litellm.success_callback = []
    litellm.failure_callback = []
    litellm._async_success_callback = []
    litellm._async_failure_callback = []
    logging.getLogger("LiteLLM").addFilter(_SuppressBatchOutputFileNone())


async def shutdown(ctx: dict) -> None:
    await ctx["db_engine"].dispose()


class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    functions = [batch_submit, batch_poll]
    cron_jobs = [
        # batch_submit runs every 5 min to collect pending jobs and submit to provider batch API
        cron(batch_submit, minute=set(range(0, 60, 5))),
        # batch_poll runs offset by 1 min so it doesn't race with submit
        cron(batch_poll, minute=set(range(1, 60, 5))),
    ]
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = 50
    job_timeout = 300
