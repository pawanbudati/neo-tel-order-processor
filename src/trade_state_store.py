import json
import os
from pathlib import Path
from typing import Any, Dict


class TradeStateStore:
    """Small atomic JSON store for active staged-exit state."""

    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> Dict[str, Dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            # Do not silently overwrite unreadable live-trade state.
            raise RuntimeError(f"Cannot read active trade state at {self.path}")

    def save(self, trades: Dict[str, Dict[str, Any]]) -> None:
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        temp_path.write_text(json.dumps(trades, separators=(",", ":")), encoding="utf-8")
        os.replace(temp_path, self.path)
