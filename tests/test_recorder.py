import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from kalshi_agent.kalshi.auth import KalshiSigner, generate_test_key
from kalshi_agent.kalshi.client import KalshiClient
from kalshi_agent.kalshi.ws import KalshiWebSocket, SequenceTracker, WebSocketClosedError
from kalshi_agent.recorder.book import (
    MICROS_PER_CENT,
    MICROS_PER_DOLLAR,
    OrderBook,
    parse_count,
    parse_price,
)
from kalshi_agent.recorder.service import Recorder
from kalshi_agent.recorder.writer import RecordWriter, read_records

BASE = "https://api.elections.kalshi.com/trade-api/v2"


# --------------------------------------------------------------------------- writer
def test_writer_roundtrip_and_rotation(tmp_path):
    w = RecordWriter(tmp_path, rotate_seconds=3600, flush_every=1)
    base = 1_800_000_000_000_000_000  # ns
    w.write("ticker", {"a": 1}, received_ns=base)
    w.write("trade", {"b": 2}, received_ns=base + 3_600 * 10**9)  # next hour -> new file
    w.close()

    files = sorted(tmp_path.glob("capture-*.jsonl.gz"))
    assert len(files) == 2, "an hour boundary must start a new file"
    first = list(read_records(files[0]))
    assert first == [{"t": base, "ch": "ticker", "m": {"a": 1}}]
    assert w.records_written == 2


def test_writer_uncompressed_and_preserves_raw(tmp_path):
    w = RecordWriter(tmp_path, compress=False, flush_every=1)
    weird = {"nested": [1, {"x": None}], "unicode": "café"}
    w.write("orderbook_delta", weird, received_ns=1)
    w.close()
    path = next(tmp_path.glob("*.jsonl"))
    assert json.loads(path.read_text())["m"] == weird


# --------------------------------------------------------------------------- prices
@pytest.mark.parametrize(
    "value,micros",
    [
        (8, 80_000),  # legacy: 8 whole cents
        ("0.0800", 80_000),  # current: fixed-point dollars
        ("0.0010", 1_000),  # a tenth of a cent, the tapered tick in the tails
        ("0.9990", 999_000),
        ("0.123456", 123_456),  # responses can carry six decimals
    ],
)
def test_parse_price(value, micros):
    assert parse_price(value) == micros


def test_parse_price_keeps_sub_cent_resolution():
    # The bug this guards: rounding to whole cents collapses distinct levels in the tails.
    assert parse_price("0.0810") != parse_price("0.0800")
    assert parse_price("0.0810") - parse_price("0.0800") == MICROS_PER_CENT // 10


def test_parse_count_fractional():
    assert parse_count(3) == 300
    assert parse_count("300.00") == 30_000
    assert parse_count("0.01") == 1


# --------------------------------------------------------------------------- book
def snapshot(yes, no):
    return {"market_ticker": "M", "yes_dollars_fp": yes, "no_dollars_fp": no}


def test_book_snapshot_and_best_prices():
    b = OrderBook("M")
    assert b.stale
    b.apply_snapshot(snapshot([["0.4000", "100.00"]], [["0.5500", "200.00"]]), seq=1)
    assert not b.stale
    assert b.best_yes_bid.price_cents == 40.0
    assert b.best_no_bid.price_cents == 55.0
    # buying YES costs 1 - best NO bid, NOT 1 - the YES ask
    assert b.best_yes_ask_micros == MICROS_PER_DOLLAR - 550_000
    assert b.best_no_ask_micros == MICROS_PER_DOLLAR - 400_000
    assert b.spread_micros == 450_000 - 400_000


