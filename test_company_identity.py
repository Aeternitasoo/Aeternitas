"""Company discovery regressions: synthetic HTTP, never live providers/models."""
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from company_identity import CompanyError, DIRECTORY_URL, directory_entries, normalize_ticker, resolve_company

TABLE = {'0':dict(cik_str=1321655,ticker='PLTR',title='Synthetic Palantir'),
         '1':dict(cik_str=1067983,ticker='BRK-B',title='Synthetic Berkshire')}


def test_auto_discovery_cache_receipt_and_no_configuration_write(tmp_path):
    calls=[]
    def handler(request):
        calls.append(request)
        assert str(request.url)==DIRECTORY_URL
        assert 'test@example.com' in request.headers['user-agent']
        return httpx.Response(200,json=TABLE)
    transport=httpx.MockTransport(handler)
    first=resolve_company(' pltr ',tmp_path,'test@example.com',transport=transport)
    assert (first['ticker'],first['cik'],first['name'])==('PLTR','0001321655','Synthetic Palantir')
    assert first['source']=='sec_directory' and not first['directory']['cache_hit']
    assert hashlib.sha256((tmp_path/first['directory']['snapshot_path']).read_bytes()).hexdigest()==first['directory']['sha256']
    second=resolve_company('PLTR',tmp_path,'test@example.com',transport=transport)
    assert second['directory']['cache_hit'] and len(calls)==1
    assert resolve_company('BRK.B',tmp_path,'test@example.com',transport=transport)['ticker']=='BRK-B'
    resolve_company('PLTR',tmp_path,'test@example.com',transport=transport,refresh=True)
    assert len(calls)==2
    assert not (tmp_path/'config/companies.json').exists()


def test_actual_directory_failure_shape_unsupported_placeholder_is_not_global_failure():
    # Real 2026-10-24 SEC response contained ticker NONE. (other fields synthetic).
    table={**TABLE,'2':dict(cik_str=1,ticker='NONE.',title='Synthetic placeholder')}
    assert directory_entries(json.dumps(table))['PLTR']['cik']=='0001321655'


@pytest.mark.parametrize('value',['../PLTR','', '.', 'PLTR/../NVDA',';echo','苹果',None,'A'*16])
def test_bad_inputs_rejected_without_io(tmp_path,value):
    with pytest.raises(CompanyError,match='ticker'):
        resolve_company(value,tmp_path,'test@example.com')
    assert not list(tmp_path.rglob('*'))


def test_foreign_market_not_silently_mapped(tmp_path):
    with pytest.raises(CompanyError) as error:
        resolve_company('BABA.HK',tmp_path,'test@example.com')
    assert error.value.code=='unsupported_market'
    assert not list(tmp_path.rglob('*'))


def test_missing_ticker_and_ambiguous_directory_do_not_guess(tmp_path):
    transport=httpx.MockTransport(lambda request:httpx.Response(200,json=TABLE))
    with pytest.raises(CompanyError) as error:
        resolve_company('UNKNOWN',tmp_path,'test@example.com',transport=transport)
    assert error.value.code=='unknown_company'
    duplicate={**TABLE,'3':dict(cik_str=99,ticker='PLTR',title='Other issuer')}
    with pytest.raises(CompanyError,match='ambiguous'):directory_entries(json.dumps(duplicate))


def test_directory_failure_is_redacted_and_no_stale_fallback(tmp_path):
    good=httpx.MockTransport(lambda request:httpx.Response(200,json=TABLE))
    resolve_company('PLTR',tmp_path,'test@example.com',transport=good)
    bad=httpx.MockTransport(lambda request:httpx.Response(403,text='private upstream text'))
    with pytest.raises(CompanyError) as error:
        resolve_company('PLTR',tmp_path,'test@example.com',transport=bad,refresh=True)
    assert error.value.code=='company_lookup_unavailable' and 'private' not in str(error.value)
    pointer=next((tmp_path/'cache').glob('web-*.json'))
    receipt=json.loads(pointer.read_text())
    (tmp_path/receipt['snapshot_path']).write_text('tampered')
    with pytest.raises(CompanyError):resolve_company('PLTR',tmp_path,'test@example.com',transport=good)


def test_existing_overrides_work_offline(tmp_path):
    assert resolve_company('aapl',tmp_path,None,overrides={'AAPL':{'cik':'0000320193'}})['source']=='local_override'
    assert not list(tmp_path.rglob('*'))


def test_service_persists_identity_without_making_receipt_part_of_idempotency(app_state):
    # Reuse isolated real PostgreSQL and the application's actual routes.
    service,client,calls=app_state
    seen=[]
    service.transports['company_directory']=httpx.MockTransport(lambda request:(seen.append(request) or httpx.Response(200,json=TABLE)))
    first=client.post('/research-runs',json={**PAYLOAD,'ticker':'pltr'},headers={'Idempotency-Key':'company-discovery'})
    assert first.status_code==202,first.text
    row=first.json()
    assert row['ticker']=='PLTR' and row['cik']=='0001321655' and row['company_identity']['name']=='Synthetic Palantir'
    assert client.get('/research-runs/'+row['run_id']).json()['company_identity']==row['company_identity']
    second=client.post('/research-runs',json={**PAYLOAD,'ticker':'PLTR'},headers={'Idempotency-Key':'company-discovery'})
    assert second.status_code==202 and second.json()['run_id']==row['run_id']
    assert len(seen)==1 and not calls['model']
    assert len(client.get('/research-runs?ticker=PLTR').json()['items'])==1
    unknown=client.post('/research-runs',json={**PAYLOAD,'ticker':'UNKNOWN'})
    assert unknown.status_code==400 and unknown.json()['error']['code']=='unknown_company'
    assert len(client.get('/research-runs').json()['items'])==1
    assert client.get('/companies/resolve?ticker=PLTR').status_code==200


# Existing fixture creates/drops its own schema; no access to production data.
from test_m3_application import app_state, PAYLOAD
