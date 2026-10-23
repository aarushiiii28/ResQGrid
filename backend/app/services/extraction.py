"""M12 extraction adapter. Fills a strict schema (field, confidence, evidence) from the caller's
words. Default is a deterministic rule-based adapter that needs no key and no network; an LLM
adapter is optional behind LLM_EXTRACTION + ANTHROPIC_API_KEY. Neither adapter may diagnose,
dispatch, downgrade urgency or reassign: they only propose field values with evidence, and
invalid output or conflicts are flagged for human review."""

import json
import re

import httpx
from pydantic import ValidationError

from app.core.config import Settings
from app.schemas.call import CATEGORIES, ExtractedValue, ExtractionOutput, ExtractionResult

# severity order: the first matching category wins (most response-relevant first)
CATEGORY_RULES: list[tuple[str, float, list[str]]] = [
    ("cardiac", 0.85, ["heart attack", "chest pain", "cardiac", "no pulse", "not breathing", "stopped breathing", "heart"]),
    ("stroke", 0.85, ["stroke", "face is drooping", "face drooping", "slurred", "cannot move one side", "can't move his arm", "can't move her arm"]),
    ("neonatal", 0.8, ["newborn", "new born", "baby", "infant", "premature", "just gave birth"]),
    ("trauma", 0.8, ["accident", "crash", "collision", "hit by", "stabbed", "shot", "fell from", "fell down", "bleeding", "broken bone", "fracture", "injured", "injury"]),
    ("breathing", 0.8, ["can't breathe", "cannot breathe", "cant breathe", "struggling to breathe", "choking", "choked", "asthma", "gasping", "wheezing"]),
    ("unconscious", 0.75, ["unconscious", "unresponsive", "passed out", "fainted", "collapsed", "not waking", "won't wake", "not responding"]),
]
UNCONSCIOUS = ["unconscious", "unresponsive", "passed out", "not waking", "won't wake", "not responding", "collapsed", "fainted", "not conscious"]
CONSCIOUS = ["awake", "is conscious", "talking", "responding", "answering", "alert", "speaking", "conscious"]
BREATHING_NO = ["not breathing", "stopped breathing", "no breathing", "isn't breathing", "can't breathe", "cannot breathe"]
BREATHING_YES = ["breathing", "is breathing", "still breathing"]
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
                "a couple": 2, "both": 2}

OPERATOR_SYSTEM_PROMPT = (
    "You extract structured fields from an emergency caller's words for a SIMULATED dispatch demo. "
    "Return ONLY JSON with optional keys reported_category (one of "
    + ", ".join(CATEGORIES)
    + "), conscious (bool), breathing (bool), patient_count (int). Each value is an object "
    '{"value":..., "confidence":0..1, "evidence":"exact quote from the caller"}. '
    "Never diagnose, never recommend dispatch, never assess urgency, never invent facts. "
    "Omit a key when the caller did not say it."
)


def _find(text: str, needles: list[str]) -> str | None:
    for n in needles:
        i = text.find(n)
        if i >= 0:
            return text[i : i + len(n)]
    return None


