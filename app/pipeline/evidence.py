"""Source-grounded recruiting evidence shared by both classifiers and delivery."""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.models import SourceItem
from app.pipeline.extract import extract_experience_range


_EXCLUDED = re.compile(r"결격|기재\s*제외|경력\s*기재|지원서\s*작성|작성\s*안내|우대|참고사항|유의사항|기타사항|선호", re.I)
_STOP = re.compile(r"^(?:\d+[.)]\s*)?(?:결격사유|지원방법|입사지원|전형절차|채용절차|제출서류|기타사항|유의사항|우대사항)\s*[:：]?$", re.I)
_DUTY = re.compile(r"담당업무|주요업무|수행업무|업무내용|직무내용|모집분야|모집부문", re.I)
_REQUIREMENT = re.compile(r"자격요건|지원자격|희망직위|모집분야|모집부문|근무형태|채용형태", re.I)
_SENIOR = re.compile(r"경력직|경력자\s*채용|대리\s*[~～-]\s*과장|(?:대리|과장|차장|부장)\s*(?:급|직급|이상)|(?:경력|관련\s*업무|실무)\s*\d+\s*년\s*이상", re.I)
_INTERN = re.compile(r"인턴(?:십)?|체험형|채용연계형|\bintern(?:ship)?\b", re.I)
_TRAINEE = re.compile(r"트레이니|\btrainee\b", re.I)


@dataclass(frozen=True)
class RecruitingEvidence:
    target_text: str
    duty_text: str
    requirements_text: str
    seniority: str
    experience_min: int | None
    experience_max: int | None
    explicit_intern: bool
    explicit_senior: bool


def recruiting_evidence(item: SourceItem) -> RecruitingEvidence:
    title = item.title_raw.strip()
    target_lines: list[str] = []
    duty_lines: list[str] = []
    requirement_lines: list[str] = []
    section = ""
    stopped = False
    for raw in item.body_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if _STOP.fullmatch(line):
            stopped = True
        if stopped or _EXCLUDED.search(line):
            continue
        if _DUTY.search(line):
            section = "duty"
        elif _REQUIREMENT.search(line):
            section = "requirement"
        elif re.match(r"^(?:\d+[.)]\s*)?(?:급여|복리후생|근무지|접수기간|회사소개)\s*[:：]", line):
            section = ""
        target_lines.append(line)
        if section == "duty":
            duty_lines.append(line)
        elif section == "requirement":
            requirement_lines.append(line)
    target = "\n".join([title, *target_lines])
    duties = "\n".join([title, *duty_lines]) if duty_lines else target
    requirements = "\n".join([title, *requirement_lines]) if requirement_lines else target
    minimum, maximum = extract_experience_range(requirements)
    intern = bool(_INTERN.search(requirements))
    trainee = bool(_TRAINEE.search(requirements))
    senior = bool(_SENIOR.search(requirements)) or (minimum is not None and minimum >= 4)
    if senior and (intern or trainee):
        seniority = "Unknown"  # Mixed recruitment cannot be assigned to an intern cohort.
    elif senior:
        seniority = "Experienced"
    elif intern:
        seniority = "Intern"
    elif trainee:
        seniority = "Trainee"
    elif re.search(r"신입|new graduate|졸업예정", requirements, re.I):
        seniority = "New Graduate"
    elif minimum is not None and maximum is not None and maximum <= 3:
        seniority = "Junior"
    elif re.search(r"경력|experienced|senior", requirements, re.I):
        seniority = "Experienced"
    else:
        seniority = "Unknown"
    return RecruitingEvidence(target, duties, requirements, seniority, minimum, maximum, intern or trainee, senior)
