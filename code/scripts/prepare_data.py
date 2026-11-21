"""Prepare reproducible data without evaluating held-out instances."""

from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from funpack.data import generate_suite


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=Path("data"))
    parser.add_argument("--notebook", type=Path,
                        help="Pinned FunSearch notebook; adjacent source manifest.json required")
    args = parser.parse_args()
    manifest = generate_suite(args.out_dir, args.notebook)
    print(json.dumps({"manifest": str(args.out_dir / "manifest.json"),
                      "splits": {name: info["instances"] for name, info in manifest["splits"].items()},
                      "probe_count": manifest["probes"]["count"],
                      "heldout_evaluated": False}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
