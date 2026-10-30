#!/usr/bin/env python3
"""tempnet-data exporter.

Reads the tempnet node (RPC) and the listener capture (/mnt/tempnet/events.csv),
computes chain timing metrics, writes:
  data/stats.json      machine-readable snapshot
  data/events.csv      copy of the listener capture
  index.html           self-contained dashboard (data inlined)
  history/YYYY-MM-DD.jsonl  append-only one-line summaries
and commits+pushes on the hybrid rule: 10 new blocks OR 15 min OR a reorg,
whatever comes first.
"""

import csv
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
HIST = os.path.join(BASE, "history")
STATE = os.path.join(BASE, ".state.json")
EVENTS_SRC = os.environ.get("TEMPNET_EVENTS", "/mnt/tempnet/events.csv")
LOG_SRC = os.environ.get("TEMPNET_LOG", "/mnt/tempnet/data/tempnet/debug.log")
CLI = os.environ.get("TEMPNET_CLI", "/mnt/tempnet/bchn-tempnet/build/src/bitcoin-cli")
DATADIR = os.environ.get("TEMPNET_DATADIR", "/mnt/tempnet/data")
COMPACT_CSV = os.path.join(DATA, "compact.csv")
COMPACT_OFFSET = os.path.join(BASE, ".compact_offset")
BLOCK_TRIGGER = 10
SECS_TRIGGER = 300
SERIES_N = 240

REORG_EVENTS = {"REORGANIZE", "DisconnectTip", "BlockDisconnected"}

RE_RECON = re.compile(
    r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) Successfully reconstructed block ([0-9a-f]{64}) "
    r"with (\d+) txn prefilled, (\d+) txn from mempool \(incl at least (\d+) from extra pool\) "
    r"and (\d+) txn requested"
)


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def rpc(*args, check=True):
    cmd = [CLI, "-tempnet", "-datadir=" + DATADIR, *args]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        if check:
            raise RuntimeError(f"rpc {' '.join(args)} failed: {p.stderr.strip()}")
        return None
    out = p.stdout.strip()
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return out


def pct(vals, p):
    """Nearest-rank percentile."""
    if not vals:
        return None
    s = sorted(vals)
    k = int(round((p / 100.0) * (len(s) - 1)))
    return s[max(0, min(len(s) - 1, k))]


def summarize(vals):
    if not vals:
        return {"n": 0}
    return {
        "n": len(vals),
        "mean": round(sum(vals) / len(vals), 2),
        "median": round(float(pct(vals, 50)), 2),
        "p90": round(float(pct(vals, 90)), 2),
        "p99": round(float(pct(vals, 99)), 2),
        "max": round(float(max(vals)), 2),
        "min": round(float(min(vals)), 2),
    }


def update_compact_index():
    """Append new compact-block reconstructions from debug.log to data/compact.csv."""
    if not os.path.exists(LOG_SRC):
        return
    try:
        offset = int(open(COMPACT_OFFSET, encoding="utf-8").read().strip())
    except (FileNotFoundError, ValueError):
        offset = 0
    size = os.path.getsize(LOG_SRC)
    if offset > size:
        # bitcoind shrinks debug.log to ~10 MB on restart; restart the index from the
        # new end instead of waiting for the file to grow past the old offset.
        print(f"compact index: offset {offset} > log size {size}, resetting to {size}",
              file=sys.stderr)
        offset = size
    with open(LOG_SRC, "rb") as f:
        f.seek(offset)
        chunk = f.read()
    cut = chunk.rfind(b"\n") + 1
    complete = chunk[:cut]
    if not complete:
        with open(COMPACT_OFFSET, "w", encoding="utf-8") as f:
            f.write(str(offset))
        return
    rows = []
    for line in complete.decode("utf-8", "replace").splitlines():
        m = RE_RECON.match(line)
        if m:
            rows.append([m.group(1), m.group(2), m.group(3), m.group(4), m.group(5), m.group(6)])
    new_file = not os.path.exists(COMPACT_CSV)
    with open(COMPACT_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["log_time", "hash", "prefilled", "from_mempool", "extra_pool", "requested"])
        w.writerows(rows)
    with open(COMPACT_OFFSET, "w", encoding="utf-8") as f:
        f.write(str(offset + len(complete)))


