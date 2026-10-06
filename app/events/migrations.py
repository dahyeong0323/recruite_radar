from pathlib import Path
from datetime import datetime
from app.events.models import CanonicalEvent, SCHEMA_VERSION
from app.vault.frontmatter import parse_frontmatter, atomic_write_text


def migrate(repository, *, apply=False):
    """Validate before any write; preserve future schemas and create exact backups."""
    plans = []
    for path in sorted((repository.root / 'Events').rglob('*.md')):
        original = path.read_text(encoding='utf-8')
        metadata, _ = parse_frontmatter(original)
        version = metadata.get('schema_version', 1)
        if version > SCHEMA_VERSION: raise ValueError(f'unsupported future Event schema: {path.name}')
        event = CanonicalEvent.model_validate(metadata)
        if version < SCHEMA_VERSION or 'schema_version' not in metadata:
            event.schema_version = SCHEMA_VERSION
            plans.append((path, original, event))
    if apply:
        for path, original, event in plans:
            backup = repository.system / 'Migration_Backups' / f'v{SCHEMA_VERSION}' / path.name
            if not backup.exists(): atomic_write_text(backup, original)
            repository.write(event)
        repository.rebuild()
    return {'apply': apply, 'version': SCHEMA_VERSION, 'changes': [path.name for path, _, _ in plans]}
