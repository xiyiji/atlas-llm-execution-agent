"""Transactional persistence for tasks, events, and episodic memory.

SQLite is the zero-configuration default; PostgreSQL is selected by setting
``DATABASE_URL``. The rest of the application only depends on this repository.
"""

from __future__ import annotations

import json
import threading
import uuid

from sqlalchemy import Float, Index, String, Text, create_engine, delete, inspect, select
from sqlalchemy import event as sa_event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from . import config
from .models import Event, Task


class Base(DeclarativeBase):
    pass


class TaskRecord(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False, index=True)


class EventRecord(Base):
    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    ts: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(80), nullable=False)
    agent: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    data: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    __table_args__ = (Index("ix_events_tenant_task_ts", "tenant_id", "task_id", "ts"),)


class EpisodicRecord(Base):
    __tablename__ = "episodic_memory"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    ts: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(String(40), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("ix_memory_tenant_ts", "tenant_id", "ts"),)


class Store:
    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = database_url or config.DATABASE_URL
        connect_args = {"check_same_thread": False, "timeout": 30} if self.database_url.startswith("sqlite") else {}
        self.engine = create_engine(
            self.database_url,
            pool_pre_ping=True,
            connect_args=connect_args,
        )
        if self.database_url.startswith("sqlite"):
            sa_event.listen(self.engine, "connect", self._sqlite_pragmas)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self._init_lock = threading.Lock()
        self._initialized = False

    @staticmethod
    def _sqlite_pragmas(connection, _record) -> None:
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=30000")
        except Exception:
            pass
        finally:
            cursor.close()

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if not self._initialized:
                if config.ENVIRONMENT == "production":
                    if not inspect(self.engine).has_table("tasks"):
                        raise RuntimeError("Database schema is missing; run `alembic upgrade head`")
                else:
                    Base.metadata.create_all(self.engine)
                self._initialized = True

    def healthcheck(self) -> bool:
        self.initialize()
        with self.sessions() as session:
            session.execute(select(1))
        return True

    def save_task(self, task: Task) -> None:
        self.initialize()
        payload = task.model_dump_json()
        with self.sessions.begin() as session:
            record = session.get(TaskRecord, task.id)
            if record is None:
                record = TaskRecord(
                    id=task.id,
                    tenant_id=task.tenant_id,
                    status=task.status.value,
                    payload=payload,
                    created_at=task.created_at,
                    updated_at=task.updated_at,
                )
                session.add(record)
            else:
                record.tenant_id = task.tenant_id
                record.status = task.status.value
                record.payload = payload
                record.updated_at = task.updated_at

    def get_task(self, task_id: str, tenant_id: str | None = None) -> Task | None:
        self.initialize()
        with self.sessions() as session:
            record = session.get(TaskRecord, task_id)
            if record is None or (tenant_id is not None and record.tenant_id != tenant_id):
                return None
            return Task.model_validate_json(record.payload)

    def list_tasks(self, tenant_id: str | None = None, statuses: list[str] | None = None, limit: int = 50) -> list[Task]:
        self.initialize()
        statement = select(TaskRecord).order_by(TaskRecord.updated_at.desc()).limit(limit)
        if tenant_id is not None:
            statement = statement.where(TaskRecord.tenant_id == tenant_id)
        if statuses:
            statement = statement.where(TaskRecord.status.in_(statuses))
        with self.sessions() as session:
            return [Task.model_validate_json(row.payload) for row in session.scalars(statement)]

    def save_event(self, event: Event) -> None:
        self.initialize()
        with self.sessions.begin() as session:
            if session.get(EventRecord, event.id) is None:
                session.add(EventRecord(
                    id=event.id,
                    task_id=event.task_id,
                    tenant_id=event.tenant_id,
                    ts=event.ts,
                    type=event.type,
                    agent=event.agent,
                    message=event.message,
                    data=json.dumps(event.data, ensure_ascii=False),
                ))

    def events_after(self, task_id: str, tenant_id: str, ts: float = 0.0, limit: int = 500) -> list[dict]:
        self.initialize()
        statement = (
            select(EventRecord)
            .where(EventRecord.task_id == task_id, EventRecord.tenant_id == tenant_id, EventRecord.ts > ts)
            .order_by(EventRecord.ts.asc())
            .limit(limit)
        )
        with self.sessions() as session:
            return [self._event_dict(row) for row in session.scalars(statement)]

    def audit_tail(self, tenant_id: str, limit: int = 200) -> list[dict]:
        self.initialize()
        statement = (
            select(EventRecord)
            .where(EventRecord.tenant_id == tenant_id)
            .order_by(EventRecord.ts.desc())
            .limit(limit)
        )
        with self.sessions() as session:
            return [self._event_dict(row) for row in reversed(list(session.scalars(statement)))]

    @staticmethod
    def _event_dict(row: EventRecord) -> dict:
        return {
            "id": row.id,
            "task_id": row.task_id,
            "tenant_id": row.tenant_id,
            "ts": row.ts,
            "type": row.type,
            "agent": row.agent,
            "message": row.message,
            "data": json.loads(row.data),
        }

    def store_memory(self, tenant_id: str, ts: float, goal: str, outcome: str, summary: str) -> None:
        self.initialize()
        with self.sessions.begin() as session:
            session.add(EpisodicRecord(
                id=f"mem_{uuid.uuid4().hex[:12]}",
                tenant_id=tenant_id,
                ts=ts,
                goal=goal,
                outcome=outcome,
                summary=summary,
            ))
            stale_ids = list(session.scalars(
                select(EpisodicRecord.id)
                .where(EpisodicRecord.tenant_id == tenant_id)
                .order_by(EpisodicRecord.ts.desc())
                .offset(100)
            ))
            if stale_ids:
                session.execute(delete(EpisodicRecord).where(EpisodicRecord.id.in_(stale_ids)))

    def recall_memory(self, tenant_id: str, limit: int = 5) -> list[dict]:
        self.initialize()
        statement = (
            select(EpisodicRecord)
            .where(EpisodicRecord.tenant_id == tenant_id)
            .order_by(EpisodicRecord.ts.desc())
            .limit(limit)
        )
        with self.sessions() as session:
            rows = list(reversed(list(session.scalars(statement))))
            return [{"ts": row.ts, "goal": row.goal, "outcome": row.outcome, "summary": row.summary} for row in rows]


STORE = Store()
