"""Integration cases use fresh databases; existing test or user data is never reset."""

import os

import pytest

from evals.integration.database_support import temporary_database


@pytest.fixture
def database():
    url = os.getenv("DECAGENT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set DECAGENT_TEST_DATABASE_URL to a disposable dedicated database")
    with temporary_database(url) as database:
        database.migrate()
        yield database
