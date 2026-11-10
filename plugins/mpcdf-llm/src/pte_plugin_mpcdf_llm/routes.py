"""Routes of the MPCDF LLM plugin."""

from fastapi import APIRouter

router = APIRouter(prefix="/api/plugins/mpcdf-llm", tags=["mpcdf-llm"])
