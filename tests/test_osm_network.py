from src.countries import get_country
from src.osm_network import (
    _a79_ok,
    _attach_grid_elements,
    _eic_tokens,
    _event_index,
    _filter_hits,
    _grid_name_match,
    _in_country_mask,
    _keep_plant,
    _keep_substation,
    _lookup_plant,
    _build_eic_lookup,
    _match,
    _merge_plants,
    _osm_jobs,
    _plant_query,
    _region_tso,
    _sub_query,
    _tokens,
    backfill_from_grid,
    grid_name_index,
    line_between_stations,
    load_eic_plants,
    match_line_ends,
    match_station,
    route_between,
    build_line_graph,
)
from src.static_grid import compact_eic


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


def test_mastr_overrides_opsd_on_same_eic(tmp_path):
    opsd = tmp_path / "opsd.csv"
    mastr = tmp_path / "mastr.csv"
    opsd.write_text(
        "name_bnetza,name_uba,city,lat,lon,eic_code_plant,eic_code_block\n"
        "Neurath alt,Neurath,Grevenbroich,51.0,6.5,11WD7NEUR-B-KW-B,\n",
        encoding="utf-8",
    )
    mastr.write_text(
        "name_bnetza,name_uba,city,lat,lon,eic_code_plant,eic_code_block\n"
        "Neurath MaStR,Neurath,Grevenbroich,51.039,6.6,11WD7NEUR-B-KW-B,\n",
        encoding="utf-8",
    )
    plants = load_eic_plants(opsd, extra=mastr)
    lookup = _build_eic_lookup(plants)
    hit = _lookup_plant("11WD7NEUR-B-KW-B", lookup)
    assert hit is not None
    assert abs(hit["lat"] - 51.039) < 1e-6
    assert "mastr" in hit["name"].lower()


def test_manual_assets_win_and_add_cluster(tmp_path):
    opsd = tmp_path / "opsd.csv"
    mastr = tmp_path / "mastr.csv"
    manual = tmp_path / "manual.csv"
    header = "name_bnetza,name_uba,city,lat,lon,eic_code_plant,eic_code_block\n"
    opsd.write_text(header + "Neurath,Neurath,Grevenbroich,51.039,6.6,11WD7NEUR-B-KW-B,\n", encoding="utf-8")
    mastr.write_text(header + "Neurath MaStR,,Grevenbroich,51.03765,6.61229,11WD7NEUR-B-KW-B,\n", encoding="utf-8")
    manual.write_text(
        header
        + "Neurath kuratiert,,Grevenbroich,51.0365,6.61332,11WD7NEUR-B-KW-B,\n"
        + "Nordsee AWZ (Offshore-Cluster),,Nordsee,54.4,7.0,11WD2OGTI000264J,\n",
        encoding="utf-8",
    )
    plants = load_eic_plants(opsd, extra=mastr, manual=manual)
    lookup = _build_eic_lookup(plants)
    neur = _lookup_plant("11WD7NEUR-B-KW-B", lookup)
    assert neur is not None and "kuratiert" in neur["name"]
    awz = _lookup_plant("11WD2OGTI000264J", lookup)
    assert awz is not None
    assert abs(awz["lat"] - 54.4) < 1e-6 and abs(awz["lon"] - 7.0) < 1e-6


def test_merge_plants_overlay_wins():
    base = [{"key": "11WD7NEURBKB", "name": "alt", "lat": 1, "lon": 1, "tokens": []}]
    overlay = [{"key": "11WD7NEURBKB", "name": "neu", "lat": 2, "lon": 2, "tokens": ["neurath"]}]
    merged = _merge_plants(base, overlay)
    assert len(merged) == 1
    assert merged[0]["name"] == "neu"


def test_merge_plants_keeps_base_coords_if_mastr_locked():
    base = [{"key": "11WD7NEUR1BF", "name": "Neurath OPSD", "lat": 51.0365, "lon": 6.61332, "tokens": ["neurath"]}]
    overlay = [{"key": "11WD7NEUR1BF", "name": "Neurath F", "lat": None, "lon": None, "tokens": ["neurath"]}]
    merged = _merge_plants(base, overlay)
    assert merged[0]["name"] == "Neurath F"
    assert abs(merged[0]["lat"] - 51.0365) < 1e-6
    assert abs(merged[0]["lon"] - 6.61332) < 1e-6


