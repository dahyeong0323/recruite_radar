from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import yaml

from app.models import ClassificationResult
from app.utils.dates import days_until


def _weights() -> dict:
    path = Path(__file__).resolve().parents[2] / "config" / "scoring.yaml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


WEIGHTS = _weights()


def _weight(group: str, name: str, default: int) -> int:
    return int((WEIGHTS.get(group) or {}).get(name, default))


def calculate_relevance(result: ClassificationResult) -> int:
    sector_points = _weight("relevance", "sector", 25) if result.sector in {"VC", "CVC", "PE", "IB", "Alternative Investment", "Research", "Corporate Finance"} else 0
    front_office_points = _weight("relevance", "investment_front_office", 25) if result.front_office else 0
    seniority_max = _weight("relevance", "intern_junior", 20)
    seniority_points = seniority_max if result.seniority in {"Intern", "Trainee", "New Graduate", "Junior"} else round(seniority_max * 0.4) if result.seniority == "Unknown" else 3
    career_max = _weight("relevance", "career_development", 15)
    career_points = career_max if result.role_family in {"Investment", "Deal Execution", "Research", "Portfolio Management"} else round(career_max * 0.47) if result.role_family in {"Fund Management", "Fundraising / IR", "Finance / Accounting"} else 0
    employer_max = _weight("relevance", "employer_role_signal", 10)
    employer_points = employer_max if result.sector in {"VC", "CVC", "PE", "IB"} else round(employer_max * 0.5) if result.sector != "Irrelevant" else 0
    confidence_points = round(result.classification_confidence * _weight("relevance", "confidence", 5))
    return min(100, sector_points + front_office_points + seniority_points + career_points + employer_points + confidence_points)


def calculate_actionability(
    result: ClassificationResult,
    *,
    active: bool | None,
    location: str | None = None,
    deadline: date | datetime | None = None,
    requirements_present: bool = False,
) -> int:
    eligibility_max = _weight("actionability", "eligibility", 30)
    eligibility = eligibility_max if result.student_eligible is True else round(eligibility_max * 0.5) if result.student_eligible is None else 0
    seniority_max = _weight("actionability", "seniority", 25)
    seniority = seniority_max if result.seniority in {"Intern", "Trainee", "New Graduate", "Junior"} else round(seniority_max * 0.4) if result.seniority == "Unknown" else 0
    open_score = _weight("actionability", "open", 20) if active is True or (active is None and (days_until(deadline) is None or days_until(deadline) >= 0)) else 0
    location_max = _weight("actionability", "location", 10)
    location_score = location_max if location else round(location_max * 0.5)
    start_max = _weight("actionability", "start_date", 10)
    start_score = start_max if result.seniority in {"Intern", "Trainee", "New Graduate"} else round(start_max * 0.5)
    requirements_score = _weight("actionability", "requirements", 5) if requirements_present else 0
    return min(100, eligibility + seniority + open_score + location_score + start_score + requirements_score)


def priority_for(result: ClassificationResult, *, active: bool | None) -> str:
    if active is False:
        return "Archive" if result.relevance_score < 65 else "C"
    if result.seniority in {"Experienced", "Senior", "Unknown"}:
        return "C" if result.relevance_score >= 40 else "Archive"
    if result.front_office and result.sector in {"VC", "CVC", "PE", "IB"} and result.seniority in {"Intern", "Trainee"}:
        return "A" if result.relevance_score >= 85 else "B"
    if result.relevance_score >= 65:
        return "B"
    if result.relevance_score >= 40:
        return "C"
    return "Archive"


def apply_scores(
    result: ClassificationResult,
    *,
    active: bool | None,
    location: str | None = None,
    deadline: date | datetime | None = None,
    requirements_present: bool = False,
) -> ClassificationResult:
    relevance = calculate_relevance(result)
    actionability = calculate_actionability(
        result,
        active=active,
        location=location,
        deadline=deadline,
        requirements_present=requirements_present,
    )
    updated = result.model_copy(update={"relevance_score": relevance, "actionability_score": actionability})
    return updated.model_copy(update={"priority": priority_for(updated, active=active)})
