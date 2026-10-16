"""Integration cases use fresh databases; existing test or user data is never reset."""

import os
from uuid import uuid4

import pytest
from psycopg import sql

from decagent.storage.database import Database


@pytest.fixture
def database():
    url = os.getenv("DECAGENT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set DECAGENT_TEST_DATABASE_URL to a disposable dedicated database")
    administrator = Database(url)
    name = "case_" + uuid4().hex
    with administrator.connect(autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    database = Database(url.rsplit("/", 1)[0] + "/" + name)
    database.migrate()
    return database
