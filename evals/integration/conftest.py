"""Integration cases use fresh databases; existing test or user data is never reset."""

import os

import pytest

from evals.integration.database_support import temporary_database


@pytest.fixture
def database(migrated_database_template):
    url = os.getenv("DECAGENT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set DECAGENT_TEST_DATABASE_URL to a disposable dedicated database")
    with temporary_database(url, template=migrated_database_template) as database:
        yield database


@pytest.fixture(scope="session")
def migrated_database_template():
    url = os.getenv("DECAGENT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set DECAGENT_TEST_DATABASE_URL to a disposable dedicated database")
    with temporary_database(url, prefix="template_") as template:
        template.migrate()
        yield template
