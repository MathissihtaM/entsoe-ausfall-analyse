from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from src.fetch_outages import MAX_OFFSET, PAGE_SIZE, _parse_period, _stem_capped, _stem_progress, window_state


def _dt(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _zip_xmls(n: int) -> bytes:
    buf = BytesIO()
    with ZipFile(buf, "w") as zf:
        for i in range(n):
            zf.writestr(f"{i}.xml", "<a/>")
    return buf.getvalue()


def test_parse_period_rejects_sibling_prefix():
    name = "A78_DE_LU_AT_202110110000_202209260000_off0000.zip"
    assert _parse_period(name, "A78_DE_LU") is None
    parsed = _parse_period(name, "A78_DE_LU_AT")
    assert parsed is not None
    assert parsed[0] == _dt("2021-10-11")


def test_later_chunk_not_covered_by_first_window(tmp_path: Path, monkeypatch):
    from src import fetch_outages

    monkeypatch.setattr(fetch_outages, "RAW_DIR", tmp_path)
    folder = tmp_path / "DE"
    folder.mkdir()
    (folder / "A77_DE_LU_202110110000_202209260000_off0000.zip").write_bytes(_zip_xmls(3))
    first = window_state("DE", "A77_DE_LU", _dt("2021-10-11"), _dt("2022-09-26"))
    later = window_state("DE", "A77_DE_LU", _dt("2022-09-26"), _dt("2023-09-11"))
    assert first == "ok"
    assert later == "missing"


def test_near_equal_timestamps_count_as_same_window(tmp_path: Path, monkeypatch):
    from src import fetch_outages

    monkeypatch.setattr(fetch_outages, "RAW_DIR", tmp_path)
    folder = tmp_path / "DE"
    folder.mkdir()
    (folder / "A79_DE_LU_202110100611_202209250611_off0000.empty").write_text("no-data\n")
    state = window_state("DE", "A79_DE_LU", _dt("2021-10-11"), _dt("2022-09-26"))
    assert state == "empty"


def test_full_page_before_max_is_incomplete(tmp_path: Path, monkeypatch):
    from src import fetch_outages

    monkeypatch.setattr(fetch_outages, "RAW_DIR", tmp_path)
    folder = tmp_path / "DE"
    folder.mkdir()
    stem = "A77_DE_TENNET_202309110000_202408260000"
    (folder / f"{stem}_off0000.zip").write_bytes(_zip_xmls(PAGE_SIZE))
    (folder / f"{stem}_off2800.zip").write_bytes(_zip_xmls(PAGE_SIZE))
    assert _stem_progress("DE", stem) == "incomplete"
    state = window_state("DE", "A77_DE_TENNET", _dt("2023-09-11"), _dt("2024-08-26"))
    assert state == "incomplete"


def test_full_last_page_is_capped(tmp_path: Path, monkeypatch):
    from src import fetch_outages

    monkeypatch.setattr(fetch_outages, "RAW_DIR", tmp_path)
    folder = tmp_path / "DE"
    folder.mkdir()
    stem = "A77_DE_LU_202110110000_202209260000"
    for offset in range(0, MAX_OFFSET + PAGE_SIZE, PAGE_SIZE):
        (folder / f"{stem}_off{offset:04d}.zip").write_bytes(_zip_xmls(PAGE_SIZE))
    assert _stem_capped("DE", stem)
    state = window_state("DE", "A77_DE_LU", _dt("2021-10-11"), _dt("2022-09-26"))
    assert state == "capped"
