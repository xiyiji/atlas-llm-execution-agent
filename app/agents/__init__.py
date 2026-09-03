"""Committee composition."""

from .base import Agent
from .browser import Browser
from .coder import Coder
from .planner import Planner
from .safety import Safety
from .verifier import Verifier

COMMITTEE: dict[str, Agent] = {
    "planner": Planner(),
    "safety": Safety(),
    "coder": Coder(),
    "browser": Browser(),
    "verifier": Verifier(),
}

__all__ = ["COMMITTEE"]
