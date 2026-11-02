"""M6 controlled source matrix; no external service calls."""

import os
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest

import evidence_repository as repository
from scripts.audit_m6_sources import controlled_campaign, history_coverage_campaign


def test_complete_cache_refresh_update_and_failure_campaign(tmp_path):
    result=controlled_campaign(tmp_path)
    assert [s["transport_requests"] for s in result["stages"]] == [5,0,5,5,5]
    assert len(result["failures"]) == 3
    assert result["future_restatement_and_next_Shanghai_day_news_excluded"]
    assert result["external_model_requests"] == result["external_source_requests"] == 0


def test_real_directory_history_and_insufficient_coverage(tmp_path):
    result=history_coverage_campaign(tmp_path)
    assert result["historical_directory_requested"]
    assert result["zero_news_state"] == "no_results_returned"


@pytest.mark.skipif(not os.environ.get("AETERNITAS_TEST_DATABASE"),reason="Requires explicit isolated real PostgreSQL")
def test_update_and_bad_response_preserve_historical_SQL_and_files(tmp_path):
    dsn=os.environ["AETERNITAS_TEST_DATABASE"]
    schema="m6_source_test_"+uuid4().hex
    with psycopg.connect(dsn,autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        with repository.connect(dsn,schema=schema) as connection:
            repository.migrate(connection)
            result=controlled_campaign(tmp_path,connection)
            assert result["database"]["old_facts_and_news_unchanged"]
            assert result["database"]["duplicate_import_idempotent"]
    finally:
        with psycopg.connect(dsn,autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
