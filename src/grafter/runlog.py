"""Structured per-run log: one JSON object per line in `<run_dir>/run.jsonl`.

String values that look like secrets are redacted before they reach disk.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any

from grafter.gateway import redact
from grafter.secret_scan import RULES

_RESERVED = frozenset({"ts", "run_id"})


def new_run_id(now: datetime | None = None) -> str:
    """`YYYYMMDDTHHMMSSZ-xxxxxx` in UTC; naive datetimes are taken as UTC."""
    now = now or datetime.now(UTC)
    now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    return f"{now:%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"


def _looks_secret(value: str) -> bool:
    return value.startswith("sk-") or any(p.search(value) for p in RULES.values())


def _clean(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return redact(value) if _looks_secret(value) else value
    if isinstance(value, Mapping):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_clean(v) for v in value]
    return _clean(str(value))


class RunLog:
    def __init__(self, run_dir: Path, run_id: str) -> None:
        self.run_dir = Path(run_dir)
        self.run_id = run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self.run_dir / "run.jsonl"

    def event(self, kind: str, **fields: Any) -> None:
        clash = _RESERVED.intersection(fields)
        if clash:
            raise ValueError(f"reserved event fields: {sorted(clash)}")
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "run_id": self.run_id,
            "kind": kind,
            **_clean(fields),
        }
        # Open per event: each line is written, flushed and closed, so a crash loses nothing.
        with self.path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def close(self) -> None:
        """No-op: nothing is held open between events."""

    def __enter__(self) -> RunLog:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
