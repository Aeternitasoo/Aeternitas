CREATE TABLE research_runs (
    run_id text PRIMARY KEY CHECK (run_id ~ '^[0-9a-f]{32}$'),
    ticker text NOT NULL CHECK (ticker ~ '^[A-Z][A-Z0-9.-]{0,14}$'),
    cik text NOT NULL CHECK (cik ~ '^[0-9]{10}$'),
    as_of_date date NOT NULL,
    question text NOT NULL CHECK (length(question) BETWEEN 1 AND 1200),
    options jsonb NOT NULL CHECK (jsonb_typeof(options) = 'object'),
    data_root text NOT NULL,
    billing_mode text NOT NULL CHECK (billing_mode IN ('chatgpt_plan','api_key')),
    model text NOT NULL,
    status text NOT NULL DEFAULT 'queued' CHECK (status IN
      ('queued','running','completed','completed_with_gaps','failed','interrupted')),
    step text NOT NULL DEFAULT 'queued',
    model_attempts integer NOT NULL DEFAULT 0 CHECK (model_attempts BETWEEN 0 AND 8),
    model_usage jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(model_usage) = 'array'),
    dataset_id text REFERENCES datasets(dataset_id),
    error text,
    worker_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz,
    CHECK ((status IN ('completed','completed_with_gaps','failed','interrupted')) = (finished_at IS NOT NULL))
);
CREATE INDEX research_runs_queue ON research_runs(created_at,run_id) WHERE status='queued';
CREATE TABLE run_steps (
    run_id text NOT NULL REFERENCES research_runs(run_id),
    step text NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal > 0),
    status text NOT NULL CHECK (status IN ('running','completed','failed','interrupted')),
    model_attempts integer NOT NULL DEFAULT 0 CHECK (model_attempts BETWEEN 0 AND 8),
    error text,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    PRIMARY KEY (run_id,step),
    UNIQUE (run_id,ordinal)
);
