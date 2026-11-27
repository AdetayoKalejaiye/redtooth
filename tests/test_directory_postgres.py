"""Runs against a real Postgres when REDTOOTH_TEST_DATABASE_URL is set (skipped otherwise)."""
import os

import pytest

from redtooth.directory import InMemoryDirectory, PostgresStore

URL = os.environ.get("REDTOOTH_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="set REDTOOTH_TEST_DATABASE_URL to test against Postgres")


def test_registrations_survive_a_restart():
    import psycopg

    with psycopg.connect(URL, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS redtooth_directory")
    store = PostgresStore(URL)
    d = InMemoryDirectory(store=store)
    tok = d.claim("+14155550199", "Sam")
    assert tok and d.claim("+1 415 555 0199", "Mallory") is None
    store.flush()
    again = InMemoryDirectory(store=PostgresStore(URL))       # a new process, empty disk
    assert again.claim("+14155550199", "Mallory") is None    # still taken
    assert again.tokens[tok] == "+14155550199"               # Sam's device still signed in
