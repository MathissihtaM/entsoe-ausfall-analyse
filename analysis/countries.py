"""Länderprofile für ENTSO-E-Netzausfälle. Weitere Länder: ein Eintrag in COUNTRIES."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Sequence, Tuple


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
        """Interne Regelzonen plus Bidding-Zone, dann Grenzen in beide Richtungen."""
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
        return pairs

    def bidding_zones(self) -> List[Zone]:
        """Zonen für A77/A79/A80 (Bidding-Zone plus die vier ÜNB-Regelzonen)."""
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

# DE+LU plus Nordsee/Ostsee vor der Kueste (Offshore-Kabel).
# Westkante folgt grob der FR/LU/BE/NL-Grenze, damit Elsass/Lothringen
# nicht mit ins deutsche Netz fallen.
MAP_MASKS = {
    "DE": {
        "type": "Polygon",
        "coordinates": [[
            # Suedrand: Basel entlang CH/AT
            [7.56, 47.58],
            [8.57, 47.59],
            [9.66, 47.54],
            [10.48, 47.39],
            [12.18, 47.68],
            [13.15, 47.47],
            [13.83, 48.77],
            [15.02, 49.02],
            # Ostrand
            [15.04, 51.00],
            [14.94, 51.66],
            [14.41, 52.89],
            [14.16, 53.92],
            [13.15, 54.85],
            # Nordsee/Ostsee-Beule
            [11.40, 54.75],
            [10.20, 54.90],
            [8.90, 54.90],
            [8.20, 55.50],
            [6.90, 55.90],
            [3.35, 55.55],
            [3.35, 53.45],
            [6.40, 53.47],
            # NL/BE-Grenze nach Sueden
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
            # Saarland / Pfalz, dann Rhein nach Basel (DE oestlich)
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


# Interconnector → üblicher deutscher ÜNB, wenn der Absender die Plattform ist.
BORDER_TSO_DE = {
    "DK_1": "TenneT",
    "NO_2": "TenneT",
    "NL": "TenneT",
    "DK_2": "50Hertz",
    "PL": "50Hertz",
    "SE_4": "50Hertz",
    "CZ": "50Hertz",
    "AT": "TenneT",
    "CH": "TransnetBW",
    "FR": "Amprion",
    "BE": "Amprion",
}

ASSET_EIC_TSO = {
    "11TD": "TenneT",
}


def tso_from_asset_eic(asset_eic: str) -> str:
    for prefix, label in ASSET_EIC_TSO.items():
        if (asset_eic or "").startswith(prefix):
            return label
    return ""


def tso_from_text(*parts: str) -> str:
    blob = " ".join(parts).lower()
    if "50hertz" in blob or "50 hertz" in blob:
        return "50Hertz"
    if "amprion" in blob:
        return "Amprion"
    if "transnet" in blob:
        return "TransnetBW"
    if "tennet" in blob:
        return "TenneT"
    return ""


def tso_label(
    country: Country,
    sender_eic: str,
    domain_eics: Iterable[str],
    *,
    asset_eic: str = "",
    asset_name: str = "",
    location: str = "",
) -> str:
    if sender_eic in country.tso_senders:
        return country.tso_senders[sender_eic]
    named = tso_from_text(asset_name, location)
    if named:
        return named
    from_asset = tso_from_asset_eic(asset_eic)
    if from_asset:
        return from_asset
    area_tso = country.tso_from_control_eic()
    for eic in domain_eics:
        if eic in area_tso:
            return area_tso[eic]
    if country.code == "DE":
        zones = country.zone_by_eic()
        for eic in domain_eics:
            zone = zones.get(eic)
            if zone and zone.code in BORDER_TSO_DE:
                return BORDER_TSO_DE[zone.code]
    return "unbekannt"


def known_tso_labels(country: Country) -> Sequence[str]:
    return tuple(z.label for z in country.control_areas)
