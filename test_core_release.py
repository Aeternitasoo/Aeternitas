from scripts.check_core_release import matches
import subprocess
from pathlib import Path


def test_secret_shapes_are_reported_without_values():
    value=b'sk-proj-'+b'A'*40
    found=matches(value)
    assert found and found[0]['pattern']=='openai_key'
    assert 'A'*40 not in str(found)


def test_credential_url_and_private_key_are_not_allowed():
    assert matches(b'https:'+b'//user:'+b'veryprivatepassword'+b'@example.test/path')
    assert matches(b'-----BEGIN '+b'PRIVATE KEY-----')


def test_current_core_has_no_removed_price_import():
    import ast
    removed={'research_agent','lab_price_experiment','lab_agent_batch','prospective_snapshot'}
    root=Path(__file__).parent
    for p in root.glob('*.py'):
        for n in ast.walk(ast.parse(p.read_text())):
            if isinstance(n,ast.ImportFrom):assert n.module not in removed


def test_core_help_is_safe_from_external_cwd(tmp_path):
    import sys
    # Installed editable entrypoint imports modules from the checkout, without cwd assumptions.
    r=subprocess.run([sys.executable,'-B','-m','core_cli','--help'],cwd=tmp_path,capture_output=True,text=True,timeout=10)
    assert r.returncode==0 and 'research' in r.stdout
