from __future__ import annotations

import random
from typing import Protocol


class RandomSource(Protocol):
    @property
    def seed(self) -> int: ...

    def random(self) -> float: ...

    def uniform(self, lower: float, upper: float) -> float: ...


class SeededRandomSource:
    def __init__(self, seed: int) -> None:
        self._seed = seed
        self._random = random.Random(seed)

    @property
    def seed(self) -> int:
        return self._seed

    def random(self) -> float:
        return self._random.random()

    def uniform(self, lower: float, upper: float) -> float:
        return self._random.uniform(lower, upper)

