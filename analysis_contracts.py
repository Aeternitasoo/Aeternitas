"""Versioned analysis output contract; source trust is checked before selection.

The JSON schema describes the shape and bounds of an AnalystResult. Validation
also checks the evidence-dependent rules a schema cannot express: membership,
exact quotations and unique source identities. Neither check proves that a
quotation supports the meaning of a claim; that remains a separate review.
"""

from pathlib import Path
import re
import unicodedata


RESULT_SCHEMA_VERSION = "analyst-result-m4-v1"
PROMPT_VERSION = "evidence-roles-m6-v1"
HISTORICAL_PROMPT_VERSION = "evidence-roles-m4-v1"
SUPPORTED_PROMPT_VERSIONS = (HISTORICAL_PROMPT_VERSION, PROMPT_VERSION)
ROLES = ("fundamentals", "events", "risk", "synthesis")
ROLE_PROMPT_VERSIONS = {role: f"{role}-m6-v1" for role in ROLES}
# Retained as concise role descriptions for callers and synthetic providers.
ROLE_TASKS = {
    "fundamentals": "解释经营变化，仅根据财务事实、程序计算和财报片段。区分事实与可能解释。",
    "events": "整理本次新闻摘要中的事件，区分已经发生的事实与媒体判断；没有新闻则报告缺口。",
    "risk": "审阅已有分析及原始证据，指出冲突、口径、时间和信息缺口，提出有依据的反方判断。",
    "synthesis": "回答用户问题，综合支持与反对证据、主要风险和仍需研究的问题；不能新增外部事实。",
}
PROMPT_DIRECTORY = Path(__file__).resolve().parent / "prompts" / "m6"


def role_prompt_versions(version=None):
    version = version or PROMPT_VERSION
    if version == PROMPT_VERSION:
        return ROLE_PROMPT_VERSIONS
    if version == HISTORICAL_PROMPT_VERSION:
        return {role: f'{role}-m4-v1' for role in ROLES}
    raise ValueError('Unknown analysis prompt version')

MAX_CLAIMS = 6
MAX_GAPS = 6
MAX_CLAIM_CHARACTERS = 1000
MAX_GAP_CHARACTERS = 600
MIN_CITATIONS = 1
MAX_CITATIONS = 3
MIN_QUOTE_CHARACTERS = 12
MAX_QUOTE_CHARACTERS = 300

RESULT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "claims": {
            "type": "array", "minItems": 0, "maxItems": MAX_CLAIMS,
            "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "text": {"type": "string", "minLength": 1,
                             "maxLength": MAX_CLAIM_CHARACTERS, "pattern": r"\S"},
                    "stance": {"type": "string", "enum": ["support", "challenge", "neutral"]},
                    "citations": {
                        "type": "array", "minItems": MIN_CITATIONS, "maxItems": MAX_CITATIONS,
                        "uniqueItems": True,
                        "items": {
                            "type": "object", "additionalProperties": False,
                            "properties": {
                                "evidence_id": {"type": "string", "minLength": 1, "pattern": r"\S"},
                                "quote": {"type": "string", "minLength": MIN_QUOTE_CHARACTERS,
                                          "maxLength": MAX_QUOTE_CHARACTERS},
                            },
                            "required": ["evidence_id", "quote"],
                        },
                    },
                },
                "required": ["text", "stance", "citations"],
            },
        },
        "gaps": {
            "type": "array", "minItems": 0, "maxItems": MAX_GAPS,
            "items": {"type": "string", "minLength": 1,
                      "maxLength": MAX_GAP_CHARACTERS, "pattern": r"\S"},
        },
    },
    "required": ["claims", "gaps"],
}


def _model_schema(value):
    # The provider subset does not advertise uniqueItems. Keep the local schema
    # and the identity-aware validator stricter than its decoding constraints.
    if isinstance(value, dict):
        return {key: _model_schema(child) for key, child in value.items() if key != "uniqueItems"}
    if isinstance(value, list):
        return [_model_schema(child) for child in value]
    return value


MODEL_RESULT_SCHEMA = _model_schema(RESULT_SCHEMA)

# Chinese numerals are letters in Unicode, so category N alone does not catch
# prose such as “利润增长百分之二十”. Match clear quantities, not words such as
# “一方面”, “一致”, “一般”, “万一”, “单一成本” or “一起成长”.
_CN_NUMBER = "零〇○一二三四五六七八九十百千万亿兆两壹贰叁肆伍陆柒捌玖拾佰仟萬億"
_CN_TOKEN = rf"[{_CN_NUMBER}]+(?:[点點][{_CN_NUMBER}]+)?"
_CN_QUANTIFIED = re.compile(
    rf"(?:百分之|千分之|万分之){_CN_TOKEN}"
    rf"|{_CN_TOKEN}(?:个?百分点|[％%]|倍|美元|人民币|港元|欧元|日元|英镑|元|股|"
    rf"财年|年度|年|季度|个月|月|日|成(?!本|为|功|员|分|交|立|果|不变))"
    rf"|[一二三四五六七八九两壹贰叁肆伍陆柒捌玖][{_CN_NUMBER}]*[十百千万亿兆萬億]"
    rf"|(?:翻倍|减半|腰斩|一半|半数|近半|过半)"
)
_CN_FINANCIAL_BARE = re.compile(
    rf"(?:收入|营收|利润|现金流|估值|市值|负债|资产|毛利率|净利率|利润率|同比|环比)"
    rf"(?:约|大约|为|达|达到|增加|增长|减少|下降|提升|降至|升至|超过|不足|近|仅|了|是|至|：|:|\s)*"
    rf"{_CN_TOKEN}(?=$|[，。；、,.;：:\s])"
)


