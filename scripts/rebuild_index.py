from app.config import load_settings
from app.vault.dashboard import write_dashboards
from app.vault.index import rebuild_index


if __name__ == "__main__":
    settings = load_settings()
    entries = rebuild_index(settings.radar_root)
    write_dashboards(settings.radar_root, entries)
    print(f"rebuilt {len(entries)} jobs")
