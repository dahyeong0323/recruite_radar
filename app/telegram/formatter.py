from __future__ import annotations

from app.models import ClassificationResult, SourceItem
from app.pipeline.normalize import normalize_item
from app.utils.dates import days_until


def job_alert(job_id: str, item: SourceItem, classification: ClassificationResult) -> tuple[str, dict]:
    normalized = normalize_item(item)
    remaining = days_until(item.deadline)
    deadline = f"{item.deadline.date().isoformat()} · D-{remaining}" if item.deadline and remaining is not None else "미상"
    evidence = "\n".join(f"• {line}" for line in classification.reasoning_short[:3]) or "• 원문 확인 필요"
    if classification.category == "Content":
        highlight = "☀️ SUMMER HIGH" if classification.summer_fit == "HIGH" else "🔥 STRATEGY / GLOBAL / IP / INVESTMENT" if classification.content_subcategory in {
            "Content Strategy", "Global Strategy / Global Business", "IP Business",
            "Investment / Corporate Development", "Corporate Strategy", "Content Acquisition / Sourcing",
        } and classification.relevance_score >= 55 else ""
        eligibility = classification.student_eligibility or ("재학생 지원 가능" if classification.student_eligible is True else "재학생 지원 불가" if classification.student_eligible is False else "미상")
        highlight_line = f"{highlight}\n" if highlight else ""
        text = f"""[CONTENT] {normalized.company or 'Unknown Company'} — {normalized.title}
{highlight_line}

직무: {classification.content_subcategory or 'Other Content'}
회사: {normalized.company or 'Unknown Company'}
인턴 기간: {classification.internship_duration or '미상'}
지원자격: {eligibility}
졸업 요건: {classification.graduation_requirement or '미상'}
마감일: {deadline}
Summer Fit: {classification.summer_fit} — {classification.summer_fit_reason or '근거 부족'}
Score: {classification.relevance_score} · Actionability: {classification.actionability_score}
링크: {item.source_url}"""
    else:
        track = classification.sector if classification.category == "Finance" else classification.category
        text = f"""🔥 {classification.priority} | {track} {classification.seniority.upper()} | {classification.relevance_score}

{normalized.company or 'Unknown Company'}
{normalized.title}

📍 {item.raw_metadata.get('location') or '미상'}
📅 마감 {deadline}
💼 {track} / {classification.role_family}
🎓 {classification.seniority}

왜 잡혔나
{evidence}

Relevance {classification.relevance_score}
Actionability {classification.actionability_score}
Confidence {round(classification.classification_confidence * 100)}%"""
    markup = {
        "inline_keyboard": [
            [{"text": "공고 열기", "url": item.source_url}],
            [{"text": "⭐ 관심", "callback_data": f"interest:{job_id}"}, {"text": "📝 지원예정", "callback_data": f"plan:{job_id}"}, {"text": "🙈 무시", "callback_data": f"ignore:{job_id}"}],
        ]
    }
    return text, markup
