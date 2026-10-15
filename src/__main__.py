"""Start der Auswertung: ``python -m src --country DE --years 5``.

Ohne Zusatzflags werden fehlende API-Fenster nachgeladen, alle ZIP-Dateien
neu gelesen und Karte plus HTML geschrieben. ``--cache-only`` überspringt
die API. ``--rebuild-network`` lässt die Meldungen in ``outages.json`` stehen
und ordnet sie nur neu dem Netz zu; ``--years`` gilt dann nicht.
``--refresh-refs`` lädt Static Grid Model und MaStR und schreibt die
Lookup-CSVs; der Standardlauf liest nur diese Dateien.
"""

from __future__ import annotations

import argparse
import json
import sys

from src.countries import get_country, relabel_event_tso
from src.fetch_outages import FETCH_WORKERS, OUTAGES_JSON, fetch_country, load_token
from src.mastr_plants import refresh_mastr_ref
from src.osm_network import build_network
from src.parse_outages import keep_event
from src.static_grid import refresh_grid_ref
from src.write_html import write_html


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ENTSO-E Netz-Störungsmeldungen abrufen und HTML schreiben")
    parser.add_argument("--country", default="DE", help="Ländercode (Standard: DE)")
    parser.add_argument("--years", type=int, default=5, help="Rückblick in Jahren")
    parser.add_argument("--cache-only", action="store_true", help="Nur vorhandene ZIP-Dateien parsen")
    parser.add_argument("--workers", type=int, default=FETCH_WORKERS, help="Parallele API-Abrufe")
    parser.add_argument(
        "--rebuild-network",
        action="store_true",
        help="Nur OSM/EIC-Karte neu matchen, vorhandenes outages.json behalten",
    )
    parser.add_argument(
        "--refresh-refs",
        action="store_true",
        help="Static Grid Model und MaStR neu laden, Lookup-CSVs schreiben",
    )
    args = parser.parse_args(argv)

    if args.refresh_refs:
        refresh_grid_ref()
        refresh_mastr_ref()
        flags = set(argv or ["--refresh-refs"])
        more = flags - {"--refresh-refs", "--country", args.country, str(args.years)}
        if not more and not args.rebuild_network and not args.cache_only:
            return 0

    country = get_country(args.country)
    if args.rebuild_network:
        if not OUTAGES_JSON.is_file():
            print(f"fehlt: {OUTAGES_JSON}", file=sys.stderr)
            return 1
        payload = json.loads(OUTAGES_JSON.read_text(encoding="utf-8"))
        events = [ev for ev in (payload.get("events") or []) if keep_event(ev, country)]
        for ev in events:
            relabel_event_tso(country, ev)
        payload["events"] = events
        payload["count"] = len(events)
        payload["network"] = build_network(country, events)
        OUTAGES_JSON.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        dest = write_html(payload)
        print(f"{payload.get('count', 0)} Meldungen -> {dest}")
        return 0
    cache_only = args.cache_only
    if not cache_only:
        try:
            load_token()
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            print("Nutze nur den lokalen ZIP-Cache.", file=sys.stderr)
            cache_only = True

    payload = fetch_country(country, years=args.years, cache_only=cache_only, workers=args.workers)

    dest = write_html(payload)
    print(f"{payload.get('count', 0)} Meldungen -> {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
