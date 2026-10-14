from src.countries import get_country
from src.osm_network import (
    _a79_ok,
    _eic_tokens,
    _event_index,
    _filter_hits,
    _in_country_mask,
    _keep_plant,
    _keep_substation,
    _lookup_plant,
    _build_eic_lookup,
    _match,
    _osm_jobs,
    _plant_query,
    _region_tso,
    _sub_query,
    _tokens,
    load_eic_plants,
)


def test_eic_tokens_expand_plant_names():
    toks = _eic_tokens("11WD7NEUR-B-KW-B")
    assert "neurath" in toks
    toks = _eic_tokens("11WD8JAEN1L----Z")
    assert "janschwalde" in toks


def test_zonal_event_not_osm_indexed():
    events = [
        {
            "reason": "Störung",
            "asset_name": "intra_zonal",
            "location": "intra_zonal",
            "asset_eic": "11WD7NEUR-B-KW-B",
        },
        {
            "reason": "Störung",
            "asset_name": "unlocated",
            "location": "unlocated",
            "asset_eic": "11WD7NEUR-B-KW-B",
        },
    ]
    items, index = _event_index(events)
    assert items == []
    assert index == {}


def test_plant_queries_stay_on_tiles():
    jobs, point_start = _osm_jobs(get_country("DE"))
    names = [path.name for path, _query in jobs]
    assert point_start == 16
    assert names[:16] == [f"osm_lines_{i}.json" for i in range(16)]
    assert "osm_plants.json" not in names
    assert [name for name in names if name.startswith("osm_points_") and name.endswith("_1.json")] == [
        f"osm_points_{i}_1.json" for i in range(16)
    ]


def test_overpass_queries_are_prefiltered():
    sub = _sub_query(47.0, 6.0, 55.0, 15.0)
    assert "voltage" in sub
    assert '["name"]({47.0,6.0,55.0,15.0});' not in sub
    plant = _plant_query(47.0, 6.0, 55.0, 15.0)
    assert "plant:source" in plant
    assert "plant:output:electricity" in plant


def test_keep_hv_substation_drops_distribution():
    assert _keep_substation({"substation": "minor_distribution"}) is False
    assert _keep_substation({"voltage": "380000"}) is True
    assert _keep_substation({"substation": "transmission"}) is True


def test_keep_plant_filters_solar():
    assert _keep_plant({"plant:source": "solar"}) is False
    assert _keep_plant({"plant:source": "coal"}) is True
    assert _keep_plant({"plant:output:electricity": "800 MW"}) is True


def test_zonal_generic_mitte_does_not_match_random_uw():
    events = [
        {
            "reason": "Störung",
            "asset_name": "intra_zonal",
            "location": "intra_zonal",
            "asset_eic": "11WD8MITT5G----0",
        }
    ]
    items, index = _event_index(events)
    assert _match("Umspannwerk Mitte", items, index) == []
    assert _match("Umspannwerk Dresden-Mitte", items, index) == []
    assert _match("Biomasseheizkraftwerk Berlin-Neukölln", items, index) == []


def test_single_station_token_matches():
    events = [{"reason": "Störung", "asset_name": "Garenfeld", "location": "", "asset_eic": ""}]
    items, index = _event_index(events)
    assert _match("Pumpspeicher Garenfeld", items, index) == [0]
    assert _tokens("Umspannwerk Borken") == ["borken"]


def test_generic_windpark_and_sudost_do_not_match_inland():
    events = [
        {
            "reason": "Störung",
            "asset_name": "TTG/155/WINDPARK BORKUM-DOLWINALPHA/AC109",
            "location": "",
            "asset_eic": "",
            "doc_type": "A79",
        },
        {
            "reason": "Störung",
            "asset_name": "Arkona Becken/Südost - Lubmin 261",
            "location": "Arkona Becken/Südost - Lubmin",
            "asset_eic": "",
            "doc_type": "A79",
        },
    ]
    items, index = _event_index(events)
    assert _match("Umspannstation Windpark Waltersdorf", items, index) == []
    assert _match("Torgau-Windpark Arzberg-Belgern", items, index) == []
    assert _match("Umspannwerk Flughafen Südost", items, index) == []
    assert _match("Umspannwerk Paderborn-Südost", items, index) == []
    assert _match("Borkum Riffgrund", items, index) == [0]
    assert _match("Umspannwerk Lubmin", items, index) == [1]
    assert "dolwin" in _tokens("WINDPARK BORKUM-DOLWINALPHA")


def test_ambiguous_single_token_needs_unique_place():
    events = [{"reason": "Störung", "asset_name": "Bergen", "location": "", "asset_eic": ""}]
    items, index = _event_index(events)
    assert _match("Umspannwerk Bergen", items, index, unique_tokens=set()) == []
    assert _match("Umspannwerk Bergen", items, index, unique_tokens={"bergen"}) == [0]


