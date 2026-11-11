import os

import psycopg
from psycopg.rows import dict_row


def get_db():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError(
            "DATABASE_URL is not set. Configure it with the Supabase "
            "PostgreSQL connection string."
        )
    return psycopg.connect(database_url, row_factory=dict_row)
