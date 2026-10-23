import asyncio
import json

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.schemas.call import ExtractedValue, ExtractionOutput
from app.services.extraction import ExtractionService, rule_extract, validate_output


def run(c):
    return asyncio.run(c)


def test_rules_extract_category_consciousness_count_with_evidence():
    o = rule_extract("my father collapsed, he is not responding and not breathing, just him")
    assert o.reported_category.value == "cardiac" and o.reported_category.evidence == "not breathing"
    assert o.conscious.value is False and o.breathing.value is False
    assert o.patient_count.value == 1 and 0 < o.patient_count.confidence < 1


def test_rules_do_not_guess_when_vague():
    o = rule_extract("please help me")
    assert o.reported_category is None and o.conscious is None and o.patient_count is None


def test_rules_counts_and_trauma():
    o = rule_extract("car crash on the bypass, three people injured, one is awake")
    assert o.reported_category.value == "trauma" and o.patient_count.value == 3
    assert o.conscious.value is True


@pytest.mark.parametrize("bad", [
    {"reported_category": {"value": "diagnosis:flu", "confidence": 0.9, "evidence": "x"}},
    {"reported_category": {"value": "cardiac", "confidence": 1.7, "evidence": "x"}},
    {"reported_category": {"value": "cardiac", "confidence": 0.9, "evidence": ""}},
    {"conscious": {"value": "yes", "confidence": 0.9, "evidence": "x"}},
    {"patient_count": {"value": 0, "confidence": 0.9, "evidence": "x"}},
    {"patient_count": {"value": True, "confidence": 0.9, "evidence": "x"}},
    {"dispatch_vehicle": {"value": "AMB-01", "confidence": 1, "evidence": "x"}},   # extra field: forbidden
    {"urgency": {"value": 4, "confidence": 1, "evidence": "x"}},                    # cannot assign urgency
])
def test_strict_schema_rejects_invalid_output(bad):
    out, problems = validate_output(bad, "x")
    assert out is None and problems and problems[0].startswith("schema_invalid")


def test_invalid_json_and_hallucinated_evidence_rejected():
    assert validate_output("not json", "hello")[0] is None
    good = {"reported_category": {"value": "cardiac", "confidence": 0.9, "evidence": "heart attack"}}
    out, problems = validate_output(good, "he has a heart attack")
    assert out and not problems
    out, problems = validate_output(good, "he fell off a ladder")  # quote is not in the transcript
    assert out is None and problems == ["evidence_not_in_transcript:reported_category"]


def test_model_accepts_only_known_fields():
    with pytest.raises(ValidationError):
        ExtractionOutput(reported_category=ExtractedValue(value="other", confidence=0.5, evidence="e"), priority=1)


class FakeLLM:
    def __init__(self, reply=None, exc=None):
        self.reply, self.exc = reply, exc

    async def extract(self, text):
        if self.exc:
            raise self.exc
        return self.reply


def svc(llm):
    return ExtractionService(Settings(llm_extraction=True, anthropic_api_key="k"), llm)


def test_llm_valid_output_is_used():
    reply = json.dumps({"reported_category": {"value": "stroke", "confidence": 0.9, "evidence": "face is drooping"}})
    r = run(svc(FakeLLM(reply)).extract("her face is drooping"))
    assert r.adapter == "llm" and r.valid and not r.review_required
    assert r.output.reported_category.value == "stroke"


def test_llm_invalid_output_falls_back_and_goes_to_human_review():
    r = run(svc(FakeLLM('{"reported_category": {"value": "cardiac"}}')).extract("heart attack"))
    assert r.adapter == "rules" and not r.valid and r.review_required
    assert "llm_output_rejected" in r.review_reasons
    assert r.output.reported_category.value == "cardiac"  # still usable, but flagged


def test_llm_outage_falls_back_and_flags_review():
    r = run(svc(FakeLLM(exc=TimeoutError())).extract("car crash"))
    assert r.adapter == "rules" and r.review_required and r.review_reasons[0].startswith("llm_unavailable")


def test_llm_disabled_by_default_and_needs_no_key():
    s = ExtractionService(Settings())
    assert not s.llm_enabled
    r = run(s.extract("chest pain"))
    assert r.adapter == "rules" and r.valid and not r.review_required
