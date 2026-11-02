"""Small offline boundary regressions for reusable Lab financial/stream code.

All inputs are synthetic; no files, credentials, database or HTTP transport.
"""

from decimal import localcontext
import json

import httpx
import pytest

from chatgpt_stream import read_plan_response
from research_runtime import Budget
from sources.sec import SourceError
from test_financial_facts import make_facts, rows, summarize


class Chunks(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    def __iter__(self):
        yield from self.chunks


def consume(chunks, budget=None):
    response = httpx.Response(200, stream=Chunks(chunks))
    try:
        return read_plan_response(response, budget if budget is not None else Budget())
    finally:
        response.close()


def completed_wire(*, line_end="\n"):
    result = {"claims": [], "gaps": ["本例仅验证中文分块解析"]}
    event = {"type": "response.completed", "response": {
        "status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": json.dumps(result, ensure_ascii=False)}]}]}}
    return ("data: " + json.dumps(event, ensure_ascii=False) + line_end * 2).encode("utf-8"), result


def test_financial_half_even_rounding_survives_low_global_decimal_precision():
    raw = make_facts()
    rows(raw)[1]["val"] = 20000
    rows(raw, "operating_income")[1]["val"] = 201
    rows(raw, "net_income")[1]["val"] = -203
    # Exact ratios are 1.005% and -1.015%; financial output uses half-even.
    with localcontext() as context:
        context.prec = 4
        current = summarize(raw)["annual_periods"][0]
    assert current["operating_margin"]["value"] == "1.00"
    assert current["net_margin"]["value"] == "-1.02"


def test_sse_crlf_and_chinese_utf8_can_be_split_at_every_byte():
    wire, expected = completed_wire(line_end="\r\n")
    actual = consume(wire[index:index + 1] for index in range(len(wire)))
    assert actual["status"] == "completed"
    assert json.loads(actual["output"][0]["content"][0]["text"]) == expected


def test_sse_rechecks_deadline_before_accepting_completion():
    class ExpiringBudget:
        checks = 0

        def remaining(self):
            self.checks += 1
            if self.checks == 2:
                raise SourceError("Synthetic completion deadline exceeded")
            return 1

    wire, _ = completed_wire()
    budget = ExpiringBudget()
    with pytest.raises(SourceError, match="completion deadline exceeded"):
        consume([wire], budget)
    assert budget.checks == 2


def test_sse_heartbeat_only_stream_cannot_exceed_total_wire_budget():
    # Each comment is individually bounded, but the full stream exceeds 2 MiB.
    comment = b":" + b" " * 65500 + b"\n\n"
    with pytest.raises(SourceError, match="local byte budget"):
        consume(comment for _ in range(33))


@pytest.mark.parametrize("terminated", [False, True])
def test_sse_rejects_oversized_event_with_or_without_separator(terminated):
    wire = b":" + b" " * 131072 + (b"\n\n" if terminated else b"")
    with pytest.raises(SourceError, match="event exceeds local size budget"):
        consume([wire])