def has_unverified_number(text):
    """Reject numeric Unicode characters and explicit Chinese quantities."""
    return (any(unicodedata.category(character).startswith("N") for character in text)
            or _CN_QUANTIFIED.search(text) is not None
            or _CN_FINANCIAL_BARE.search(text) is not None)


def validate_analysis(result, evidence):
    """Validate a bounded result against precisely this role's supplied text.

    The caller owns company/date/source validation. Minimal frozen evidence
    fixtures therefore need only ``id`` and ``text`` here. Evidence and result
    objects are never changed by this function.
    """
    if not isinstance(result, dict) or set(result) != {"claims", "gaps"}:
        raise ValueError("Output must contain only claims and gaps")
    claims, gaps = result["claims"], result["gaps"]
    if (not isinstance(claims, list) or len(claims) > MAX_CLAIMS
            or not isinstance(gaps, list) or len(gaps) > MAX_GAPS):
        raise ValueError("Output list limit exceeded")
    if not claims and not gaps:
        raise ValueError("Empty analysis needs an explicit information gap")
    if not isinstance(evidence, (list, tuple)):
        raise ValueError("Role evidence must be a list of source objects")
    by_id = {}
    for source in evidence:
        if (not isinstance(source, dict) or not isinstance(source.get("id"), str)
                or not source["id"].strip() or not isinstance(source.get("text"), str)):
            raise ValueError("Role evidence needs a nonempty text id and source text")
        if source["id"] in by_id:
            raise ValueError("Duplicate evidence id in role input")
        by_id[source["id"]] = source
    for claim_index, claim in enumerate(claims):
        if not isinstance(claim, dict) or set(claim) != {"text", "stance", "citations"}:
            raise ValueError(f"claims[{claim_index}]: Invalid claim structure")
        text = claim["text"]
        if (not isinstance(text, str) or not text.strip()
                or len(text) > MAX_CLAIM_CHARACTERS):
            raise ValueError(f"claims[{claim_index}].text: Claim text limit exceeded")
        if has_unverified_number(text):
            raise ValueError(f"claims[{claim_index}].text: Put numbers in deterministic tables / original quotations, not free prose")
        if not isinstance(claim["stance"], str) or claim["stance"] not in {"support", "challenge", "neutral"}:
            raise ValueError(f"claims[{claim_index}].stance: Invalid stance")
        citations = claim["citations"]
        if not isinstance(citations, list) or not MIN_CITATIONS <= len(citations) <= MAX_CITATIONS:
            raise ValueError(f"claims[{claim_index}].citations: Each claim needs one to three citations")
        cited_ids = set()
        for cite_index, cite in enumerate(citations):
            location = f"claims[{claim_index}].citations[{cite_index}]"
            if not isinstance(cite, dict) or set(cite) != {"evidence_id", "quote"}:
                raise ValueError(f"{location}: Invalid citation structure")
            eid, quote = cite["evidence_id"], cite["quote"]
            if not isinstance(eid, str) or eid not in by_id:
                raise ValueError(f"{location}.evidence_id: Citation is outside this role's evidence set")
            if eid in cited_ids:
                raise ValueError(f"{location}.evidence_id: Duplicate citation evidence id within claim")
            cited_ids.add(eid)
            if not isinstance(quote, str) or not MIN_QUOTE_CHARACTERS <= len(quote) <= MAX_QUOTE_CHARACTERS:
                raise ValueError(f"{location}.quote: Citation quote must contain twelve to three hundred characters")
            if quote not in by_id[eid]["text"]:
                raise ValueError(f"{location}.quote: Citation quote is not an exact supplied source substring")
    for gap_index, gap in enumerate(gaps):
        if (not isinstance(gap, str) or not gap.strip()
                or len(gap) > MAX_GAP_CHARACTERS or has_unverified_number(gap)):
            raise ValueError(f"gaps[{gap_index}]: Gap must be concise text without unverified numbers")
    return result


def prompt_instruction(role, version=None):
    """Read the immutable-version common instructions and role instructions."""
    if role not in ROLES:
        raise ValueError("Unknown analysis role")
    version = version or PROMPT_VERSION
    role_prompt_versions(version)
    directory = (PROMPT_DIRECTORY if version == PROMPT_VERSION else
                 Path(__file__).resolve().parent / 'prompts' / 'm4')
    common = (directory / "common-v1.md").read_text(encoding="utf-8")
    specific = (directory / f"{role}-v1.md").read_text(encoding="utf-8")
    return common.strip() + "\n\n" + ROLE_TASKS[role] + "\n" + specific.strip()
