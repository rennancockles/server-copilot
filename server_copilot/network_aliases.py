"""Names and notes the owner gives to the devices he knows, kept in a small JSON file by MAC.

The file is written by hand and mounted read-only: nothing here changes it. It maps a MAC address
(in any usual notation) to an alias, a note, or both::

    {
      "f4:7b:5e:00:00:09": {"alias": "TV da sala", "note": "Samsung 65 OLED, comprada em 2024"},
      "00-1B-A9-00-00-50": "Impressora do escritório"
    }

A device that is not in the file keeps the name it reports itself. A phone with a randomized MAC
changes address, so it will not keep its alias.
"""

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from server_copilot.network_snapshot import (
    MAX_DETAIL_LENGTH,
    Snapshot,
    clean,
    normalize_mac,
)

MAX_FILE_BYTES = 256 * 1024
MAX_ENTRIES = 1000


class DeviceAlias(BaseModel):
    alias: str | None = None
    note: str | None = None


class AliasFile(BaseModel):
    entries: dict[str, DeviceAlias] = {}
    ignored: int = 0
    error: str | None = None

    @property
    def warning(self) -> str | None:
        if self.error:
            return self.error
        if self.ignored:
            return f"{self.ignored} entrada(s) do arquivo de apelidos foram ignoradas."
        return None


def _entry(value: Any) -> DeviceAlias | None:
    if isinstance(value, str):
        value = {"alias": value}
    if not isinstance(value, dict):
        return None
    alias, note = value.get("alias"), value.get("note")
    entry = DeviceAlias(
        alias=clean(alias) if isinstance(alias, str) and clean(alias) else None,
        note=clean(note, MAX_DETAIL_LENGTH) if isinstance(note, str) and clean(note) else None,
    )
    return entry if entry.alias or entry.note else None


def load_aliases(path: str) -> AliasFile:
    """Read the alias file; a missing one means no aliases, an unreadable one is reported."""
    if not path:
        return AliasFile()
    file = Path(path)
    try:
        if not file.is_file():
            return AliasFile()
        if file.stat().st_size > MAX_FILE_BYTES:
            return AliasFile(error="O arquivo de apelidos é grande demais.")
        document = json.loads(file.read_text("utf-8"))
    except (OSError, ValueError):
        return AliasFile(error="Não foi possível ler o arquivo de apelidos.")
    if not isinstance(document, dict):
        return AliasFile(error="O arquivo de apelidos deve ser um objeto JSON: MAC para apelido.")
    entries: dict[str, DeviceAlias] = {}
    ignored = 0
    for key, value in list(document.items())[:MAX_ENTRIES]:
        mac = normalize_mac(key) if isinstance(key, str) else None
        entry = _entry(value)
        if mac is None or entry is None:
            ignored += 1
            continue
        entries[mac] = entry
    return AliasFile(entries=entries, ignored=ignored + max(0, len(document) - MAX_ENTRIES))


def apply_aliases(snapshot: Snapshot, entries: dict[str, DeviceAlias]) -> Snapshot:
    """A copy of the snapshot whose devices carry the alias and note given to their MAC."""
    if not entries:
        return snapshot

    def given(mac: str | None) -> dict[str, str | None]:
        found = entries.get(mac) if mac else None
        return {"alias": found.alias, "note": found.note} if found else {}

    return snapshot.model_copy(
        update={
            "devices": [d.model_copy(update=given(d.mac)) for d in snapshot.devices],
            "offline_devices": [
                d.model_copy(update=given(d.mac)) for d in snapshot.offline_devices
            ],
        }
    )
