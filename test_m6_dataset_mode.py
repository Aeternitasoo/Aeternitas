"""A frozen real-source analysis is a distinct dataset, not a fresh retrieval."""
from copy import deepcopy

import pytest

import evidence_repository as repository
from test_evidence_repository import DSN, database, bundle, counts


@pytest.mark.skipif(not DSN, reason='Requires explicit isolated real PostgreSQL')
def test_frozen_dataset_preserves_existing_retrievals_and_history(database, tmp_path):
    original = bundle(tmp_path)
    old_id = repository.import_bundle(database, tmp_path, original)
    before = counts(database)
    frozen = deepcopy(original)
    # Synthetic source bytes exercise the real classification and DB constraint.
    frozen['evidence_kind'] = 'frozen_live_evidence'
    new_id = repository.import_bundle(database, tmp_path, frozen)
    assert new_id != old_id
    assert repository.import_bundle(database, tmp_path, frozen) == new_id
    after = counts(database)
    for table in ('companies', 'source_snapshots', 'retrievals', 'filings',
                  'fact_observations', 'fact_locations', 'evidence_versions'):
        assert after[table] == before[table]
    rows = database.execute('SELECT dataset_id,evidence_kind FROM datasets ORDER BY dataset_id').fetchall()
    assert {row['dataset_id']: row['evidence_kind'] for row in rows} == {
        old_id: 'controlled_test', new_id: 'frozen_live_evidence'}
    old_facts = repository.facts(database, 'AAPL', old_id)
    new_facts = repository.facts(database, 'AAPL', new_id)
    assert [{k: v for k, v in row.items() if k != 'dataset_id'} for row in old_facts] == [
        {k: v for k, v in row.items() if k != 'dataset_id'} for row in new_facts]
    assert after['datasets'] == before['datasets'] + 1
