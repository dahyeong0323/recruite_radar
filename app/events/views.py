from app.vault.frontmatter import atomic_write_text


def write_views(root, events):
    alive = [e for e in events if not e.merged_into]
    upcoming = [e for e in alive if e.facts.event_status in {'announced', 'upcoming', 'happening', 'postponed'}
                and (e.facts.start_date is not None or e.facts.event_status == 'postponed')]
    groups = {'Upcoming': upcoming, 'High Priority': [e for e in upcoming if e.evaluation.priority == 'A'],
              'Geneva': [e for e in upcoming if e.facts.city == 'Geneva'],
              'Zurich': [e for e in upcoming if e.facts.city == 'Zurich'],
              'Finance': [e for e in alive if e.evaluation.scores.get('finance', 0) >= 50],
              'Korean Corporate': [e for e in alive if e.evaluation.korea_verified and e.facts.participating_organizations],
              'Attended': [e for e in alive if e.user_status == 'attended'],
              'Watch': [e for e in alive if e.assessment_status in {'watching', 'stale'}]}
    for title, rows in groups.items():
        lines = [f'# Events — {title}', '', '| Date | City | Event | Priority | Score |', '|---|---|---|---|---:|']
        for event in sorted(rows, key=lambda e: (str(e.facts.start_date or '9999'), -e.evaluation.overall_score)):
            label = event.facts.title.replace('|', '/').replace('\n', ' ')
            link = f'[[{event.file_path.removesuffix(".md")}|{label}]]'
            lines.append(f'| {event.facts.start_date or "?"} | {event.facts.city or "?"} | {link} | {event.evaluation.priority} | {event.evaluation.overall_score} |')
        atomic_write_text(root / 'Event_Views' / (title.replace(' ', '_') + '.md'), '\n'.join(lines) + '\n')
