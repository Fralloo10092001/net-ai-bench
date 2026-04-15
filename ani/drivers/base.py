from abc import ABC, abstractmethod

#Definisce l’interfaccia astratta comune a tutti i driver
#Questo aumenta l’estensibilità lato NOS: nuovo driver → stessa interfaccia
#MCP server non cambia
class BaseDriver(ABC):
    @abstractmethod
    def get_running_config(self, device: str) -> dict:
        pass

    @abstractmethod
    def update_config(self, device: str, commands: list[str]) -> dict:
        pass

    @abstractmethod
    def execute_validation(self, device: str, command: str) -> dict:
        pass