def test_single_token_rejects_wrong_tso_region():
    events = [{
        "reason": "Störung",
        "asset_name": "Bergen",
        "location": "",
        "asset_eic": "",
        "tso": "50Hertz",
    }]
    items, index = _event_index(events)
    assert _match(
        "Umspannwerk Bergen", items, index,
        unique_tokens={"bergen"},
        region_tso="TenneT",
        events=events,
    ) == []
    assert _match(
        "Umspannwerk Bergen", items, index,
        unique_tokens={"bergen"},
        region_tso="50Hertz",
        events=events,
    ) == [0]
    assert _region_tso(13.4, 52.52) == "50Hertz"
    assert _region_tso(11.58, 48.14) == "TenneT"
    assert _region_tso(6.96, 50.94) == "Amprion"
    assert _region_tso(9.18, 48.78) == "TransnetBW"


def test_a79_not_placed_in_southern_germany():
    events = [
        {
            "reason": "Störung",
            "asset_name": "TTG/155/DOLWIN GAMMA-MERKUR/AC156",
            "location": "",
            "asset_eic": "",
            "doc_type": "A79",
        }
    ]
    items, index = _event_index(events)
    hits = _match("SHD Merkur", items, index)
    assert hits == [0]
    assert _filter_hits(hits, events, lat=50.39, name="SHD Merkur") == []
    assert _filter_hits(hits, events, lat=53.8, name="SHD Merkur") == [0]
    assert _a79_ok(53.02, "Konverter Diele")
    assert not _a79_ok(50.39, "SHD Merkur", events[0])
    osterath = {
        "reason": "Störung",
        "asset_name": "TTG/600/A-Nord Osterath-Emden O/DC620",
        "location": "Osterath",
        "asset_eic": "",
        "doc_type": "A79",
    }
    assert _a79_ok(51.27, "Umspannwerk Osterath", osterath)
    assert _filter_hits([0], [osterath], lat=51.27, name="Umspannwerk Osterath") == [0]


def test_vianden_from_short_eic_core():
    assert "vianden" in _eic_tokens("11WD72VIA2H-KW-0")


def test_eic_plant_lookup_from_opsd_csv(tmp_path):
    csv = tmp_path / "plants.csv"
    csv.write_text(
        "name_bnetza,name_uba,city,lat,lon,eic_code_plant,eic_code_block\n"
        "Neurath,Neurath,Grevenbroich,51.039,6.6,11WD7NEUR-B-KW-B,11WD7NEUR-B-KW-5\n"
        "Jänschwalde,Jänschwalde,Peitz,51.834,14.45,11WD8JAEN1L----Z,11WD8JAEN1L---AD\n",
        encoding="utf-8",
    )
    plants = load_eic_plants(csv)
    lookup = _build_eic_lookup(plants)
    neur = _lookup_plant("11WD7NEUR-B-KW-B", lookup)
    jaen = _lookup_plant("11WD8JAEN1L----Z", lookup)
    assert neur is not None and abs(neur["lat"] - 51.039) < 1e-6
    assert jaen is not None
    assert "jänschwalde" in jaen["name"].lower() or "janschwalde" in jaen["name"].lower()
    assert _lookup_plant("11W0-0000-0026-Y", lookup) is None


def test_dedupe_merges_nearby_chain_not_distant_names():
    from src.osm_network import _dedupe_points

    def pt(name, lon, ev):
        return {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, 51.0]},
            "properties": {"name": name, "ev": ev},
        }

    line = {"type": "Feature", "geometry": {"type": "LineString", "coordinates": []}, "properties": {}}
    merged = _dedupe_points([
        line,
        pt("Neurath", 6.60, [1]),
        pt("Neurath", 6.61, [1, 2]),
        pt("Other", 6.62, [1, 2]),
        pt("Neurath", 8.00, [1]),
    ])
    points = [f for f in merged if f["geometry"]["type"] == "Point"]
    assert merged[0] is line
    assert len(points) == 2
    assert points[0]["properties"]["ev"] == [1, 2]
    assert points[1]["properties"]["name"] == "Neurath"
    assert points[1]["geometry"]["coordinates"][0] == 8.00


def test_map_mask_keeps_germany_drops_neighbor_interior():
    de = get_country("DE")
    inside = [
        (12.12, 49.63),    # Etzenricht
        (12.88, 50.86),    # Röhrsdorf
        (12.88, 50.52),    # Markersbach
        (14.95, 51.06),    # Hagenwerder
        (14.81, 50.90),    # Zittau
        (7.95, 47.57),     # Bad Säckingen
        (13.46, 48.57),    # Passau
    ]
    outside = [
        (13.33, 50.35),    # Hradec (CZ)
        (13.34, 49.59),    # Přeštice (CZ)
        (12.68, 50.16),    # Vítkov (CZ)
        (13.12, 48.26),    # St. Peter (AT)
        (11.01, 47.21),    # Kühtai (AT)
        (10.97, 47.27),    # Silz (AT)
        (9.87, 47.08),     # Rodund (AT)
        (15.08, 51.19),    # Mikułowa (PL)
    ]
    for lon, lat in inside:
        assert _in_country_mask(lon, lat, de), (lon, lat)
    for lon, lat in outside:
        assert not _in_country_mask(lon, lat, de), (lon, lat)