def test_book_applies_deltas_and_removes_empty_levels():
    b = OrderBook("M")
    b.apply_snapshot(snapshot([["0.4000", "100.00"]], [["0.5500", "200.00"]]), seq=1)
    b.apply_delta({"side": "yes", "price_dollars": "0.4000", "delta_fp": "50.00"}, seq=2)
    assert b.yes[400_000] == 15_000
    b.apply_delta({"side": "yes", "price_dollars": "0.4000", "delta_fp": "-150.00"}, seq=3)
    assert 400_000 not in b.yes, "a level drained to zero must disappear"
    b.apply_delta({"side": "yes", "price_dollars": "0.3900", "delta_fp": "10.00"}, seq=4)
    assert b.best_yes_bid.price_cents == 39.0


def test_book_legacy_integer_payload():
    b = OrderBook("M")
    b.apply_snapshot({"market_ticker": "M", "yes": [[40, 100]], "no": [[55, 200]]}, seq=1)
    assert b.best_yes_bid.price_cents == 40.0
    b.apply_delta({"side": "no", "price": 55, "delta": -200}, seq=2)
    assert b.best_no_bid is None


def test_stale_book_ignores_deltas_until_resnapshot():
    b = OrderBook("M")
    b.apply_snapshot(snapshot([["0.4000", "100.00"]], []), seq=1)
    b.mark_stale()
    b.apply_delta({"side": "yes", "price_dollars": "0.4000", "delta_fp": "-100.00"}, seq=2)
    assert b.yes[400_000] == 10_000, "deltas must not be applied to a book known to be stale"
    b.apply_snapshot(snapshot([["0.4200", "10.00"]], []), seq=9)
    assert not b.stale and b.best_yes_bid.price_cents == 42.0


def test_depth_to_buy():
    b = OrderBook("M")
    # NO bids at 55 and 54 => YES asks at 45 and 46
    b.apply_snapshot(snapshot([], [["0.5500", "20.00"], ["0.5400", "30.00"]]), seq=1)
    assert b.depth_to_buy("yes", 45 * MICROS_PER_CENT) == 2_000
    assert b.depth_to_buy("yes", 46 * MICROS_PER_CENT) == 5_000
    assert b.depth_to_buy("yes", 44 * MICROS_PER_CENT) == 0


# --------------------------------------------------------------------------- sequences
def test_sequence_tracker():
    t = SequenceTracker()
    assert t.check(1, 1) is None and t.check(1, 2) is None
    gap = t.check(1, 5)
    assert gap is not None and gap.expected == 3 and gap.received == 5
    assert t.check(1, 4) is None, "replays behind the pointer are ignored, not re-reported"
    assert t.check(2, 100) is None, "each subscription is tracked independently"


# --------------------------------------------------------------------------- fake socket
class FakeSocket:
    """Stands in for a websockets connection: records sends, replays scripted messages."""

    def __init__(self, incoming):
        self.incoming = list(incoming)
        self.sent = []
        self.closed = False

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    async def close(self):
        self.closed = True

    def __aiter__(self):
        async def gen():
            for m in self.incoming:
                yield json.dumps(m)

        return gen()


def make_ws(incoming):
    sock = FakeSocket(incoming)

    async def connect(url, **kwargs):
        return sock

    ws = KalshiWebSocket(
        "wss://x/trade-api/ws/v2", KalshiSigner("k", generate_test_key()), connect=connect
    )
    return ws, sock


async def test_ws_subscribe_payloads():
    ws, sock = make_ws([])
    await ws.connect()
    await ws.subscribe(["orderbook_delta"], market_tickers=["M1"])
    await ws.subscribe(["cfbenchmarks_value_5hz"], index_ids=["BRTI"])
    await ws.request_snapshot(7, ["M1"])
    assert sock.sent[0] == {
        "id": 1,
        "cmd": "subscribe",
        "params": {"channels": ["orderbook_delta"], "market_tickers": ["M1"]},
    }
    assert sock.sent[1]["params"] == {"channels": ["cfbenchmarks_value_5hz"], "index_ids": ["BRTI"]}
    assert sock.sent[2] == {
        "id": 3,
        "cmd": "update_subscription",
        "params": {"sid": 7, "action": "get_snapshot", "market_tickers": ["M1"]},
    }


