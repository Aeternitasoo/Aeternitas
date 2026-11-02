"""All prices below are synthetic, not real VST observations."""
import hashlib
import json
from pathlib import Path
import pytest
from yahoo_csv import import_csv
from time_machine import run, verify_manifest
from market_data import load_csv
import aeternitas

HEADER='Date,Open,High,Low,Close,Adj Close,Volume\n'
ROWS='2022-06-30,10,12,9,11,8,100\n2022-07-01,11,13,10,12,9,0\n'

def source(tmp_path, text=HEADER+ROWS):
    path=tmp_path/'SYNTHETIC.csv';path.write_text(text);return path

def ingest(tmp_path, text=HEADER+ROWS, **kw):
    return import_csv(source(tmp_path,text), root=tmp_path/'snapshots',synthetic=True,**kw)

def test_complete_pipeline_preserves_raw_prices_versions_and_rejection(tmp_path):
    folder,m=ingest(tmp_path,cutoffs=['2022-06-30T20:01:00Z','2024-06-28T20:01:00Z'])
    assert (folder/'raw/input.csv').read_bytes()==(HEADER+ROWS).encode()
    assert m['raw_sha256']==hashlib.sha256((HEADER+ROWS).encode()).hexdigest()
    assert ',11,8,2' in (folder/'normalized/prices.csv').read_text()
    data=load_csv(folder/'normalized/market.csv')
    assert str(data.bars[0].close)=='11' and data.bars[0].available_at is None and data.bars[0].retrieved_at is None
    manifests=[json.loads(p.read_text()) for p in sorted((folder/'validation').glob('*.json'))]
    assert all(verify_manifest(x) for x in manifests)
    assert all(not x['content']['research_inputs'] for x in manifests)
    assert manifests[0]['input_fingerprint']!=manifests[1]['input_fingerprint']
    reasons=manifests[0]['content']['summary']['issue_counts']
    assert reasons['daily_period_not_closed']==2 and reasons['availability_time_unknown']==2
    assert reasons['retrieval_time_unresolved']==2
    again=run(folder/'normalized/market.csv',symbol='VST',start='2022-06-30',end='2022-07-01',cutoff='2022-06-30T20:01:00Z')
    assert again['manifest_fingerprint']==manifests[0]['manifest_fingerprint']

@pytest.mark.parametrize('text',[HEADER.replace('Adj Close,','')+ROWS,HEADER+ROWS.replace('2022-06-30','bad'),
    HEADER+ROWS+ROWS.splitlines()[0]+'\n',HEADER+ROWS.replace(',10,12,',',,12,'),
    HEADER+ROWS.replace(',12,9,11,',',8,9,11,'),HEADER+ROWS.replace(',100',',-1'),
    HEADER+ROWS.replace(',100',',1.5'),HEADER+ROWS.replace(',8,100',',NaN,100'),
    'garbled',HEADER,HEADER+ROWS.replace(',10,12,',',0,12,')])
def test_bad_sources_are_archived_with_errors_without_normalized_output(tmp_path,text):
    folder,m=ingest(tmp_path,text)
    assert m['status']=='failed' and not (folder/'normalized/market.csv').exists()
    assert json.loads((folder/'quality_report.json').read_text())['errors']

def test_sort_and_alias_are_explicit_and_repeat_import_never_overwrites(tmp_path):
    text=HEADER.replace('Adj Close','Adjusted_Close')+'\n'.join(reversed(ROWS.strip().splitlines()))+'\n'
    first,m=ingest(tmp_path,text)
    raw=(first/'raw/input.csv').read_bytes()
    second,_=import_csv(tmp_path/'SYNTHETIC.csv',root=tmp_path/'snapshots',synthetic=True)
    assert first!=second and (first/'raw/input.csv').read_bytes()==raw
    q=json.loads((first/'quality_report.json').read_text())
    assert any(t['rule']=='sort_ascending_date' for t in q['transformations'])
    assert m['start']=='2022-06-30'

def test_hash_pin_corruption_and_input_change(tmp_path):
    first,m=ingest(tmp_path)
    _,bad=import_csv(tmp_path/'SYNTHETIC.csv',root=tmp_path/'snapshots',expected_sha256='0'*64,synthetic=True)
    assert bad['status']=='failed'
    (tmp_path/'SYNTHETIC.csv').write_text(HEADER+ROWS.replace(',8,100',',7,100'))
    second,changed=import_csv(tmp_path/'SYNTHETIC.csv',root=tmp_path/'snapshots',synthetic=True)
    assert changed['raw_sha256']!=m['raw_sha256']
    assert load_csv(first/'normalized/market.csv').bars[0].source_version!=load_csv(second/'normalized/market.csv').bars[0].source_version

def test_invalid_encoding(tmp_path):
    p=tmp_path/'corrupt.csv';p.write_bytes(b'\xff\xfe\x00')
    folder,m=import_csv(p,root=tmp_path/'snapshots',synthetic=True)
    assert m['status']=='failed'

def test_missing_sessions_and_outliers_are_reported_without_repair(tmp_path):
    calendar=tmp_path/'calendar.json'
    calendar.write_text(json.dumps({'exchange':'XNYS','timezone':'America/New_York',
        'coverage_start':'2022-06-30','coverage_end':'2022-07-05','sessions':['2022-06-30','2022-07-01','2022-07-05'],
        'available_at':'2026-01-01T00:00:00Z','retrieved_at':'2026-01-01T00:00:00Z',
        'source':'SYNTHETIC calendar for unit test only','version':'synthetic-1'}))
    text=HEADER+ROWS.replace('2022-07-01,11,13,10,12','2022-07-05,21,23,20,22')
    folder,m=ingest(tmp_path,text,calendar_path=calendar)
    q=json.loads((folder/'quality_report.json').read_text())
    assert q['missing_sessions']==['2022-07-01'] and q['anomalies'][0]['line']==3
    assert len(load_csv(folder/'normalized/market.csv').bars)==2
    assert str(load_csv(folder/'normalized/market.csv').bars[1].close)=='22'

def test_cli_help_and_import_need_no_configuration(tmp_path,monkeypatch,capsys):
    monkeypatch.setattr(aeternitas,'load_config',lambda *a:pytest.fail('credentials must not be read'))
    with pytest.raises(SystemExit):aeternitas.main(['import-vst-csv','--help'])
    capsys.readouterr()
    assert aeternitas.main(['import-vst-csv',str(source(tmp_path)),'--output-root',str(tmp_path/'snapshots')])==0
    output=json.loads(capsys.readouterr().out)
    assert len(list((Path(output['snapshot'])/'validation').glob('*.json')))==2
