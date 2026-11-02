"""Fixed offline checks, including failures that cannot be solved by quote matching."""
from copy import deepcopy
import pytest
from evidence_repository import digest
import research_intelligence as ri


@pytest.mark.parametrize('stage,expected',[(0,'source_present_not_extracted'),
    (1,'extracted_not_supplied'),(2,'supplied_not_cited'),(3,'cited_semantics_unreviewed')])
def test_four_pipeline_stages(tmp_path,monkeypatch,stage,expected):
    monkeypatch.setattr(ri,'audit_inputs',lambda *a:None)
    p=tmp_path/'source.txt';p.write_text('cash flow statement; cash flow statement')
    filing={'id':'f','kind':'filing','text':'business','source':{'sha256':ri.sha(p)}, 'locator':{'normalized_path':p.name,'normalized_sha256':ri.sha(p)}}
    item={'id':'x','kind':'financial_fact','text':'cash flow statement'}
    bundle={'ticker':'X','evidence':[filing]+([item] if stage else [])}
    supplied=[item] if stage>=2 else []
    response={'sections':[{'claims':[{'citations':[{'evidence_id':'x'}]}]}]} if stage==3 else {}
    result=ri.completeness(tmp_path,bundle,supplied,response,[{'id':'cash','terms':['cash flow'],'purpose':'support'}],max_hits=1)
    assert result['topics'][0]['state']==expected
    assert result['topics'][0]['truncated'] is True
    assert result['complete'] is False


def test_missing_search_is_not_proof_of_absence(tmp_path,monkeypatch):
    monkeypatch.setattr(ri,'audit_inputs',lambda *a:None)
    r=ri.completeness(tmp_path,{'ticker':'X','evidence':[]},[],{},[{'id':'x','terms':['foo'],'purpose':'counterevidence'}])
    assert r['topics'][0]['state']=='not_found_in_scanned_sources'
    assert not r['topics'][0]['source_absence_proven']


def test_invalid_query_is_not_silently_omitted(tmp_path,monkeypatch):
    monkeypatch.setattr(ri,'audit_inputs',lambda *a:None)
    with pytest.raises(ValueError):
        ri.completeness(tmp_path,{'ticker':'X','evidence':[]},[],{},[{'id':'x','terms':[],'purpose':'support'}])


def metric(value='25533000000',year=2025):
    return dict(metric_name='total_revenue',definition_id='consolidated-revenues-v1',accounting_definition='Total operating revenue',entity='CEG',segment=None,
      period_start=f'{year}-01-01',period_end=f'{year}-12-31',currency='USD',unit='USD',basis='GAAP',scope='consolidated',
      source_reference={},value=value,definition_review='approved')


def test_consolidated_growth_not_customer_contract_growth():
    assert float(ri.comparable_growth(metric(),metric('23568000000',2024)))==pytest.approx(8.3375763747)


@pytest.mark.parametrize('key,value',[('definition_id','customer-contract-revenue'),('entity','subsidiary'),('scope','segment'),('unit','USD millions'),('basis','non-GAAP'),('definition_review','pending')])
def test_incomparable_metrics_blocked(key,value):
    c=metric();c[key]=value
    with pytest.raises(ValueError):ri.comparable_growth(c,metric('23568000000',2024))


def test_source_value_period_and_revision_bound():
    m=metric();s={'units':{'USD':[dict(val=25533000000,start='2025-01-01',end='2025-12-31',accn='ref')]}}
    m['source_reference']={'content_fingerprint':digest(s),'json_pointer':'/units/USD/0','accession':'ref'}
    assert ri.validate_metric(m,s)==m
    bad=deepcopy(m);bad['value']='22663000000'
    with pytest.raises(ValueError):ri.validate_metric(bad,s)
    s['units']['USD'][0]['val']=1
    with pytest.raises(ValueError):ri.validate_metric(m,s)


def dossier():
    return dict(claim_id='a',text='AI contributes to growth',classification='inference',support=[{'evidence_id':'e','quote':'a sufficiently long exact source'}],
      counterevidence=[],counterevidence_search={'status':'searched_not_found','source_pins':['pin']},
      falsifier={'observation':'non-AI share exceeds half','logical_bridge':'share is not marginal contribution','effect':'weakens','alternative_explanations':['mixed demand']},unknowns=['AI contribution'])


def test_exact_quote_does_not_prove_logic_or_absent_counterevidence():
    d=dossier();r=ri.claim_review(d,[{'id':'e','text':'a sufficiently long exact source'}])
    assert not r['eligible_for_promotion'] and r['entailment']=='requires_independent_review'
    assert not r['counterevidence_found']


def test_bad_quote_and_missing_falsification_bridge_rejected():
    d=dossier();d['falsifier'].pop('logical_bridge')
    with pytest.raises(ValueError):ri.claim_review(d,[{'id':'e','text':'a sufficiently long exact source'}])


def test_counterevidence_search_requires_provenance():
    d=dossier();d['counterevidence_search']={}
    with pytest.raises(ValueError):ri.claim_review(d,[{'id':'e','text':'a sufficiently long exact source'}])
    d=dossier();d['support'][0]['quote']='false quote contents'
    with pytest.raises(ValueError):ri.claim_review(d,[{'id':'e','text':'a sufficiently long exact source'}])
