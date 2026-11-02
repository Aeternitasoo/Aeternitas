CREATE TABLE companies (
    cik text PRIMARY KEY CHECK (cik ~ '^[0-9]{10}$'),
    ticker text NOT NULL UNIQUE CHECK (ticker ~ '^[A-Z][A-Z0-9.-]{0,15}$'),
    name text NOT NULL CHECK (length(name) > 0)
);

CREATE TABLE source_snapshots (
    snapshot_id text PRIMARY KEY CHECK (snapshot_id ~ '^[0-9a-f]{64}$'),
    cik text NOT NULL REFERENCES companies(cik),
    source_url text NOT NULL,
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    snapshot_path text NOT NULL,
    size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
    query jsonb NOT NULL,
    UNIQUE (snapshot_id, cik)
);
CREATE TABLE retrievals (
    retrieval_id text PRIMARY KEY,
    snapshot_id text NOT NULL REFERENCES source_snapshots(snapshot_id),
    fetched_at timestamptz NOT NULL,
    forced_refresh boolean NOT NULL,
    request_attempts integer NOT NULL CHECK (request_attempts BETWEEN 1 AND 3)
);
CREATE TABLE filings (
    cik text NOT NULL REFERENCES companies(cik),
    accession text NOT NULL CHECK (accession ~ '^[0-9]{10}-[0-9]{2}-[0-9]{6}$'),
    form text NOT NULL CHECK (form IN ('10-K','10-Q','10-K/A','10-Q/A')),
    filed_at date NOT NULL,
    document text,
    PRIMARY KEY (cik, accession)
);
CREATE TABLE fact_observations (
    observation_id text PRIMARY KEY CHECK (observation_id ~ '^[0-9a-f]{64}$'),
    cik text NOT NULL,
    accession text NOT NULL,
    metric text NOT NULL CHECK (metric IN ('revenue','operating_income','net_income')),
    taxonomy text NOT NULL CHECK (taxonomy = 'us-gaap'),
    tag text NOT NULL,
    unit text NOT NULL CHECK (unit = 'USD'),
    value numeric NOT NULL CHECK (value NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)),
    period_start date NOT NULL,
    period_end date NOT NULL,
    filed_at date NOT NULL,
    CHECK (period_start < period_end AND period_end <= filed_at),
    CHECK (period_end - period_start + 1 BETWEEN 330 AND 400),
    FOREIGN KEY (cik, accession) REFERENCES filings(cik, accession),
    UNIQUE (cik,taxonomy,tag,unit,period_start,period_end,accession,filed_at),
    UNIQUE (observation_id,cik)
);
CREATE TABLE fact_locations (
    fact_id text PRIMARY KEY CHECK (fact_id ~ '^[0-9a-f]{64}$'),
    cik text NOT NULL,
    observation_id text NOT NULL,
    snapshot_id text NOT NULL,
    json_pointer text NOT NULL CHECK (json_pointer LIKE '/facts/us-gaap/%/units/USD/%'),
    FOREIGN KEY (observation_id,cik) REFERENCES fact_observations(observation_id,cik),
    FOREIGN KEY (snapshot_id,cik) REFERENCES source_snapshots(snapshot_id,cik),
    UNIQUE(snapshot_id,json_pointer)
);
CREATE TABLE datasets (
    dataset_id text PRIMARY KEY CHECK (dataset_id ~ '^[0-9a-f]{64}$'),
    cik text NOT NULL REFERENCES companies(cik),
    as_of_date date NOT NULL,
    evidence_kind text NOT NULL CHECK (evidence_kind IN ('live_api','controlled_test')),
    parser_version text NOT NULL,
    bundle_sha256 text NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE dataset_sources (
    dataset_id text NOT NULL REFERENCES datasets(dataset_id),
    retrieval_id text NOT NULL REFERENCES retrievals(retrieval_id),
    PRIMARY KEY(dataset_id,retrieval_id)
);
CREATE TABLE dataset_facts (
    dataset_id text NOT NULL REFERENCES datasets(dataset_id),
    fact_id text NOT NULL REFERENCES fact_locations(fact_id),
    metric text NOT NULL,
    period_end date NOT NULL,
    PRIMARY KEY(dataset_id,fact_id),
    UNIQUE(dataset_id,metric,period_end)
);
CREATE TABLE calculations (
    dataset_id text NOT NULL REFERENCES datasets(dataset_id),
    calculation_id text NOT NULL,
    title text NOT NULL,
    formula text NOT NULL,
    formula_version text NOT NULL,
    rounding text NOT NULL,
    value numeric CHECK (value NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)),
    unit text NOT NULL CHECK (unit = 'percent'),
    unavailable_reason text,
    CHECK ((value IS NULL) = (unavailable_reason IS NOT NULL)),
    PRIMARY KEY(dataset_id,calculation_id)
);
CREATE TABLE calculation_inputs (
    dataset_id text NOT NULL,
    calculation_id text NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    fact_id text NOT NULL,
    FOREIGN KEY (dataset_id,calculation_id) REFERENCES calculations(dataset_id,calculation_id),
    FOREIGN KEY (dataset_id,fact_id) REFERENCES dataset_facts(dataset_id,fact_id),
    PRIMARY KEY(dataset_id,calculation_id,ordinal)
);
CREATE TABLE evidence_versions (
    version_id text PRIMARY KEY CHECK (version_id ~ '^[0-9a-f]{64}$'),
    cik text NOT NULL REFERENCES companies(cik),
    evidence_id text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('financial_fact','calculation','filing','news')),
    source_snapshot_id text NOT NULL REFERENCES source_snapshots(snapshot_id),
    payload jsonb NOT NULL,
    UNIQUE(version_id,evidence_id)
);
CREATE TABLE dataset_evidence (
    dataset_id text NOT NULL REFERENCES datasets(dataset_id),
    evidence_id text NOT NULL,
    version_id text NOT NULL,
    PRIMARY KEY(dataset_id,evidence_id),
    FOREIGN KEY(version_id,evidence_id) REFERENCES evidence_versions(version_id,evidence_id)
);
CREATE INDEX datasets_company_date ON datasets(cik,as_of_date DESC,imported_at DESC);
