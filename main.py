from fastapi import FastAPI
from src.llm_service.api.rest.chat import router as chat_router

app = FastAPI(
    title="ai-service",
    description="Multi-provider LLM gateway",
    version="0.1.0",
)

app.include_router(chat_router)