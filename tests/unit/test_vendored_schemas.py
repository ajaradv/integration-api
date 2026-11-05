import hashlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# Pinned SHA-256 of the vendored copies of the workspace contract files.
PINNED = {
    "app/schemas/tive-incoming-schema.json": (
        "45bc72c281dd863324d455fd98252c2c34db419854d07562f5b47caae6a0694f"
    ),
    "app/schemas/px-sensor-schema.json": (
        "58a1401a4cf88bcee4acbbc35ac7e72701219d99ff4cb2d5108f0b6d2f6f735e"
    ),
    "app/schemas/px-location-schema.json": (
        "d313dd905ef1fb5af240fc8de4bca5dce5955984f6005de5e19705bd12e7eb57"
    ),
    "tests/fixtures/sample-tive-payloads.json": (
        "25d4eb8ccee49b6560456cec851a47adb8307bca07524c382b7849da23c5174f"
    ),
}


@pytest.mark.parametrize("name", list(PINNED))
def test_vendored_file_matches_pinned_hash(name: str) -> None:
    digest = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
    assert digest == PINNED[name], (
        f"{name} changed; update the pin only after updating the workspace source"
    )
