#!/usr/bin/env python3
"""Tempnet capture listener.

Tails the node's debug.log and appends chain events to a CSV, stamping each row
with the *local wall-clock time at which the line was read*. That local time is
the interesting part: it is the only record of when we learned about a block,
and it never becomes part of the canonical chain.

Captures:
  UpdateTip        a block was connected (with its header timestamp)
  REORGANIZE       a reorg happened
  DisconnectTip    a block was unconnected
  block_compact    a block was reconstructed from a cmpctblock (BIP152),
                   including how many transactions had to be requested
  block_inv        a full block arrived through the announcement path

Environment:
  TEMPNET_LOG     node debug.log   (default /mnt/tempnet/data/tempnet/debug.log)
  TEMPNET_EVENTS  output CSV       (default /mnt/tempnet/events.csv)

Run: python3 tempnet_listener.py
"""
import csv
import os
import re
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

LOG = Path(os.environ.get("TEMPNET_LOG", "/mnt/tempnet/data/tempnet/debug.log"))
OUT = Path(os.environ.get("TEMPNET_EVENTS", "/mnt/tempnet/events.csv"))

FIELDS = ["ts_local", "event", "height", "hash", "header_time", "via", "raw"]

RE_TIP = re.compile(
    r"UpdateTip: new best=([0-9a-f]{64}) height=(\d+) .*? date='?([^' ]+)"
)
RE_REORG = re.compile(r"REORGANIZE: disconnect (\d+), connect (\d+)")
RE_DISCONNECT = re.compile(r"DisconnectTip\(\): block=([0-9a-f]{64}) height=(\d+)")
RE_RECON = re.compile(
    r"Successfully reconstructed block ([0-9a-f]{64}) with (\d+) txn prefilled, "
    r"(\d+) txn from mempool .*? and (\d+) txn requested"
)
RE_RECEIVED = re.compile(r"received: block ([0-9a-f]{64})")

recent_compact = deque(maxlen=64)


def ensure_header():
    if not OUT.exists():
        OUT.parent.mkdir(parents=True, exist_ok=True)
        with OUT.open("w", newline="") as f:
            csv.writer(f).writerow(FIELDS)


def write_row(row):
    with OUT.open("a", newline="") as f:
        csv.writer(f).writerow(row)


def handle_line(line, now):
    m = RE_TIP.search(line)
    if m:
        via = "compact" if m.group(1) in recent_compact else "direct"
        write_row([now, "UpdateTip", m.group(2), m.group(1), m.group(3), via,
                   line.strip()[:300]])
        return
    m = RE_REORG.search(line)
    if m:
        write_row([now, "REORGANIZE", "", "", "",
                   f"disconnect={m.group(1)} connect={m.group(2)}", line.strip()[:300]])
        return
    m = RE_DISCONNECT.search(line)
    if m:
        write_row([now, "DisconnectTip", m.group(2), m.group(1), "", "",
                   line.strip()[:300]])
        return
    m = RE_RECON.search(line)
    if m:
        recent_compact.append(m.group(1))
        write_row([now, "block_compact", "", m.group(1), "", "compact",
                   line.strip()[:300]])
        return
    m = RE_RECEIVED.search(line)
    if m:
        write_row([now, "block_inv", "", m.group(1), "", "direct",
                   line.strip()[:300]])
        return


def main():
    ensure_header()
    if not LOG.exists():
        print(f"waiting for {LOG} ...", file=sys.stderr)
    f = None
    while True:
        if f is None:
            try:
                f = LOG.open("r", errors="replace")
                f.seek(0, os.SEEK_END)
            except FileNotFoundError:
                time.sleep(2)
                continue
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        line = f.readline()
        if not line:
            time.sleep(0.05)
            continue
        try:
            handle_line(line, now)
        except Exception as e:
            print(f"parse error: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