async def test_ws_requires_connection():
    ws, _ = make_ws([])
    with pytest.raises(WebSocketClosedError):
        await ws.subscribe(["ticker"])


# --------------------------------------------------------------------------- recorder
def a_market(ticker, minutes_to_close, strike=None):
    now = datetime.now(UTC)
    return {
        "ticker": ticker,
        "event_ticker": "KXBTC15M-X",
        "status": "active",
        "open_time": (now + timedelta(minutes=minutes_to_close - 15)).isoformat(),
        "close_time": (now + timedelta(minutes=minutes_to_close)).isoformat(),
        "floor_strike": strike,
    }


@respx.mock
async def test_recorder_discovers_only_relevant_windows(tmp_path):
    respx.get(f"{BASE}/markets").mock(
        return_value=httpx.Response(
            200,
            json={
                "markets": [
                    a_market("LIVE", 5),  # in progress
                    a_market("NEXT", 18),  # opens soon, inside the pre-open window
                    a_market("FAR", 300),  # tomorrow: ignore
                    a_market("DONE", -30),  # long settled: ignore
                ],
                "cursor": "",
            },
        )
    )
    ws, _ = make_ws([])
    client = KalshiClient(BASE, KalshiSigner("k", generate_test_key()))
    rec = Recorder(client, ws, RecordWriter(tmp_path), watch_strikes=False)
    try:
        found = [m.ticker for m in await rec.discover_markets()]
    finally:
        await client.close()
    assert found == ["LIVE", "NEXT"]


@respx.mock
async def test_recorder_session_captures_and_rebuilds_book(tmp_path):
    respx.get(f"{BASE}/markets").mock(
        return_value=httpx.Response(200, json={"markets": [a_market("M1", 5)], "cursor": ""})
    )
    messages = [
        {"type": "subscribed", "id": 1, "msg": {"channel": "cfbenchmarks_value_5hz", "sid": 1}},
        {"type": "subscribed", "id": 3, "msg": {"channel": "orderbook_delta", "sid": 3}},
        {
            "type": "cfbenchmarks_value",
            "sid": 1,
            "seq": 1,
            "msg": {"index_id": "BRTI", "value": "110000.00", "last_60s_average": "109990.00"},
        },
        {
            "type": "orderbook_snapshot",
            "sid": 3,
            "seq": 1,
            "msg": snapshot([["0.4000", "100.00"]], [["0.5500", "200.00"]]),
        },
        {
            "type": "orderbook_delta",
            "sid": 3,
            "seq": 2,
            "msg": {
                "market_ticker": "M",
                "side": "yes",
                "price_dollars": "0.4100",
                "delta_fp": "25.00",
            },
        },
        {
            "type": "orderbook_delta",
            "sid": 3,
            "seq": 4,  # gap: 3 is missing
            "msg": {
                "market_ticker": "M",
                "side": "yes",
                "price_dollars": "0.4200",
                "delta_fp": "5.00",
            },
        },
    ]
    ws, sock = make_ws(messages)
    client = KalshiClient(BASE, KalshiSigner("k", generate_test_key()))
    writer = RecordWriter(tmp_path, flush_every=1)
    rec = Recorder(client, ws, writer, watch_strikes=False, refresh_seconds=999, stats_seconds=999)
    try:
        await ws.connect()
        await rec.session()
        await asyncio.sleep(0)  # let the spawned re-snapshot request run
    finally:
        await rec.stop()
        await client.close()

    # every message reached disk, verbatim, with a local timestamp
    records = list(read_records(next(tmp_path.glob("*.jsonl.gz"))))
    kinds = [r["ch"] for r in records]
    assert kinds.count("orderbook_delta") == 2 and "cfbenchmarks_value" in kinds
    assert all(isinstance(r["t"], int) and r["t"] > 0 for r in records)
    assert records[2]["m"]["msg"]["value"] == "110000.00"

    # the book was rebuilt, the gap was noticed, and a re-snapshot was requested
    assert rec.stats.gaps == 1
    assert rec.stats.index_ticks == 1
    book = rec.books["M"]
    assert book.stale, "after a gap the book must refuse to be trusted"
    assert any(s.get("params", {}).get("action") == "get_snapshot" for s in sock.sent)