def rule_extract(caller_text: str) -> ExtractionOutput:
    t = caller_text.lower()
    out: dict = {}
    for cat, conf, needles in CATEGORY_RULES:
        ev = _find(t, needles)
        if ev:
            out["reported_category"] = ExtractedValue(value=cat, confidence=conf, evidence=ev)
            break
    if not out and _find(t, ["sick", "hurt", "pain", "help", "ill", "emergency"]):
        pass  # vague: leave category unknown rather than guess
    ev_no = _find(t, UNCONSCIOUS)
    masked = t
    for phrase in UNCONSCIOUS:  # "not responding" must not count as "responding"
        masked = masked.replace(phrase, " " * len(phrase))
    ev_yes = _find(masked, [w for w in CONSCIOUS if w != "conscious"] + ["is conscious"])
    if ev_no and not (ev_yes and masked.find(ev_yes) > t.find(ev_no)):
        out["conscious"] = ExtractedValue(value=False, confidence=0.85, evidence=ev_no)
    elif ev_yes:
        out["conscious"] = ExtractedValue(value=True, confidence=0.7, evidence=ev_yes)
    b_no = _find(t, BREATHING_NO)
    if b_no:
        out["breathing"] = ExtractedValue(value=False, confidence=0.85, evidence=b_no)
    elif re.search(r"\b(is|still)\s+breathing\b", t):
        m = re.search(r"\b(is|still)\s+breathing\b", t)
        out["breathing"] = ExtractedValue(value=True, confidence=0.7, evidence=m.group(0))
    m = re.search(r"\b(\d{1,3})\s*(people|persons|patients|injured|victims|men|women|children|kids)\b", t)
    if m and 1 <= int(m.group(1)) <= 500:
        out["patient_count"] = ExtractedValue(value=int(m.group(1)), confidence=0.85, evidence=m.group(0))
    else:
        m = re.search(r"\b(one|two|three|four|five|six|seven|eight|nine|ten|a couple|both)\b( of (them|us))?( people| persons| patients| person| men| women| children| adults| kids)?", t)
        if m and (m.group(4) or m.group(2) or m.group(1) in ("a couple", "both")):
            out["patient_count"] = ExtractedValue(value=NUMBER_WORDS[m.group(1)], confidence=0.7, evidence=m.group(0).strip())
        elif re.search(r"\b(a man|a woman|someone|somebody|my (father|mother|dad|mom|husband|wife|son|daughter|friend)|he is|she is|a boy|a girl|a child|the person)\b", t):
            ev = re.search(r"\b(a man|a woman|someone|somebody|my (father|mother|dad|mom|husband|wife|son|daughter|friend)|he is|she is|a boy|a girl|a child|the person)\b", t).group(0)
            out["patient_count"] = ExtractedValue(value=1, confidence=0.55, evidence=ev)
    return ExtractionOutput(**out)


def validate_output(raw: str | dict, caller_text: str) -> tuple[ExtractionOutput | None, list[str]]:
    """Strict validation of adapter output. Returns (output, problems). Evidence must literally
    appear in what the caller said, which blocks invented facts."""
    problems: list[str] = []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        out = ExtractionOutput.model_validate(data)
    except (json.JSONDecodeError, ValidationError, TypeError) as e:
        return None, [f"schema_invalid: {type(e).__name__}"]
    low = caller_text.lower()
    for name, v in out:
        if v is not None and v.evidence.lower() not in low:
            problems.append(f"evidence_not_in_transcript:{name}")
    return (out if not problems else None), problems


class LLMExtractor:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.s, self.transport = settings, transport

    async def extract(self, caller_text: str) -> str:
        async with httpx.AsyncClient(transport=self.transport, timeout=8.0) as c:
            r = await c.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": self.s.anthropic_api_key, "anthropic-version": "2023-06-01"},
                json={"model": self.s.llm_model, "max_tokens": 400, "system": OPERATOR_SYSTEM_PROMPT,
                      "messages": [{"role": "user", "content": caller_text}]},
            )
            r.raise_for_status()
            return r.json()["content"][0]["text"]


class ExtractionService:
    def __init__(self, settings: Settings, llm: LLMExtractor | None = None):
        self.s = settings
        self.llm = llm

    @property
    def llm_enabled(self) -> bool:
        return bool(self.s.llm_extraction and (self.s.anthropic_api_key or self.llm))

    async def extract(self, caller_text: str) -> ExtractionResult:
        if self.llm_enabled:
            adapter = self.llm or LLMExtractor(self.s)
            try:
                raw = await adapter.extract(caller_text)
                out, problems = validate_output(raw, caller_text)
                if out is not None:
                    return ExtractionResult(adapter="llm", output=out)
                fallback = rule_extract(caller_text)
                return ExtractionResult(adapter="rules", output=fallback, valid=False, review_required=True,
                                        review_reasons=["llm_output_rejected", *problems])
            except Exception as e:
                fallback = rule_extract(caller_text)
                return ExtractionResult(adapter="rules", output=fallback, valid=False, review_required=True,
                                        review_reasons=[f"llm_unavailable:{type(e).__name__}"])
        out = rule_extract(caller_text)
        # defence in depth: even our own rules must satisfy the strict schema + evidence check
        checked, problems = validate_output(out.model_dump(mode="json"), caller_text)
        if checked is None:
            return ExtractionResult(adapter="rules", output=ExtractionOutput(), valid=False,
                                    review_required=True, review_reasons=problems)
        return ExtractionResult(adapter="rules", output=checked)
