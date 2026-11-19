"""Replay a single symbol's regular-session five-minute bars."""
import argparse
import csv
import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Timestamps must include a UTC offset.")
    return parsed.astimezone(ET)


def replay(bars, level, start, end, sessions, threshold, events):
    totals = defaultdict(int)
    history = defaultdict(dict)
    expected = {}
    for at, close, volume in bars:
        if at > end:
            break
        day = at.date()
        minute = at.hour * 60 + at.minute
        if at.weekday() >= 5 or not 575 <= minute <= 960 or minute % 5:
            continue
        if minute != expected.get(day, 575):
            continue  # A missing bar makes this session's later totals incomplete.
        expected[day] = minute + 5
        totals[day] += volume
        previous = [total for date, total in history[minute].items() if date < day][-sessions:]
        history[minute][day] = totals[day]
        if at < start or len(previous) < sessions or not sum(previous):
            continue
        relative_volume = totals[day] / mean(previous)
        if level * 0.98 <= close <= level and relative_volume >= threshold:
            public_events = [
                event for published, scheduled, event in events
                if published <= at <= scheduled <= at + timedelta(days=7)
            ]
            return {
                "timestamp": at.isoformat(),
                "price": close,
                "watch_level": level,
                "relative_volume": round(relative_volume, 2),
                "public_upcoming_events": public_events,
            }
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bars", type=Path)
    parser.add_argument("--level", type=float, required=True)
    parser.add_argument("--from", dest="start", type=timestamp, required=True)
    parser.add_argument("--through", type=timestamp, required=True)
    parser.add_argument("--baseline-sessions", type=int, default=20)
    parser.add_argument("--rvol", type=float, default=1.3)
    parser.add_argument("--events", type=Path)
    args = parser.parse_args()
    if args.baseline_sessions < 1 or args.level <= 0 or args.rvol <= 0 or args.start > args.through:
        parser.error("Use positive settings and a cutoff at or after the replay start.")
    with args.bars.open(newline="") as file:
        bars = sorted(
            (timestamp(row["bar_end"]), float(row["close"]), int(row["volume"]))
            for row in csv.DictReader(file)
        )
    if len({at for at, _, _ in bars}) != len(bars):
        parser.error("Use one row per bar end for one symbol.")
    events = []
    if args.events:
        events = [
            (timestamp(event["published_at"]), timestamp(event["scheduled_at"]), event)
            for event in json.loads(args.events.read_text())
        ]
    print(json.dumps(replay(
        bars, args.level, args.start, args.through,
        args.baseline_sessions, args.rvol, events,
    ), indent=2))


if __name__ == "__main__":
    main()
