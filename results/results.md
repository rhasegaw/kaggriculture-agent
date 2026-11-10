# Results and evidence boundary

Status: archival research record, prepared 2026-11-09. The hosted result below is official; local research results are explicitly labeled as proxies.

## Official final result

The Kaggle official final leaderboard record was retrieved from Kaggle’s internal leaderboard services:

- competition: [Kaggriculture](https://www.kaggle.com/competitions/kaggriculture/overview)
- final leaderboard: [Kaggle leaderboard](https://www.kaggle.com/competitions/kaggriculture/leaderboard)
- team: Ryo Hasegawa, one member
- final submission: `56719108`
- rank: `76 / 10,148`
- displayed score: `4,856.5`
- medal: `SILVER`
- submissions recorded for the team: `2`

The structured capture is in [`official_final.json`](https://github.com/rhasegaw/kaggriculture-agent/blob/main/results/official_final.json). It was obtained from Kaggle’s `GetLeaderboard` and `GetRanksForUsers` endpoints on 2026-11-09 local time / 2026-11-10 02:51:29 UTC. The response field was named `privateLeaderboard`, but Kaggriculture’s rules describe a single official final leaderboard for this simulation competition.

## Retained implementation

[`main.py`](https://github.com/rhasegaw/kaggriculture-agent/blob/main/main.py) is a single-file closed-loop controller. The retained source:

1. Mirrors the game’s crop, animal, shop, land, market, and 720-turn constants (`main.py:38-87`).
2. Computes inventory-dependent prices and one-unit-at-a-time sale limits (`main.py:90-128`).
3. Parses the observed farm and opponent state, maintains per-player state, and refreshes a daily plan (`main.py:325-469`, `main.py:970-1004`).
4. Builds jobs from current tiles, plants, animals, shed stock, and available seeds (`main.py:1066-1406`).
5. Assigns the farmer and hired hands by urgency, production deadlines, walking distance, and market/feeding constraints (`main.py:1440-1563`, `main.py:1826-2091`).
6. Returns the required `farmer`, `hands`, and `market` action lists through `agent(obs, config=None)` (`main.py:4075-4128`, `main.py:4207-4220`).

Replay/tape and route branches remain in the retained file for provenance, but their environment switches default to off. The selected development line used a replayed opening followed by the planner; the private opening library is not included. The released default runs the planner from step 0. Whether submission `56719108` embedded that opening tape is not determinable from retained artifacts.

## Local research proxies

Unverified local proxy figures are omitted from this release because the inspected evidence does not establish their exact build, knob settings, or private assets. The official final score remains the only hosted completion metric.

Two negative findings were retained because they explain the design boundary:

- A market-order tape was compared on `n=4` leader games: it produced approximately `-107k`, `-84k`, `-66k`, and `-89k`, versus baseline losses of `-4.5k`, `-3.6k`, `-1.8k`, and `-17k`. Recorded orders depended on the reference farm’s state, so the tape was left disabled.
- Full-game action-history replay reached `31/192` in the recorded local battery with heavy losses. The development line selected in the record was a replayed opening keyed to the shop draw followed by the closed-loop planner; pure full-game replay was rejected. The private opening library is not shipped, so these proxy results do not characterize the released default configuration.

These retained local findings are not represented as independently reproducible results in this package. The official final score remains the only hosted completion metric.

## Source mapping limitation

The retained public `main.py` has SHA-256 `5a6668f4cd7ded9b9865f1334881e97dda0a3d2545a1fe31fd1d40aedddcefd6`. The inspected final hosted submission is identified by submission ID `56719108`, but its exact source hash is not present in the retained local artifacts. This repository therefore makes a source-lineage claim, not a byte-identical-final-submission claim.
