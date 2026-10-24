# tempnet-data

Live telemetry from the **BCH tempnet** — the faster-blocks test network used for the 2027
chipnet upgrade pre-upgrade testing (see the
[tempnet thread](https://bitcoincashresearch.org/t/tempnet-2027-chipnet-upgrade-pre-upgrade-testing/2173)).

This repository is a **machine-generated dataset**, refreshed automatically by a node running on a
Raspberry Pi. It complements the aggregate measurements already posted in that thread with
node-local timing data that never becomes canonical.

- **Dashboard**: <https://rufitnes.github.io/tempnet-data/>
- **Machine-readable snapshot**: [`data/stats.json`](data/stats.json)
- **Raw capture**: [`data/events.csv`](data/events.csv)
- **Daily summaries**: [`history/`](history/)

## What is measured

| Metric | Definition |
|---|---|
| `interval` | Difference between the **header timestamps** of two consecutive blocks (`nHeight > nHeight-1`). |
| `window_10` | Sum of 10 consecutive intervals (non-overlapping chunks, in height order). |
| `receive_delay` | **Local wall-clock receive time** − block header timestamp. Positive = the local node learned about the block after the time written in the header. This is *not* propagation time from the miner; it includes the miner's timestamp imprecision, relay latency and our own clock drift. |
| `mtp_lag` | Tip header time − median time past (`mediantime`) of the tip. |
| `via` | How the block reached us, parsed from the node log (`direct` announcement vs `compact` block relay). |
| `reorgs` | `REORGANIZE` / `DisconnectTip` / `BlockDisconnected` log events. |

All percentiles are **nearest-rank**.

### Why these numbers look odd

The tempnet targets 60-second blocks, but its hashrate is tiny and bursty. Expect a **median
interval well below 60 s** and a **p99 in the hundreds of seconds**, with occasional stalls of
hours. That skew is the phenomenon being studied, not a measurement bug.

## Method

1. `bitcoind` (BCHN `v29.2.1-tempnet-*`, fork of chipnet) runs with `-tempnet -debug=all` on a
   Raspberry Pi 4 (4 GB, ext4 on an old spinning disk) and keeps a persistent `debug.log`.
2. A log listener tails `debug.log` and appends every `UpdateTip` (plus reorg events) to
   `events.csv` with the **local receive time** at the moment the line is read.
3. The exporter (`tempnet_export.py`) derives the metrics from that capture, cross-checks the tip
   through RPC, and writes `data/stats.json`, this dashboard, the raw CSV and a daily `.jsonl`
   summary.
4. A commit is pushed when **10 new blocks** have arrived, **15 minutes** have elapsed, or a
   **reorg** is seen — whichever comes first. GitHub Pages then serves the dashboard.

Capture begins at the first block observed after the listener fix (see `captured.from_height` in
`stats.json`); earlier blocks are not in the dataset.

## Caveats

- The host clock is **not** disciplined by a local NTP daemon configured for sub-second accuracy.
  Treat `receive_delay` as indicative, and its sub-second digits as noise.
- `receive_delay` is measured on **one** node with one network path, so it is a sample — not a
  network-wide propagation statistic.
- Blocks captured during initial block download or after a restart may be missing from the CSV.
- This is a **test network** with no economic value. Data may be lost if the host or disk dies.

## Consuming `stats.json`

Top-level keys: `generated_at`, `chain`, `network` (target time, difficulty, hashrate, peers,
mempool), `sync` (height, headers, progress, IBD flag), `tip` (height, hash, header time,
mediantime, `mtp_lag_s`), `captured` (first/last height and time in the CSV), `reorgs`, then
`intervals`, `intervals_last_100`, `intervals_last_24h`, `windows_10`, `receive_delay` (each with
`n`, `mean`, `median`, `p90`, `p99`, `min`, `max`), `via`, and `series` (last 240 blocks:
`h`, `t`, `dt`, `recv`).

## License

Code: MIT. Data (`data/`, `history/`): dedicated to the public domain (CC0).
