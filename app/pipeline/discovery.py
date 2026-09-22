"""Broad-source routing. Finance and Content keep their existing scorers."""
from __future__ import annotations

import re

from app.models import Category, ClassificationResult, SourceItem
from app.pipeline.content import _duration, _eligibility, _skill_lines, _summer_fit, is_content_item
from app.pipeline.extract import extract_evidence_lines
from app.utils.text import clean_text


FINANCE = ("벤처캐피탈", "벤처투자", "투자심사", "투자", "사모펀드", "기업금융", "자산운용", "대체투자", "증권", "투자은행", "investment banking", "private equity", "corporate finance", "cvc", "pef", " m&a ", " ib ", " vc ")
CONTENT = ("콘텐츠", "엔터테인먼트", "웹툰", "웹소설", "음악사업", "아티스트", "k-pop", "ott", "영상사업", "ip사업", "ip 사업", "content business")
BEAUTY = ("화장품", "뷰티", "코스메틱", "beauty", "cosmetic", "스킨케어", "소비재", "consumer brand", "fmcg")
GAMING = ("게임", "gaming", "game business", "e-sports", "이스포츠", "게임사업", "게임퍼블리싱", "game publishing")


def inferred_category(item: SourceItem) -> Category | None:
    explicit = item.raw_metadata.get("category")
    if explicit in {"Finance", "Content", "Beauty / Consumer", "Gaming / Consumer Internet"}:
        return explicit
    if is_content_item(item):
        return "Content"
    text = f" {clean_text(item.company_raw or '').casefold()} {clean_text(item.title_raw).casefold()} "
    for category, terms in (("Gaming / Consumer Internet", GAMING), ("Beauty / Consumer", BEAUTY), ("Content", CONTENT), ("Finance", FINANCE)):
        if any(term in text for term in terms):
            return category
    return None


def is_junior_title(title: str, employment_type: str | None = None) -> bool:
    text = f"{title} {employment_type or ''}".casefold()
    if re.search(r"경력\s*[3-9]\s*년|시니어|senior|assistant manager|assistant engineer|인재풀|talent pool", text):
        return False
    return bool(re.search(r"인턴|intern(?:ship)?|체험형|\bRA\b|research assistant|어시스턴트", text, re.I))


def classify_consumer(item: SourceItem, category: Category) -> ClassificationResult:
    title = clean_text(item.title_raw).casefold()
    body = clean_text(item.body_text)
    duties = clean_text(str(item.raw_metadata.get("duties_text") or body)).casefold()
    high = ("전략", "strategy", "글로벌", "해외사업", "global business", "사업개발", "business development", "corp dev", "투자", "investment", "시장조사", "market research")
    medium = ("브랜드", "brand", "마케팅", "marketing", "서비스 기획", "product", "플랫폼", "사업기획", "기획")
    low = ("디자인", "촬영", "cs", "운영보조", "업로드", "개발", "engineer", "developer")
    tier = "low" if any(x in title for x in low) else "high" if any(x in title for x in high) else "medium" if any(x in title for x in medium) else "low"
    role = "Investment" if any(x in title for x in ("투자", "investment", "corp dev")) else "Research" if any(x in title for x in ("리서치", "research", "시장조사")) else "Marketing" if any(x in title for x in ("마케팅", "marketing", "브랜드", "brand")) else "Operations" if "운영" in title else "Technology" if any(x in title for x in ("개발", "engineer")) else "Other"
    eligible, eligibility, graduation = _eligibility(body)
    duration, min_weeks, max_weeks = _duration(body, item)
    summer, reason = _summer_fit(item, eligible, min_weeks, max_weeks)
    seniority = "Intern" if re.search(r"인턴|intern", title, re.I) else "Trainee" if re.search(r"\bRA\b|assistant|어시스턴트", title, re.I) else "New Graduate" if "신입" in title else "Unknown"
    relevance = {"high": 40, "medium": 25, "low": 5}[tier] + (25 if eligible is True else 0 if eligible is False else 5) + {"HIGH": 20, "POSSIBLE": 10}.get(summer, 0) + (10 if seniority in {"Intern", "Trainee"} else 5 if seniority == "New Graduate" else 0)
    if any(x in duties for x in ("france", "europe", "프랑스", "유럽")):
        relevance += 5
    if eligible is False:
        relevance = min(20, relevance)
    priority = "Archive" if item.active is False or eligible is False else "A" if relevance >= 80 and summer not in {"LOW", "INELIGIBLE"} else "B" if relevance >= 55 else "C"
    actionability = min(100, (40 if eligible is True else 15 if eligible is None else 0) + (30 if summer == "HIGH" else 20 if summer == "POSSIBLE" else 5 if summer == "UNKNOWN" else 0) + (20 if item.active is not False else 0) + (10 if item.deadline else 5))
    evidence = extract_evidence_lines(body, high + medium, limit=6)
    return ClassificationResult(
        category=category, sector="Unknown", subsector=category, role_family=role, seniority=seniority,
        relevance_score=min(100, relevance), actionability_score=actionability, priority=priority,
        student_eligible=eligible, student_eligibility=eligibility, graduation_requirement=graduation,
        internship_duration=duration, duration_min_weeks=min_weeks, duration_max_weeks=max_weeks,
        summer_fit=summer, summer_fit_reason=reason,
        required_skills=_skill_lines(body, False), preferred_skills=_skill_lines(body, True),
        classification_confidence=0.8 if tier != "low" else 0.65,
        reasoning_short=evidence or [f"{category} 직무: {tier}"],
    )
