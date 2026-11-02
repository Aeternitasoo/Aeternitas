"""Real PostgreSQL integration; opt in with AETERNITAS_TEST_DATABASE.

Every test owns a fresh isolated schema; it never truncates project tables.
"""

import json
import os
from datetime import date
from decimal import Decimal
from uuid import uuid4

import httpx
import psycopg
import pytest
from psycopg import sql

import evidence_repository as repository
from research_inputs import collect_inputs
from research_runtime import Budget
from research_test_support import CONFIG, transports
from test_financial_facts import make_facts


DSN=os.environ.get("AETERNITAS_TEST_DATABASE")
pytestmark=pytest.mark.skipif(not DSN,reason="Real PostgreSQL tests require explicit AETERNITAS_TEST_DATABASE")
TABLES=("companies","source_snapshots","retrievals","filings","fact_observations","fact_locations",
        "datasets","dataset_sources","dataset_facts","calculations","calculation_inputs","evidence_versions","dataset_evidence")


@pytest.fixture
def database():
    schema="m2_test_"+uuid4().hex
    with psycopg.connect(DSN,autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        with repository.connect(DSN,schema=schema) as connection:
            repository.migrate(connection)
            yield connection
    finally:
        with psycopg.connect(DSN,autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def counts(connection):
    return {table:connection.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(table))).fetchone()["n"] for table in TABLES}


def bundle(root,raw=None):
    transport,_=transports()
    if raw is not None:
        encoded=json.dumps(raw).replace('"decimal-test-value"','120.123456789012345678901234567890123456789')
        transport["sec"]=httpx.MockTransport(lambda _:httpx.Response(200,content=encoded,headers={"content-type":"application/json"}))
    return collect_inputs(root,"AAPL","0000320193",date(2025,10,1),"Synthetic",CONFIG,
                          refresh=True,news_days=14,budget=Budget(),transports=transport,evidence_kind="controlled_test")


def test_atomic_import_round_trip_and_duplicate_import(database,tmp_path):
    data=bundle(tmp_path)
    identity=repository.import_bundle(database,tmp_path,data)
    before=counts(database)
    assert repository.import_bundle(database,tmp_path,data)==identity
    assert counts(database)==before
    facts=repository.facts(database,"AAPL",identity)
    assert len(facts)==6 and all(isinstance(f["value"],Decimal) for f in facts)
    assert any(f["value"]==0 for f in facts) and any(f["value"]<0 for f in facts)
    assert all(f["json_pointer"].startswith("/facts/us-gaap/") for f in facts)
    assert len(repository.calculations(database,identity))==5
    eid=data["evidence"][-1]["id"]
    assert repository.evidence(database,identity,eid)==data["evidence"][-1]
    repository.migrate(database)  # Migration checksums make repeat init safe.
    assert len(repository.list_datasets(database))==1


def test_refresh_unchanged_bytes_keeps_observations_and_new_retrievals(database,tmp_path):
    first=repository.import_bundle(database,tmp_path,bundle(tmp_path))
    before=counts(database)
    second=repository.import_bundle(database,tmp_path,bundle(tmp_path))
    after=counts(database)
    assert first!=second and before["fact_observations"]==after["fact_observations"]==6
    assert before["fact_locations"]==after["fact_locations"]==6
    assert after["retrievals"]>before["retrievals"] and after["datasets"]==2


def test_restatement_keeps_original_dataset_and_observation(database,tmp_path):
    first=repository.import_bundle(database,tmp_path,bundle(tmp_path))
    raw=make_facts()
    rows=raw["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"]["units"]["USD"]
    rows.append({**rows[-1],"val":130,"accn":"0000320193-25-000003","filed":"2025-05-01"})
    second=repository.import_bundle(database,tmp_path,bundle(tmp_path,raw))
    def revenue(dataset):
        return next(f["value"] for f in repository.facts(database,"AAPL",dataset) if f["metric"]=="revenue" and f["period_end"]==date(2024,12,31))
    assert revenue(first)==120 and revenue(second)==130
    assert counts(database)["fact_observations"]==7


def test_changed_value_in_identical_filing_is_rejected_atomically(database,tmp_path):
    repository.import_bundle(database,tmp_path,bundle(tmp_path))
    before=counts(database)
    raw=make_facts();raw["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"]["units"]["USD"][-1]["val"]=999
    with pytest.raises(ValueError,match="changed value"):
        repository.import_bundle(database,tmp_path,bundle(tmp_path,raw))
    assert counts(database)==before


def test_mid_import_database_failure_rolls_back_every_domain_table(database,tmp_path):
    data=bundle(tmp_path)
    database.execute("CREATE FUNCTION fail_evidence() RETURNS trigger LANGUAGE plpgsql AS $$ "
                     "BEGIN RAISE EXCEPTION 'synthetic mid-import failure' USING ERRCODE='23514'; END $$")
    database.execute("CREATE TRIGGER injected_failure BEFORE INSERT ON evidence_versions FOR EACH ROW EXECUTE FUNCTION fail_evidence()")
    with pytest.raises(psycopg.errors.CheckViolation):repository.import_bundle(database,tmp_path,data)
    assert all(n==0 for n in counts(database).values())
    database.execute("DROP TRIGGER injected_failure ON evidence_versions")
    assert repository.import_bundle(database,tmp_path,data)
    assert counts(database)["fact_observations"]==6


