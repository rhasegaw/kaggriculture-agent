"""Lightweight, dependency-free release smoke test for main.py."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "main.py"


def load_agent_module():
    # Keep the smoke test runnable without kaggle-environments. The dependency
    # is optional; this base smoke intentionally disables engine synchronization.
    os.environ.setdefault("KG_ENGINE_SYNC", "0")
    os.environ.setdefault("KG_DEBUG", "0")
    spec = importlib.util.spec_from_file_location("kaggriculture_release_main", SOURCE)
    if spec is None or spec.loader is None:
        raise AssertionError("could not create an import specification")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    module = load_agent_module()

    assert module.market_price("WHEAT", 10_000) == 25
    assert module.market_price("WHEAT", 9_000) > module.market_price("WHEAT", 10_000)
    assert module.market_price("WHEAT", 12_000) < module.market_price("WHEAT", 10_000)
    assert module.units_above_price("WHEAT", 10_000, 20, 3) == 3

    fallback = module.agent({})
    assert set(fallback) == {"farmer", "hands", "market"}
    assert fallback["farmer"] == ["PASS"]
    assert fallback["hands"] == []
    assert fallback["market"] == []

    source = SOURCE.read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    manifest = json.loads((ROOT / "source_manifest.json").read_text(encoding="utf-8"))
    assert digest == manifest["sha256"]
    print("smoke: import, market arithmetic, fallback schema, and hash checks passed")


if __name__ == "__main__":
    main()
