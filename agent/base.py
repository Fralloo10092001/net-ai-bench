from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RunResult:
    history: list[dict[str, Any]]
    final_reasoning: str
    mode: str
    usage: dict[str, int] = field(
        default_factory=lambda: {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
    )
    usage_breakdown: dict[str, dict[str, int]] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


class Agent(ABC):
    @abstractmethod
    async def run(self, session, task_text: str, max_steps: int) -> RunResult:
        raise NotImplementedError