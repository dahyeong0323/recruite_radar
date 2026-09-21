from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import yaml

from app.content_watchlist import content_company_for
from app.models import ClassificationResult, ContentSubcategory, SourceItem
from app.pipeline.extract import extract_evidence_lines
from app.utils.text import clean_text


SUBCATEGORY_TERMS: list[tuple[ContentSubcategory, tuple[str, ...]]] = [
    ("Investment / Corporate Development", ("corporate development", "corp dev", "투자", "m&a", "인수합병")),
    ("Global Strategy / Global Business", ("global strategy", "global business", "글로벌 전략", "글로벌 사업", "해외사업", "유럽", "france", "europe")),
    ("Content Acquisition / Sourcing", ("content acquisition", "콘텐츠 수급", "콘텐츠 소싱", "라이선스 수급", "판권")),
    ("Market Research / Insights", ("market research", "consumer insight", "user research", "시장 조사", "시장조사", "인사이트", "트렌드 분석", "research assistant")),
    ("Corporate Strategy", ("corporate strategy", "business strategy", "사업전략", "경영전략", "전략실")),
    ("Content Strategy", ("content strategy", "콘텐츠 전략", "콘텐츠전략", "편성 전략")),
    ("IP Business", ("ip business", "ip사업", "ip 사업", "라이선스 사업", "licensing")),
    ("Business Development", ("business development", "사업개발", "파트너십", "제휴사업")),
    ("Marketing Strategy", ("marketing strategy", "마케팅 전략", "그로스", "growth", "crm", "performance marketing", "퍼포먼스 마케팅")),
    ("Platform / Product Business", ("product manager", "product business", "platform business", "서비스 기획", "플랫폼 사업", "플랫폼 기획", "pm", "서비스 전략")),
    ("Content Planning", ("content planning", "콘텐츠 기획", "콘텐츠기획", "웹툰 pd", "웹소설 pd", "md")),
    ("A&R / Artist", ("a&r", "artist", "아티스트", "캐스팅", "매니지먼트")),
    ("Engineering / Data", ("engineer", "developer", "개발", "엔지니어", "데이터", "qa", "ios", "android", "backend", "frontend", "ai/ml")),
    ("Design / Creative", ("design", "디자인", "visual", "creative", "그래픽", "영상 편집")),
    ("Production", ("production", "제작", "촬영", "프로덕션", "연출")),
    ("HR / Corporate Support", ("human resources", "인사", "채용운영", "총무", "재무", "회계", "법무", "compliance")),
    ("Operations", ("operations", "operation", "운영", "업로드", "모니터링")),
]

HIGH_SUBCATEGORIES = {
    "Content Strategy", "Global Strategy / Global Business", "IP Business",
    "Business Development", "Corporate Strategy", "Content Acquisition / Sourcing",
    "Investment / Corporate Development", "Market Research / Insights", "Marketing Strategy",
}
MEDIUM_SUBCATEGORIES = {"Platform / Product Business", "Content Planning"}


def is_content_item(item: SourceItem, project_root: Path | None = None) -> bool:
    return item.raw_metadata.get("category") == "Content" or content_company_for(item.company_raw, project_root) is not None


def _subcategory(text: str) -> ContentSubcategory:
    lowered = text.casefold()
    if any(term in lowered for term in ("global", "글로벌", "해외", "유럽", "france", "europe")) and any(term in lowered for term in ("strategy", "전략", "business", "사업")):
        return "Global Strategy / Global Business"
    for name, terms in SUBCATEGORY_TERMS:
        if any(term.casefold() in lowered for term in terms):
            return name
    return "Other Content"


