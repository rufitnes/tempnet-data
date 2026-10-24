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
import base64
import json
import os
import subprocess
import sys
import urllib.request

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


def rpc_stdin(lines):
    """JSON-RPC batch over HTTP (much faster than one bitcoin-cli per call)."""
    cookie_path = os.path.join(DATADIR, "tempnet", ".cookie")
    with open(cookie_path, encoding="utf-8") as f:
        auth = "Basic " + base64.b64encode(f.read().strip().encode()).decode()
    port = os.environ.get("TEMPNET_RPCPORT", "48332")
    url = f"http://127.0.0.1:{port}/"
    out = []
    for i in range(0, len(lines), 1000):
        chunk = lines[i:i + 1000]
        payload = [{"jsonrpc": "1.0", "id": j, "method": m, "params": p}
                   for j, (m, p) in enumerate(chunk)]
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers={"Authorization": auth, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            res = json.loads(r.read())
        res.sort(key=lambda x: x["id"])
        for x in res:
            if x.get("error"):
                raise RuntimeError(x["error"])
            out.append(x["result"])
    return out


def header_time(block_hex):
    """Timestamp from a serialised 80-byte block header (big-endian uint32)."""
    return int.from_bytes(bytes.fromhex(block_hex[136:144]), "little")


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
    hashes = rpc_stdin([("getblockhash", [h]) for h in range(start, end + 1)])
    headers = rpc_stdin([("getblockheader", [h, False]) for h in hashes])
    if len(hashes) != len(headers):
        raise RuntimeError(f"batch mismatch: {len(hashes)} hashes, {len(headers)} headers")
    for h, hsh, hdr in zip(range(start, end + 1), hashes, headers):
        blocks.append({"height": h, "hash": hsh, "time": header_time(hdr)})

    intervals = [blocks[i + 1]["time"] - blocks[i]["time"]
                 for i in range(len(blocks) - 1)]

    mtps = []
    for i in range(10, len(blocks)):
        window = sorted(b["time"] for b in blocks[i - 10:i + 1])
        mtps.append({"height": blocks[i]["height"], "time": blocks[i]["time"],
                     "mtp": window[5], "lag": blocks[i]["time"] - window[5]})

    windows = [{"height": blocks[i + 9]["height"], "span": sum(intervals[i:i + 10])}
               for i in range(0, len(intervals) - 9, 10)]

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
