#!/usr/bin/env python3
"""Offline measurement over a block range (aggregate, canonical chain only).

Fetches header timestamps through RPC and reports block intervals, MTP lag and
10-block windows. Use this to reproduce the aggregate numbers posted in the
tempnet thread; use tempnet_export.py for the live node-local capture.

Usage:
    python3 tools/measure.py                      # last 1000 blocks
    python3 tools/measure.py --from 327989        # from the tempnet fork point
    python3 tools/measure.py --from 327989 --to 339900 --out data.json

Environment:
    TEMPNET_CLI     path to bitcoin-cli
    TEMPNET_DATADIR node datadir
"""
import argparse
import json
import os
import subprocess
import sys

CLI = os.environ.get(
    "TEMPNET_CLI", "/mnt/tempnet/bchn-tempnet/build/src/bitcoin-cli")
DATADIR = os.environ.get("TEMPNET_DATADIR", "/mnt/tempnet/data")


def rpc(*args):
    cmd = [CLI, "-tempnet", "-datadir=" + DATADIR] + [str(a) for a in args]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    out = r.stdout.strip()
    return json.loads(out) if out[:1] in ("{", "[") else out


def pct(vals, p):
    s = sorted(vals)
    k = int(round((p / 100.0) * (len(s) - 1)))
    return s[max(0, min(len(s) - 1, k))]


def stats(vals):
    if not vals:
        return {}
    return {
        "n": len(vals),
        "mean": round(sum(vals) / len(vals), 2),
        "median": pct(vals, 50),
        "p90": pct(vals, 90),
        "p99": pct(vals, 99),
        "min": min(vals),
        "max": max(vals),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="frm", type=int, default=None)
    ap.add_argument("--to", dest="to", type=int, default=None)
    ap.add_argument("--out", default="tempnet_measure.json")
    a = ap.parse_args()

    tip = int(rpc("getblockcount"))
    start = a.frm if a.frm is not None else max(1, tip - 1000)
    end = a.to if a.to is not None else tip
    print(f"tip={tip}  measuring {start}..{end}", file=sys.stderr)

    blocks = []
    for h in range(start, end + 1):
        hsh = rpc("getblockhash", h)
        hdr = rpc("getblockheader", hsh)
        blocks.append({"height": h, "hash": hsh, "time": hdr["time"]})

    intervals = [blocks[i + 1]["time"] - blocks[i]["time"]
                 for i in range(len(blocks) - 1)]

    mtps = []
    for i in range(10, len(blocks)):
        window = sorted(b["time"] for b in blocks[i - 10:i + 1])
        mtps.append({"height": blocks[i]["height"], "time": blocks[i]["time"],
                     "mtp": window[5], "lag": blocks[i]["time"] - window[5]})

    windows = [{"height": blocks[i]["height"],
                "span": blocks[i]["time"] - blocks[i - 9]["time"]}
               for i in range(9, len(blocks))]

    out = {
        "tip": tip,
        "range": [start, end],
        "target_time": rpc("getblockchaininfo").get("target_time", 60),
        "intervals": stats(intervals),
        "windows_10": stats([w["span"] for w in windows]),
        "mtp_lag": stats([m["lag"] for m in mtps]),
        "blocks": blocks,
        "mtps": mtps,
    }
    with open(a.out, "w") as f:
        json.dump(out, f, indent=2)
    print(json.dumps({k: out[k] for k in
                      ("tip", "range", "intervals", "windows_10", "mtp_lag")},
                     indent=2))


if __name__ == "__main__":
    main()