def _duration(text: str, item: SourceItem) -> tuple[str | None, int | None, int | None]:
    without_probation = re.sub(r"수습\s*(?:기간은?\s*)?\d+\s*개월", "", text, flags=re.I)
    range_match = re.search(r"(\d+)\s*[~～-]\s*(\d+)\s*개월", without_probation)
    if range_match:
        low, high = int(range_match.group(1)), int(range_match.group(2))
        return range_match.group(0), low * 4, high * 4
    months = re.search(r"(?:최소\s*)?(\d+)\s*개월(?:\s*(?:이상|동안))?", without_probation)
    if months:
        value = int(months.group(1))
        return months.group(0), value * 4, value * 4
    weeks = re.search(r"(\d+)\s*[~～-]\s*(\d+)\s*주", without_probation)
    if weeks:
        return weeks.group(0), int(weeks.group(1)), int(weeks.group(2))
    if item.start_date and item.end_date and item.end_date >= item.start_date:
        value = max(1, ((item.end_date - item.start_date).days + 6) // 7)
        return f"{item.start_date.isoformat()} ~ {item.end_date.isoformat()}", value, value
    return None, None, None


def _eligibility(text: str) -> tuple[bool | None, str | None, str | None]:
    lines = [clean_text(line) for line in text.splitlines() if clean_text(line)]
    relevant = [line for line in lines if any(term in line.casefold() for term in ("재학", "휴학", "대학생", "학부", "student", "졸업", "학위"))]
    evidence = relevant[0] if relevant else None
    joined = " ".join(relevant).casefold()
    ineligible_terms = ("졸업자만", "졸업자에 한", "졸업예정자만", "졸업예정자에 한", "재학생 지원 불가")
    eligible_terms = ("재학/휴학", "재학 또는 휴학", "재학생", "휴학생", "대학생", "학부생", "undergraduate", "student")
    graduation = evidence if evidence and any(term in joined for term in ("졸업", "학위")) else None
    if any(term in joined for term in ineligible_terms):
        return False, evidence, graduation
    if "학위" in joined and ("소지" in joined or "졸업예정" in joined) and not any(term in joined for term in eligible_terms):
        return False, evidence, graduation
    if any(term in joined for term in eligible_terms):
        return True, evidence, graduation
    return None, evidence, graduation


def _summer_fit(item: SourceItem, eligible: bool | None, min_weeks: int | None, max_weeks: int | None) -> tuple[str, str]:
    if eligible is False:
        return "INELIGIBLE", "현재 학부 재학생 자격과 맞지 않는 지원 요건"
    if min_weeks is not None and min_weeks >= 16:
        return "LOW", f"최소 근무기간이 약 {min_weeks}주"
    if item.start_date and item.end_date:
        in_summer = item.start_date.month >= 6 and item.end_date.month <= 9
        if max_weeks is not None and max_weeks <= 13 and in_summer:
            return "HIGH", "3개월 이하이며 6~9월 안에 종료 가능"
        if not in_summer:
            return "LOW", "확인된 근무 일정이 6~9월 여름 기간과 맞지 않음"
    start_text = str(item.raw_metadata.get("start_date_text") or "")
    if re.search(r"10월|11월|12월|1월|2월|3월|4월|5월", start_text):
        return "LOW", "확인된 시작 월이 여름 기간과 맞지 않음"
    if max_weeks is not None and max_weeks <= 13:
        return "POSSIBLE", "3개월 이하이나 정확한 시작·종료일 확인 필요"
    return "UNKNOWN", "근무기간 또는 일정 정보 부족"


def _skill_lines(text: str, preferred: bool) -> list[str]:
    markers = ("우대", "preferred") if preferred else ("필수", "필요 역량", "자격요건", "requirements")
    result: list[str] = []
    active = False
    for raw in text.splitlines():
        line = clean_text(raw)
        if not line:
            continue
        if any(marker in line.casefold() for marker in markers):
            active = True
            continue
        if active and any(marker in line.casefold() for marker in ("전형", "지원", "근무", "우대" if not preferred else "필수")):
            break
        if active and len(line) <= 240:
            result.append(line.lstrip("-• "))
        if len(result) >= 6:
            break
    return result


def _weights() -> dict:
    path = Path(__file__).resolve().parents[2] / "config" / "content_scoring.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def classify_content(item: SourceItem) -> ClassificationResult:
    title = clean_text(item.title_raw)
    duties = clean_text(str(item.raw_metadata.get("duties_text") or item.body_text))
    text = f"{title}\n{duties}"
    title_subcategory = _subcategory(title)
    subcategory = title_subcategory if title_subcategory != "Other Content" else _subcategory(duties)
    eligible, eligibility_text, graduation = _eligibility(item.body_text)
    duration, min_weeks, max_weeks = _duration(item.body_text, item)
    summer_fit, summer_reason = _summer_fit(item, eligible, min_weeks, max_weeks)
    seniority = "Intern" if re.search(r"인턴|internship|\bintern\b", text, re.I) else "Trainee" if re.search(r"assistant|어시스턴트|\bRA\b", title, re.I) else "Unknown"
    role_family = {
        "Investment / Corporate Development": "Investment",
        "Market Research / Insights": "Research",
        "Marketing Strategy": "Marketing",
        "Operations": "Operations",
        "HR / Corporate Support": "HR / Admin",
        "Engineering / Data": "Technology",
    }.get(subcategory, "Other")
    weights = _weights()
    tier = "high" if subcategory in HIGH_SUBCATEGORIES else "medium" if subcategory in MEDIUM_SUBCATEGORIES else "low"
    relevance = int(weights["role_fit"][tier])
    relevance += int(weights["student_eligibility"]["eligible" if eligible is True else "ineligible" if eligible is False else "unknown"])
    relevance += int(weights["summer_fit"].get(summer_fit, 0))
    if seniority in {"Intern", "Trainee"}:
        relevance += int(weights["junior_fit"])
    duty_lower = duties.casefold()
    if any(term in duty_lower for term in ("france", "french", "europe", "프랑스", "유럽")):
        relevance += int(weights["europe_signal"])
    relevance = min(100, relevance)
    if eligible is False:
        relevance = min(20, relevance)
    actionability = min(100, (40 if eligible is True else 15 if eligible is None else 0) + (30 if summer_fit == "HIGH" else 20 if summer_fit == "POSSIBLE" else 5 if summer_fit == "UNKNOWN" else 0) + (20 if item.active is not False else 0) + (10 if item.deadline else 5))
    if item.active is False or eligible is False:
        priority = "Archive"
    elif relevance >= int(weights["priority"]["A"]) and summer_fit not in {"LOW", "INELIGIBLE"}:
        priority = "A"
    elif relevance >= int(weights["priority"]["B"]):
        priority = "B"
    else:
        priority = "C"
    evidence = extract_evidence_lines(item.body_text, ("인턴", "재학", "졸업", "개월", "전략", "글로벌", "IP", "리서치", "투자"), limit=6)
    return ClassificationResult(
        category="Content", content_subcategory=subcategory, sector="Unknown", subsector=subcategory,
        role_family=role_family, seniority=seniority, front_office=False,
        relevance_score=relevance, actionability_score=actionability, priority=priority,
        student_eligible=eligible, student_eligibility=eligibility_text,
        graduation_requirement=graduation, internship_duration=duration,
        duration_min_weeks=min_weeks, duration_max_weeks=max_weeks,
        summer_fit=summer_fit, summer_fit_reason=summer_reason,
        required_skills=_skill_lines(item.body_text, False), preferred_skills=_skill_lines(item.body_text, True),
        classification_confidence=0.9 if subcategory != "Other Content" and seniority != "Unknown" else 0.7,
        reasoning_short=evidence or [f"콘텐츠 직무 분류: {subcategory}"],
    )
