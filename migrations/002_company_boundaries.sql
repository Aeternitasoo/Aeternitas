-- Protect company identity at the SQL boundary, including hand-written INSERTs.
ALTER TABLE datasets ADD UNIQUE(dataset_id,cik);
ALTER TABLE fact_locations ADD UNIQUE(fact_id,cik);
ALTER TABLE filings ADD UNIQUE(cik,accession,filed_at);
ALTER TABLE fact_observations ADD FOREIGN KEY(cik,accession,filed_at)
    REFERENCES filings(cik,accession,filed_at);

ALTER TABLE dataset_facts ADD COLUMN cik text;
UPDATE dataset_facts f SET cik=d.cik FROM datasets d WHERE d.dataset_id=f.dataset_id;
ALTER TABLE dataset_facts ALTER COLUMN cik SET NOT NULL;
ALTER TABLE dataset_facts ADD FOREIGN KEY(dataset_id,cik) REFERENCES datasets(dataset_id,cik);
ALTER TABLE dataset_facts ADD FOREIGN KEY(fact_id,cik) REFERENCES fact_locations(fact_id,cik);

ALTER TABLE evidence_versions ADD UNIQUE(version_id,evidence_id,cik);
ALTER TABLE evidence_versions ADD FOREIGN KEY(source_snapshot_id,cik) REFERENCES source_snapshots(snapshot_id,cik);
ALTER TABLE evidence_versions ADD CHECK (
    payload ?& ARRAY['id','company_cik','kind','text'] AND
    payload->>'id'=evidence_id AND payload->>'company_cik'=cik AND payload->>'kind'=kind
);
ALTER TABLE dataset_evidence ADD COLUMN cik text;
UPDATE dataset_evidence e SET cik=d.cik FROM datasets d WHERE d.dataset_id=e.dataset_id;
ALTER TABLE dataset_evidence ALTER COLUMN cik SET NOT NULL;
ALTER TABLE dataset_evidence ADD FOREIGN KEY(dataset_id,cik) REFERENCES datasets(dataset_id,cik);
ALTER TABLE dataset_evidence ADD FOREIGN KEY(version_id,evidence_id,cik) REFERENCES evidence_versions(version_id,evidence_id,cik);
