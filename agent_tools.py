"""Read-only research tools bound by the host to pinned dataset aliases.

The model supplies an alias, never a filename, CIK or endpoint to fetch.
Source audit, financial selection and citation semantics stay in old modules.
"""
from functools import partial
import json
from pathlib import Path

from agent_harness import Tool, ToolRegistry, _object
from harness_contracts import HarnessError
from research_analysis import validate_result
from research_pipeline import audit_inputs
from research_workflow import sha, digest, validate_output


def _bundle(datasets, dataset):
    if dataset not in datasets:
        raise HarnessError('dataset_not_registered')
    handle=datasets[dataset]
    path=Path(handle['bundle_path'])
    if sha(path)!=handle['sha256']:
        raise HarnessError('dataset_version_changed')
    bundle=json.loads(path.read_text())
    audit_inputs(handle['source_root'],bundle)
    return bundle


def financial_summary(*, datasets, dataset):
    bundle=_bundle(datasets,dataset)
    return {'dataset':dataset,'bundle_sha256':datasets[dataset]['sha256'],
            'financials':bundle['financials'], 'scope':'SEC annual selected metrics; no new extraction or issuer certification'}


def evidence_lookup(*, datasets, dataset, evidence_ids):
    bundle=_bundle(datasets,dataset);by={e['id']:e for e in bundle['evidence']}
    if any(eid not in by for eid in evidence_ids):
        raise HarnessError('evidence_not_found')
    return {'dataset':dataset,'evidence':[{k:by[eid][k] for k in
        ('id','kind','title','text','published_at','source_url','source')} for eid in evidence_ids]}


def verify_citations(*, datasets, dataset, citations):
    bundle=_bundle(datasets,dataset)
    validate_result({'claims':[{'text':'引文核对','stance':'neutral','citations':citations}],'gaps':[]},bundle['evidence'])
    return {'membership_and_exact_quotes':True,'semantic_entailment_verified':False,
            'verified_evidence_ids':[c['evidence_id'] for c in citations]}


def research_tools(datasets):
    """Host must separately grant research.read / evidence.verify permissions."""
    if not datasets or len(datasets)>8:
        raise HarnessError('invalid_dataset_bindings')
    for name, handle in datasets.items():
        if not isinstance(name,str) or set(handle)!={'bundle_path','source_root','sha256'}:
            raise HarnessError('invalid_dataset_handle')
    dataset={'type':'string','enum':sorted(datasets)}
    string={'type':'string','minLength':1,'maxLength':200}
    citations={'type':'array','minItems':1,'maxItems':3,'items':_object({
        'evidence_id':string,'quote':{'type':'string','minLength':12,'maxLength':300}})}
    registry=ToolRegistry()
    for name, description, schema, fn, permission in [
        ('research.financial_summary','读取已核验冻结数据的既有年度财务与计算',_object({'dataset':dataset}),financial_summary,'research.read'),
        ('research.evidence','按ID读取已核验冻结证据，不下载新数据',_object({'dataset':dataset,'evidence_ids':{'type':'array','items':string,'minItems':1,'maxItems':3}}),evidence_lookup,'research.read'),
        ('research.verify_citations','沿用既有ID和逐字引用检查，不认证金融推断',_object({'dataset':dataset,'citations':citations}),verify_citations,'evidence.verify')]:
        registry.register(Tool(name,description,schema,partial(fn,datasets=datasets),
            frozenset({permission}),timeout_seconds=15,output_bytes=65536,repeat_safe=True,
            version='existing-research-tools-v1',binding_fingerprint=digest(datasets)))
    return registry


def research_result_validator(value, *, config, evidence):
    """Optional final validator reuses the Research Workflow contract unchanged."""
    return validate_output(value, config, evidence)
