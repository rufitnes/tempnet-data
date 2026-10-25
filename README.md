# tempnet-data

Live telemetry from the **BCH tempnet** — the faster-blocks test network used for the 2027
chipnet upgrade pre-upgrade testing (see the
[tempnet thread](https://bitcoincashresearch.org/t/tempnet-2027-chipnet-upgrade-pre-upgrade-testing/2173)).

A single Raspberry Pi runs a tempnet node plus two small scripts that capture what the chain itself
never records, and commits the result here every few minutes.

- **Dashboard**: <https://rufitnes.github.io/tempnet-data/>
- **Machine-readable snapshot**: [`data/stats.json`](data/stats.json)
- **Raw capture**: [`data/events.csv`](data/events.csv)
- **Compact-relay index**: [`data/compact.csv`](data/compact.csv)
- **Daily summaries**: [`history/`](history/)
- **Full-range aggregate**: [`data/full-range-measure.json`](data/full-range-measure.json)

## Why this exists

Aggregate block-timing numbers are easy to compute from the canonical chain. The open questions in
the tempnet thread are about everything the chain *throws away*:

1. **Orphaned / disconnected blocks** — they vanish from the chain, so only node logs remember them.
2. **Local receive times** — when a node actually learned about a block, versus the timestamp the
   miner wrote into the header.
3. **Compact-block relay (BIP152)** — whether the fast relay path was exercised, and how many
   transactions still had to be requested.
4. **Propagation signals** — announcements observed by the node.

This repository records those signals continuously, from one node, and publishes them in the open so
anyone can check, extend or challenge the measurements. It is deliberately small: one node, one
clock, plain text files, no database.

## Repository layout

```
tempnet_listener.py   tails debug.log -> events.csv (local receive times)
tempnet_export.py     derives metrics -> stats.json, index.html, history/, compact.csv
index.tpl.html        dashboard template (data is inlined at generation time)
tools/measure.py      offline aggregate measurement over any block range
systemd/              unit files used on the host (paths assume /mnt/tempnet)
data/                 published dataset (generated)
history/              one JSON line per snapshot, per day
data/full-range-measure.json  aggregate over the whole tempnet history
```

## What is measured

| Metric | Definition |
|---|---|
| `interval` | Difference between the **header timestamps** of two consecutive blocks. |
| `window_10` | Sum of 10 consecutive intervals (non-overlapping chunks, in height order, aligned to the first interval of the range — a stall near a chunk boundary can be split across two windows). |
| `timestamp_deviation` | **Local wall-clock receive time** − block header timestamp. It is *not* a propagation measure: a miner may timestamp anywhere between MTP and +2 h, and need not refresh the timestamp on every hash, so an honest miner can legitimately stamp a block minutes "in the past" relative to real time. What we publish is therefore *timestamp deviation from our clock*, an upper bound on the actual receive delay. (Renamed from `receive_delay` on 2026-10-24 — the old name implied a delay we cannot isolate.) |
| `mtp_lag` | Tip header time − median time past (`mediantime`) of the tip. |
| `via` | `compact` when the node logged a successful compact-block reconstruction for that block hash, `direct` otherwise. Reflects **our** receive path only. |
| `compact.missing_tx` | `... and N txn requested` after reconstructing from a `cmpctblock` — transactions missing from the compact relay. |
| `reorgs` | `REORGANIZE` / `DisconnectTip` / `BlockDisconnected` log events. |

All percentiles are **nearest-rank**.

### Why the median is below the target (it is expected)

With constant hashrate and difficulty, block intervals are **exponentially distributed**: if the
mean is the 60 s target, the **median is `60 × ln 2 ≈ 41.6 s`**, p90 ≈ 138 s, p99 ≈ 276 s, and
about **0.67 %** of blocks exceed 5 minutes. A median of ~43-45 s is not a sign of faster blocks;
it is the shape of the distribution.

The tempnet also **drops difficulty to 1 whenever no block is found for 2× the target time**
(2 minutes). That truncates the tail, so a long stall can only happen when hashrate is effectively
zero or the miner is not picking up the difficulty drop — and then a difficulty-1 block still takes
time. Example: height 328,223 (16 Oct) was mined at difficulty 1 after a **20,863 s** gap, which
implies roughly **200 kH/s** of mining at that moment.

So: a low median is expected; a *shorter* tail than the exponential prediction is the difficulty
drop working; long stalls are an observation about miners, not about the measurement.

## Method

1. `bitcoind` (BCHN `v29.2.1-tempnet-*`, a fork of chipnet) runs with `-tempnet -debug=all` and
   keeps a persistent `debug.log`.
2. `tempnet_listener.py` tails that log and appends every `UpdateTip`, reorg, compact-block
   reconstruction and full-block arrival to `events.csv`, stamping each row with the **local time
   at which the line was read**.
3. `tempnet_export.py` derives the metrics from that capture, cross-checks the tip over RPC, and
   writes `data/stats.json`, the dashboard, the raw capture, the compact-relay index and a daily
   `.jsonl` summary.
4. A snapshot is committed when **10 new blocks** have arrived, **5 minutes** have elapsed, or a
   **reorg** is observed — whichever comes first — and pushed with a per-repository deploy key.
   GitHub Pages serves the dashboard from `index.html`.

Capture begins at the first block observed after the listener fix (`captured.from_height` in
`stats.json`); earlier blocks are not in `events.csv`, although `compact.csv` is backfilled from the
node log and therefore reaches further back.

## Reproducing / building on it

Requirements: a synced tempnet node built with `-debug=all`, Python 3.11+ (stdlib only), and a git
remote with push access if you want to publish.

```bash
# 1. capture (writes $TEMPNET_EVENTS, default /mnt/tempnet/events.csv)
TEMPNET_LOG=/mnt/tempnet/data/tempnet/debug.log \
TEMPNET_EVENTS=/mnt/tempnet/events.csv \
python3 tempnet_listener.py

# 2. publish (first run initialises the dataset; later runs commit on the hybrid rule)
TEMPNET_CLI=/path/to/bitcoin-cli TEMPNET_DATADIR=/mnt/tempnet/data \
python3 tempnet_export.py

# 3. optional: aggregate numbers straight from the chain
TEMPNET_CLI=/path/to/bitcoin-cli python3 tools/measure.py --from 327989
```

Both scripts read their paths from the environment (`TEMPNET_LOG`, `TEMPNET_EVENTS`, `TEMPNET_CLI`,
`TEMPNET_DATADIR`), so nothing is hard-coded to one machine. The `systemd/` units show the
deployment used here: node, listener, exporter and a five-minute timer.

Ideas that would improve the dataset, if you want to build on it: multiple nodes with synchronised
clocks (turns this deviation into a real propagation measurement), `-blocknotify` hooks for exact
block-arrival times, and per-peer attribution of announcements.

## Consuming `stats.json`

Top-level keys: `generated_at`, `chain`, `network` (target time, difficulty, hashrate, peers,
mempool), `sync` (height, headers, progress, IBD flag), `tip` (height, hash, header time,
mediantime, `mtp_lag_s`), `captured` (first/last height and time in the CSV), `reorgs`, then
`expected` (exponential model for the target time), `intervals`, `intervals_last_100`, `intervals_last_24h`, `windows_10`, `timestamp_deviation` (each with
`n`, `mean`, `median`, `p90`, `p99`, `min`, `max`), `via`, `compact` (blocks reconstructed from
`cmpctblock` vs captured, and `missing_tx` stats), and `series` (last 240 blocks: `h`, `t`, `dt`,
`tsdev`).

`data/compact.csv` columns: `log_time,hash,prefilled,from_mempool,extra_pool,requested`.
`history/YYYY-MM-DD.jsonl` holds one compact summary line per snapshot.

## Caveats

- The host clock is **not** disciplined by a local NTP daemon for sub-second accuracy. Treat
  `timestamp_deviation` as indicative and its sub-second digits as noise.
- Everything is measured on **one** node with **one** network path: a sample, not a network-wide
  statistic. No claim here is about propagation across the network.
- Blocks captured during initial block download or after a restart may be missing from the CSV.
- This is a **test network** with no economic value. Data may be lost if the host or disk dies.

## License

Code (`*.py`, `index.tpl.html`, `systemd/`): MIT — see [LICENSE](LICENSE).
Data (`data/`, `history/`): dedicated to the public domain (CC0).