def read_compact():
    out = {}
    if not os.path.exists(COMPACT_CSV):
        return out
    with open(COMPACT_CSV, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                out[r["hash"]] = {
                    "t": r["log_time"],
                    "prefilled": int(r["prefilled"]),
                    "from_mempool": int(r["from_mempool"]),
                    "extra_pool": int(r["extra_pool"]),
                    "requested": int(r["requested"]),
                }
            except (KeyError, ValueError):
                continue
    return out


def host_clock():
    """Clock sync status of the capturing host (the measurement's error floor)."""
    def grab(cmd, key):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
        except Exception:
            return None
        for line in out.splitlines():
            if line.strip().startswith(key):
                return line.split(":", 1)[1].strip()
        return None

    src = "systemd-timesyncd"
    offset = grab(["timedatectl", "timesync-status"], "Offset")
    if offset is None:
        src = "chrony"
        offset = grab(["chronyc", "tracking"], "System time")
    if offset is None:
        return None

    def ms(v):
        """Parse a systemd-timesyncd duration like '-11.837ms' into milliseconds."""
        m = re.search(r"-?\d+(?:[.,]\d+)?", v or "")
        if not m:
            return None
        x = float(m.group(0).replace(",", "."))
        if "µs" in (v or "") or "us" in (v or ""):
            return round(x / 1000.0, 3)
        if "ms" in (v or ""):
            return round(x, 3)
        return None

    return {
        "source": src,
        "offset_raw": offset,
        "offset_ms": ms(offset) if src == "systemd-timesyncd" else None,
        "delay_ms": ms(grab(["timedatectl", "timesync-status"], "Delay") or ""),
        "jitter_ms": ms(grab(["timedatectl", "timesync-status"], "Jitter") or ""),
        "stratum": grab(["timedatectl", "timesync-status"], "Stratum"),
        "poll_interval": grab(["timedatectl", "timesync-status"], "Poll interval"),
        "note": "offset is as of the last NTP poll, not the moment of capture",
    }


def read_events():
    rows = []
    if not os.path.exists(EVENTS_SRC):
        return rows
    with open(EVENTS_SRC, newline="", encoding="utf-8", errors="replace") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    return rows


def chain_blocks(rows, compact):
    """Unique consecutive UpdateTip blocks, ordered by height."""
    by_h = {}
    for r in rows:
        if r.get("event") != "UpdateTip":
            continue
        try:
            h = int(r["height"])
        except (KeyError, ValueError, TypeError):
            continue
        if h not in by_h:
            by_h[h] = r
    heights = sorted(by_h)
    out = []
    for h in heights:
        if out and h != out[-1]["height"] + 1:
            continue
        r = by_h[h]
        ht = parse_ts(r.get("header_time"))
        tl = parse_ts(r.get("ts_local"))
        info = compact.get(r.get("hash"))
        out.append({
            "height": h,
            "hash": r.get("hash"),
            "header_time": ht,
            "local_time": tl,
            "via": "compact" if info else "direct",
            "compact": info,
        })
    return out


def compute(blocks, tip_height):
    intervals, delays, via_counts = [], [], {}
    reassembled, requested = 0, []
    series = []
    prev = None
    for b in blocks:
        via_counts[b["via"]] = via_counts.get(b["via"], 0) + 1
        if b.get("compact"):
            reassembled += 1
            requested.append(b["compact"]["requested"])
        if b["local_time"] and b["header_time"]:
            delays.append(round((b["local_time"] - b["header_time"]).total_seconds(), 2))
        if prev is not None and b["header_time"] and prev["header_time"]:
            dt = (b["header_time"] - prev["header_time"]).total_seconds()
            if dt >= 0:
                intervals.append(dt)
                series.append({
                    "h": b["height"],
                    "t": iso(b["header_time"]),
                    "dt": round(dt, 1),
                    "tsdev": (round((b["local_time"] - b["header_time"]).total_seconds(), 1)
                             if b["local_time"] and b["header_time"] else None),
                })
        prev = b

    windows = [sum(intervals[i:i + BLOCK_TRIGGER])
               for i in range(0, len(intervals) - BLOCK_TRIGGER + 1, BLOCK_TRIGGER)]

    tip_time = blocks[-1]["header_time"] if blocks else None
    if tip_time:
        cutoff = tip_time.timestamp() - 86400
        last24 = [iv for iv, s in zip(intervals, series)
                  if s["t"] and parse_ts(s["t"]).timestamp() >= cutoff]
    else:
        last24 = []

    t = 60
    expected = {
        "model": "exponential (constant hashrate and difficulty)",
        "target_time": t,
        "median": round(t * 0.6931471805599453, 2),
        "p90": round(t * 2.302585092994046, 2),
        "p99": round(t * 4.605170185988092, 2),
        "over_5min_pct": round(100 * 0.006737946999085467, 3),
    }
    return {
        "expected": expected,
        "intervals": summarize(intervals),
        "intervals_last_100": summarize(intervals[-100:]),
        "intervals_last_24h": summarize(last24),
        "windows_10": summarize(windows),
        "timestamp_deviation": summarize(delays),
        "via": via_counts,
        "compact": {
            "captured_via_compact": reassembled,
            "captured_total": len(blocks),
            "missing_tx": summarize(requested),
        },
        "series": series[-SERIES_N:],
    }


def reorg_events(rows):
    out = []
    for r in rows:
        if r.get("event") in REORG_EVENTS:
            out.append({
                "ts": r.get("ts_local"),
                "event": r.get("event"),
                "height": r.get("height"),
                "hash": r.get("hash"),
            })
    return out


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_state(st):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1)


