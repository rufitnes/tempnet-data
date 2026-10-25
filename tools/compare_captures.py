#!/usr/bin/env python3
"""Compare two capture files block by block.

Takes two `events.csv` files produced by tempnet_listener.py on different nodes and
computes, for every block seen by both, the difference between the local receive
times. Because the miner's header timestamp is identical for both nodes, subtracting
within the same block cancels it exactly: what remains is the difference in delivery
time between the two network paths (plus the difference of the two clock errors).

Two comparisons are reported, because the two events happen at different moments:

  connect          UpdateTip line read  (after validation, on both nodes)
  reconstruction   block_compact line read (cmpctblock reassembled, earlier)

Comparing like with like matters: mixing them would fold validation time into what
looks like propagation.

Usage:
    python3 tools/compare_captures.py --a events-pichan.csv --b events-susopc.csv \
        --a-name pichan-starlink --b-name susopc-mobile
"""
import argparse
import csv
import json
from datetime import datetime


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load(path):
    tips, comp = {}, {}
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            h = r.get("hash")
            if not h:
                continue
            if r["event"] == "UpdateTip" and h not in tips:
                tips[h] = {"ts": r["ts_local"], "height": int(r["height"]),
                           "header_time": r.get("header_time")}
            elif r["event"] == "block_compact" and h not in comp:
                comp[h] = r["ts_local"]
    return tips, comp


def pct(vals, p):
    s = sorted(vals)
    k = int(round((p / 100.0) * (len(s) - 1)))
    return s[max(0, min(len(s) - 1, k))]


def summary(vals, label, a, b):
    if not vals:
        return {"event": label, "n": 0}
    return {
        "event": label,
        "n": len(vals),
        "mean_ms": round(sum(vals) / len(vals), 1),
        "median_ms": round(pct(vals, 50), 1),
        "p10_ms": round(pct(vals, 10), 1),
        "p90_ms": round(pct(vals, 90), 1),
        "p99_ms": round(pct(vals, 99), 1),
        "min_ms": round(min(vals), 1),
        "max_ms": round(max(vals), 1),
        "positive": f"{a} later than {b}" if sum(1 for v in vals if v > 0) > len(vals) / 2
                    else f"{a} earlier than {b}",
    }


def precision(ts):
    return 3 if "." in (ts or "") else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", required=True)
    ap.add_argument("--b", required=True)
    ap.add_argument("--a-name", default="A")
    ap.add_argument("--b-name", default="B")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    ta, ca = load(args.a)
    tb, cb = load(args.b)

    shared = sorted(set(ta) & set(tb), key=lambda h: ta[h]["height"])
    connect = [(ta[h]["ts"], tb[h]["ts"]) for h in shared]
    recon = [(ca[h], cb[h]) for h in shared if h in ca and h in cb]

    out = {
        "a": args.a_name, "b": args.b_name,
        "shared_blocks": len(shared),
        "height_range": [ta[shared[0]]["height"], ta[shared[-1]]["height"]] if shared else None,
        "resolution": {"a_tips": sorted({precision(t[0]) for t in connect}),
                       "b_tips": sorted({precision(t[1]) for t in connect})},
        "connect": summary([(parse_ts(x) - parse_ts(y)).total_seconds() * 1000
                            for x, y in connect], "connect (UpdateTip)", args.a_name, args.b_name),
        "reconstruction": summary([(parse_ts(x) - parse_ts(y)).total_seconds() * 1000
                                   for x, y in recon], "reconstruction (cmpctblock)",
                                  args.a_name, args.b_name),
    }

    # announce/reconstruct -> connect, per node
    gaps = {}
    for name, tips, comp in ((args.a_name, ta, ca), (args.b_name, tb, cb)):
        vals = [(parse_ts(tips[h]["ts"]) - parse_ts(comp[h])).total_seconds() * 1000
                for h in shared if h in tips and h in comp]
        gaps[name] = summary(vals, f"reconstruct->connect ({name})", "connect", "reconstruct")
    out["reconstruct_to_connect"] = gaps

    print(json.dumps(out, indent=1))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=1)


if __name__ == "__main__":
    main()
