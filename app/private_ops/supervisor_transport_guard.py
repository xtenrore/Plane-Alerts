"""Resilience guard for the Private AI Operations Supervisor transport.

Provider calls that return neither user-visible text nor a tool request are unusable
responses, not successes. Treat them as bounded provider failures so the existing
Supervisor route failover can try the next approved free slot/provider. Likewise,
refuse a provider that is still asking for tools after the engine's bounded tool
rounds are already represented in the conversation.
"""
from __future__ import annotations

from typing import Mapping, Sequence

from .provider_adapters import ProviderFailure, Slot
from .supervisor import ChatResult, MAX_TOOL_ROUNDS, SupervisorTransport


class GuardedSupervisorTransport:
    """Wrap a Supervisor transport and reject unusable successful HTTP responses."""

    def __init__(self, delegate: SupervisorTransport | None = None) -> None:
        self._delegate = delegate or SupervisorTransport()

    @staticmethod
    def _prior_tool_rounds(messages: Sequence[Mapping[str, object]]) -> int:
        return sum(
            1
            for message in messages
            if message.get("role") == "assistant" and bool(message.get("tool_calls"))
        )

    def chat(
        self,
        slot: Slot,
        model: str,
        messages: Sequence[Mapping[str, object]],
        *,
        tools: Sequence[Mapping[str, object]] = (),
        affinity: str = "",
    ) -> ChatResult:
        result = self._delegate.chat(slot, model, messages, tools=tools, affinity=affinity)
        content = result.content.strip() if isinstance(result.content, str) else ""

        if not content and not result.tool_calls:
            raise ProviderFailure("empty_response")

        if result.tool_calls and self._prior_tool_rounds(messages) >= MAX_TOOL_ROUNDS:
            raise ProviderFailure("tool_round_exhausted")

        return result
