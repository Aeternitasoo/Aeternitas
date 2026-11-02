"""M4 contract boundaries are independent of model/network/source retrieval."""

from copy import deepcopy

import pytest

from analysis_contracts import (
    MAX_CLAIM_CHARACTERS, MAX_CLAIMS, MAX_GAP_CHARACTERS, MAX_GAPS,
    MAX_CITATIONS, MAX_QUOTE_CHARACTERS, MIN_CITATIONS, MIN_QUOTE_CHARACTERS,
    MODEL_RESULT_SCHEMA, PROMPT_VERSION, RESULT_SCHEMA, RESULT_SCHEMA_VERSION, ROLE_PROMPT_VERSIONS,
    ROLES, ROLE_TASKS, prompt_instruction, validate_analysis,
)


EVIDENCE = [
    {"id": "E-supplied", "text": "Exact supplied source sentence; revenue was USD 123 in 2025."},
    {"id": "E-other", "text": "A separate supplied source sentence for the contrary position."},
    {"id": "E-third", "text": "Another independent supplied source sentence in the same scope."},
]


def valid_result(text="经营解释仍需核对原文"):
    return {"claims": [{"text": text, "stance": "neutral", "citations": [
        {"evidence_id": EVIDENCE[0]["id"], "quote": EVIDENCE[0]["text"]}
    ]}], "gaps": []}


def test_valid_result_keeps_numbers_only_in_exact_quote_and_does_not_mutate():
    result, evidence = valid_result(), deepcopy(EVIDENCE)
    original_result, original_evidence = deepcopy(result), deepcopy(evidence)
    assert validate_analysis(result, evidence) is result
    assert result == original_result and evidence == original_evidence


@pytest.mark.parametrize("number", ["123", "１２３", "١٢٣", "१२३", "²", "Ⅳ", "½", "一百万元", "壹佰万元",
                                    "百分之二十", "二成", "三倍", "两亿美元", "二〇二六年", "翻倍", "减半",
                                    "一半", "一亿", "一千", "零点五元"])
@pytest.mark.parametrize("field", ["text", "gap"])
def test_ascii_unicode_and_explicit_chinese_values_cannot_bypass_number_policy(number, field):
    result = valid_result("经营情况含" + number)
    if field == "gap":
        result = {"claims": [], "gaps": ["仍需核实" + number]}
    with pytest.raises(ValueError, match="Put numbers|without unverified numbers"):
        validate_analysis(result, EVIDENCE)


@pytest.mark.parametrize("text", [
    "一方面经营受到需求影响，另一方面需检查成本", "公司的一般经营情况仍待核实", "资料尚不足以形成一致判断",
    "不要把单一成本因素当作全部原因", "万一材料有误仍应回查原文", "不能一成不变地沿用旧解释",
    "经营解释仍有一定局限", "新闻指出半导体需求受到影响",
])
def test_ordinary_chinese_words_are_not_mistaken_for_quantified_financial_values(text):
    assert validate_analysis(valid_result(text), EVIDENCE)


@pytest.mark.parametrize("mutate,match", [
    (lambda result: result.update(extra="ignored"), "only claims and gaps"),
    (lambda result: result["claims"][0].update(extra="ignored"), "Invalid claim structure"),
    (lambda result: result["claims"][0]["citations"][0].update(source="outside"), "Invalid citation structure"),
    (lambda result: result["claims"][0].update(stance=[]), "Invalid stance"),
    (lambda result: result["claims"][0].update(stance="buy"), "Invalid stance"),
    (lambda result: result["claims"][0].update(text=" \n\t"), "Claim text limit"),
    (lambda result: result.update(gaps=[" \n\t"]), "Gap must be concise"),
])
def test_extra_fields_invalid_stances_and_blank_strings_rejected(mutate, match):
    result = valid_result()
    mutate(result)
    with pytest.raises(ValueError, match=match):
        validate_analysis(result, EVIDENCE)


def test_schema_bounds_and_program_boundaries_agree():
    properties = RESULT_SCHEMA["properties"]
    claim = properties["claims"]["items"]["properties"]
    quote = claim["citations"]["items"]["properties"]["quote"]
    assert properties["claims"]["maxItems"] == MAX_CLAIMS == 6
    assert properties["gaps"]["maxItems"] == MAX_GAPS == 6
    assert claim["text"]["minLength"] == properties["gaps"]["items"]["minLength"] == 1
    assert claim["text"]["maxLength"] == MAX_CLAIM_CHARACTERS == 1000
    assert properties["gaps"]["items"]["maxLength"] == MAX_GAP_CHARACTERS == 600
    assert claim["citations"]["minItems"] == MIN_CITATIONS == 1
    assert claim["citations"]["maxItems"] == MAX_CITATIONS == 3
    assert quote["minLength"] == MIN_QUOTE_CHARACTERS == 12
    assert quote["maxLength"] == MAX_QUOTE_CHARACTERS == 300
    model_citations = MODEL_RESULT_SCHEMA["properties"]["claims"]["items"]["properties"]["citations"]
    assert "uniqueItems" not in model_citations
    assert model_citations["minItems"] == MIN_CITATIONS
    assert model_citations["maxItems"] == MAX_CITATIONS
    assert model_citations["items"] == claim["citations"]["items"]
    assert claim["citations"]["uniqueItems"] is True

    source = [{"id": "E", "text": "原" * (MAX_QUOTE_CHARACTERS + 1)}]
    result = {"claims": [{"text": "论" * MAX_CLAIM_CHARACTERS, "stance": "support",
                          "citations": [{"evidence_id": "E", "quote": "原" * MIN_QUOTE_CHARACTERS}]}],
              "gaps": ["缺" * MAX_GAP_CHARACTERS]}
    assert validate_analysis(result, source)
    result["claims"][0]["citations"][0]["quote"] = "原" * MAX_QUOTE_CHARACTERS
    assert validate_analysis(result, source)
    result["claims"][0]["citations"][0]["quote"] += "原"
    with pytest.raises(ValueError, match="Citation quote must contain"):
        validate_analysis(result, source)
    result["claims"][0]["citations"][0]["quote"] = "原" * (MIN_QUOTE_CHARACTERS - 1)
    with pytest.raises(ValueError, match="Citation quote must contain"):
        validate_analysis(result, source)


