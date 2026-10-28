#!/usr/bin/env python3
"""
build_dict.py — Trasforma CC-CEDICT in dict.json per "Lettore di testi cinesi".

Uso tipico:
    python3 build_dict.py                  # scarica CC-CEDICT e crea dict.json
    python3 build_dict.py --input cedict_ts.u8
    python3 build_dict.py --url https://.../cedict_ts.u8.gz
    python3 build_dict.py --limit 20000    # dizionario ridotto (utile per test)

Formato di input (una voce per riga):
    Tradizionale Semplificato [pin1 yin1] /definizione 1/definizione 2/

Formato di output (JSON compatto):
    {
      "meta": { ... },
      "entries": [ ["sempl.", "trad.", "pīnyīn", ["def1", "def2"]], ... ],
      "index":   { "parola": [indice, ...], ... }
    }

"index" contiene sia le forme semplificate sia quelle tradizionali, così
l'app può cercare una parola indipendentemente dalla grafia del testo.

Licenza dei dati: CC-CEDICT è distribuito con licenza CC BY-SA 4.0.
Questo script non modifica la licenza dei dati generati.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

# ---------------------------------------------------------------------------
# Sorgenti predefinite
# ---------------------------------------------------------------------------

# MDBG è la fonte ufficiale di CC-CEDICT. Il file è distribuito in gzip.
DEFAULT_URLS = [
    "https://www.mdbg.net/chinese/export/cedict/cedict_1_0_ts_utf-8_mdbg.txt.gz",
]

USER_AGENT = "hanzi-reader-build-dict/1.0 (+https://cc-cedict.org/)"
DEFAULT_OUTPUT = "dict.json"


# ---------------------------------------------------------------------------
# Pinyin numerico -> pinyin con segni diacritici
# ---------------------------------------------------------------------------

# Tabelle dei segni di tono, indicizzate per tono 1..4.
TONE_MARKS = {
    "a": "āáǎà",
    "e": "ēéěè",
    "i": "īíǐì",
    "o": "ōóǒò",
    "u": "ūúǔù",
    "ü": "ǖǘǚǜ",
    "A": "ĀÁǍÀ",
    "E": "ĒÉĚÈ",
    "I": "ĪÍǏÌ",
    "O": "ŌÓǑÒ",
    "U": "ŪÚǓÙ",
    "Ü": "ǕǗǙǛ",
}

VOWELS = set("aeiouü")

# Una sillaba è una sequenza di lettere (anche con "u:" o "v") seguita da
# una cifra di tono 1-5. Il match può avvenire più volte nello stesso token,
# per esempio in "Xi1'an1" o in "nu:e4".
SYLLABLE_RE = re.compile(r"([A-Za-züÜ:]+)([1-5])")


def _normalize_umlaut(text: str) -> str:
    """Converte le grafie ASCII di ü usate da CC-CEDICT (u:, v) in ü."""
    return (
        text.replace("u:", "ü")
        .replace("U:", "Ü")
        .replace("v", "ü")
        .replace("V", "Ü")
    )


def _mark_syllable(syllable: str, tone: int) -> str:
    """
    Applica il segno del tono a una singola sillaba.

    Regole di posizione (standard Hanyu Pinyin):
      1. se c'è una "a", il segno va sulla "a";
      2. altrimenti se c'è una "e", va sulla "e";
      3. altrimenti se c'è "ou", va sulla "o";
      4. altrimenti va sull'ultima vocale (iu -> u, ui -> i, ...).
    Il tono 5 (neutro) non ha segno, ma conserva la dieresi della ü.
    """
    syllable = _normalize_umlaut(syllable)

    # Tono neutro o assente: nessun segno, ma u: -> ü è già stato applicato.
    if tone not in (1, 2, 3, 4):
        return syllable

    lower = syllable.lower()
    mark_index = -1

    if "a" in lower:
        mark_index = lower.index("a")
    elif "e" in lower:
        mark_index = lower.index("e")
    elif "ou" in lower:
        mark_index = lower.index("o")
    else:
        for i in range(len(lower) - 1, -1, -1):
            if lower[i] in VOWELS:
                mark_index = i
                break

    if mark_index < 0:
        # Sillabe senza vocale (m2, n2, ng2, hm5, ...): niente segno.
        return syllable

    original = syllable[mark_index]
    marks = TONE_MARKS.get(original)
    if marks is None and original.lower() in TONE_MARKS:
        marks = TONE_MARKS[original.lower()]
    if marks is None:
        return syllable

    marked = marks[tone - 1]
    return syllable[:mark_index] + marked + syllable[mark_index + 1 :]


def pinyin_to_diacritics(pinyin: str) -> str:
    """
    Converte una stringa pinyin numerica in pinyin con diacritici.

    Esempi:
        "ni3 hao3"        -> "nǐ hǎo"
        "lu:e4"           -> "lüè"
        "nu:3 er2"        -> "nǚ ér"
        "yi1 bu4 zuo4"    -> "yī bù zuò"
        "Xi1'an1"         -> "Xī'ān"
        "A1"              -> "A"
    """
    return SYLLABLE_RE.sub(
        lambda m: _mark_syllable(m.group(1), int(m.group(2))), pinyin
    )


# ---------------------------------------------------------------------------
# Lettura e parsing di CC-CEDICT
# ---------------------------------------------------------------------------

# Le definizioni possono contenere spazi e caratteri speciali; i separatori
# sono le barre. I gruppi \S+ catturano le due forme del lemma.
LINE_RE = re.compile(r"^(\S+)\s+(\S+)\s+\[([^\]]*)\]\s+/(.*)/\s*$")


def open_source(path: Path) -> io.TextIOBase:
    """Apre un file CC-CEDICT, gestendo anche file compressi con gzip."""
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return io.StringIO(raw.decode("utf-8-sig"))


def download_source(url: str) -> bytes:
    """Scarica una sorgente con uno User-Agent esplicito e un timeout."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def iter_cedict_lines(stream: Iterable[str]) -> Iterator[str]:
    """Restituisce solo le righe di dati, ignorando commenti e righe vuote."""
    for line in stream:
        line = line.rstrip("\r\n")
        if not line or line.startswith("#"):
            continue
        yield line


