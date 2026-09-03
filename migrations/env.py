from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app import config as app_config
from app.storage import Base


configuration = context.config
configuration.set_main_option("sqlalchemy.url", app_config.DATABASE_URL)
if configuration.config_file_name:
    fileConfig(configuration.config_file_name)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=app_config.DATABASE_URL, target_metadata=target_metadata, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(configuration.get_section(configuration.config_ini_section), prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_offline() if context.is_offline_mode() else run_migrations_online()
