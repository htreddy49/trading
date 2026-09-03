"""Append-only capture of everything the exchange sends us.

Every message is written verbatim alongside the local time it arrived. Keeping the raw
payload matters: a decoder bug six months from now should be fixable by re-reading the
capture, not by collecting the data again. Files are gzipped JSON lines, rotated on the
hour, which makes them cheap to store and trivial to replay with standard tools.

The local timestamp is nanoseconds since the epoch, taken the instant the message was
read off the socket, and is deliberately separate from any timestamp inside the message:
the difference between the two is the latency measurement the strategy depends on.
"""

from __future__ import annotations

import gzip
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, TextIO

from kalshi_agent.logging import get_logger

log = get_logger(__name__)


class RecordWriter:
    def __init__(
        self,
        directory: str | Path,
        *,
        prefix: str = "capture",
        rotate_seconds: int = 3600,
        flush_every: int = 200,
        compress: bool = True,
    ) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.prefix = prefix
        self.rotate_seconds = rotate_seconds
        self.flush_every = flush_every
        self.compress = compress
        self._handle: TextIO | None = None
        self._period: int | None = None
        self._since_flush = 0
        self.records_written = 0
        self.bytes_written = 0
        self.current_path: Path | None = None

    # -- rotation ---------------------------------------------------------------------
    def _period_for(self, epoch_seconds: float) -> int:
        return int(epoch_seconds // self.rotate_seconds)

    def _path_for(self, epoch_seconds: float) -> Path:
        stamp = datetime.fromtimestamp(epoch_seconds, UTC).strftime("%Y%m%dT%H%M%SZ")
        suffix = ".jsonl.gz" if self.compress else ".jsonl"
        return self.directory / f"{self.prefix}-{stamp}{suffix}"

    def _ensure_open(self, epoch_seconds: float) -> TextIO:
        period = self._period_for(epoch_seconds)
        if self._handle is not None and period == self._period:
            return self._handle
        self.close()
        path = self._path_for(period * self.rotate_seconds)
        self.current_path = path
        self._handle = (
            gzip.open(path, "at", encoding="utf-8", compresslevel=6)
            if self.compress
            else path.open("a", encoding="utf-8")
        )
        self._period = period
        log.info("recorder.file_opened", path=str(path))
        return self._handle

    # -- writing ------------------------------------------------------------------------
    def write(self, channel: str, message: Any, *, received_ns: int | None = None) -> None:
        received_ns = received_ns if received_ns is not None else time.time_ns()
        handle = self._ensure_open(received_ns / 1e9)
        line = json.dumps(
            {"t": received_ns, "ch": channel, "m": message}, separators=(",", ":"), default=str
        )
        handle.write(line)
        handle.write("\n")
        self.records_written += 1
        self.bytes_written += len(line) + 1
        self._since_flush += 1
        if self._since_flush >= self.flush_every:
            self.flush()

    def flush(self) -> None:
        if self._handle is not None:
            self._handle.flush()
        self._since_flush = 0

    def close(self) -> None:
        if self._handle is not None:
            self._handle.flush()
            self._handle.close()
            self._handle = None
            self._period = None

    def __enter__(self) -> RecordWriter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def read_records(path: str | Path):
    """Iterate a capture file, compressed or not. Used by the replay tooling."""
    path = Path(path)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:  # type: ignore[operator]
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)
