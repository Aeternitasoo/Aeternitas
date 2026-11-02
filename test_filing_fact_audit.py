import pytest

from filing_fact_audit import InlineFacts


def test_inline_audit_checks_context_currency_scale_and_entity_not_only_visible_number():
    html='''<xbrli:context id="c"><xbrli:entity><xbrli:identifier>0000320193</xbrli:identifier></xbrli:entity>
    <xbrli:period><xbrli:startDate>2024-01-01</xbrli:startDate><xbrli:endDate>2024-12-31</xbrli:endDate></xbrli:period></xbrli:context>
    <xbrli:unit id="u"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
    <ix:nonFraction id="f" contextRef="c" unitRef="u" name="us-gaap:OperatingIncomeLoss" scale="6" sign="-" format="ixt:num-dot-decimal">1,234</ix:nonFraction>'''
    fact={"taxonomy":"us-gaap","tag":"OperatingIncomeLoss","period_start":"2024-01-01", "period_end":"2024-12-31","value":"-1234000000"}
    parser=InlineFacts();parser.feed(html)
    assert parser.match(fact,"0000320193")[0]["value"]=="-1234000000"
    for broken in (html.replace("iso4217:USD","iso4217:EUR"),html.replace('id="c">','id="c"><xbrldi:explicitMember>segment</xbrldi:explicitMember>'),html.replace("0000320193","0000789019")):
        parser=InlineFacts();parser.feed(broken)
        with pytest.raises(ValueError):parser.match(fact,"0000320193")
