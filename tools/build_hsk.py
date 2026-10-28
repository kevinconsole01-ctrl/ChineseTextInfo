#!/usr/bin/env python3
"""Genera hsk.json (HSK 2.0) e hsk3.json (HSK 3.0) per il lettore.

Fonte: https://github.com/drkameleon/complete-hsk-vocabulary
Autore: Yanis Zafirópulos (Dr.Kameleon) — licenza MIT.
Le liste "exclusive" contengono solo le parole introdotte in quel livello;
qui vengono unite in un'unica mappa { "parola": livello }.

HSK 2.0 → livelli 1-6  → hsk.json
HSK 3.0 → livelli 1-9  → hsk3.json (la lista ufficiale 7-9 è unica, quindi
                            tutte le parole di quel blocco hanno livello 7)

Uso: python3 tools/build_hsk.py
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

BASE = "https://raw.githubusercontent.com/drkameleon/complete-hsk-vocabulary/main/wordlists/exclusive"
ROOT = Path(__file__).resolve().parent.parent

SYSTEMS = {
    "hsk.json": {"path": "old", "levels": range(1, 7)},
    "hsk3.json": {"path": "new", "levels": range(1, 8)},
}


def fetch_json(url: str):
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.load(response)


def build(name: str, folder: str, levels) -> dict[str, int]:
    words: dict[str, int] = {}
    for level in levels:
        url = f"{BASE}/{folder}/{level}.json"
        entries = fetch_json(url)
        for entry in entries:
            word = entry.get("simplified", "").strip()
            if not word:
                continue
            # Le liste sono esclusive: una parola non dovrebbe comparire due
            # volte. Se succede, vince il livello più basso (più conservativo).
            words[word] = min(level, words.get(word, level))
        print(f"  {folder}-{level}: {len(entries):>5} voci", file=sys.stderr)
    print(f"{name}: {len(words)} parole uniche", file=sys.stderr)
    return dict(sorted(words.items(), key=lambda item: (item[1], item[0])))


def main() -> None:
    for name, config in SYSTEMS.items():
        print(f"Costruisco {name}…", file=sys.stderr)
        words = build(name, config["path"], config["levels"])
        target = ROOT / name
        # Una voce per riga: file compatto ma con diff leggibili.
        lines = ",\n".join(
            f"  {json.dumps(word, ensure_ascii=False)}: {level}"
            for word, level in words.items()
        )
        target.write_text("{\n" + lines + "\n}\n", encoding="utf-8")
        print(f"Scritto {target} ({target.stat().st_size} byte)", file=sys.stderr)


if __name__ == "__main__":
    main()
