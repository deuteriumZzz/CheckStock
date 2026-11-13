import os

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql+psycopg://checkstock:checkstock@localhost:5433/checkstock"
)
