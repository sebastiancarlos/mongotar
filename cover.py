#!/usr/bin/env -S uv run python
"""Run the test suite under coverage"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# The CLI tests spawn the installed `mongotar` console script as subprocesses,
# so we set this env-var to get CLI subprocess coverage.
os.environ["COVERAGE_PROCESS_START"] = str(ROOT / "pyproject.toml")


def cov(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([sys.executable, "-m", "coverage", *args], cwd=ROOT)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--no-report",
        action="store_true",
        help="Skip the coverage report.",
    )
    args = parser.parse_args()

    cov("erase")
    test_result = cov("run", "test.py")
    cov("combine", "--quiet")
    if args.no_report:
        return test_result.returncode
    report_result = cov("report")
    return test_result.returncode or report_result.returncode


if __name__ == "__main__":
    sys.exit(main())
