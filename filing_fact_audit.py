"""Bounded cross-check of selected USD facts against inline XBRL in saved HTML.

This is an audit of our three supported tags, not a general XBRL processor.
Unsupported transformations/contexts never count as a successful match.
"""

import re
from decimal import Decimal, localcontext
from html.parser import HTMLParser

from financial_facts import METRIC_TAGS
from models import normalize_cik


class InlineFacts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.contexts, self.units, self.facts = {}, {}, []
        self.context = self.unit = self.fact = self.field = None
        self.supported = {"us-gaap:" + tag for tags in METRIC_TAGS.values() for tag in tags}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "xbrli:context":
            self.context = {"id": attrs.get("id"), "dimensioned": False}
        elif tag == "xbrli:unit":
            self.unit = {"id": attrs.get("id"), "measures": [], "divided": False}
        elif self.context is not None and tag in {"xbrldi:explicitmember", "xbrldi:typedmember"}:
            self.context["dimensioned"] = True
        elif self.context is not None and tag in {"xbrli:startdate", "xbrli:enddate", "xbrli:identifier"}:
            self.field = (tag, [])
        elif self.unit is not None and tag == "xbrli:divide":
            self.unit["divided"] = True
        elif self.unit is not None and tag == "xbrli:measure":
            self.field = (tag, [])
        elif tag == "ix:nonfraction" and attrs.get("name") in self.supported:
            self.fact = {**attrs, "text": []}

    def handle_data(self, text):
        if self.field:
            self.field[1].append(text)
        if self.fact is not None:
            self.fact["text"].append(text)

    def handle_endtag(self, tag):
        if self.field and self.field[0] == tag:
            text = "".join(self.field[1]).strip()
            if tag == "xbrli:measure":
                self.unit["measures"].append(text)
            else:
                self.context[tag.split(":")[1]] = text
            self.field = None
        if tag == "xbrli:context" and self.context is not None:
            self.contexts[self.context["id"]] = self.context
            self.context = None
        elif tag == "xbrli:unit" and self.unit is not None:
            self.units[self.unit["id"]] = self.unit
            self.unit = None
        elif tag == "ix:nonfraction" and self.fact is not None:
            self.facts.append(self.fact)
            self.fact = None

    def match(self, selected, cik):
        matches = []
        for item in self.facts:
            if item["name"] != selected["taxonomy"] + ":" + selected["tag"]:
                continue
            context = self.contexts.get(item.get("contextref"), {})
            unit = self.units.get(item.get("unitref"), {})
            if context.get("dimensioned") or unit.get("divided") or unit.get("measures") != ["iso4217:USD"]:
                continue
            if context.get("startdate") != selected["period_start"] or context.get("enddate") != selected["period_end"]:
                continue
            if normalize_cik(context.get("identifier", "")) != cik:
                continue
            if item.get("format") not in {"ixt:num-dot-decimal", "ixt:numdotdecimal"}:
                continue
            text = "".join(item["text"]).strip().replace(",", "")
            if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", text):
                continue
            scale = item.get("scale", "0")
            if not re.fullmatch(r"-?[0-9]{1,2}", scale) or not -12 <= int(scale) <= 12:
                continue
            with localcontext() as ctx:
                ctx.prec = max(50, len(text) + 20)
                value = Decimal(text) * (Decimal(10) ** int(scale))
                if item.get("sign") == "-":
                    value = -value
            if value == Decimal(selected["value"]):
                matches.append({"html_fact_id": item.get("id"), "context_ref": item["contextref"],
                                "display_value": "".join(item["text"]), "scale": int(scale), "value": str(value),
                                "period_start": context["startdate"], "period_end": context["enddate"],
                                "unit": "USD", "consolidated_context": True})
        if not matches:
            raise ValueError("Selected financial observation has no matching supported consolidated inline XBRL fact")
        return matches