@pytest.mark.parametrize("field,limit", [("claims", MAX_CLAIMS), ("gaps", MAX_GAPS)])
def test_output_list_upper_limits(field, limit):
    result = valid_result()
    sample = result["claims"][0] if field == "claims" else "需要其他原始资料"
    result[field] = [deepcopy(sample) for _ in range(limit)]
    assert validate_analysis(result, EVIDENCE)
    result[field].append(deepcopy(sample))
    with pytest.raises(ValueError, match="Output list limit"):
        validate_analysis(result, EVIDENCE)


@pytest.mark.parametrize("field,limit", [("text", MAX_CLAIM_CHARACTERS), ("gap", MAX_GAP_CHARACTERS)])
def test_string_upper_limits(field, limit):
    result = valid_result()
    if field == "text":
        result["claims"][0]["text"] = "论" * (limit + 1)
    else:
        result["gaps"] = ["缺" * (limit + 1)]
    with pytest.raises(ValueError, match="Claim text limit|Gap must be concise"):
        validate_analysis(result, EVIDENCE)


def test_citation_count_and_uniqueness_are_enforced_by_identity():
    result = valid_result()
    result["claims"][0]["citations"] = [{"evidence_id": item["id"], "quote": item["text"]}
                                         for item in EVIDENCE]
    assert validate_analysis(result, EVIDENCE)
    result["claims"][0]["citations"].append(deepcopy(result["claims"][0]["citations"][0]))
    with pytest.raises(ValueError, match="one to three citations"):
        validate_analysis(result, EVIDENCE)
    result = valid_result()
    result["claims"][0]["citations"].append({"evidence_id": EVIDENCE[0]["id"], "quote": EVIDENCE[0]["text"][1:]})
    with pytest.raises(ValueError, match="Duplicate citation evidence id"):
        validate_analysis(result, EVIDENCE)


def test_duplicate_input_evidence_ids_fail_even_if_one_would_have_matched():
    evidence = deepcopy(EVIDENCE)
    evidence.append({"id": evidence[0]["id"], "text": "Conflicting source identity and different text."})
    with pytest.raises(ValueError, match="Duplicate evidence id"):
        validate_analysis(valid_result(), evidence)


@pytest.mark.parametrize("mutation", ["unknown_id", "changed_punctuation", "fabricated_substring", "combined_quote"])
def test_source_membership_and_exact_contiguous_quote_are_required(mutation):
    result = valid_result()
    cite = result["claims"][0]["citations"][0]
    if mutation == "unknown_id":
        cite["evidence_id"] = "E-from-another-role"
    elif mutation == "changed_punctuation":
        cite["quote"] = EVIDENCE[0]["text"].replace(";", ",")
    elif mutation == "combined_quote":
        cite["quote"] = EVIDENCE[0]["text"][:15] + EVIDENCE[0]["text"][-15:]
    else:
        cite["quote"] = "A financial result that was never supplied."
    with pytest.raises(ValueError, match="outside this role|not an exact supplied source substring"):
        validate_analysis(result, EVIDENCE)


def test_no_evidence_can_return_explicit_gap_but_not_empty_or_unsupported_claims():
    assert validate_analysis({"claims": [], "gaps": ["本次没有可用新闻材料"]}, [])
    with pytest.raises(ValueError, match="explicit information gap"):
        validate_analysis({"claims": [], "gaps": []}, [])
    with pytest.raises(ValueError, match="outside this role"):
        validate_analysis(valid_result(), [])


def test_each_role_has_versioned_specific_instructions_and_common_trust_boundary():
    assert RESULT_SCHEMA_VERSION != PROMPT_VERSION
    assert set(ROLE_PROMPT_VERSIONS) == set(ROLES) == set(ROLE_TASKS)
    instructions = {role: prompt_instruction(role) for role in ROLES}
    assert len(set(instructions.values())) == len(ROLES)
    for role, instruction in instructions.items():
        assert ROLE_PROMPT_VERSIONS[role] in instruction
        assert PROMPT_VERSION in instruction and RESULT_SCHEMA_VERSION in instruction
        assert ROLE_TASKS[role] in instruction
        assert "数据" in instruction and "shell" in instruction and "凭据" in instruction
        assert "previous_attempt_output_untrusted" in instruction
    assert "financial_fact" in instructions["fundamentals"]
    assert "只引用本角色收到的 news" in instructions["events"]
    assert "哪两种表述不一致" in instructions["risk"]
    assert "prior_analyses_untrusted" in instructions["risk"]
    assert "回答本次用户研究问题" in instructions["synthesis"]
    with pytest.raises(ValueError, match="Unknown analysis role"):
        prompt_instruction("../../outside")
