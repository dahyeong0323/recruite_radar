from __future__ import annotations

from app.models import ClassificationResult, SourceItem
from app.pipeline.normalize import normalize_item
from app.utils.dates import days_until


def job_alert(job_id: str, item: SourceItem, classification: ClassificationResult) -> tuple[str, dict]:
    normalized = normalize_item(item)
    remaining = days_until(item.deadline)
    deadline = f"{item.deadline.date().isoformat()} · D-{remaining}" if item.deadline and remaining is not None else "미상"
    evidence = "\n".join(f"• {line}" for line in classification.reasoning_short[:3]) or "• 원문 확인 필요"
    text = f"""🔥 {classification.priority} | {classification.sector} {classification.seniority.upper()} | {classification.relevance_score}

{normalized.company or 'Unknown Company'}
{normalized.title}

📍 {item.raw_metadata.get('location') or '미상'}
📅 마감 {deadline}
💼 {classification.sector} / {classification.role_family}
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
