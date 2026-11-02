ALTER TABLE research_runs ADD FOREIGN KEY(dataset_id,cik) REFERENCES datasets(dataset_id,cik);
ALTER TABLE research_runs ADD CHECK (status NOT IN ('completed','completed_with_gaps') OR dataset_id IS NOT NULL);
ALTER TABLE run_steps ADD CHECK ((status='running') = (finished_at IS NULL));
