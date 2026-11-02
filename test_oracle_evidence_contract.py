"""Authorization and argument roles are independent. Fixed offline examples."""
from copy import deepcopy
import pytest
from research_quality import authorized_index,check_reference,challenge,model_schema
from research_workflow import schema_for
from test_research_workflow import config,output
from test_research_quality import enhanced

TEXT='The company faces material competition and pricing pressure.'

def fixture(authorized=True):
    evidence=[{'id':'e','text':TEXT,'source_authorized':authorized,'intelligence_purpose':'supporting'}]
    return evidence,{'evidence':deepcopy(evidence),'context':{'audited_metrics':{}},'source_pins':{'synthetic':'f'*64}}

@pytest.mark.parametrize('role',['supporting','counterevidence','contextual'])
def test_authorized_source_all_roles(role):
    evidence,package=fixture();index=authorized_index(evidence,package)
    result=check_reference({'evidence_id':'e','quote':TEXT},role,'claim-1',index)
    assert result['semantic_relationship']=='pending_independent_review'
    assert result['authorization']=='current_frozen_input'

@pytest.mark.parametrize('role',['supporting','counterevidence','contextual'])
def test_unauthorized_source_never_granted_by_role(role):
    evidence,package=fixture(False)
    with pytest.raises(ValueError,match='not authorized'):
        check_reference({'evidence_id':'e','quote':TEXT},role,'c',authorized_index(evidence,package))

def test_additional_source_and_modified_source_rejected():
    evidence,package=fixture();altered=deepcopy(evidence);altered[0]['text']+=' changed'
    for items in [altered,evidence+[{'id':'injected','text':TEXT}]]:
        with pytest.raises(ValueError,match='differs'):authorized_index(items,package)

@pytest.mark.parametrize('cite',[{'evidence_id':'missing','quote':TEXT},
    {'evidence_id':'e','quote':'not an exact source quotation'},
    {'evidence_id':'e','quote':'x'*301}, {'evidence_id':'e','quote':'short'}])
def test_invalid_id_quote_or_length(cite):
    evidence,package=fixture()
    with pytest.raises(ValueError):check_reference(cite,'counterevidence','c',authorized_index(evidence,package))

@pytest.mark.parametrize('role,claim,relationship',[('invalid','c',None),('supporting','',None),('counterevidence','c','supports')])
def test_invalid_claim_relationship(role,claim,relationship):
    evidence,package=fixture()
    with pytest.raises(ValueError):check_reference({'evidence_id':'e','quote':TEXT},role,claim,authorized_index(evidence,package),relationship=relationship)

def response():
    evidence,package=fixture();result=output({'evidence':evidence,'sections':config()['sections']});enhanced(result)
    return evidence,package,result

def test_duplicate_quote_and_role_switch_do_not_bypass_checks():
    evidence,package,result=response();claim=result['sections'][0]['claims'][0]
    claim['citations'].append(deepcopy(claim['citations'][0]))
    with pytest.raises(ValueError,match='Duplicate'):challenge(result=result,evidence=evidence,package=package,schema=model_schema(schema_for(config())))

def test_label_without_actual_reference_not_proof_of_counter_relationship():
    evidence,package,result=response();result['sections'][0]['claims'][0]['counterevidence_status']='found'
    with pytest.raises(ValueError,match='not attached'):challenge(result=result,evidence=evidence,package=package,schema=model_schema(schema_for(config())))

def test_exact_reference_does_not_establish_semantic_opposition():
    evidence,package=fixture();r=check_reference({'evidence_id':'e','quote':TEXT},'counterevidence','claim',authorized_index(evidence,package))
    assert r['relationship']=='challenges' and r['semantic_relationship']=='pending_independent_review'

def test_denied_role_marker_cannot_be_switched_into_support_permission():
    evidence,package=fixture(False);evidence[0]['source_authorized']=True
    with pytest.raises(ValueError,match='differs'):authorized_index(evidence,package)
