# Kaggriculture closed-loop planner

This repository documents a closed-loop Kaggriculture controller: observe state, plan jobs, assign labor, and place market orders. The associated team finished rank 76/10,148 with a displayed score of 4,856.5 and a Silver medal on the official final leaderboard; the exact final-submission source mapping remains unresolved.

The companion portfolio page is [Kaggriculture](https://rhasegaw.github.io/work/kaggriculture.html).

## What is here

- [`main.py`](https://github.com/rhasegaw/kaggriculture-agent/blob/main/main.py) — the retained single-file controller. Its public entry point is `agent(obs, config=None)`.
- [`results/results.md`](https://github.com/rhasegaw/kaggriculture-agent/blob/main/results/results.md) — official final leaderboard evidence and the source-mapping limitation.
- [`results/official_final.json`](https://github.com/rhasegaw/kaggriculture-agent/blob/main/results/official_final.json) — the structured official result record captured on 2026-11-09 (America/Chicago), with the corresponding UTC timestamp.
- [`paper/paper.md`](https://github.com/rhasegaw/kaggriculture-agent/blob/main/paper/paper.md) — a dated technical report, explicitly marked as not peer-reviewed.
- [`paper/paper.pdf`](https://github.com/rhasegaw/kaggriculture-agent/blob/main/paper/paper.pdf) — rendered report.
- [`tests/smoke_test.py`](https://github.com/rhasegaw/kaggriculture-agent/blob/main/tests/smoke_test.py) — a standard-library-only smoke check for import, market arithmetic, fallback action shape, and source-hash integrity. A separate first-decision check verifies a non-fallback planner action; the optional engine-loader smoke checks short-run integration.
- [`source_manifest.json`](https://github.com/rhasegaw/kaggriculture-agent/blob/main/source_manifest.json) — SHA-256 provenance for the retained source snapshot.
- [`THIRD_PARTY_NOTICES.md`](https://github.com/rhasegaw/kaggriculture-agent/blob/main/THIRD_PARTY_NOTICES.md) — attribution and license boundaries for the optional Kaggle runtime dependency.

`main.py` is the retained user-owned source snapshot, © 2026 Ryo Hasegawa. All rights reserved; this archive makes no new license grant. See `THIRD_PARTY_NOTICES.md` for the separate Kaggle dependency notice.

## Quick verification

From the repository root:

```text
py -3 -m py_compile main.py
py -3 tests/smoke_test.py

python3 -m py_compile main.py
python3 tests/smoke_test.py
```

The smoke test does not need network access or Kaggle credentials. Engine synchronization is on by default and degrades to the built-in constants, with one stderr line, when `kaggle-environments` is absent; the smoke test sets `KG_ENGINE_SYNC=0`.

## Optional engine evaluation

The competition runtime was based on `kaggle-environments` 1.32.7. On a Python version supported by that package, install the optional dependency:

```text
py -3 -m pip install -r requirements.txt
python3 -m pip install -r requirements.txt
```

Then load `main.py` through the official Kaggle Environments API or the competition submission loader. Release validation used Python 3.11.15 and a four-step self-play loader smoke: both agents finished `DONE` with rewards `150.0` and `150.0`. This checks short-run loading, not a competition result. A separate first-decision check at step 0 returned `BUILD_PASTURE` plus hiring and purchase orders, with engine synchronization enabled and no stderr; it did not take the fallback path. A full 720-turn run is a separate evaluation exercise. The package intentionally excludes private replay tapes, oracle schedules, development logs, and research data; the corresponding experimental knobs are disabled by default in `main.py`.

## Evidence boundary

The official final leaderboard record is rank 76 of 10,148, score 4,856.5, Silver, submission 56719108. That result is verified through Kaggle’s leaderboard services. The exact source hash for submission 56719108 was not retained locally, so this package documents the retained planner lineage rather than claiming byte identity with the final hosted submission. See the report and results file for the distinction.

## Scope

The selected development line used a replayed opening keyed to the shop draw followed by the planner; the private opening library is not shipped. The released default runs the planner from step 0. Source comments reference public leaderboard handles and public episode replays; the referenced libraries are not included. The opening values in the source docstring describe a historical development variant; active defaults are `OPEN_MELONS=11`, `OPEN_SHEEP=3`, and `OPEN_COWS=1` (`main.py:299-303`). Do not treat local panel scores as leaderboard results. Third-party runtime licensing remains separate from the retained source.
