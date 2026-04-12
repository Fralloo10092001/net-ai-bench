from abc import ABC, abstractmethod


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