def parse_cedict(stream: Iterable[str], verbose: bool = True):
    """
    Converte le righe CC-CEDICT in (entries, index).

    entries: lista di [semplificato, tradizionale, pinyin, [definizioni]]
    index:   dizionario parola -> lista di indici in entries

    Le voci identiche (stesse forme, stesso pinyin, stesse definizioni) vengono
    deduplicate; le omonimie con definizioni diverse restano tutte.
    """
    entries: list[list] = []
    index: dict[str, list[int]] = {}
    seen: set[tuple[str, str, str, tuple[str, ...]]] = set()
    skipped = 0
    total = 0

    for line in iter_cedict_lines(stream):
        total += 1
        match = LINE_RE.match(line)
        if not match:
            skipped += 1
            continue

        traditional, simplified, numeric_pinyin, raw_defs = match.groups()
        definitions = [part.strip() for part in raw_defs.split("/") if part.strip()]
        if not definitions:
            skipped += 1
            continue

        pinyin = pinyin_to_diacritics(numeric_pinyin.strip())

        # Deduplica esatta: CC-CEDICT contiene alcune righe ripetute.
        key = (simplified, traditional, pinyin, tuple(definitions))
        if key in seen:
            continue
        seen.add(key)

        entry_index = len(entries)
        entries.append([simplified, traditional, pinyin, definitions])

        # Indicizza entrambe le grafie. Se sono uguali, una sola chiave.
        index.setdefault(simplified, []).append(entry_index)
        if traditional != simplified:
            index.setdefault(traditional, []).append(entry_index)

    if verbose:
        print(f"  righe lette:      {total:,}")
        print(f"  righe scartate:   {skipped:,}")
        print(f"  voci uniche:      {len(entries):,}")
        print(f"  chiavi indice:    {len(index):,}")

    return entries, index


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def build_payload(entries: list[list], index: dict[str, list[int]]) -> dict:
    """Costruisce l'oggetto JSON finale con i metadati di attribuzione."""
    return {
        "meta": {
            "name": "CC-CEDICT",
            "description": "Chinese-English dictionary",
            "license": "CC BY-SA 4.0",
            "licenseUrl": "https://creativecommons.org/licenses/by-sa/4.0/",
            "sourceUrl": "https://www.mdbg.net/chinese/dictionary?page=cc-cedict",
            "generated": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            "entries": len(entries),
            "format": 1,
        },
        "entries": entries,
        "index": index,
    }


