from __future__ import annotations

import json
import re
from typing import Any

import httpx

from app.config import Settings
from app.models import ClassificationResult, SourceItem
from app.pipeline.extract import extract_evidence_lines, extract_experience_range
from app.pipeline.evidence import recruiting_evidence
from app.pipeline.normalize import NormalizedItem, normalize_item
from app.pipeline.score import apply_scores


VC_TERMS = ("VC", "벤처캐피탈", "벤처투자", "투자심사", "투자본부", "투자팀", "신기술금융", "신기사", "CVC", "스타트업 투자")
PE_TERMS = ("PE", "PEF", "Private Equity", "사모투자", "Buyout", "바이아웃", "Private Debt", "기업투자")
IB_TERMS = ("IB", "Investment Banking", "기업금융", "M&A", "ECM", "DCM", "IPO", "인수금융", "구조화금융", "PF", "Project Finance", "Syndication", "Coverage", "커버리지", "메자닌")
ADJACENT_TERMS = ("FAS", "Transaction", "Deal", "Valuation", "Financial Modeling", "Research", "RA", "대체투자", "기업분석", "산업분석")
NEGATIVE_TERMS = ("경영지원", "펀드관리", "회계", "컴플라이언스", "리스크관리", "운용지원", "백오피스", "HR", "총무", "마케팅")
FRONT_TERMS = ("투자심사", "투자본부", "투자팀", "투자검토", "산업분석", "기업분석", "재무모델", "Financial Modeling", "Valuation", "Deal", "M&A", "ECM", "DCM", "IPO", "기업금융", "인수금융", "구조화금융", "PF")
ROLE_TERMS = {
    "Investment": ("투자", "투자심사", "investment", "portfolio selection"),
    "Deal Execution": ("deal", "m&a", "인수", "거래", "transaction"),
    "Research": ("research", "리서치", "기업분석", "산업분석", "RA"),
    "Portfolio Management": ("포트폴리오", "portfolio management", "value creation"),
    "Fundraising / IR": ("fundraising", "fund raising", "IR", "LP", "출자", "펀드레이징"),
    "Fund Management": ("펀드관리", "펀드 운용", "fund administration"),
    "Risk": ("risk", "리스크", "위험관리", "신용공여", "신용심사", "기업금융 심사"),
    "Compliance": ("compliance", "컴플라이언스"),
    "Operations": ("operations", "운용지원", "운영지원", "서무"),
    "Finance / Accounting": ("회계", "재무회계", "accounting", "finance"),
    "Sales": ("영업", "sales"),
    "Marketing": ("마케팅", "marketing"),
    "HR / Admin": ("경영지원", "인사", "총무", "hr", "admin"),
    "Technology": ("개발", "engineer", "technology", "IT"),
}


def _term_present(text: str, term: str) -> bool:
    lowered, needle = text.casefold(), term.casefold()
    if re.fullmatch(r"[a-z]{1,3}", needle):
        return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", lowered) is not None
    return needle in lowered


def _contains(text: str, terms: tuple[str, ...]) -> bool:
    return any(_term_present(text, term) for term in terms)


def _sector(text: str) -> tuple[str, str | None]:
    if _contains(text, IB_TERMS):
        return "IB", "General IB"
    if _contains(text, PE_TERMS):
        return "PE", "Investment"
    if _contains(text, VC_TERMS):
        return "VC", "Investment"
    if _contains(text, ("대체투자", "alternative investment")):
        return "Alternative Investment", "Investment"
    if _contains(text, ("자산운용", "asset management", "운용팀")):
        return "Asset Management", "Investment"
    if _contains(text, ("research", "리서치", "RA")):
        return "Research", "Research"
    if _contains(text, ("corporate finance", "재무", "finance")):
        return "Corporate Finance", "Corporate Finance"
    return "Unknown", None