def git(*args):
    p = subprocess.run(["git", "-C", BASE, *args], capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {p.stderr.strip()}")
    return p.stdout.strip()


def render_html(stats):
    tpl_path = os.path.join(BASE, "index.tpl.html")
    with open(tpl_path, encoding="utf-8") as f:
        tpl = f.read()
    return tpl.replace("/*__DATA__*/null", json.dumps(stats, separators=(",", ":")))


def main():
    os.makedirs(DATA, exist_ok=True)
    os.makedirs(HIST, exist_ok=True)

    try:
        info = rpc("getblockchaininfo")
        net = rpc("getnetworkinfo", check=False) or {}
        peers = rpc("getconnectioncount", check=False)
        hashrate = rpc("getnetworkhashps", check=False)
        mempool = rpc("getmempoolinfo", check=False) or {}
        header = rpc("getblockheader", info["bestblockhash"], check=False) or {}
    except Exception as e:
        print(f"node unavailable, skipping: {e}", file=sys.stderr)
        return 0

    rows = read_events()
    update_compact_index()
    blocks = chain_blocks(rows, read_compact())
    metrics = compute(blocks, info["blocks"])
    reorgs = reorg_events(rows)

    tip_time = parse_ts(header.get("time") and datetime.fromtimestamp(
        header["time"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    mediantime = info.get("mediantime")
    mtp_lag = (int(tip_time.timestamp()) - int(mediantime)) if tip_time and mediantime else None

    now = utcnow()
    stats = {
        "generated_at": iso(now),
        "chain": "temp",
        "network": {
            "target_time": info.get("target_time", 60),
            "difficulty": info.get("difficulty"),
            "hashrate": hashrate,
            "peers": peers,
            "mempool_tx": mempool.get("size"),
            "version": net.get("subversion"),
        },
        "host_clock": host_clock(),
        "sync": {
            "blocks": info.get("blocks"),
            "headers": info.get("headers"),
            "progress": round(info.get("verificationprogress", 0), 6),
            "ibd": info.get("initialblockdownload"),
        },
        "tip": {
            "height": info.get("blocks"),
            "hash": info.get("bestblockhash"),
            "header_time": iso(tip_time) if tip_time else None,
            "mediantime": (datetime.fromtimestamp(mediantime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                           if mediantime else None),
            "mtp_lag_s": mtp_lag,
        },
        "captured": {
            "from_height": blocks[0]["height"] if blocks else None,
            "to_height": blocks[-1]["height"] if blocks else None,
            "blocks": len(blocks),
            "from_time": iso(blocks[0]["header_time"]) if blocks and blocks[0]["header_time"] else None,
            "to_time": iso(blocks[-1]["header_time"]) if blocks and blocks[-1]["header_time"] else None,
        },
        "reorgs": {"count": len(reorgs), "events": reorgs[-10:]},
        **metrics,
    }

    with open(os.path.join(DATA, "stats.json"), "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=1)
        f.write("\n")
    with open(os.path.join(BASE, "index.html"), "w", encoding="utf-8") as f:
        f.write(render_html(stats))
    if os.path.exists(EVENTS_SRC):
        with open(EVENTS_SRC, encoding="utf-8", errors="replace") as src, \
                open(os.path.join(DATA, "events.csv"), "w", encoding="utf-8") as dst:
            dst.write(src.read())

    st = load_state() or {}
    height = info.get("blocks") or 0
    summary = {
        "generated_at": stats["generated_at"],
        "height": height,
        "intervals": stats["intervals"],
        "windows_10": stats["windows_10"],
        "timestamp_deviation": stats["timestamp_deviation"],
        "mtp_lag_s": mtp_lag,
        "peers": peers,
    }
    with open(os.path.join(HIST, now.strftime("%Y-%m-%d") + ".jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(summary, separators=(",", ":")) + "\n")

    first = "committed_at" not in st
    last_h = st.get("height", 0)
    blocks_since = height - last_h
    committed_at = parse_ts(st.get("committed_at"))
    elapsed = (now - committed_at).total_seconds() if committed_at else 1e9
    new_reorg = len(reorgs) > st.get("reorg_count", 0)

    should = first or blocks_since >= BLOCK_TRIGGER or elapsed >= SECS_TRIGGER or new_reorg
    if not should:
        save_state({"height": last_h, "committed_at": st.get("committed_at"),
                    "reorg_count": len(reorgs),
                    "last_check": stats["generated_at"], "last_commit": st.get("last_commit")})
        print(f"no commit yet: +{blocks_since} blocks, {int(elapsed)}s since last commit")
        return 0

    reason = "initial" if first else (
        "reorg" if new_reorg else (f"{blocks_since} blocks" if blocks_since >= BLOCK_TRIGGER
                                   else f"{int(elapsed)}s"))
    msg = f"snapshot {height} ({stats['generated_at']}, {reason})"

    git("add", "-A")
    git("-c", "user.name=tempnet-data bot", "-c", "user.email=tempnet-data@asgaya.org",
        "commit", "-q", "-m", msg)
    git("push", "-q", "origin", "HEAD:main")
    save_state({"height": height, "committed_at": stats["generated_at"],
                "reorg_count": len(reorgs), "last_commit": msg})
    print(f"committed: {msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
