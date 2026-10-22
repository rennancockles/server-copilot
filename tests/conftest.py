import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def hermetic_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [n for n in os.environ if n.startswith("SERVER_COPILOT_")]:
        monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
