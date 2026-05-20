from abc import ABC, abstractmethod
from typing import Any


# Definisce l'interfaccia astratta comune a tutti i driver dei diversi NOS
class BaseDriver(ABC):
    # Restituisce uno snapshot testuale dello stato corrente del dispositivo
    @abstractmethod
    def get_running_config(self, device: str) -> dict:
        pass

    # Applica una lista di comandi di configurazione al dispositivo
    @abstractmethod
    def update_config(self, device: str, commands: list[str]) -> dict:
        pass

    # Esegue un comando di validazione/read-only sul dispositivo
    @abstractmethod
    def execute_validation(self, device: str, command: str) -> dict:
        pass

    # Cattura uno snapshot strutturato dello stato utile per un eventuale rollback
    @abstractmethod
    def capture_rollback_state(self, device: str) -> dict[str, Any]:
        pass

    # Ripristina uno snapshot precedentemente catturato
    @abstractmethod
    def restore_rollback_state(self, device: str, snapshot: dict[str, Any]) -> dict:
        pass
