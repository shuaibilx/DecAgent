"""The shared PostgreSQL fixture must remove each test database after use."""

import inspect
import os

import pytest
from conftest import database as database_fixture
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from decagent.storage.database import Database
from evals.integration.database_support import temporary_database

pytestmark = pytest.mark.integration


def database_exists(administrator, name):
    with administrator.connect() as connection:
        return connection.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname=%s) AS present",
            (name,),
        ).fetchone()["present"]


def drop_database(administrator, name):
    with administrator.connect(autocommit=True) as connection:
        connection.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )


def test_database_fixture_drops_its_database_after_teardown():
    url = os.getenv("DECAGENT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set DECAGENT_TEST_DATABASE_URL to a disposable dedicated database")

    administrator = Database(url)
    with temporary_database(url, prefix="fixture_template_") as template:
        template.migrate()
        result = database_fixture.__wrapped__(template)
        if not inspect.isgenerator(result):
            # Keep the RED test isolated even before the fixture gains teardown.
            drop_database(administrator, conninfo_to_dict(result.url)["dbname"])
            pytest.fail("The database fixture must yield its database and clean it up afterward")

        try:
            database = next(result)
            name = conninfo_to_dict(database.url)["dbname"]
            assert database_exists(administrator, name)
            with database.connect() as connection:
                assert connection.execute(
                    "SELECT count(*) AS n FROM app.schema_migrations"
                ).fetchone()["n"] > 0
        finally:
            result.close()

        assert not database_exists(administrator, name)
