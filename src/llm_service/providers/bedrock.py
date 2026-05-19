from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any, Optional

from src.llm_service.providers.base import BaseLLMProvider, StreamEvent
from src.llm_service.schemas.chat import (
    CacheBlock, ChatResponse, Choice, ChoiceMessage, NormalisedLLMRequest, UsageBlock,
)
from src.shared.db.enums import BatchJobStatus, Transport
from src.shared.db.models import BatchJob
from src.shared.schemas.envelope import CostBlock, GuardrailsBlock, LatencyBlock, PolicyBlock
from src.shared.secrets.backend import TenantKeyPayload


class BedrockTransport(BaseLLMProvider):
    """Direct boto3 Bedrock transport for batch inference.
    """

    def __init__(self, regions: Optional[list[str]] = None) -> None:
        pass

    async def chat(self, request: NormalisedLLMRequest, key: TenantKeyPayload) -> ChatResponse:
        raise NotImplementedError("BedrockTransport does not handle chat — routed via LiteLLMTransport")

    async def stream(
        self, request: NormalisedLLMRequest, key: TenantKeyPayload
    ) -> AsyncIterator[StreamEvent]:
        raise NotImplementedError("BedrockTransport does not handle stream — routed via LiteLLMTransport")

    def _boto_clients(self, key: TenantKeyPayload) -> tuple[Any, Any]:
        import boto3
        creds: dict[str, Any] = {
            "aws_access_key_id": key.data["access_key_id"],
            "aws_secret_access_key": key.data["secret_access_key"],
            "region_name": key.data.get("region", "us-east-1"),
        }
        if key.data.get("aws_session_token"):
            creds["aws_session_token"] = key.data["aws_session_token"]
        return boto3.client("bedrock", **creds), boto3.client("s3", **creds)

    def _to_model_input(self, req: dict[str, Any]) -> dict[str, Any]:
        """Convert normalised OpenAI-format request to Bedrock model-specific input."""
        model_id: str = req["model"]
        messages: list[dict[str, Any]] = req["messages"]
        params: dict[str, Any] = req.get("params") or {}
        max_tokens = params.get("max_tokens") or 1024

        if model_id.startswith("anthropic."):
            payload: dict[str, Any] = {
                "anthropic_version": "bedrock-2023-05-31",
                "messages": messages,
                "max_tokens": max_tokens,
            }
            for param in ("temperature", "top_p"):
                if params.get(param) is not None:
                    payload[param] = params[param]
            if req.get("tools"):
                payload["tools"] = req["tools"]
            return payload

        if model_id.startswith("meta.llama"):
            prompt = "\n".join(f"{m['role'].capitalize()}: {m['content']}" for m in messages)
            return {
                "prompt": prompt,
                "max_gen_len": max_tokens,
                "temperature": params.get("temperature", 0.7),
            }

        if model_id.startswith("amazon.titan"):
            prompt = "\n".join(m["content"] for m in messages if isinstance(m["content"], str))
            return {
                "inputText": prompt,
                "textGenerationConfig": {
                    "maxTokenCount": max_tokens,
                    "temperature": params.get("temperature", 0.7),
                },
            }

        # Generic fallback — pass through as-is and let Bedrock reject it if unsupported
        return {"messages": messages, "max_tokens": max_tokens}

    def _write_result(self, model_id: str, model_output: dict[str, Any], job: BatchJob) -> None:
        """Parse Bedrock model output and write a ChatResponse into job.result."""
        content_text: Optional[str] = None
        tool_calls: Optional[list[dict[str, Any]]] = None
        input_tokens = 0
        output_tokens = 0
        finish_reason = "stop"

        if model_id.startswith("anthropic."):
            for block in model_output.get("content") or []:
                if block.get("type") == "text":
                    content_text = block["text"]
                elif block.get("type") == "tool_use":
                    if tool_calls is None:
                        tool_calls = []
                    tool_calls.append({
                        "id": block["id"], "type": "function",
                        "function": {"name": block["name"], "arguments": json.dumps(block["input"])},
                    })
            usage = model_output.get("usage") or {}
            input_tokens = usage.get("input_tokens", 0) or 0
            output_tokens = usage.get("output_tokens", 0) or 0
            finish_reason = model_output.get("stop_reason") or "stop"

        elif model_id.startswith("meta.llama"):
            content_text = model_output.get("generation", "")
            input_tokens = model_output.get("prompt_token_count", 0) or 0
            output_tokens = model_output.get("generation_token_count", 0) or 0
            finish_reason = model_output.get("stop_reason") or "stop"

        elif model_id.startswith("amazon.titan"):
            results = model_output.get("results") or []
            if results:
                content_text = results[0].get("outputText", "")
            input_tokens = model_output.get("inputTextTokenCount", 0) or 0
            output_tokens = sum(r.get("tokenCount", 0) for r in results)
            finish_reason = (results[0].get("completionReason") or "stop").lower() if results else "stop"

        else:
            content_text = str(model_output)

        job.result = ChatResponse(
            id=job.request_id, created=int(time.time()), tenant_id=job.tenant_id,
            provider=job.provider, model=job.model, transport=Transport.DIRECT,
            choices=[Choice(
                index=0,
                message=ChoiceMessage(role="assistant", content=content_text, tool_calls=tool_calls),
                finish_reason=finish_reason,
            )],
            usage=UsageBlock(
                input_tokens=input_tokens, output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
            ),
            cost=CostBlock(), latency_ms=LatencyBlock(),
            cache=CacheBlock(our_cache_hit=False),
            guardrails=GuardrailsBlock(), policy=PolicyBlock(),
        ).model_dump()

    async def batch_submit(self, jobs: list[BatchJob], key: TenantKeyPayload) -> None:
        print(f"BedrockTransport.batch_submit: {len(jobs)} jobs")
        s3_bucket_name = key.data.get("s3_bucket_name")
        role_arn = key.data.get("role_arn")
        if not s3_bucket_name:
            raise ValueError("s3_bucket_name required in Bedrock credentials for batch")
        if not role_arn:
            raise ValueError("role_arn required in Bedrock credentials for batch")

        loop = asyncio.get_event_loop()
        bedrock_client, s3_client = self._boto_clients(key)

        model_id = jobs[0].normalised_request["model"]
        jsonl_content = "\n".join(
            json.dumps({"recordId": str(job.id), "modelInput": self._to_model_input(job.normalised_request)})
            for job in jobs
        )

        batch_id = str(uuid.uuid4())
        input_key = f"batch-input/{batch_id}.jsonl"
        output_prefix = f"s3://{s3_bucket_name}/batch-output/{batch_id}/"

        print(f"BedrockTransport.batch_submit: uploading to s3://{s3_bucket_name}/{input_key}")
        await loop.run_in_executor(None, lambda: s3_client.put_object(
            Bucket=s3_bucket_name, Key=input_key, Body=jsonl_content.encode("utf-8"),
        ))

        print(f"BedrockTransport.batch_submit: creating job for model={model_id}")
        response = await loop.run_in_executor(None, lambda: bedrock_client.create_model_invocation_job(
            roleArn=role_arn,
            clientRequestToken=batch_id,
            modelId=model_id,
            inputDataConfig={"s3InputDataConfig": {
                "s3Uri": f"s3://{s3_bucket_name}/{input_key}",
                "s3InputFormat": "JSONL",
            }},
            outputDataConfig={"s3OutputDataConfig": {
                "s3Uri": output_prefix,
            }},
        ))
        job_arn: str = response["jobArn"]
        print(f"BedrockTransport.batch_submit: job_arn={job_arn}")
        submitted_at = datetime.now(timezone.utc)
        for job in jobs:
            job.status = BatchJobStatus.SUBMITTED
            job.upstream_batch_id = job_arn
            job.submitted_at = submitted_at

    async def batch_poll(
        self, upstream_batch_id: str, jobs: list[BatchJob], key: TenantKeyPayload,
    ) -> None:
        print(f"BedrockTransport.batch_poll: job_arn={upstream_batch_id}")
        s3_bucket_name = key.data.get("s3_bucket_name")
        if not s3_bucket_name:
            raise ValueError("s3_bucket_name required in Bedrock credentials for batch")

        loop = asyncio.get_event_loop()
        bedrock_client, s3_client = self._boto_clients(key)

        job_info = await loop.run_in_executor(
            None, lambda: bedrock_client.get_model_invocation_job(jobIdentifier=upstream_batch_id)
        )
        status: str = job_info["status"]
        print(f"BedrockTransport.batch_poll: status={status}")

        if status not in ("Completed", "Failed", "Stopped", "PartiallyCompleted", "Expired"):
            return

        completed_at = datetime.now(timezone.utc)

        if status in ("Failed", "Stopped", "Expired"):
            for job in jobs:
                job.status = BatchJobStatus.FAILED
                job.error_code = f"bedrock_{status.lower()}"
                job.completed_at = completed_at
            return

        # Completed or PartiallyCompleted — read results from S3
        output_uri: str = job_info["outputDataConfig"]["s3OutputDataConfig"]["s3Uri"]
        output_prefix = output_uri.removeprefix(f"s3://{s3_bucket_name}/")
        print(f"BedrockTransport.batch_poll: listing output under {output_prefix}")

        list_resp = await loop.run_in_executor(
            None, lambda: s3_client.list_objects_v2(Bucket=s3_bucket_name, Prefix=output_prefix)
        )
        output_keys = [
            obj["Key"] for obj in (list_resp.get("Contents") or [])
            if obj["Key"].endswith(".jsonl.out")
        ]
        print(f"BedrockTransport.batch_poll: {len(output_keys)} output file(s)")

        jobs_by_id = {str(job.id): job for job in jobs}
        model_id = jobs[0].model

        for output_key in output_keys:
            s3_obj = await loop.run_in_executor(
                None, lambda k=output_key: s3_client.get_object(Bucket=s3_bucket_name, Key=k)
            )
            for line in s3_obj["Body"].read().decode("utf-8").splitlines():
                if not line.strip():
                    continue
                record = json.loads(line)
                job = jobs_by_id.get(record.get("recordId"))
                if job is None:
                    continue
                if record.get("error"):
                    job.status = BatchJobStatus.FAILED
                    job.error_code = "bedrock_record_error"
                else:
                    self._write_result(model_id, record.get("modelOutput") or {}, job)
                    job.status = BatchJobStatus.COMPLETE
                job.completed_at = completed_at
