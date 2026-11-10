"""MPCDF LLM plugin: start MPCDF-hosted LLM endpoints and offer them as free models."""

from .plugin import MpcdfLlmPlugin
from .routes import router

__all__ = ["MpcdfLlmPlugin", "router"]
