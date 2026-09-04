from __future__ import annotations

from collections import Counter
from pathlib import Path

from app.models import IndexEntry
from app.utils.dates import days_until
from app.vault.frontmatter import atomic_write_text
from app.utils.clock import today


def _link(entry: IndexEntry) -> str:
    path = entry.file_path.removeprefix("Career/Recruiting_Radar/").removesuffix(".md")
    return f"[[{path}|{entry.company or 'Unknown'} — {entry.title}]]"


def _table(entries: list[IndexEntry], limit: int = 25) -> str:
    rows = ["| P | Company | Role | Track | Deadline | Score | Status |", "|---|---|---|---|---|---:|---|"]
    for entry in entries[:limit]:
        remaining = days_until(entry.deadline)
        deadline = f"D-{remaining}" if remaining is not None and remaining >= 0 else "마감/미상" if remaining is None else f"D+{-remaining}"
        icon = "🔥" if entry.priority == "A" else "⭐" if entry.priority == "B" else ""
        rows.append(f"| {icon} | {_link(entry)} | {entry.title} | {entry.sector} | {deadline} | {entry.relevance_score} | {entry.user_status} |")
    return "\n".join(rows)


def render_dashboard(entries: list[IndexEntry]) -> str:
    active = [entry for entry in entries if entry.status == "active"]
    a_jobs = [entry for entry in active if entry.priority == "A"]
    b_jobs = [entry for entry in active if entry.priority == "B"]
    urgent = [entry for entry in active if (days_until(entry.deadline) is not None and 0 <= days_until(entry.deadline) <= 7)]
    counts = Counter(entry.sector for entry in active)
    statuses = Counter(entry.user_status for entry in entries)
    return f"""# Korea Finance Recruiting Radar

> 자동 생성 파일. 원문과 사용자 상태는 개별 Job note에서 관리한다.

## Last Refresh

- Generated at: {today().isoformat()}
- System health: [[_System/Health]]
- Machine index: [[_System/index.json]]

## Active A Jobs ({len(a_jobs)})

{_table(a_jobs)}

## Active B Jobs ({len(b_jobs)})

{_table(b_jobs)}

## Deadline <= 7 days ({len(urgent)})

{_table(urgent)}

## New Internships

{_table([entry for entry in active if entry.seniority in {'Intern', 'Trainee'}])}

## New Junior Jobs

{_table([entry for entry in active if entry.seniority in {'Junior', 'New Graduate'}])}

## Statistics

| Sector | Count |
|---|---:|
| VC | {counts.get('VC', 0)} |
| PE | {counts.get('PE', 0)} |
| IB | {counts.get('IB', 0)} |
| Other finance | {sum(value for key, value in counts.items() if key not in {'VC', 'PE', 'IB'})} |

| User status | Count |
|---|---:|
| Interested | {statuses.get('interested', 0)} |
| Will Apply | {statuses.get('will_apply', 0)} |
| Applied | {statuses.get('applied', 0)} |

## Generated Views

- [[01_Internships]]
- [[02_Junior]]
- [[03_Interested]]
- [[04_Will_Apply]]
- [[05_Applied]]
"""


def render_filtered_view(title: str, entries: list[IndexEntry]) -> str:
    return f"# {title}\n\n> 자동 생성 파일.\n\n{_table(entries)}\n"


def write_dashboards(radar_root: Path, entries: list[IndexEntry]) -> None:
    atomic_write_text(radar_root / "00_Dashboard.md", render_dashboard(entries))
    active = [entry for entry in entries if entry.status == "active"]
    views = {
        "01_Internships.md": [entry for entry in active if entry.seniority in {"Intern", "Trainee"}],
        "02_Junior.md": [entry for entry in active if entry.seniority in {"Junior", "New Graduate"}],
        "03_Interested.md": [entry for entry in entries if entry.user_status == "interested"],
        "04_Will_Apply.md": [entry for entry in entries if entry.user_status == "will_apply"],
        "05_Applied.md": [entry for entry in entries if entry.user_status == "applied"],
    }
    for filename, view_entries in views.items():
        atomic_write_text(radar_root / filename, render_filtered_view(filename.removesuffix(".md"), view_entries))
