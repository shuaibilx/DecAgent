"""Helpers for isolated PostgreSQL databases used by integration tests."""

from contextlib import contextmanager
from uuid import uuid4

from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from decagent.storage.database import Database


def database_name(database):
    """Return the database name regardless of whether its URL is URI or keyword DSN form."""
    return conninfo_to_dict(database.url)["dbname"]


@contextmanager
def temporary_database(url, *, prefix="case_", template=None):
    administrator = Database(url)
    name = prefix + uuid4().hex
    with administrator.connect(autocommit=True) as connection:
        create = sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name))
        if template is not None:
            template_name = conninfo_to_dict(template.url)["dbname"]
            create += sql.SQL(" TEMPLATE {}").format(sql.Identifier(template_name))
        connection.execute(create)

    try:
        yield Database(make_conninfo(url, dbname=name))
    finally:
        with administrator.connect(autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )
