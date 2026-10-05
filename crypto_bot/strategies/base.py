from typing import Protocol


class Strategy(Protocol):
    def evaluate(self, closes: list[float]) -> dict: ...
