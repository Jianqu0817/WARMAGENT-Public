"""Bounded correction run for P03/P05/P06/P16; each case gets its own worker."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FIXTURE = PROJECT_ROOT / "var/benchmarks/perf-20261025-224806-smoke-n1-M/fixtures/M"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", choices=("S", "M", "L"), default="M")
    parser.add_argument("--fixture-source", type=Path)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--tag", default="corrected-targeted")
    args = parser.parse_args()
    fixture_source = args.fixture_source
    if fixture_source is None and args.size == 'M' and DEFAULT_FIXTURE.exists():
        fixture_source = DEFAULT_FIXTURE
    if fixture_source is not None and not (fixture_source / "fixture.json").is_file():
        parser.error(f"fixture.json not found under {fixture_source}")
    failed = False
    for case, repeats in (("P03", 1), ("P05", 3), ("P06", 1), ("P16", 1)):
        command = [sys.executable, "-m", "benchmarks.runtime_perf.runner",
                   "--cases", case, "--size", args.size, "--repeats", str(repeats),
                   "--case-timeout", str(args.timeout),
                   "--tag", f"{args.tag}-{case}", "--quick"]
        if fixture_source is not None:
            command.extend(["--fixture-source", str(fixture_source)])
        result = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
        failed |= result.returncode != 0
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