@respx.mock
async def test_recorder_falls_back_when_5hz_feed_is_unavailable(tmp_path):
    respx.get(f"{BASE}/markets").mock(
        return_value=httpx.Response(200, json={"markets": [], "cursor": ""})
    )
    ws, sock = make_ws(
        [
            {"type": "error", "id": 1, "msg": {"code": 8, "msg": "unknown channel"}},
        ]
    )
    client = KalshiClient(BASE, KalshiSigner("k", generate_test_key()))
    rec = Recorder(
        client,
        ws,
        RecordWriter(tmp_path),
        watch_strikes=False,
        refresh_seconds=999,
        stats_seconds=999,
    )
    try:
        await ws.connect()
        await rec.session()
        await asyncio.sleep(0)  # let the fallback subscribe run
    finally:
        await rec.stop()
        await client.close()
    channels = [
        s["params"].get("channels", [None])[0] for s in sock.sent if s["cmd"] == "subscribe"
    ]
    assert "cfbenchmarks_value_5hz" in channels
    assert "cfbenchmarks_value" in channels, "must fall back to the once-a-second feed"


# --------------------------------------------------------------------------- inspect
def test_capture_summary(tmp_path):
    from kalshi_agent.recorder.inspect import summarise

    w = RecordWriter(tmp_path, flush_every=1)
    t0 = 1_800_000_000_000_000_000
    sec = 1_000_000_000
    for i in range(4):  # index ticks one second apart
        w.write(
            "cfbenchmarks_value",
            {"sid": 1, "seq": i + 1, "msg": {"index_id": "BRTI", "value": "110000.00"}},
            received_ns=t0 + i * sec,
        )
    # a five second stall, and a windowed average (only sent in the settlement minute)
    w.write(
        "cfbenchmarks_value",
        {"sid": 1, "seq": 5, "msg": {"last_60s_windowed_average_15min": "109990.00"}},
        received_ns=t0 + 9 * sec,
    )
    w.write(
        "orderbook_snapshot",
        {"sid": 2, "seq": 1, "msg": {"market_ticker": "KXBTC15M-A"}},
        received_ns=t0 + 10 * sec,
    )
    w.write(
        "orderbook_delta",
        {"sid": 2, "seq": 4, "msg": {"market_ticker": "KXBTC15M-A"}},  # gap: 2 and 3 missing
        received_ns=t0 + 11 * sec,
    )
    w.write(
        "strike_watch",
        {"ticker": "KXBTC15M-A", "seconds_after_open": 1.75, "floor_strike": 110_000.0},
        received_ns=t0 + 12 * sec,
    )
    w.write(
        "strike_watch",
        {"ticker": "KXBTC15M-A", "seconds_after_open": 2.0, "floor_strike": 110_000.0},
        received_ns=t0 + 13 * sec,
    )
    w.close()

    s = summarise(tmp_path).as_dict()
    assert s["records"] == 9
    assert s["index_ticks"] == 5
    assert s["settlement_window_ticks"] == 1
    assert s["index_stalls_over_2s"] == 1 and s["max_index_gap_s"] == 6.0
    assert s["sequence_gaps"] == 1
    assert s["markets_seen"] == 1
    assert s["strikes_timed"] == 1
    assert s["strike_delay_median_s"] == 1.75, "the earliest sighting is the true delay"


def test_capture_summary_empty_directory(tmp_path):
    from kalshi_agent.recorder.inspect import summarise

    s = summarise(tmp_path).as_dict()
    assert s["records"] == 0 and s["files"] == 0 and s["index_rate_hz"] == 0.0