def _weighted_sector(item: SourceItem, normalized: NormalizedItem, target_text: str) -> tuple[str, str | None]:
    fields = ((normalized.title, 6), (normalized.company or "", 5), (target_text, 1))
    groups = {"VC": VC_TERMS, "PE": PE_TERMS, "IB": IB_TERMS}
    scores = {
        sector: sum(weight for text, weight in fields for term in terms if _term_present(text, term))
        for sector, terms in groups.items()
    }
    best = max(scores, key=scores.get)
    if scores[best] > 0:
        if best == "VC" and any(_term_present(normalized.title + " " + (normalized.company or ""), term) for term in ("CVC", "기업주도형 벤처캐피탈")):
            return "CVC", "Investment"
        return best, "General IB" if best == "IB" else "Investment"
    return _sector(target_text)


def _seniority(text: str) -> tuple[str, int | None, int | None]:
    minimum, maximum = extract_experience_range(text)
    if _contains(text, ("인턴", "internship", "intern", "체험형 인턴", "채용연계형 인턴")):
        return "Intern", minimum, maximum
    if _contains(text, ("trainee", "트레이니")):
        return "Trainee", minimum, maximum
    if _contains(text, ("신입", "new graduate", "졸업예정")):
        return "New Graduate", minimum, maximum
    if minimum is not None and maximum is not None and maximum <= 3:
        return "Junior", minimum, maximum
    if minimum is not None and minimum >= 4:
        return "Experienced", minimum, maximum
    if _contains(text, ("경력직", "경력", "senior", "experienced")):
        return "Experienced", minimum, maximum
    return "Unknown", minimum, maximum


def _role_family(text: str) -> str:
    if _contains(text, ("신용공여", "신용심사", "기업금융 심사", "리스크", "위험관리")):
        return "Risk"
    if _contains(text, ("서무", "운용지원", "운영지원")):
        return "Operations"
    for role, terms in ROLE_TERMS.items():
        if _contains(text, tuple(terms)):
            return role
    return "Other"


def rule_based_classify(item: SourceItem) -> ClassificationResult:
    normalized = normalize_item(item)
    report = recruiting_evidence(item)
    text = report.target_text
    sector, subsector = _weighted_sector(item, normalized, text)
    seniority, experience_min, experience_max = report.seniority, report.experience_min, report.experience_max
    role = _role_family(report.duty_text)
    front_office = role not in {"Risk", "Operations", "Compliance", "HR / Admin"} and _contains(report.duty_text, FRONT_TERMS) and not (
        _contains(normalized.title, NEGATIVE_TERMS) and not _contains(normalized.title, FRONT_TERMS)
    )
    explicit_student = _contains(text, ("대학생", "재학생", "졸업예정자", "학부생", "undergraduate", "student"))
    explicit_not_student = report.explicit_senior
    conversion = True if _contains(text, ("정규직 전환", "채용연계", "전환 가능", "conversion")) else None
    student_eligible = False if explicit_not_student else True if explicit_student else None
    evidence = extract_evidence_lines(text, VC_TERMS + PE_TERMS + IB_TERMS + ADJACENT_TERMS, limit=6)
    confidence = 0.88 if sector != "Unknown" and seniority != "Unknown" else 0.62 if sector != "Unknown" else 0.35
    if not evidence:
        evidence = ["소스 원문을 보존했으며 분류 근거가 제한적임"]
    result = ClassificationResult(
        sector=sector,
        subsector=subsector,
        role_family=role,
        seniority=seniority,
        front_office=front_office,
        student_eligible=student_eligible,
        conversion_possible=conversion,
        experience_min=experience_min,
        experience_max=experience_max,
        classification_confidence=confidence,
        reasoning_short=evidence,
    )
    return apply_scores(
        validate_source_result(item, result),
        active=item.active,
        location=str(item.raw_metadata.get("location") or "") or None,
        deadline=item.deadline,
        requirements_present=bool(item.body_text),
    )


