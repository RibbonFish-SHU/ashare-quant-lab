"""Build the offline 2023 conditional-membership and quote-gap diagnostic candidate."""

import argparse
from pathlib import Path

from ashare_lab.state_evidence.membership_gaps import build, summary_text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "project-root",
        "input-root",
        "reconciliation",
        "events",
        "original-plan",
        "bao-candidate",
        "community-candidate",
        "output",
    ):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--state-candidate", type=Path)
    result = build(**vars(parser.parse_args()))
    print(summary_text(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