def test_match_station_normalized_unique():
    points = [
        {"name": "Umspannwerk Röhrsdorf", "lon": 12.88, "lat": 50.86, "kind": "substation"},
        {"name": "Umspannwerk Dresden-Süd", "lon": 13.73, "lat": 51.0, "kind": "substation"},
        {"name": "Kraftwerk Röhrsdorf", "lon": 12.87, "lat": 50.85, "kind": "plant"},
    ]
    hit = match_station("Röhrsdorf", points)
    assert hit is not None
    assert hit["name"] == "Umspannwerk Röhrsdorf"
    assert match_station("xyz", points) is None


def test_st_peter_follows_the_other_end():
    points = [
        {"name": "Umspannwerk St. Peter", "lon": 8.651, "lat": 54.306, "kind": "substation"},
        {"name": "Station Sankt Peter", "lon": 6.802, "lat": 51.125, "kind": "substation"},
        {"name": "Station Sankt Peter", "lon": 6.801, "lat": 51.123, "kind": "substation"},
        {"name": "Station Norf", "lon": 6.773, "lat": 51.147, "kind": "substation"},
        {"name": "Umspannwerk Pirach", "lon": 12.748, "lat": 48.148, "kind": "substation"},
    ]
    assert match_station("St. Peter", points) is None
    nrw_a, nrw_b = match_line_ends("Norf", "St. Peter", points)
    assert nrw_a is not None and nrw_b is not None
    assert abs(nrw_b["lat"] - 51.125) < 0.02
    at_a, at_b = match_line_ends("Pirach", "St. Peter", points)
    assert at_b is not None
    assert abs(at_b["lat"] - 48.263) < 0.01 and abs(at_b["lon"] - 13.089) < 0.01


def test_line_between_stations_picks_connecting_line():
    a = {"lon": 12.88, "lat": 50.86, "name": "Röhrsdorf"}
    b = {"lon": 13.33, "lat": 50.35, "name": "Hradec"}
    lines = [
        {"name": "other", "coords": [[8.0, 50.0], [8.1, 50.1]]},
        {"name": "Röhrsdorf-Hradec", "coords": [[12.88, 50.86], [13.10, 50.60], [13.33, 50.35]]},
    ]
    hit = line_between_stations(lines, a, b)
    assert hit is not None
    assert hit["name"] == "Röhrsdorf-Hradec"


def test_attach_grid_uses_osm_line_or_straight():
    key = compact_eic("11TD8L551------X")
    table = {
        key: {
            "key": key,
            "eic": "11TD8L551------X",
            "name": "Röhrsdorf - Hradec",
            "tso": "50Hertz",
            "type": "Line",
            "sub_from": "Röhrsdorf",
            "sub_to": "Hradec",
            "kv": 380,
            "nominal_mw": 1600,
        }
    }
    events = [{
        "doc_type": "A78",
        "reason": "Störung",
        "asset_eic": "11TD8L551------X",
        "asset_name": "Röhrsdorf-Hradec",
    }]
    points = [
        {"name": "Umspannwerk Röhrsdorf", "lon": 12.88, "lat": 50.86, "kind": "substation"},
        {"name": "Umspannwerk Hradec", "lon": 13.33, "lat": 50.35, "kind": "substation"},
    ]
    lines = [{
        "name": "Röhrsdorf-Hradec 380",
        "kv": 380,
        "coords": [[12.88, 50.86], [13.10, 50.60], [13.33, 50.35]],
        "op_tso": "50Hertz",
    }]
    matched = []
    matched_events = set()
    added = _attach_grid_elements(events, lines, points, matched, matched_events, table)
    assert added == 1
    assert matched_events == {0}
    assert matched[0]["geometry"]["type"] == "LineString"
    assert len(matched[0]["geometry"]["coordinates"]) >= 3
    assert matched[0]["properties"]["ev"] == [0]

    matched2 = []
    seen2 = set()
    added2 = _attach_grid_elements(events, [], points, matched2, seen2, table)
    assert added2 == 0
    assert matched2 == []


def test_route_stitches_segments_between_stations():
    lines = [
        {"name": "a", "kv": 380, "coords": [[6.77, 51.15], [6.78, 51.14], [6.800, 51.130]]},
        {"name": "b", "kv": 380, "coords": [[6.800, 51.130], [6.81, 51.125], [6.802, 51.125]]},
        {"name": "shortcut", "kv": 110, "coords": [[6.77, 51.15], [8.6, 54.3], [6.802, 51.125]]},
    ]
    graph = build_line_graph(lines)
    route = route_between(graph, {"lon": 6.773, "lat": 51.147}, {"lon": 6.802, "lat": 51.125})
    assert route is not None
    assert len(route) >= 4
    assert abs(route[0][0] - 6.77) < 0.01
    assert abs(route[-1][1] - 51.125) < 0.01
    assert all(pt[1] < 52 for pt in route)


