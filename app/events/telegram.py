from app.events.models import CanonicalEvent


def event_message(event: CanonicalEvent, kind='new'):
    f, e = event.facts, event.evaluation
    label = 'UPDATE' if kind == 'update' else 'REMINDER' if kind.startswith('reminder') else e.priority
    watching = kind == 'watch' or event.assessment_status in {'watching', 'stale'}
    date = str(f.start_date or '날짜 미확인')
    if not f.start_date and f.schedule_year and f.schedule_month:
        date = f'{f.schedule_year}-{f.schedule_month:02d} · 정확한 날짜 미확인'
    if f.end_date and f.end_date != f.start_date: date += ' ~ ' + str(f.end_date)
    if f.start_time: date += ' ' + f.start_time.strftime('%H:%M')
    price = '가격 미확인' if f.ticket_price_min is None else f'{f.ticket_price_min:g} {f.currency or ""}'.strip()
    lines = [f'🇨🇭 [EVENT · {label}] {f.title[:400]}', f'📅 {date} — {f.timezone}',
             f'📍 {f.city or "도시 미확인"} · {(f.venue or "장소 미확인")[:200]}',
             '🏛 ' + (', '.join(f.organizers) or '주최자 미확인')[:300],
             '🤝 ' + (', '.join(f.participating_organizations) or '참가 기관 미확인')[:300], '',
             '왜 중요한지: ' + '; '.join(e.reasons)[:700],
             f'관련성 {e.overall_score} · 네트워킹 {e.scores.get("networking", 0)} · 접근성 {e.scores.get("accessibility", 0)}',
             f'참석: {f.access_type} · 학생: {f.student_accessibility}',
             f'상태: {f.event_status} · 등록: {f.registration_status}',
             f'등록 마감: {f.registration_deadline or "미확인"} · {price}']
    if event.conflicts: lines.append('⚠️ 출처 간 조건 차이 있음: 확인 필요')
    if watching:
        lines[0] = f'🇨🇭 [EVENT WATCH] {f.title[:400]}'
        lines.append('⚠️ 행사 계획 확인 · 일정/참석 조건 재검증 중 (' + event.assessment_status + ')')
    if f.business_application_url:
        lines.append('기업 참가 모집: ' + (str(f.business_application_deadline) if f.business_application_deadline else '마감 미확인') + ' · 현지 참석 등록과 별도')
    if f.registration_url: lines.append('등록: ' + f.registration_url)
    if f.official_url: lines.append('공식 안내: ' + f.official_url)
    elif event.observations: lines.append('발견 원문: ' + event.observations[0].source_url)
    markup = {'inline_keyboard': [[{'text': '관심', 'callback_data': f'ev:interest:{event.event_id}'},
                                  {'text': '등록 완료', 'callback_data': f'ev:registered:{event.event_id}'}],
                                 [{'text': '참석 완료', 'callback_data': f'ev:attended:{event.event_id}'},
                                  {'text': '무시', 'callback_data': f'ev:ignore:{event.event_id}'}]]}
    return '\n'.join(lines)[:4000], markup


def select_events(command, events):
    events = [e for e in events if not e.merged_into]
    if command == '/events_saved': return [e for e in events if e.user_status in {'interested', 'registered'}]
    if command == '/events_watch': return [e for e in events if e.assessment_status in {'watching', 'stale'}]
    active = [e for e in events if e.facts.event_status not in {'completed', 'cancelled'}
              and (e.facts.start_date is not None or e.facts.event_status == 'postponed')]
    if command == '/events_high': return [e for e in active if e.evaluation.priority == 'A']
    if command == '/events_geneva': return [e for e in active if e.facts.city == 'Geneva']
    if command == '/events_zurich': return [e for e in active if e.facts.city == 'Zurich']
    return active
