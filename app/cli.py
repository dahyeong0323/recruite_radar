from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from datetime import date

from app.config import load_settings
from app.health.monitor import health_state, write_health_note
from app.models import SourceItem
from app.pipeline.update import IngestionPipeline
from app.service import RadarService
from app.vault.dashboard import write_dashboards
from app.vault.index import rebuild_index


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Korea Finance Recruiting Radar")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("bootstrap-vault")
    rebuild = sub.add_parser("rebuild-index")
    rebuild.add_argument("--project-root", type=Path)
    fixture = sub.add_parser("ingest-fixtures")
    fixture.add_argument("--fixture", type=Path, required=True)
    fixture.add_argument("--project-root", type=Path)
    collect = sub.add_parser("collect")
    collect.add_argument("--source", choices=["kvca", "vcs", "kofia", "saramin"], required=True)
    collect.add_argument("--project-root", type=Path)
    backfill = sub.add_parser("backfill")
    backfill.add_argument("--source", choices=["kvca", "vcs", "kofia"], required=True)
    backfill.add_argument("--from", dest="from_date", type=date.fromisoformat, required=True)
    backfill.add_argument("--project-root", type=Path)
    health = sub.add_parser("health")
    health.add_argument("--project-root", type=Path)
    return parser


def bootstrap(settings) -> None:
    root = settings.radar_root
    for directory in (root, root / "Jobs", root / "_System", root / "config"):
        directory.mkdir(parents=True, exist_ok=True)
    state_path = root / "_System" / "state.json"
    if not state_path.exists():
        state_path.write_text(json.dumps({"sources": {}, "updated_at": None}, indent=2) + "\n", encoding="utf-8")
    health_path = root / "_System" / "Health.md"
    if not health_path.exists():
        write_health_note(root, state="DEGRADED", source_rows=[], notes=["아직 collector가 한 번도 성공하지 않았습니다."])
    entries = rebuild_index(root)
    write_dashboards(root, entries)
    config_readme = root / "config" / "README.md"
    if not config_readme.exists():
        config_readme.write_text("# Recruiting Radar configuration\n\nPrivate candidate preferences and aliases belong here. Never store secrets.\n", encoding="utf-8")


def load_fixture(path: Path) -> list[SourceItem]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    return [SourceItem.model_validate(row) for row in rows]


async def run(args) -> int:
    settings = load_settings(getattr(args, "project_root", None))
    bootstrap(settings)
    if args.command == "bootstrap-vault":
        print(settings.radar_root)
        return 0
    if args.command == "rebuild-index":
        entries = rebuild_index(settings.radar_root)
        write_dashboards(settings.radar_root, entries)
        print(f"rebuilt {len(entries)} jobs")
        return 0
    if args.command == "ingest-fixtures":
        metrics = await IngestionPipeline(settings).ingest(load_fixture(args.fixture))
        print(metrics.model_dump_json(indent=2))
        return 0
    if args.command == "collect":
        result = await RadarService(settings).collect_source(args.source)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "backfill":
        result = await RadarService(settings).backfill(args.source, args.from_date)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "health":
        print(health_state(settings.state_path))
        return 0
    return 1


def main() -> None:
    args = _parser().parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
