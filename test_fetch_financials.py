import httpx

import fetch_financials
from sources.sec import SecClient
from test_financial_facts import make_facts


def test_cli_writes_traceable_financial_input_and_does_not_claim_ai(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SEC_CONTACT", "test@example.com")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=make_facts()))
    monkeypatch.setattr(fetch_financials, "SecClient", lambda root, contact: SecClient(root, contact, transport=transport))
    assert fetch_financials.main(["AAPL", "--as-of", "2025-10-01", "--data-dir", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert "20.00%" in output and "尚不是 AI" in output
    assert len(list((tmp_path / "financials").glob("*.json"))) == 1
    assert len(list((tmp_path / "retrievals").glob("*.json"))) == 1


def test_malformed_company_fails_before_network_or_writing(tmp_path, capsys):
    assert fetch_financials.main(["../UNKNOWN", "--data-dir", str(tmp_path)]) == 1
    assert "stock ticker" in capsys.readouterr().err
    assert not list(tmp_path.rglob("*.json"))
