"""Länderprofil und Zuordnung einer Meldung zum Übertragungsnetzbetreiber.

Ein Profil bündelt, was die API nicht als „Deutschland“ kennt: die Gebotszone
DE/LU, die vier Regelzonen, die Nachbarzonen für Grenzkuppelleitungen und die
Absenderkennungen (EIC) der vier ÜNB. Die Kartenmaske schneidet später das
OpenStreetMap-Netz auf dieses Gebiet.

``tso_label`` läuft eine feste Reihenfolge ab, weil im Dokument oft nur die
Gebotszone steht und der Betreiber fehlt. Der erste Treffer gilt.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


@dataclass(frozen=True)
class Zone:
    code: str
    eic: str
    label: str


@dataclass(frozen=True)
class Country:
    code: str
    label: str
    bidding_zone: Zone
    control_areas: Tuple[Zone, ...]
    neighbors: Tuple[Zone, ...]
    tso_senders: Dict[str, str]
    map_center: Tuple[float, float]
    map_zoom: float

    def zone_by_eic(self) -> Dict[str, Zone]:
        out: Dict[str, Zone] = {self.bidding_zone.eic: self.bidding_zone}
        for z in self.control_areas:
            out[z.eic] = z
        for z in self.neighbors:
            out[z.eic] = z
        return out

    def tso_from_control_eic(self) -> Dict[str, str]:
        return {z.eic: z.label for z in self.control_areas}

    def a78_corridors(self) -> List[Tuple[Zone, Zone]]:
        """Interne Regelzonen plus Gebotszone, dann Grenzen in beide Richtungen.

        Amprion, TenneT, 50Hertz und TransnetBW hängen Innennetz-Meldungen
        an die eigene Regelzone gegen den Nachbarn, nicht an DE/LU. Deshalb
        zusätzlich jede Regelzone × jeder Nachbar.
        """
        pairs: List[Tuple[Zone, Zone]] = []
        seen: set[Tuple[str, str]] = set()

        def add(a: Zone, b: Zone) -> None:
            key = (a.eic, b.eic)
            if key in seen:
                return
            seen.add(key)
            pairs.append((a, b))

        for z in self.control_areas:
            add(z, z)
        add(self.bidding_zone, self.bidding_zone)
        for i, a in enumerate(self.control_areas):
            for b in self.control_areas[i + 1 :]:
                add(a, b)
                add(b, a)
        for nb in self.neighbors:
            add(self.bidding_zone, nb)
            add(nb, self.bidding_zone)
            for z in self.control_areas:
                add(z, nb)
                add(nb, z)
        return pairs

    def bidding_zones(self) -> List[Zone]:
        """Zonen für A77/A79/A80 (Gebotszone plus die vier ÜNB-Regelzonen)."""
        return [*self.control_areas, self.bidding_zone]


DE_50HZ = Zone("DE_50HZ", "10YDE-VE-------2", "50Hertz")
DE_AMPRION = Zone("DE_AMPRION", "10YDE-RWENET---I", "Amprion")
DE_TENNET = Zone("DE_TENNET", "10YDE-EON------1", "TenneT")
DE_TRANSNET = Zone("DE_TRANSNET", "10YDE-ENBW-----N", "TransnetBW")
DE_LU = Zone("DE_LU", "10Y1001A1001A82H", "Deutschland/Luxemburg")

NEIGHBORS_DE: Tuple[Zone, ...] = (
    Zone("AT", "10YAT-APG------L", "Österreich"),
    Zone("BE", "10YBE----------2", "Belgien"),
    Zone("CH", "10YCH-SWISSGRIDZ", "Schweiz"),
    Zone("CZ", "10YCZ-CEPS-----N", "Tschechien"),
    Zone("DK_1", "10YDK-1--------W", "Dänemark (DK1)"),
    Zone("DK_2", "10YDK-2--------M", "Dänemark (DK2)"),
    Zone("FR", "10YFR-RTE------C", "Frankreich"),
    Zone("NL", "10YNL----------L", "Niederlande"),
    Zone("NO_2", "10YNO-2--------T", "Norwegen (NO2)"),
    Zone("PL", "10YPL-AREA-----S", "Polen"),
    Zone("SE_4", "10Y1001A1001A47J", "Schweden (SE4)"),
)

# Absender-EIC (10X) der vier deutschen ÜNB.
DE_TSO_SENDERS: Dict[str, str] = {
    "10XDE-VE-TRANSM-W": "50Hertz",
    "10XDE-RWENET---W": "Amprion",
    "10XDE-EON-NETZ-C": "TenneT",
    "10XDE-ENBW--TNGX": "TransnetBW",
}

# Deutschland und Luxemburg plus Nord- und Ostsee vor der Küste (Seekabel).
# Die Westkante folgt grob der Grenze zu Frankreich, Luxemburg, Belgien
# und den Niederlanden, damit Elsass und Lothringen nicht ins deutsche Netz fallen.
MAP_MASKS = {
    "DE": {
        "type": "Polygon",
        "coordinates": [[
            # Südrand: Basel entlang der Grenze zur Schweiz und zu Österreich
            [7.56, 47.58],
            [7.99, 47.54],  # Bad Säckingen
            [8.57, 47.59],
            [9.66, 47.54],
            [10.48, 47.39],
            [12.18, 47.68],
            # Inn: Deutschland nördlich, St. Peter (AT) südlich
            [12.50, 47.88],
            [12.83, 48.16],
            [13.02, 48.27],
            [13.30, 48.45],
            [13.48, 48.55],  # Passau nördlich des Inn
            [13.62, 48.68],
            [13.83, 48.77],  # Dreiländereck DE-AT-CZ
            # DE-CZ westlich von Böhmen (Hradec, Přeštice, Vítkov bleiben draußen)
            [13.55, 48.95],
            [13.20, 49.15],
            [12.85, 49.32],
            [12.62, 49.55],
            [12.52, 49.70],
            [12.42, 50.05],
            [12.38, 50.28],
            [12.55, 50.40],
            [12.80, 50.45],
            [13.05, 50.48],
            [13.15, 50.55],  # Markersbach
            [13.40, 50.62],
            [13.75, 50.75],
            [14.10, 50.85],
            [14.45, 50.90],
            [14.85, 50.89],  # Zittau
            [15.05, 51.08],  # Hagenwerder / Görlitz, westlich von Mikułowa
            [15.00, 51.25],
            [14.94, 51.66],
            [14.41, 52.89],
            [14.16, 53.92],
            [13.15, 54.85],
            # Ausbuchtung in Nord- und Ostsee
            [11.40, 54.75],
            [10.20, 54.90],
            [8.90, 54.90],
            [8.20, 55.50],
            [6.90, 55.90],
            [3.35, 55.55],
            [3.35, 53.45],
            [6.40, 53.47],
            # Grenze zu den Niederlanden und zu Belgien, nach Süden
            [6.70, 52.55],
            [6.50, 51.90],
            [6.18, 51.45],
            [6.02, 50.78],
            [6.08, 50.35],
            # Luxemburg komplett (Gebotszone DE-LU)
            [6.14, 50.18],
            [5.73, 50.18],
            [5.73, 49.45],
            [6.22, 49.45],
            [6.40, 49.46],
            # Saarland und Pfalz, dann der Rhein nach Basel (Deutschland östlich davon)
            [6.55, 49.22],
            [6.85, 49.12],
            [7.20, 49.12],
            [7.70, 49.06],
            [8.12, 48.97],
            [7.90, 48.75],
            [7.79, 48.58],
            [7.68, 48.35],
            [7.56, 48.12],
            [7.59, 47.95],
            [7.55, 47.82],
            [7.54, 47.70],
            [7.56, 47.58],
        ]],
    },
}

MAP_BOUNDS = {
    "DE": [[3.2, 47.2], [15.4, 56.0]],
}


GERMANY = Country(
    code="DE",
    label="Deutschland",
    bidding_zone=DE_LU,
    control_areas=(DE_50HZ, DE_AMPRION, DE_TENNET, DE_TRANSNET),
    neighbors=NEIGHBORS_DE,
    tso_senders=DE_TSO_SENDERS,
    map_center=(10.45, 51.16),
    map_zoom=5.6,
)

COUNTRIES: Dict[str, Country] = {
    GERMANY.code: GERMANY,
}


def get_country(code: str) -> Country:
    key = code.strip().upper()
    if key not in COUNTRIES:
        known = ", ".join(sorted(COUNTRIES))
        raise KeyError(f"Unbekanntes Land {code!r}. Vorhanden: {known}")
    return COUNTRIES[key]


# Nur Grenzen mit genau einem deutschen ÜNB. FR/CH/AT/NL teilen sich mehrere.
BORDER_TSO_DE = {
    "DK_1": "TenneT",
    "NO_2": "TenneT",
    "DK_2": "50Hertz",
    "PL": "50Hertz",
    "SE_4": "TenneT",  # Baltic Cable (Herrenwyk), einzige Leitung Deutschland–SE4
    "CZ": "50Hertz",
    "BE": "Amprion",  # ALEGrO
}

# Gemeinsame 50Hertz-TenneT-Leitung, kein einzelner Betreiber.
JOINT_50H_TENNET = "50Hertz/TenneT"

# Stelle 4–5 der deutschen 11W-/11T-EIC: D2 TenneT, D4 TransnetBW, D7 Amprion, D8 50Hertz.
_EIC_ISSUER = {"D2": "TenneT", "D4": "TransnetBW", "D7": "Amprion", "D8": "50Hertz"}
_EIC_COMPACT = re.compile(r"[^A-Za-z0-9]")

# Bundesland mit genau einer Regelzone. Hessen ist geteilt und fehlt bewusst.
STATE_TSO = {
    "Baden-Württemberg": "TransnetBW",
    "Bayern": "TenneT",
    "Berlin": "50Hertz",
    "Brandenburg": "50Hertz",
    "Bremen": "TenneT",
    "Hamburg": "50Hertz",
    "Mecklenburg-Vorpommern": "50Hertz",
    "Niedersachsen": "TenneT",
    "Nordrhein-Westfalen": "Amprion",
    "Rheinland-Pfalz": "Amprion",
    "Saarland": "Amprion",
    "Sachsen": "50Hertz",
    "Sachsen-Anhalt": "50Hertz",
    "Schleswig-Holstein": "TenneT",
    "Thüringen": "50Hertz",
}

_PLANT_CSV = Path(__file__).resolve().parent.parent / "data" / "ref" / "conventional_power_plants_DE.csv"
_PLANT_TSO: Optional[Dict[str, Dict[str, Any]]] = None


def _compact_eic(eic: str) -> str:
    return _EIC_COMPACT.sub("", eic or "").upper()


def _fold(text: str) -> str:
    return (
        text.lower()
        .replace("ä", "ae")
        .replace("ö", "oe")
        .replace("ü", "ue")
        .replace("ß", "ss")
    )


def tso_from_operator_name(name: str) -> str:
    """Nur ein ausgeschriebener ÜNB-Name. Verteilnetzbetreiber bleiben leer."""
    folded = _fold(name or "")
    hits = []
    if "50hertz" in folded or "50 hertz" in folded:
        hits.append("50Hertz")
    if "amprion" in folded:
        hits.append("Amprion")
    if "transnet" in folded:
        hits.append("TransnetBW")
    if "tennet" in folded:
        hits.append("TenneT")
    unique = list(dict.fromkeys(hits))
    if len(unique) == 1:
        return unique[0]
    return ""


# Internationale Kuppel: ``10T-DE-FR``, ``10A-CH-DE``. Weder Token DE → Ausland.
_INTL_PAIR = re.compile(r"^10[TA]-([A-Z]{2})-([A-Z]{2})(?:-|$)", re.I)


def fully_abroad_eic(asset_eic: str) -> bool:
    """True, wenn die Anlage nicht auf dem deutschen Netz liegt.

    Deutsche Vergabe ist ``11…``. ``10T-DE-…`` / ``10T-…-DE`` und sonstige
    ``10…`` (ENTSO-E, z. B. Handewitt–Kassø) bleiben. Andere Stellen
    (``12`` CH, ``17`` FR, ``45`` DK, ``46`` SE, ``50`` NO, …) und Kuppel
    ohne DE (``10T-BE-FR``, ``10T-DK-DK``) gelten als komplett im Ausland.
    """
    eic = (asset_eic or "").strip().upper()
    if len(eic) < 3:
        return False
    if eic.startswith("11"):
        return False
    pair = _INTL_PAIR.match(eic)
    if pair:
        return pair.group(1) != "DE" and pair.group(2) != "DE"
    if eic.startswith("10"):
        return False
    return eic[:2].isdigit()


# Deutsches Ende einer Kuppel oder klare DE-Anlage. Ein Treffer hält die Meldung.
_DE_PLACES = frozenset({
    "etzenricht", "etz-hra", "etz-pre", "roehrsdorf", "rohrsdorf",
    "hagenwerder", "handewitt", "jardelund",
    "ensdorf", "eichstetten", "tiengen", "kuehmoos", "kuhmoos",
    "gronau", "niederrhein", "kruen", "krun", "oberbrunn", "altheim",
    "simbach", "pleinting", "pirach", "obermooweiler", "leupolz",
    "trossingen", "stockach", "gurtweil", "herrenwyk", "wilster",
    "buesterhof", "buscherhof", "buerstadt", "burstadt", "saeckingen",
    "sackingen", "audorf", "conneforde", "elsfleth",
    "rheinfeld", "opladen", "vierraden",
})

# Nur Nachbarland, kein deutsches Umspannwerk. Ohne DE-Ort in derselben
# Meldung gilt die Anlage als komplett im Ausland (Westtirol–Bürs, Tauern–Lienz).
_FOREIGN_PLACES = frozenset({
    "westtirol", "buers", "tauern", "lienz", "hradec", "prestice",
    "beznau", "asphard", "mikulowa", "doetinchem", "hengelo", "vigy",
    "kassoe", "kasso", "kruseberg", "tonstad", "vandans", "rodund",
    "kuehtai", "kuhtai", "st. peter", "st peter",
    "vitkov", "kocin", "chrast", "milin", "meiningen 406",
})


def fully_abroad_name(*parts: str) -> bool:
    """True, wenn der Name nur Orte im Nachbarland nennt, keinen in DE."""
    blob = _fold(" ".join(parts))
    if not blob.strip():
        return False
    if any(place in blob for place in _DE_PLACES):
        return False
    return any(place in blob for place in _FOREIGN_PLACES)


def fully_abroad(asset_eic: str = "", asset_name: str = "", location: str = "") -> bool:
    """Komplett im Ausland: ausländische EIC oder nur Nachbarorte im Namen."""
    if fully_abroad_eic(asset_eic):
        return True
    return fully_abroad_name(asset_name, location)


def foreign_grid_eic(asset_eic: str) -> bool:
    """Anlage liegt komplett im Ausland — kein deutscher ÜNB, nicht kartieren."""
    return fully_abroad_eic(asset_eic)


def tso_from_asset_eic(asset_eic: str) -> str:
    compact = _compact_eic(asset_eic)
    if len(compact) >= 5 and compact.startswith(("11T", "11W")):
        return _EIC_ISSUER.get(compact[3:5], "")
    return ""


def tso_from_text(*parts: str) -> str:
    folded = _fold(" ".join(parts))
    has_50 = "50hertz" in folded or "50 hertz" in folded or "50h/" in folded or "50h-" in folded
    has_ttg = "tennet" in folded or "ttg/" in folded or "ttg-" in folded
    if has_50 and has_ttg:
        return JOINT_50H_TENNET
    if has_50:
        return "50Hertz"
    if "amprion" in folded:
        return "Amprion"
    if "transnet" in folded:
        return "TransnetBW"
    if has_ttg:
        return "TenneT"
    if "baltic cable" in folded or "herrenwyk" in folded:
        return "TenneT"
    return ""


def tso_from_border_name(*parts: str) -> str:
    """Deutsche Seiten der FR-/CH-Grenze. Reine Auslandsleitungen bleiben leer."""
    folded = _fold(" ".join(parts))
    if "eichstetten" in folded or "vogelgrun" in folded:
        return "TransnetBW"
    if "ensdorf" in folded:
        return "Amprion"
    if "tiengen" in folded or "kuehmoos" in folded:
        return "TransnetBW"
    return ""


def _plant_records() -> Dict[str, Dict[str, Any]]:
    global _PLANT_TSO
    if _PLANT_TSO is not None:
        return _PLANT_TSO
    found: Dict[str, Dict[str, Any]] = {}
    if not _PLANT_CSV.is_file():
        _PLANT_TSO = found
        return found
    with _PLANT_CSV.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            op_tso = tso_from_operator_name(row.get("network_operator") or "")
            state_tso = STATE_TSO.get((row.get("state") or "").strip(), "")
            try:
                lat = float(row.get("lat") or "")
                lon = float(row.get("lon") or "")
            except ValueError:
                lat, lon = None, None
            rec = {"tso": op_tso or state_tso, "lat": lat, "lon": lon}
            for raw in (row.get("eic_code_plant") or "", row.get("eic_code_block") or ""):
                key = _compact_eic(raw)
                if len(key) < 10 or key.startswith("11W0"):
                    continue
                prev = found.get(key)
                if prev is None or (rec["tso"] and not prev["tso"]):
                    found[key] = rec
    _PLANT_TSO = found
    return found


def tso_from_plant_eic(asset_eic: str) -> str:
    """ÜNB aus der Kraftwerksliste: Anschluss-ÜNB, sonst Bundesland der Anlage.

    Verteilnetzbetreiber mit mehreren Regelzonen werden nicht pauschal einem
    ÜNB zugeordnet. Es zählt die einzelne Anlage.
    """
    key = _compact_eic(asset_eic)
    if len(key) < 10 or key.startswith("11W0"):
        return ""
    rec = _plant_records().get(key)
    if rec is None:
        for n in range(min(len(key), 16), 9, -1):
            rec = _plant_records().get(key[:n])
            if rec is not None:
                break
    if not rec:
        return ""
    if rec["tso"]:
        return rec["tso"]
    if rec["lat"] is None or rec["lon"] is None:
        return ""
    from src.osm_network import _region_tso

    return _region_tso(float(rec["lon"]), float(rec["lat"])) or ""


def tso_label(
    country: Country,
    sender_eic: str,
    domain_eics: Iterable[str],
    *,
    asset_eic: str = "",
    asset_name: str = "",
    location: str = "",
) -> str:
    """Übertragungsnetzbetreiber oder ``unbekannt``. Der erste Treffer gilt.

    1. Absender-EIC der vier ÜNB
    2. Name, der genau einen ÜNB nennt; 50Hertz und TenneT zusammen bleiben
       ``50Hertz/TenneT``
    3. Regelzone in den Gebiets-EICs der Meldung
    4. Vergabestelle in der Anlagen-EIC (11W/11T, Stellen D2/D4/D7/D8)
    5. Ausländische Leitungskennung (wird in der Auswertung verworfen)
    6. Grenzname oder Nachbarzone mit nur einem deutschen ÜNB
    7. Kraftwerksliste (Anschluss-ÜNB, sonst Bundesland mit nur einer Regelzone)
    """
    if sender_eic in country.tso_senders:
        return country.tso_senders[sender_eic]
    named = tso_from_text(asset_name, location)
    if named:
        return named
    area_tso = country.tso_from_control_eic()
    for eic in domain_eics:
        if eic in area_tso:
            return area_tso[eic]
    from_asset = tso_from_asset_eic(asset_eic)
    if from_asset:
        return from_asset
    if foreign_grid_eic(asset_eic):
        return "unbekannt"
    if country.code == "DE":
        zones = country.zone_by_eic()
        neighbor_codes = []
        for eic in domain_eics:
            zone = zones.get(eic or "")
            if zone is not None:
                neighbor_codes.append(zone.code)
        eic_up = (asset_eic or "").upper()
        on_fr_ch = (
            "FR" in neighbor_codes
            or "CH" in neighbor_codes
            or "DE-FR" in eic_up
            or eic_up.startswith("10T-CH")
        )
        if on_fr_ch:
            border_name = tso_from_border_name(asset_name, location)
            if border_name:
                return border_name
        for code in neighbor_codes:
            if code in BORDER_TSO_DE:
                return BORDER_TSO_DE[code]
        from_plant = tso_from_plant_eic(asset_eic)
        if from_plant:
            return from_plant
    return "unbekannt"


def relabel_event_tso(country: Country, ev: Dict[str, Any]) -> str:
    """Setzt tso aus Absender, Name, Anlagen-EIC und Gebotszonen neu."""
    label = tso_label(
        country,
        ev.get("sender_eic") or "",
        (ev.get("in_domain"), ev.get("out_domain"), ev.get("bidding_domain")),
        asset_eic=ev.get("asset_eic") or "",
        asset_name=ev.get("asset_name") or "",
        location=ev.get("location") or "",
    )
    ev["tso"] = label
    return label
