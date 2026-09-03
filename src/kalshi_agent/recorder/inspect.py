"""Summarise a capture directory.

Answers the only question that matters after leaving the recorder running overnight:
is this data any good? It reports coverage, the index tick rate, sequence gaps, how many
settlement windows were captured end to end, and how long after each window opened the
strike appeared.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kalshi_agent.recorder.writer import read_records

NS_PER_SECOND = 1_000_000_000


@dataclass
class CaptureSummary:
    files: int = 0
    records: int = 0
    channels: Counter[str] = field(default_factory=Counter)
    first_ns: int | None = None
    last_ns: int | None = None
    index_ticks: int = 0
    index_gaps_over_2s: int = 0
    max_index_gap_s: float = 0.0
    sequence_gaps: int = 0
    markets: set[str] = field(default_factory=set)
    windowed_average_ticks: int = 0
    strike_delays: dict[str, float] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        if self.first_ns is None or self.last_ns is None:
            return 0.0
        return (self.last_ns - self.first_ns) / NS_PER_SECOND

    @property
    def index_rate_hz(self) -> float:
        return self.index_ticks / self.duration_s if self.duration_s else 0.0

    def as_dict(self) -> dict[str, Any]:
        delays = sorted(self.strike_delays.values())
        return {
            "files": self.files,
            "records": self.records,
            "duration_hours": round(self.duration_s / 3600, 2),
            "channels": dict(self.channels.most_common()),
            "markets_seen": len(self.markets),
            "index_ticks": self.index_ticks,
            "index_rate_hz": round(self.index_rate_hz, 2),
            "index_stalls_over_2s": self.index_gaps_over_2s,
            "max_index_gap_s": round(self.max_index_gap_s, 2),
            "settlement_window_ticks": self.windowed_average_ticks,
            "sequence_gaps": self.sequence_gaps,
            "strikes_timed": len(self.strike_delays),
            "strike_delay_median_s": round(delays[len(delays) // 2], 2) if delays else None,
            "strike_delay_max_s": round(delays[-1], 2) if delays else None,
            "errors": len(self.errors),
        }


def summarise(directory: str | Path) -> CaptureSummary:
    directory = Path(directory)
    paths = sorted(p for p in directory.iterdir() if p.name.endswith((".jsonl", ".jsonl.gz")))
    s = CaptureSummary(files=len(paths))
    last_index_ns: int | None = None
    last_seq: dict[int, int] = {}
    strike_first_seen: dict[str, float] = defaultdict(lambda: float("inf"))

    for path in paths:
        for record in read_records(path):
            s.records += 1
            ts, channel, msg = record.get("t"), record.get("ch", ""), record.get("m") or {}
            if isinstance(ts, int):
                s.first_ns = ts if s.first_ns is None else min(s.first_ns, ts)
                s.last_ns = ts if s.last_ns is None else max(s.last_ns, ts)
            s.channels[channel] += 1
            body = msg.get("msg") if isinstance(msg, dict) else None
            body = body if isinstance(body, dict) else {}

            if channel.startswith("cfbenchmarks"):
                s.index_ticks += 1
                if body.get("last_60s_windowed_average_15min") is not None:
                    s.windowed_average_ticks += 1
                if last_index_ns is not None and isinstance(ts, int):
                    gap = (ts - last_index_ns) / NS_PER_SECOND
                    s.max_index_gap_s = max(s.max_index_gap_s, gap)
                    if gap > 2.0:
                        s.index_gaps_over_2s += 1
                if isinstance(ts, int):
                    last_index_ns = ts
            elif channel in ("orderbook_snapshot", "orderbook_delta", "ticker", "trade"):
                ticker = body.get("market_ticker")
                if ticker:
                    s.markets.add(ticker)
            elif channel == "strike_watch":
                ticker, delay = msg.get("ticker"), msg.get("seconds_after_open")
                if ticker and delay is not None and msg.get("floor_strike") is not None:
                    strike_first_seen[ticker] = min(strike_first_seen[ticker], float(delay))
            elif channel == "error":
                s.errors.append(str(body)[:160])

            sid, seq = msg.get("sid"), msg.get("seq")
            if isinstance(sid, int) and isinstance(seq, int):
                previous = last_seq.get(sid)
                if previous is not None and seq > previous + 1:
                    s.sequence_gaps += 1
                if previous is None or seq > previous:
                    last_seq[sid] = seq

    s.strike_delays = {k: v for k, v in strike_first_seen.items() if v != float("inf")}
    return s
