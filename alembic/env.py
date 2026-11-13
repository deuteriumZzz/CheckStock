from sqlalchemy import create_engine, pool

from alembic import context
from app.config import DATABASE_URL
from app.models import Base

config = context.config
# Тесты подставляют свой URL через sqlalchemy.url; иначе берём DATABASE_URL.
url = config.get_main_option("sqlalchemy.url") or DATABASE_URL

# NullPool: соединение закрывается сразу, без «висящих» подключений после миграций.
with create_engine(url, poolclass=pool.NullPool).connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()
