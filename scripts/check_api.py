"""Configuration-only check: never sends a request and never prints the key."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from funpack.providers import preflight
from funpack.storage import read_json

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--config", default="configs/api.local.json")
args = parser.parse_args()
result = preflight(read_json(args.config), allow_paid=True)
print(json.dumps(result, ensure_ascii=False, indent=2))
sys.exit(0 if result["ok"] else 1)
