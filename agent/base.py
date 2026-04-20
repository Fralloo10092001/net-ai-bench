from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


# Contiene il risultato standard prodotto da qualunque agente dopo un run
@dataclass
class RunResult:
    history: list[dict[str, Any]]
    final_reasoning: str
    mode: str


# Definisce l'interfaccia astratta comune che ogni architettura agente deve implementare
class Agent(ABC):
    # Esegue l'agente su una sessione MCP e restituisce un RunResult uniforme
    @abstractmethod
    async def run(self, session, task_text: str, max_steps: int) -> RunResult:
        raise NotImplementedError