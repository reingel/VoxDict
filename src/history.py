from datetime import date
from pathlib import Path

import yaml

HISTORY_PATH = Path.home() / ".voxdict" / "history.yaml"
ITEMS_PER_PAGE = 10


class HistoryManager:
    def __init__(self):
        HISTORY_PATH.parent.mkdir(exist_ok=True)
        self._entries: list[dict] = self._load()

    def _load(self) -> list[dict]:
        if not HISTORY_PATH.exists():
            return []
        with open(HISTORY_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f) or []

    def add(self, word: str) -> None:
        self._entries.append({"word": word, "date": str(date.today())})
        with open(HISTORY_PATH, "w", encoding="utf-8") as f:
            yaml.dump(self._entries, f, allow_unicode=True, default_flow_style=False)

    def recent_unique(self, n: int = 100) -> list[str]:
        """Return up to n unique words, most recent first."""
        seen: set[str] = set()
        result: list[str] = []
        for entry in reversed(self._entries):
            w = entry["word"]
            if w not in seen:
                seen.add(w)
                result.append(w)
            if len(result) >= n:
                break
        return result
