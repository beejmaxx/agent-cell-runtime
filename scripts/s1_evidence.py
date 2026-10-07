"""Pytest evidence export before each in-process runtime/database is torn down."""

import json
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    live = item.funcargs.get("live")
    if report.when != "call" or live is None:
        return
    table = live.db.executions
    columns = [
        "id",
        "status",
        "failure_reason",
        "workload_name",
        "workload_uid",
        "created_at",
        "deadline_at",
        "finished_at",
        "result",
    ]
    unavailable = False
    try:
        with live.db.engine.connect() as conn:
            rows = [
                dict(row) for row in conn.execute(select(*(table.c[k] for k in columns))).mappings()
            ]
    except SQLAlchemyError:
        rows, unavailable = [], True
    directory = Path(".local/s1/evidence/database")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / (item.name + ".json")).write_text(
        json.dumps(
            {
                "test": item.nodeid,
                "outcome": report.outcome,
                "rows": rows,
                "database_unavailable": unavailable,
            },
            default=str,
            indent=2,
        )
    )
