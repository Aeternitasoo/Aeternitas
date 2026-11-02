"""M2 domain storage. Network/file validation finishes before a short SQL transaction."""

import hashlib
import json
import re
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from sources.sec import SourceError


DEFAULT_DATABASE = "dbname=aeternitas host=/var/run/postgresql"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def snapshot_id(cik, source):
    return digest([cik, source["source_url"], source.get("query", {}), source["sha256"]])


def bundle_identity(bundle):
    # The caller supplies a source bundle, never analysis/report publication data.
    return digest(bundle)


def json_value(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError("Unsupported database output")


@contextmanager
def connect(dsn=DEFAULT_DATABASE, schema="public"):
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema):
        raise ValueError("Invalid database schema")
    try:
        with psycopg.connect(dsn, connect_timeout=5, autocommit=True, row_factory=dict_row) as connection:
            connection.execute(sql.SQL("SET search_path TO {}, pg_catalog").format(sql.Identifier(schema)))
            connection.execute("SET statement_timeout = '15s'")
            yield connection
    except psycopg.Error as exc:
        # DSNs, SQL errors and connection details may contain credentials/data.
        raise SourceError(f"Evidence database operation failed ({type(exc).__name__}; "
                          f"sqlstate={exc.sqlstate or 'connection'}); no model/API fallback") from None


def migrate(connection):
    migrations = Path(__file__).parent / "migrations"
    with connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(71002102)")
        connection.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                           "(name text PRIMARY KEY, sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())")
        for path in sorted(migrations.glob("*.sql")):
            raw = path.read_bytes()
            checksum = hashlib.sha256(raw).hexdigest()
            old = connection.execute("SELECT sha256 FROM schema_migrations WHERE name=%s", (path.name,)).fetchone()
            if old:
                if old["sha256"] != checksum:
                    raise SourceError("Applied migration checksum differs; create a new migration")
                continue
            connection.execute(raw.decode())
            connection.execute("INSERT INTO schema_migrations(name,sha256) VALUES (%s,%s)", (path.name, checksum))


