from __future__ import annotations

from typing import Protocol


class ExternalAdapterDisabled(RuntimeError):
    pass


class LlmProvider(Protocol):
    async def generate_typed(self, prompt: str, schema_name: str) -> dict[str, object]: ...


class DisabledLlmProvider:
    async def generate_typed(self, prompt: str, schema_name: str) -> dict[str, object]:
        del prompt, schema_name
        raise ExternalAdapterDisabled("Real LLM providers are disabled in Phase 0")

