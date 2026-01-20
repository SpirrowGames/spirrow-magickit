"""Adapter for Lexora LLM service."""

from typing import Any

import httpx

from magickit.adapters.base import BaseAdapter
from magickit.utils.logging import get_logger

logger = get_logger(__name__)


class LexoraAdapter(BaseAdapter):
    """Adapter for Lexora LLM service.

    Provides methods for text generation and LLM-based operations.
    """

    async def health_check(self) -> bool:
        """Check if Lexora service is healthy.

        Returns:
            True if the service is healthy, False otherwise.
        """
        try:
            response = await self._get("/health")
            return response.status_code == 200
        except httpx.HTTPError as e:
            logger.warning("Lexora health check failed", error=str(e))
            return False

    async def generate(
        self,
        prompt: str,
        max_tokens: int = 1000,
        temperature: float = 0.7,
        **kwargs: Any,
    ) -> str:
        """Generate text using the LLM.

        Args:
            prompt: Input prompt for generation.
            max_tokens: Maximum tokens to generate.
            temperature: Sampling temperature (0.0-1.0).
            **kwargs: Additional generation parameters.

        Returns:
            Generated text.

        Raises:
            httpx.HTTPError: If the request fails.
        """
        payload = {
            "prompt": prompt,
            "max_tokens": max_tokens,
            "temperature": temperature,
            **kwargs,
        }

        logger.info("Generating text", prompt_length=len(prompt), max_tokens=max_tokens)
        response = await self._post("/generate", json=payload)
        result = response.json()

        return result.get("text", "")

    async def chat(
        self,
        messages: list[dict[str, str]],
        max_tokens: int = 1000,
        temperature: float = 0.7,
        **kwargs: Any,
    ) -> str:
        """Chat with the LLM using message format.

        Args:
            messages: List of chat messages with 'role' and 'content'.
            max_tokens: Maximum tokens to generate.
            temperature: Sampling temperature (0.0-1.0).
            **kwargs: Additional chat parameters.

        Returns:
            Assistant response text.

        Raises:
            httpx.HTTPError: If the request fails.
        """
        payload = {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            **kwargs,
        }

        logger.info("Chat request", message_count=len(messages), max_tokens=max_tokens)
        response = await self._post("/chat", json=payload)
        result = response.json()

        return result.get("response", "")

    async def analyze_intent(
        self,
        query: str,
        context: str = "",
    ) -> dict[str, Any]:
        """Analyze the intent of a query.

        Args:
            query: User query to analyze.
            context: Additional context.

        Returns:
            Intent analysis result with 'intent', 'entities', and 'confidence'.

        Raises:
            httpx.HTTPError: If the request fails.
        """
        payload = {
            "query": query,
            "context": context,
        }

        logger.info("Analyzing intent", query_length=len(query))
        response = await self._post("/analyze/intent", json=payload)

        return response.json()

    async def get_model_capabilities(self) -> dict[str, Any]:
        """Get all available models and their capabilities from Lexora.

        Returns:
            Dict with models, available_capabilities, and default_model_for_unknown_task.

        Raises:
            httpx.HTTPError: If the request fails.
        """
        logger.info("Fetching model capabilities")
        response = await self._get("/v1/models/capabilities")
        return response.json()

    async def classify_task(self, task_description: str) -> dict[str, Any]:
        """Classify a task and get recommended model using LLM.

        Args:
            task_description: The task to classify.

        Returns:
            Dict with recommended_model, task_type, confidence, reasoning, alternatives.

        Raises:
            httpx.HTTPError: If the request fails.
        """
        payload = {"task_description": task_description}

        logger.info("Classifying task", task_length=len(task_description))
        response = await self._post("/v1/classify-task", json=payload)
        return response.json()
