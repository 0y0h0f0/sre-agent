from __future__ import annotations

import argparse
from pathlib import Path

from .datasets.harness import run_suite


def main() -> None:
    parser = argparse.ArgumentParser(description="Run local SRE incident eval suites")
    parser.add_argument("--suite", choices=["smoke", "full"], default="smoke")
    parser.add_argument("--output", default=None, help="Write the JSON report here")
    args = parser.parse_args()

    # The CLI writes both JSON and Markdown through the harness. Keep the printed
    # Markdown as a human-readable summary for CI logs and local prompt tuning.
    output = Path(args.output) if args.output else Path("reports") / f"eval-{args.suite}.json"
    report = run_suite(args.suite, output=output)
    print(report.to_markdown())
    print(f"\nReport written to {output.as_posix()}")


if __name__ == "__main__":
    main()