def write_json(payload: dict, output: Path, pretty: bool = False) -> int:
    """Scrive dict.json e restituisce la dimensione in byte."""
    if pretty:
        text = json.dumps(payload, ensure_ascii=False, indent=1)
    else:
        text = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=False
        )
    # Scrittura atomica: evita un dict.json troncato se il processo si interrompe.
    tmp = output.with_suffix(output.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(output)
    return output.stat().st_size


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Genera dict.json dal dizionario CC-CEDICT.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="file CC-CEDICT locale (testo o .gz). Se omesso, scarica dalla rete.",
    )
    parser.add_argument(
        "--url",
        action="append",
        default=[],
        help="URL alternativo da provare (può essere ripetuto).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent / DEFAULT_OUTPUT,
        help="file JSON da creare (per default accanto a build_dict.py).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="limita il numero di righe di dati lette (0 = tutto).",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="JSON indentato (più leggibile ma molto più grande).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="riduce i messaggi a schermo.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    started = time.time()

    print("CC-CEDICT → dict.json")
    print("=" * 50)

    stream: io.TextIOBase
    if args.input:
        if not args.input.exists():
            print(f"Errore: file non trovato: {args.input}", file=sys.stderr)
            return 1
        print(f"Sorgente locale: {args.input}")
        stream = open_source(args.input)
    else:
        urls = args.url or DEFAULT_URLS
        payload_bytes = None
        last_error = None
        for url in urls:
            print(f"Download: {url}")
            try:
                payload_bytes = download_source(url)
                print(f"  scaricati {len(payload_bytes) / 1024 / 1024:.1f} MB")
                break
            except Exception as exc:  # noqa: BLE001 - mostriamo l'errore e proviamo oltre
                last_error = exc
                print(f"  non riuscito: {exc}", file=sys.stderr)
        if payload_bytes is None:
            print(f"Errore: nessuna sorgente raggiungibile ({last_error})", file=sys.stderr)
            print(
                "Suggerimento: scarica cedict_ts.u8 da mdbg.net e usa --input.",
                file=sys.stderr,
            )
            return 1

        if payload_bytes[:2] == b"\x1f\x8b":
            payload_bytes = gzip.decompress(payload_bytes)
        stream = io.StringIO(payload_bytes.decode("utf-8-sig"))

    if args.limit and args.limit > 0:
        stream = io.StringIO("\n".join(list(iter_cedict_lines(stream))[: args.limit]))

    print("Parsing e conversione del pinyin…")
    entries, index = parse_cedict(stream, verbose=not args.quiet)
    if not entries:
        print("Errore: nessuna voce valida trovata.", file=sys.stderr)
        return 1

    payload = build_payload(entries, index)
    output_path = args.output
    size = write_json(payload, output_path, pretty=args.pretty)

    print("-" * 50)
    print(f"Creato: {output_path} ({size / 1024 / 1024:.1f} MB)")
    print(f"Tempo:  {time.time() - started:.1f} s")
    print("Licenza dati: CC-CEDICT, CC BY-SA 4.0")
    print("  https://creativecommons.org/licenses/by-sa/4.0/")
    print("Aggiungi l'attribuzione a CC-CEDICT nella pagina che usa dict.json.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
