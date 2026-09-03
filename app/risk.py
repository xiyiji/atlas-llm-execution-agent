"""Deterministic risk floor combined with Safety-agent judgment."""

from __future__ import annotations

import re

from . import config
from .models import PlanStep, RiskAssessment, RiskLevel


_HEURISTICS: tuple[tuple[re.Pattern[str], int, str], ...] = (
    (re.compile(r"\b(rm\s+-rf|wipe|destroy|drop\s+(table|database))\b", re.I), 95, "destructive system or data operation"),
    (re.compile(r"\b(delete|erase|remove|purge)\b", re.I), 62, "data deletion or removal"),
    (re.compile(r"\b(password|secret|api[ -]?key|credential|token)\b", re.I), 72, "sensitive credentials"),
    (re.compile(r"\b(payment|wire|transfer|purchase|trade|bank)\b", re.I), 76, "financial transaction"),
    (re.compile(r"\b(production|prod\s+database|live\s+system)\b", re.I), 38, "production system impact"),
    (re.compile(r"\b(email|message|publish|post|send)\b", re.I), 32, "external communication"),
    (re.compile(r"\b(medical|diagnos|prescription|legal advice)\b", re.I), 65, "high-stakes advice"),
)


def heuristic_score(goal: str, steps: list[PlanStep]) -> tuple[int, list[str]]:
    text = " ".join([goal, *(f"{step.title} {step.detail}" for step in steps)])
    score = 0
    factors: list[str] = []
    for pattern, weight, factor in _HEURISTICS:
        if pattern.search(text):
            score = min(100, max(score, weight) + (8 if score else 0))
            factors.append(factor)
    if not factors:
        factors.append("No elevated-risk signals; read-only workload")
    return min(score, 100), list(dict.fromkeys(factors))


def merge(goal: str, steps: list[PlanStep], agent_score: int, agent_factors: list[str]) -> RiskAssessment:
    local_score, local_factors = heuristic_score(goal, steps)
    safe_agent_score = max(0, min(100, int(agent_score)))
    score = max(local_score, safe_agent_score)
    factors = list(dict.fromkeys([*local_factors, *(str(item) for item in agent_factors if item)]))
    if score >= 85:
        level = RiskLevel.CRITICAL
    elif score >= config.APPROVAL_THRESHOLD:
        level = RiskLevel.HIGH
    elif score >= 30:
        level = RiskLevel.MEDIUM
    else:
        level = RiskLevel.LOW
    return RiskAssessment(
        score=score,
        level=level,
        factors=factors,
        requires_approval=score >= config.APPROVAL_THRESHOLD,
        heuristic_score=local_score,
        agent_score=safe_agent_score,
    )
