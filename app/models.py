"""Shared domain models for Atlas.

This module intentionally has no dependencies on other Atlas modules.
"""

from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def new_id(prefix: str) -> str:
    """Return a compact, prefixed identifier suitable for logs and URLs."""
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class TaskStatus(str, Enum):
    QUEUED = "queued"
    PLANNING = "planning"
    ASSESSING_RISK = "assessing_risk"
    AWAITING_APPROVAL = "awaiting_approval"
    RUNNING = "running"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    DENIED = "denied"


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class PlanStep(BaseModel):
    id: str = Field(default_factory=lambda: new_id("step"))
    title: str
    agent: str
    detail: str = ""
    status: StepStatus = StepStatus.PENDING
    output: str = ""
    attempts: int = 0
    error: str = ""


class RiskAssessment(BaseModel):
    score: int = Field(ge=0, le=100)
    level: RiskLevel
    factors: list[str] = Field(default_factory=list)
    requires_approval: bool = False
    heuristic_score: int = Field(default=0, ge=0, le=100)
    agent_score: int = Field(default=0, ge=0, le=100)


class Task(BaseModel):
    id: str = Field(default_factory=lambda: new_id("task"))
    goal: str
    tenant_id: str = "default"
    auto_approve: bool = False
    status: TaskStatus = TaskStatus.QUEUED
    steps: list[PlanStep] = Field(default_factory=list)
    risk: RiskAssessment | None = None
    result: str = ""
    error: str = ""
    verified: bool | None = None
    verification: str = ""
    rework_count: int = 0
    approval_decision: bool | None = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class CreateTaskRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=8000)
    auto_approve: bool = False


class ApprovalRequest(BaseModel):
    approved: bool


class Event(BaseModel):
    id: str = Field(default_factory=lambda: new_id("evt"))
    task_id: str
    tenant_id: str = "default"
    ts: float = Field(default_factory=time.time)
    type: str
    agent: str = ""
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
