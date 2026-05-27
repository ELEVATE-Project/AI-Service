from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import yaml

_PROJECT_ROOT = Path(__file__).parents[1]
_YAML_PATH = _PROJECT_ROOT / "pricing" / "models.yaml"

if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def main() -> None:
    from src.shared.config import settings

    raw = yaml.safe_load(_YAML_PATH.read_text())
    models: dict = raw["models"]
    cutoff = date.today()
    stale: list[str] = []

    for name, entry in models.items():
        last_updated = datetime.strptime(entry["last_updated"], "%Y-%m-%d").date()
        age_days = (cutoff - last_updated).days
        if age_days > settings.pricing_staleness_days:
            stale.append(f"  {name}: last_updated={entry['last_updated']} ({age_days} days old)")

    if stale:
        print(f"pricing/models.yaml has {len(stale)} stale entries (limit: {settings.pricing_staleness_days} days):")
        for line in stale:
            print(line)
        sys.exit(1)

    print(f"all {len(models)} models fresh (within {settings.pricing_staleness_days} days)")


if __name__ == "__main__":
    main()