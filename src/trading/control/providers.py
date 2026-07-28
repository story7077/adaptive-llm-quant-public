from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CommanderProvider(StrEnum):
    CODEX_SOL_MAX = "CODEX_SOL_MAX"
    WEBGPT_SOL_PRO = "WEBGPT_SOL_PRO"


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    provider: CommanderProvider
    display_name: str
    model: str
    reasoning_profile: str
    transport: str
    description: str

    def as_payload(self) -> dict[str, str]:
        return {
            "provider": self.provider.value,
            "display_name": self.display_name,
            "model": self.model,
            "reasoning_profile": self.reasoning_profile,
            "transport": self.transport,
            "description": self.description,
        }


PROVIDER_REGISTRY: dict[CommanderProvider, ProviderDescriptor] = {
    CommanderProvider.CODEX_SOL_MAX: ProviderDescriptor(
        provider=CommanderProvider.CODEX_SOL_MAX,
        display_name="Codex Sol Max",
        model="gpt-5.6-sol",
        reasoning_profile="max",
        transport="codex_exec",
        description="격리된 일회성 Codex 실행이 공통 JSON 결정을 생성합니다.",
    ),
    CommanderProvider.WEBGPT_SOL_PRO: ProviderDescriptor(
        provider=CommanderProvider.WEBGPT_SOL_PRO,
        display_name="WebGPT 5.6 Sol Pro",
        model="gpt-5.6-sol",
        reasoning_profile="pro",
        transport="webgpt_json",
        description="WebGPT/AGBrowse 분석 결과를 공통 JSON 결정으로 투입합니다.",
    ),
}


def provider_descriptor(provider: CommanderProvider) -> ProviderDescriptor:
    return PROVIDER_REGISTRY[provider]

