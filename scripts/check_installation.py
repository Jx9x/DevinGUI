from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.installation import installation_problems, record_setup


def main() -> int:
    parser = argparse.ArgumentParser(description="Check local setup without installing or rebuilding anything.")
    parser.add_argument("--record", action="store_true", help="Record a successful manual setup after installing locks and building.")
    args = parser.parse_args()
    try:
        if args.record:
            record_setup(ROOT)
            print("[setup] Successful setup recorded.")
            return 0
        problems = installation_problems(ROOT)
    except (OSError, ValueError, RuntimeError) as error:
        problems = [str(error)]
    for problem in problems:
        print("[setup-check] " + problem, file=sys.stderr)
    if problems:
        print("[setup-check] Run setup-devingui.ps1 from the repository root, then launch again.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
