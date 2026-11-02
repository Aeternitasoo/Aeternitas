"""Loopback-only task HTTP API. No provider credentials are accepted or returned."""

import hmac
import json
import os
import secrets
import stat
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from evidence_repository import json_value
from research_service import ResearchService, ServiceError
from sources.sec import SourceError


TOKEN_PATH=Path.home()/'.config/aeternitas/local-api-token'


def local_token(path=None):
    path=Path(path or TOKEN_PATH)
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if not path.exists():
        try:
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd,'w') as output:
                output.write(secrets.token_urlsafe(32))
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode) or path.stat().st_uid!=os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError('Local API token must be an owned regular file with mode 600')
    token=path.read_text().strip()
    if len(token)<32:
        raise ValueError('Invalid local API token file')
    return token


def response(value,status=200):
    return JSONResponse(json.loads(json.dumps(value,default=json_value,ensure_ascii=False)),status_code=status)


class CreateRun(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    ticker: str=Field(min_length=1,max_length=15)
    as_of_date: str=Field(min_length=10,max_length=10)
    question: str=Field(min_length=1,max_length=1200)
    refresh: bool=False
    news_days: int=Field(default=14,ge=1,le=30)
    deadline: int=Field(default=300,ge=1,le=1800)


class CancelRun(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)


class ResumeRun(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    acknowledge_unknown: bool=False


class LocalBoundary:
    def __init__(self,app,token):
        self.app,self.token=app,token

    async def __call__(self,scope,receive,send):
        if scope['type']!='http':
            return await self.app(scope,receive,send)
        headers=dict(scope.get('headers',[]))
        host=headers.get(b'host',b'').decode('latin1').lower()
        hostname=host.split(':')[0] if not host.startswith('[') else host.partition(']')[0]+']'
        public=scope['method']=='GET' and (scope['path']=='/health' or
               scope['path'].endswith(('/report.html','/report.json')))
        error=None
        if hostname not in {'localhost','127.0.0.1','[::1]'} or scope.get('client',('127.0.0.1',))[0] not in {'127.0.0.1','::1'}:
            error=(403,'local_only','Only loopback requests with a local Host are allowed')
        elif b'origin' in headers or headers.get(b'sec-fetch-site')==b'cross-site':
            error=(403,'cross_origin_denied','Cross-origin requests are disabled')
        elif not public and not hmac.compare_digest(headers.get(b'x-aeternitas-token',b''),self.token.encode()):
            error=(401,'unauthorized','Use the local CLI or a valid local API token header')
        elif scope['method']=='POST' and headers.get(b'content-type',b'').split(b';')[0]!=b'application/json':
            error=(415,'json_required','POST requires application/json')
        if error:
            return await response({'error':{'code':error[1],'message':error[2]}},error[0])(scope,receive,send)
        if scope['method']=='POST':
            body=b''
            while True:
                event=await receive()
                if event['type']=='http.disconnect': return
                body+=event.get('body',b'')
                if len(body)>8192:
                    return await response({'error':{'code':'body_too_large','message':'Request exceeds 8 KB'}},413)(scope,receive,send)
                if not event.get('more_body'): break
            async def buffered(): return {'type':'http.request','body':body,'more_body':False}
            receive=buffered
        async def secured(event):
            if event['type']=='http.response.start':
                event['headers']+= [(b'cache-control',b'no-store'),(b'x-content-type-options',b'nosniff'),
                  (b'content-security-policy',b"default-src 'none'; style-src 'unsafe-inline'; img-src data:; frame-ancestors 'none'; base-uri 'none'")]
            await send(event)
        await self.app(scope,receive,secured)


def create_app(service, *, token=None):
    app=FastAPI(title='Aeternitas local research API',docs_url=None,redoc_url=None,openapi_url=None)
    app.add_middleware(LocalBoundary,token=token or local_token())

    @app.exception_handler(ServiceError)
    async def service_error(request,exc):
        return response({'error':{'code':exc.code,'message':exc.message}},exc.status)

    @app.exception_handler(SourceError)
    async def database_error(request,exc):
        return response({'error':{'code':'database_unavailable','message':'Database unavailable; check PostgreSQL and run db-init'}},503)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request,exc):
        return response({'error':{'code':'invalid_request','message':'Invalid fields, types or bounds; see docs/M3_APPLICATION.md'}},422)

    @app.get('/health')
    def health():
        with service.connection() as connection:
            connection.execute('SELECT run_id FROM research_runs LIMIT 0')
        return {'status':'ready','module':'M5'}

    @app.get('/companies/resolve')
    def resolve_company(ticker: str=Query(min_length=1,max_length=15), refresh: bool=False):
        return response(service.resolve(ticker,refresh=refresh))

    @app.post('/research-runs',status_code=202)
    def create(body:CreateRun,idempotency_key:str|None=Header(default=None,min_length=1,max_length=128,
               pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')):
        key=idempotency_key if idempotency_key is not None else uuid4().hex
        row=dict(service.create(idempotency_key=key,**body.model_dump()))
        row['idempotency_key']=key
        result=response(row,202)
        result.headers['Location']=row['links']['status']
        result.headers['Idempotency-Key']=key
        return result

    @app.post('/research-runs/{run_id}/cancel')
    def cancel(run_id:str,body:CancelRun):
        return response(service.cancel(run_id))

    @app.post('/research-runs/{run_id}/resume',status_code=202)
    def resume(run_id:str,body:ResumeRun):
        row=service.resume(run_id,acknowledge_unknown=body.acknowledge_unknown)
        result=response(row,202)
        result.headers['Location']=row['links']['status']
        return result

    @app.get('/research-runs')
    def listing(ticker:str|None=None,status:str|None=None,limit:int=Query(50,ge=1,le=100),offset:int=Query(0,ge=0)):
        return response({'items':service.listing(ticker=ticker,status=status,limit=limit,offset=offset),
                         'limit':limit,'offset':offset})

    @app.get('/research-runs/{run_id}')
    def get(run_id:str): return response(service.get(run_id))

    @app.get('/research-runs/{run_id}/report.html')
    def html(run_id:str):
        folder,_=service.report(run_id)
        return Response((folder/'report.html').read_bytes(),media_type='text/html')

    @app.get('/research-runs/{run_id}/report.json')
    def report_json(run_id:str):
        _,report=service.report(run_id)
        return response(report)

    @app.get('/research-runs/{run_id}/evidence')
    def evidence(run_id:str): return response({'items':service.evidence(run_id)})

    @app.get('/research-runs/{run_id}/evidence/{evidence_id:path}')
    def one_evidence(run_id:str,evidence_id:str): return response(service.evidence(run_id,evidence_id))

    return app
