import argparse
import asyncio
from datetime import date

from app.cli import bootstrap
from app.config import load_settings
from app.service import RadarService


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=["kvca", "vcs", "kofia"], required=True)
    parser.add_argument("--from", dest="from_date", type=date.fromisoformat, required=True)
    args = parser.parse_args()
    settings = load_settings()
    bootstrap(settings)
    print(asyncio.run(RadarService(settings).backfill(args.source, args.from_date)))


if __name__ == "__main__":
    main()
