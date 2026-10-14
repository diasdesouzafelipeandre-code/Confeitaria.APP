import sqlite3
from confeitaria import core

SCHEMA_VERSION = 1


def _apply_migrations(con):
    current_version = con.execute("PRAGMA user_version").fetchone()[0]
    if current_version > SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema version {current_version} is newer than supported "
            f"version {SCHEMA_VERSION}"
        )

    if current_version < 1:
        columns = {
            row[1] for row in con.execute("PRAGMA table_info(itens_receita)")
        }
        if "unidade" not in columns:
            con.execute("ALTER TABLE itens_receita ADD COLUMN unidade TEXT")
        con.execute("PRAGMA user_version = 1")
        con.commit()


def get_db(db_path=core.DB):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        con.executescript(core.SCHEMA)
        _apply_migrations(con)
    except Exception:
        con.close()
        raise
    return con


def row_to_dict(row):
    return {k: row[k] for k in row.keys()}
