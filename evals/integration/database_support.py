"""Helpers for isolated PostgreSQL databases used by integration tests."""

from contextlib import contextmanager
from uuid import uuid4

from psycopg import sql
from psycopg.conninfo import make_conninfo

from decagent.storage.database import Database


@contextmanager
def temporary_database(url, *, prefix="case_"):
    administrator = Database(url)
    name = prefix + uuid4().hex
    with administrator.connect(autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))

    try:
        yield Database(make_conninfo(url, dbname=name))
    finally:
        with administrator.connect(autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )
