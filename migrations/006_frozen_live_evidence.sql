-- M6: distinguish a new analysis of saved real sources from a fresh retrieval.
-- Keep existing dataset identities and the original migration checksums intact.
ALTER TABLE datasets DROP CONSTRAINT datasets_evidence_kind_check;
ALTER TABLE datasets ADD CONSTRAINT datasets_evidence_kind_check
 CHECK (evidence_kind IN ('live_api','controlled_test','frozen_live_evidence'));