def validate_source_result(item: SourceItem, result: ClassificationResult) -> ClassificationResult:
    """The deterministic source gate is applied to both rule and LLM results."""
    report = recruiting_evidence(item)
    normalized = normalize_item(item)
    sector, subsector = _weighted_sector(item, normalized, report.target_text)
    role = _role_family(report.duty_text)
    front = role not in {"Risk", "Operations", "Compliance", "HR / Admin"} and _contains(report.duty_text, FRONT_TERMS)
    return result.model_copy(update={
        "seniority": report.seniority,
        "experience_min": report.experience_min,
        "experience_max": report.experience_max,
        "sector": sector,
        "subsector": subsector,
        "role_family": role,
        "front_office": front,
        "student_eligible": False if report.explicit_senior else result.student_eligible,
    })


CLASSIFICATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "sector": {"type": "string", "enum": ["VC", "CVC", "PE", "IB", "Alternative Investment", "Asset Management", "Research", "Corporate Finance", "Other Finance", "Irrelevant", "Unknown"]},
        "subsector": {"type": ["string", "null"]},
        "role_family": {"type": "string", "enum": list(ROLE_TERMS) + ["Portfolio Management", "Fundraising / IR", "Other"]},
        "seniority": {"type": "string", "enum": ["Intern", "Trainee", "New Graduate", "Junior", "Experienced", "Senior", "Unknown"]},
        "front_office": {"type": "boolean"},
        "student_eligible": {"type": ["boolean", "null"]},
        "conversion_possible": {"type": ["boolean", "null"]},
        "experience_min": {"type": ["integer", "null"]},
        "experience_max": {"type": ["integer", "null"]},
        "classification_confidence": {"type": "number"},
        "reasoning_short": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
    },
    "required": ["sector", "subsector", "role_family", "seniority", "front_office", "student_eligible", "conversion_possible", "experience_min", "experience_max", "classification_confidence", "reasoning_short"],
}


class ClassificationError(RuntimeError):
    pass


class OpenAIClassifier:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=settings.http_timeout_seconds)
        self._external_client = client is not None

    async def close(self) -> None:
        if not self._external_client:
            await self.client.aclose()

    async def classify(self, item: SourceItem) -> ClassificationResult:
        if not self.settings.openai_api_key or not self.settings.openai_model_classifier:
            raise ClassificationError("OpenAI classifier is not configured")
        payload = {
            "model": self.settings.openai_model_classifier,
            "input": [
                {"role": "system", "content": [{"type": "input_text", "text": "You classify a Korean finance recruiting source. Use only facts in the source. Unknown facts must be null. Do not copy source text into reasoning_short; keep it short."}]},
                {"role": "user", "content": [{"type": "input_text", "text": json.dumps({"title": item.title_raw, "company": item.company_raw, "body": item.body_text, "metadata": item.raw_metadata}, ensure_ascii=False)}]},
            ],
            "text": {"format": {"type": "json_schema", "name": "recruiting_classification", "strict": True, "schema": CLASSIFICATION_SCHEMA}},
        }
        response = await self.client.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {self.settings.openai_api_key}", "Content-Type": "application/json"},
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
        output_text = data.get("output_text")
        if not output_text:
            for output in data.get("output", []):
                for content in output.get("content", []):
                    if content.get("type") in {"output_text", "text"}:
                        output_text = content.get("text")
                        break
        if not output_text:
            raise ClassificationError("OpenAI response did not contain structured output")
        try:
            result = ClassificationResult.model_validate(json.loads(output_text))
        except (json.JSONDecodeError, ValueError) as error:
            raise ClassificationError("OpenAI structured output failed validation") from error
        return apply_scores(validate_source_result(item, result), active=item.active, deadline=item.deadline, requirements_present=bool(item.body_text))


async def classify_item(item: SourceItem, settings: Settings) -> ClassificationResult:
    if settings.openai_api_key and settings.openai_model_classifier:
        classifier = OpenAIClassifier(settings)
        try:
            return await classifier.classify(item)
        except Exception:
            fallback = rule_based_classify(item)
            return fallback.model_copy(update={"status": "classification_pending"})
        finally:
            await classifier.close()
    return rule_based_classify(item)