def import_bundle(connection, root, bundle):
    from research_pipeline import audit_inputs
    audit_inputs(root, bundle)  # No transaction held while reading/validating sources.
    dataset = bundle_identity(bundle)
    cik, ticker = bundle["cik"], bundle["ticker"]
    with connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(71002103)")
        old = connection.execute("SELECT dataset_id FROM datasets WHERE dataset_id=%s", (dataset,)).fetchone()
        if old:
            return dataset
        company = connection.execute("SELECT ticker FROM companies WHERE cik=%s", (cik,)).fetchone()
        if company and company["ticker"] != ticker:
            raise ValueError("Company alias differs from stored identity")
        connection.execute("INSERT INTO companies(cik,ticker,name) VALUES (%s,%s,%s) ON CONFLICT(cik) DO NOTHING",
                           (cik,ticker,bundle["financials"]["company_name"]))
        connection.execute("INSERT INTO datasets(dataset_id,cik,as_of_date,evidence_kind,parser_version,bundle_sha256) "
                           "VALUES (%s,%s,%s,%s,%s,%s)",
                           (dataset,cik,bundle["as_of_date"],bundle["evidence_kind"],bundle["financials"]["parser_version"],dataset))
        for source in bundle["sources"]:
            sid = snapshot_id(cik, source)
            connection.execute("INSERT INTO source_snapshots VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                               (sid,cik,source["source_url"],source["sha256"],source["snapshot_path"],
                                source["size_bytes"],Jsonb(source.get("query", {}))))
            connection.execute("INSERT INTO retrievals VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                               (source["retrieval_id"],sid,source["fetched_at"],source["forced_refresh"],source["request_attempts"]))
            existing = connection.execute("SELECT * FROM retrievals WHERE retrieval_id=%s", (source["retrieval_id"],)).fetchone()
            if existing["snapshot_id"] != sid or existing["fetched_at"] != datetime.fromisoformat(source["fetched_at"]):
                raise ValueError("Retrieval identity conflicts with saved provenance")
            connection.execute("INSERT INTO dataset_sources VALUES (%s,%s)", (dataset,source["retrieval_id"]))

        def filing(fact, document=None):
            connection.execute("INSERT INTO filings VALUES (%s,%s,%s,%s,%s) ON CONFLICT(cik,accession) DO NOTHING",
                               (cik,fact["accession"],fact["form"],fact["filed_at"],document))
            stored = connection.execute("SELECT * FROM filings WHERE cik=%s AND accession=%s", (cik,fact["accession"])).fetchone()
            if stored["form"] != fact["form"] or stored["filed_at"].isoformat() != fact["filed_at"]:
                raise ValueError("Filing identity conflicts with prior observation")
            if document:
                if stored["document"] and stored["document"] != document:
                    raise ValueError("Filing document identity conflicts")
                connection.execute("UPDATE filings SET document=%s WHERE cik=%s AND accession=%s AND document IS NULL",
                                   (document,cik,fact["accession"]))

        for annual in bundle["financials"]["annual_periods"]:
            for metric, fact in annual["facts"].items():
                if fact is None:
                    continue
                filing(fact)
                identity = [cik, fact["taxonomy"],fact["tag"],fact["unit"],fact["period_start"],
                            fact["period_end"],fact["accession"],fact["filed_at"]]
                oid = digest(identity)
                connection.execute("INSERT INTO fact_observations VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                                   "ON CONFLICT DO NOTHING", (oid,cik,fact["accession"],metric,fact["taxonomy"],fact["tag"],
                                   fact["unit"],Decimal(fact["value"]),fact["period_start"],fact["period_end"],fact["filed_at"]))
                stored = connection.execute("SELECT value FROM fact_observations WHERE observation_id=%s", (oid,)).fetchone()
                if stored["value"] != Decimal(fact["value"]):
                    raise ValueError("Same filing observation changed value; source review required")
                sid = snapshot_id(cik, bundle["financials"]["source"])
                connection.execute("INSERT INTO fact_locations VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                                   (fact["fact_id"],cik,oid,sid,fact["source_json_pointer"]))
                connection.execute("INSERT INTO dataset_facts(dataset_id,fact_id,metric,period_end,cik) VALUES (%s,%s,%s,%s,%s)",
                                   (dataset,fact["fact_id"],metric,fact["period_end"],cik))
        for evidence in bundle["evidence"]:
            if evidence["kind"] == "filing":
                filing(evidence["locator"], evidence["locator"]["document"])
            version = digest(evidence)
            sid = snapshot_id(cik,evidence["source"])
            connection.execute("INSERT INTO evidence_versions VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                               (version,cik,evidence["id"],evidence["kind"],sid,Jsonb(evidence)))
            connection.execute("INSERT INTO dataset_evidence(dataset_id,evidence_id,version_id,cik) VALUES (%s,%s,%s,%s)",
                               (dataset,evidence["id"],version,cik))
            if evidence["kind"] != "calculation":
                continue
            calc = evidence["locator"]["calculation"]
            connection.execute("INSERT INTO calculations VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                               (dataset,evidence["id"],evidence["title"],calc["formula"],calc["formula_version"],
                                calc["rounding"],Decimal(calc["value"]) if calc["value"] is not None else None,
                                calc["unit"],calc["unavailable_reason"]))
            for index, fid in enumerate(calc["input_fact_ids"]):
                connection.execute("INSERT INTO calculation_inputs VALUES (%s,%s,%s,%s)", (dataset,evidence["id"],index,fid))
    return dataset


def list_datasets(connection):
    return connection.execute("SELECT d.*,c.ticker,c.name FROM datasets d JOIN companies c USING(cik) "
                              "ORDER BY d.imported_at DESC,d.dataset_id").fetchall()


def facts(connection, ticker, dataset=None):
    if dataset is None:
        row = connection.execute("SELECT d.dataset_id FROM datasets d JOIN companies c USING(cik) WHERE c.ticker=%s "
                                 "ORDER BY d.as_of_date DESC,d.imported_at DESC LIMIT 1", (ticker,)).fetchone()
        if not row:
            raise ValueError("No imported dataset for this company")
        dataset = row["dataset_id"]
    return connection.execute("SELECT c.ticker,d.dataset_id,d.as_of_date,df.fact_id,o.*,s.source_url,s.sha256,"
                              "l.json_pointer,f.form FROM dataset_facts df JOIN datasets d USING(dataset_id) "
                              "JOIN companies c ON c.cik=d.cik JOIN fact_locations l USING(fact_id) "
                              "JOIN fact_observations o USING(observation_id) JOIN source_snapshots s ON s.snapshot_id=l.snapshot_id "
                              "JOIN filings f ON f.cik=o.cik AND f.accession=o.accession "
                              "WHERE c.ticker=%s AND d.dataset_id=%s ORDER BY o.period_end DESC,o.metric", (ticker,dataset)).fetchall()


def evidence(connection, dataset, evidence_id):
    row = connection.execute("SELECT v.payload FROM dataset_evidence e JOIN evidence_versions v USING(version_id,evidence_id) "
                             "WHERE e.dataset_id=%s AND e.evidence_id=%s", (dataset,evidence_id)).fetchone()
    if not row:
        raise ValueError("Evidence not found in the selected dataset")
    return row["payload"]


def calculations(connection, dataset):
    return connection.execute("SELECT c.*,COALESCE(array_agg(i.fact_id ORDER BY i.ordinal) "
                              "FILTER(WHERE i.fact_id IS NOT NULL),ARRAY[]::text[]) AS input_fact_ids "
                              "FROM calculations c LEFT JOIN calculation_inputs i USING(dataset_id,calculation_id) "
                              "WHERE c.dataset_id=%s GROUP BY c.dataset_id,c.calculation_id ORDER BY c.title", (dataset,)).fetchall()
