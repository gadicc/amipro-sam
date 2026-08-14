from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from time import monotonic

from .constants import EXIT_BACKEND
from .errors import OracleError


@dataclass
class StateMachine:
    initial: str
    terminal: frozenset[str]
    transitions: Mapping[str, frozenset[str]]
    state: str = field(init=False)
    trace: list[dict[str, object]] = field(default_factory=list, init=False)
    _started: float = field(default_factory=monotonic, init=False, repr=False)

    def __post_init__(self) -> None:
        self.state = self.initial
        self.trace.append({"state": self.state, "elapsed_seconds": 0.0})

    def advance(self, next_state: str, *, evidence: str | None = None) -> None:
        allowed = self.transitions.get(self.state, frozenset())
        if next_state not in allowed:
            raise OracleError(
                f"invalid state transition {self.state!r} -> {next_state!r}",
                exit_code=EXIT_BACKEND,
            )
        self.state = next_state
        event: dict[str, object] = {
            "state": next_state,
            "elapsed_seconds": round(monotonic() - self._started, 6),
        }
        if evidence is not None:
            event["evidence"] = evidence
        self.trace.append(event)

    @property
    def complete(self) -> bool:
        return self.state in self.terminal