def test_backfill_only_when_nominal_missing():
    key = compact_eic("11TD8L551------X")
    table = {
        key: {
            "key": key,
            "nominal_mw": 1645.4,
            "kv": 380,
            "type": "Line",
        }
    }
    events = [
        {"doc_type": "A78", "asset_eic": "11TD8L551------X", "nominal_mw": None, "voltage": "unbekannt"},
        {"doc_type": "A78", "asset_eic": "11TD8L551------X", "nominal_mw": 900, "voltage": "380 kV"},
        {"doc_type": "A77", "asset_eic": "11TD8L551------X", "nominal_mw": None, "voltage": "unbekannt"},
    ]
    n_nom, n_v, n_name = backfill_from_grid(events, table)
    assert n_nom == 1
    assert n_v == 1
    assert n_name == 0
    assert events[0]["nominal_mw"] == 1645.4
    assert events[0]["voltage"] == "380 kV"
    assert events[1]["nominal_mw"] == 900
    assert events[2]["nominal_mw"] is None


def _sgm_row(name, mw, kv=380, sub_from="", sub_to="", eic=""):
    return {
        "eic": eic, "key": compact_eic(eic) or name, "name": name,
        "tso": "TenneT", "type": "Line", "sub_from": sub_from, "sub_to": sub_to,
        "kv": kv, "nominal_mw": mw,
    }


def test_grid_name_match_by_circuit_number():
    index = grid_name_index([
        _sgm_row("Hamburg/Nord - Brunsbüttel 951", 2369.45, sub_from="Hamburg/Nord", sub_to="Brunsbüttel"),
        _sgm_row("Hamburg/Nord - Brunsbüttel 952", 2369.45),
    ])
    hit = _grid_name_match("Brunsbüttel - Hamburg/Nord - Brunsbüttel 951", "unbekannt", index)
    assert hit is not None and hit["name"].endswith(" 951")
    hit2 = _grid_name_match("50H-TTG/380/BRUNSBUETTEL 50H-BRUNSBUETTEL TTG-HAMBURG N 50H/952", "380 kV", index)
    assert hit2 is not None and hit2["name"].endswith(" 952")


def test_grid_name_match_prefix_tokens_single_digit():
    index = grid_name_index([
        _sgm_row("Y-Borken (-Waldeck - Twistetal) 2", 2550.45),
        _sgm_row("Y-Borken (-Waldeck - Twistetal) 3", 2550.45),
    ])
    hit = _grid_name_match("L-380-BORK-WALD-TWIS-3", "380 kV", index)
    assert hit is not None and hit["name"].endswith(") 3")
    assert _grid_name_match("L-D8-D8-577", "unbekannt", index) is None


def test_grid_name_match_y_line_prefers_first_token():
    index = grid_name_index([
        _sgm_row("Y-Kruen (-Oberbrunn - Silz) 251", 475.171, kv=220),
        _sgm_row("Y-Oberbrunn (-Kruen - Silz) 251", 809.353, kv=220),
    ])
    hit = _grid_name_match("Oberbrunn-Kruen - Silz 251/416", "unbekannt", index)
    assert hit is not None
    assert hit["name"].startswith("Y-Oberbrunn")
    assert hit["nominal_mw"] == 809.353


def test_backfill_via_name_when_eic_unknown():
    row = _sgm_row("Hamburg/Nord - Brunsbüttel 951", 2369.45, eic="11TD8L951------O")
    table = {row["key"]: row}
    index = grid_name_index([row])
    events = [{
        "doc_type": "A78",
        "asset_eic": "10T-AT-DE-00019T",
        "asset_name": "Brunsbüttel - Hamburg/Nord - Brunsbüttel 951",
        "nominal_mw": None,
        "voltage": "unbekannt",
    }]
    n_nom, n_v, n_name = backfill_from_grid(events, table, index)
    assert (n_nom, n_name) == (1, 1)
    assert events[0]["nominal_mw"] == 2369.45
    assert events[0]["voltage"] == "380 kV"


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
