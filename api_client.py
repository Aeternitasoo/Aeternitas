"""CLI transport: exact loopback addresses, no inherited proxy and no token in URLs."""

import time
import re
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from local_api import local_token
from research_service import TERMINAL
from sources.sec import SourceError


DEFAULT_URL='http://127.0.0.1:8767'


def request_key(value=None):
    if value is None:
        return uuid4().hex
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}',value):
        raise ValueError('Idempotency key must contain 1–128 ASCII letters, digits, dot, underscore, colon or hyphen')
    return value


def local_url(url):
    parsed=urlsplit(url)
    try: port=parsed.port
    except ValueError: port=None
    if (parsed.scheme!='http' or parsed.hostname not in {'localhost','127.0.0.1','::1'}
        or not port or parsed.username or parsed.password or parsed.path not in {'','/'} or parsed.query or parsed.fragment):
        raise ValueError('API URL must be http://localhost:PORT or an exact loopback address')
    return url.rstrip('/')


class APIClient:
    def __init__(self,url=DEFAULT_URL,*,transport=None,token=None):
        self.url=local_url(url)
        self.client=httpx.Client(base_url=self.url,trust_env=False,timeout=20,
                                 headers={'X-Aeternitas-Token':token or local_token()},transport=transport)

    def __enter__(self): return self
    def __exit__(self,*args): self.client.close()

    def request(self,method,path,**kwargs):
        if method.upper()=='POST' and not any(key in kwargs for key in ('json','content','data','files')):
            kwargs['json']={}
        try:
            result=self.client.request(method,path,**kwargs)
        except httpx.HTTPError:
            raise SourceError('Local API unavailable; start: .venv/bin/python aeternitas.py serve') from None
        try: data=result.json()
        except ValueError:
            raise SourceError('Local API returned an invalid response') from None
        if result.is_error:
            error=data.get('error',{})
            raise SourceError(f"Local API {result.status_code} [{error.get('code','request_failed')}]: {error.get('message','Request failed')}")
        return data

    def create(self,payload,*,idempotency_key=None):
        key=request_key(idempotency_key)
        try:
            return self.request('POST','/research-runs',json=payload,headers={'Idempotency-Key':key})
        except SourceError as exc:
            # The request may have committed before a response was lost. There
            # is no implicit retry with a fresh key and no duplicate task here.
            raise SourceError(f'{exc}; preserve the same request and reuse --idempotency-key {key}') from None

    def wait(self,run_id,seconds=360):
        until=time.monotonic()+seconds
        previous=None
        while True:
            row=self.request('GET',f'/research-runs/{run_id}')
            mark=(row['status'],row['step'],row['model_attempts'])
            if mark!=previous:
                print(f"run={run_id} status={mark[0]} step={mark[1]} model_attempts={mark[2]}",flush=True)
                previous=mark
            if row['status'] in TERMINAL | {'cancelled'}: return row
            if time.monotonic()>=until:
                raise SourceError(f'Wait timed out; task remains saved. Use status {run_id}')
            time.sleep(min(2,max(0,until-time.monotonic())))