def test_real_unique_not_null_foreign_key_and_period_constraints(database,tmp_path):
    repository.import_bundle(database,tmp_path,bundle(tmp_path))
    for statement,params,error in [
        ("INSERT INTO companies VALUES (%s,%s,%s)",("0000320193","OTHER","Synthetic"),psycopg.errors.UniqueViolation),
        ("INSERT INTO companies VALUES (%s,%s,%s)",("0000000001","OTHER",None),psycopg.errors.NotNullViolation),
        ("INSERT INTO filings VALUES (%s,%s,%s,%s,%s)",("0000000001","0000000001-25-000001","10-K","2025-01-01",None),psycopg.errors.ForeignKeyViolation),
        ("UPDATE fact_observations SET period_end=period_start",(),psycopg.errors.CheckViolation),
        ("UPDATE fact_observations SET value='NaN'::numeric",(),psycopg.errors.CheckViolation),
        ("UPDATE fact_observations SET value=NULL",(),psycopg.errors.NotNullViolation),
    ]:
        with pytest.raises(error):
            with database.transaction():database.execute(statement,params)
    assert counts(database)["fact_observations"]==6


def test_missing_metric_is_absent_and_unavailable_calculation_is_not_zero(database,tmp_path):
    raw=make_facts()
    raw["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"].pop()
    dataset=repository.import_bundle(database,tmp_path,bundle(tmp_path,raw))
    selected=repository.facts(database,"AAPL",dataset)
    assert len(selected)==5 and any(f["value"]==0 for f in selected)
    assert not any(f["metric"]=="net_income" and f["period_end"]==date(2024,12,31) for f in selected)
    missing=next(c for c in repository.calculations(database,dataset) if c["title"]=="net_margin:2024-12-31")
    assert missing["value"] is None and missing["unavailable_reason"]=="missing_input"


def test_exact_decimal_round_trip_without_fixed_scale_rounding(database,tmp_path):
    raw=make_facts();raw["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"]["units"]["USD"][-1]["val"]="decimal-test-value"
    identity=repository.import_bundle(database,tmp_path,bundle(tmp_path,raw))
    current=next(f for f in repository.facts(database,"AAPL",identity) if f["metric"]=="revenue" and f["period_end"]==date(2024,12,31))
    assert current["value"]==Decimal("120.123456789012345678901234567890123456789")


def test_sql_cannot_attach_another_companys_facts_or_evidence(database,tmp_path):
    first=repository.import_bundle(database,tmp_path,bundle(tmp_path))
    database.execute("INSERT INTO companies VALUES ('0000789019','MSFT','Synthetic other company')")
    second="a"*64
    database.execute("INSERT INTO datasets(dataset_id,cik,as_of_date,evidence_kind,parser_version,bundle_sha256) "
                     "VALUES (%s,'0000789019','2025-10-01','controlled_test','synthetic',%s)",(second,second))
    fact=database.execute("SELECT * FROM dataset_facts WHERE dataset_id=%s LIMIT 1",(first,)).fetchone()
    entry=database.execute("SELECT * FROM dataset_evidence WHERE dataset_id=%s LIMIT 1",(first,)).fetchone()
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with database.transaction():
            database.execute("INSERT INTO dataset_facts VALUES (%s,%s,%s,%s,'0000789019')",
                             (second,fact["fact_id"],fact["metric"],fact["period_end"]))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with database.transaction():
            database.execute("INSERT INTO dataset_evidence VALUES (%s,%s,%s,'0000789019')",
                             (second,entry["evidence_id"],entry["version_id"]))


def test_query_parameters_cannot_become_sql(database,tmp_path):
    identity=repository.import_bundle(database,tmp_path,bundle(tmp_path))
    with pytest.raises(ValueError):repository.facts(database,"AAPL'; DROP TABLE companies; --")
    with pytest.raises(ValueError):repository.evidence(database,identity,"x' OR '1'='1")
    assert counts(database)["companies"]==1


def test_research_imports_domain_data_before_models_without_holding_transaction(database,tmp_path,monkeypatch):
    from contextlib import contextmanager
    from research_pipeline import run_research,saved_report
    from psycopg.pq import TransactionStatus
    transport,calls=transports()
    original=transport["model"]
    @contextmanager
    def existing_connection(_):
        yield database
    def model(request):
        assert database.info.transaction_status==TransactionStatus.IDLE
        assert counts(database)["datasets"]==1
        return original.handle_request(request)
    monkeypatch.setattr(repository,"connect",existing_connection)
    transport["model"]=httpx.MockTransport(model)
    run_id,report=run_research(tmp_path,"AAPL","0000320193",date(2025,10,1),"Synthetic",
                              {**CONFIG,"DATABASE_URL":"synthetic-connection-target"},transports=transport,
                              evidence_kind="controlled_test")
    assert len(calls["model"])==4 and report["dataset_id"]
    assert len(repository.facts(database,"AAPL",report["dataset_id"]))==6
    saved_report(tmp_path,run_id)
