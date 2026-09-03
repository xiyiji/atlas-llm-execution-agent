"""Initial durable task, event, and memory schema."""

from alembic import op
import sqlalchemy as sa


revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
    )
    op.create_index("ix_tasks_tenant_id", "tasks", ["tenant_id"])
    op.create_index("ix_tasks_status", "tasks", ["status"])
    op.create_index("ix_tasks_updated_at", "tasks", ["updated_at"])
    op.create_table(
        "events",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("task_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("ts", sa.Float(), nullable=False),
        sa.Column("type", sa.String(80), nullable=False),
        sa.Column("agent", sa.String(40), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("data", sa.Text(), nullable=False),
    )
    op.create_index("ix_events_task_id", "events", ["task_id"])
    op.create_index("ix_events_tenant_id", "events", ["tenant_id"])
    op.create_index("ix_events_ts", "events", ["ts"])
    op.create_index("ix_events_tenant_task_ts", "events", ["tenant_id", "task_id", "ts"])
    op.create_table(
        "episodic_memory",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("ts", sa.Float(), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("outcome", sa.String(40), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
    )
    op.create_index("ix_episodic_memory_tenant_id", "episodic_memory", ["tenant_id"])
    op.create_index("ix_episodic_memory_ts", "episodic_memory", ["ts"])
    op.create_index("ix_memory_tenant_ts", "episodic_memory", ["tenant_id", "ts"])


def downgrade() -> None:
    op.drop_table("episodic_memory")
    op.drop_table("events")
    op.drop_table("tasks")
