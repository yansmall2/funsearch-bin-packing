"""Download and checksum the pinned official notebook; never execute it."""
from pathlib import Path
import hashlib
import json
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
URL = "https://raw.githubusercontent.com/google-deepmind/funsearch/main/bin_packing/bin_packing.ipynb"
EXPECTED = "eec45846a8661722addbb856179327cf2ede0bc502e32be4ad58a6ac54e0aca6"


def main():
    target = ROOT / "references/funsearch/bin_packing.ipynb"
    manifest = json.loads((target.parent / "manifest.json").read_text(encoding="utf-8"))
    entries = [x for x in manifest["sources"] if x["file"] == target.name]
    if len(entries) != 1 or entries[0]["sha256"] != EXPECTED or entries[0]["url"] != URL:
        raise ValueError("source manifest differs from the pinned notebook")
    if target.exists():
        if hashlib.sha256(target.read_bytes()).hexdigest() != EXPECTED:
            raise ValueError("existing notebook changed; refusing to overwrite")
        print("Pinned notebook already present and verified. No download or evaluation.")
        return
    with urlopen(URL, timeout=30) as response:
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000 or hashlib.sha256(raw).hexdigest() != EXPECTED:
        raise ValueError("download checksum mismatch; upstream may have changed; nothing saved")
    with target.open("xb") as stream:
        stream.write(raw)
    print("Pinned notebook downloaded and verified; no code executed or benchmark evaluated.")


if __name__ == "__main__":
    main()
