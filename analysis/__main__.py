"""python -m analysis --country DE --years 5"""

from __future__ import annotations

import argparse
import json
import sys

from analysis.countries import get_country
from analysis.fetch_outages import FETCH_WORKERS, OUTAGES_JSON, fetch_country, load_token
from analysis.osm_network import build_network
from analysis.write_html import write_html


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
    args = parser.parse_args(argv)

    country = get_country(args.country)
    if args.rebuild_network:
        if not OUTAGES_JSON.is_file():
            print(f"fehlt: {OUTAGES_JSON}", file=sys.stderr)
            return 1
        payload = json.loads(OUTAGES_JSON.read_text(encoding="utf-8"))
        payload["network"] = build_network(country, payload.get("events") or [])
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
