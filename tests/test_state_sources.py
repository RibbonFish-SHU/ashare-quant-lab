"""Original-byte parsers tested with synthetic XLSX/PDF/metadata, no ignored real inputs."""

from datetime import date
import json
from pathlib import Path
import shutil
from xml.sax.saxutils import escape
import zipfile

import pytest

from ashare_lab.state_evidence.announcements import parse_announcement, pdf_text, validate_claim
from ashare_lab.state_evidence.common import digest, file_ref, read_json, save_json
from ashare_lab.state_evidence.inputs import (
    calendar_rows,
    load_inputs,
    rejected_state_sources,
    verify_daily_raw,
)
from ashare_lab.state_evidence.names import SZSE_URL, parse_names, read_name_cells

TOOL = shutil.which("pdftotext") or "D:/texlive/2025/bin/windows/pdftotext.exe"


def make_xlsx(path, rows, *, formula=False, duplicate=False):
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    sheet = "".join(
        '<row r="%s">%s</row>'
        % (
            i,
            "".join(
                f'<c r="{c}{i}" t="inlineStr">'
                + ("<f>NOW()</f>" if formula and c == "A" and i == 2 else "")
                + f"<is><t>{escape(value)}</t></is></c>"
                for c, value in zip("ABCDE", values)
            ),
        )
        for i, values in enumerate(rows, 1)
    )
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(
            "xl/workbook.xml",
            f'<workbook xmlns="{ns}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="简称变更" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        z.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
        )
        z.writestr(
            "xl/worksheets/sheet1.xml",
            f'<worksheet xmlns="{ns}"><sheetData>{sheet}</sheetData></worksheet>',
        )
        if duplicate:
            z.writestr("xl/worksheets/sheet1.xml", "bad")


HEADERS = ["变更日期", "证券代码", "证券简称", "变更前简称", "变更后简称"]


@pytest.fixture
def name_bundle(tmp_path):
    paths = {k: tmp_path / k for k in ["xlsx", "metadata", "cells", "extraction"]}
    rows = [
        HEADERS,
        ["2022-01-02", "000001", "Current ST Name", "Old", "Legacy"],
        ["2026-01-01", "000001", "Current ST Name", "Legacy", "Current ST Name"],
    ]
    make_xlsx(paths["xlsx"], rows)
    cells = read_name_cells(paths["xlsx"])
    save_json(paths["cells"], cells)
    save_json(
        paths["metadata"],
        {
            "url": SZSE_URL,
            "final_url": SZSE_URL,
            "status": 200,
            "transfer_complete": True,
            "sha256": digest(paths["xlsx"]),
            "bytes": paths["xlsx"].stat().st_size,
            "headers": {"Content-Length": str(paths["xlsx"].stat().st_size)},
            "captured_at_utc": "2026-10-03T15:00:01+00:00",
            "started_at_utc": "2026-10-03T15:00:00+00:00",
            "workbook": [{"cells_sha256": digest(paths["cells"]), "row_count": 3}],
        },
    )
    e = {
        "symbol": "000001.SZ",
        "change_date": "2022-01-02",
        "before_name": "Old",
        "after_name": "Legacy",
        "before_contains_st": False,
        "after_contains_st": False,
        "row": 2,
        "sheet": "简称变更",
        "cells": {"change_date": "A2", "symbol": "B2", "before_name": "D2", "after_name": "E2"},
        "among_100_missing_state_codes": True,
        "historical_available_time": None,
    }
    save_json(
        paths["extraction"],
        {
            "events": [e],
            "start_references": [
                {
                    "symbol": s,
                    "last_observed_change": e if s == "000001.SZ" else None,
                    "continuity_since_change_verified": False,
                    "certified_historical_st_state": None,
                }
                for s in ["000001.SZ", "000002.SZ"]
            ],
        },
    )
    return paths, {k: k for k in paths}


def parse_name_bundle(bundle):
    return parse_names(
        *bundle,
        {"000001.SZ", "000002.SZ"},
        {"000001.SZ", "000002.SZ"},
        date(2023, 1, 1),
        date(2023, 12, 31),
    )


def test_current_column_does_not_contaminate_historical_st(name_bundle):
    events, quality = parse_name_bundle(name_bundle)
    assert len(events) == 1 and events[0]["after_name"] == "Legacy"
    assert events[0]["after_contains_st"] is False and events[0]["certified_st"] is None
    assert "Current ST" not in json.dumps(events, default=str)
    assert quality["sz_symbols_without_event"] == ["000002.SZ"]
    assert len(quality["selected_future_name_events_excluded"]) == 1
    assert str(events[0]["observed_at_utc"]) == "2026-10-03 15:00:01+00:00"


@pytest.mark.parametrize(
    "change", ["date", "name", "column", "missing_event", "fake_start", "fake_normal"]
)
def test_name_extraction_cannot_override_original_cells(name_bundle, change):
    paths, _ = name_bundle
    j = read_json(paths["extraction"])
    if change == "date":
        j["events"][0]["change_date"] = "2021-01-01"
    if change == "name":
        j["events"][0]["after_name"] = "Current ST Name"
    if change == "column":
        j["events"][0]["cells"]["after_name"] = "C2"
    if change == "missing_event":
        j["events"] = []
    if change == "fake_start":
        j["start_references"][0]["last_observed_change"]["change_date"] = "2000-01-01"
    if change == "fake_normal":
        j["start_references"][1]["certified_historical_st_state"] = False
    save_json(paths["extraction"], j)
    with pytest.raises(ValueError, match="name .* differs"):
        parse_name_bundle(name_bundle)


@pytest.mark.parametrize(
    "field,value",
    [
        ("transfer_complete", False),
        ("status", 500),
        ("url", "https://other.invalid"),
        ("sha256", "0" * 64),
        ("error", "failed"),
    ],
)
def test_name_bad_metadata_rejected(name_bundle, field, value):
    paths, _ = name_bundle
    m = read_json(paths["metadata"])
    m[field] = value
    save_json(paths["metadata"], m)
    with pytest.raises(ValueError):
        parse_name_bundle(name_bundle)


def test_xlsx_formula_duplicate_and_malformed_rows_rejected(tmp_path):
    path = tmp_path / "names.xlsx"
    make_xlsx(path, [HEADERS, ["2022-01-01", "000001", "Now", "Old", "New"]], formula=True)
    with pytest.raises(ValueError, match="formula"):
        read_name_cells(path)
    with pytest.warns(UserWarning):
        make_xlsx(path, [HEADERS], duplicate=True)
    with pytest.raises(ValueError, match="duplicate XLSX"):
        read_name_cells(path)
    make_xlsx(path, [HEADERS, ["2022-01-01", "000001", "Now", "Old"]])
    with pytest.raises(ValueError, match="incomplete"):
        read_name_cells(path)


def make_pdf(path, texts):
    """Tiny synthetic PDF with ToUnicode mapping; no font/package/download required."""
    chars = sorted(set("".join(texts)))
    mapping = {c: i + 1 for i, c in enumerate(chars)}
    cmap = (
        "/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
        "/CMapName /Synthetic def /CMapType 2 def\n1 begincodespacerange <0000> <FFFF> endcodespacerange\n"
        + f"{len(chars)} beginbfchar\n"
        + "\n".join(f"<{i:04X}> <{ord(c):04X}>" for c, i in mapping.items())
        + "\nendbfchar endcmap CMapName currentdict /CMap defineresource pop end end"
    ).encode()

    def stream(body):
        return b"<< /Length " + str(len(body)).encode() + b" >>\nstream\n" + body + b"\nendstream"

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",
        b"<< /Type /Font /Subtype /Type0 /BaseFont /Synthetic /Encoding /Identity-H /DescendantFonts [4 0 R] /ToUnicode 5 0 R >>",
        b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /Synthetic /CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> /DW 500 >>",
        stream(cmap),
    ]
    ids = []
    for t in texts:
        num = len(objects) + 1
        ids.append(num)
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 6000 800] /Resources << /Font << /F1 3 0 R >> >> /Contents {num + 1} 0 R >>".encode()
        )
        body = (
            "BT /F1 10 Tf 20 750 Td <" + "".join(f"{mapping[c]:04X}" for c in t) + "> Tj ET"
        ).encode()
        objects.append(stream(body))
    objects[1] = (
        f"<< /Type /Pages /Count {len(ids)} /Kids [{' '.join(str(n) + ' 0 R' for n in ids)}] >>".encode()
    )
    data = b"%PDF-1.4\n"
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    start = len(data)
    data += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    data += b"".join(f"{o:010} 00000 n \n".encode() for o in offsets)
    data += (
        f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode()
    )
    path.write_bytes(data)


def halt_claim():
    return {
        "symbol": "688065.SH",
        "kind": "halt",
        "announcement_id": "12345",
        "announcement_number": "2023-019",
        "title": "关于重大事项的停牌公告",
        "adjunct_url": "finalpage/2023-06-15/12345.PDF",
        "effective_date": "2023-06-15",
        "effective_session": "afternoon_open",
        "body_page": 1,
        "body_quote": "本公司股票将于2023年6月15日（星期四）下午开市起停牌",
        "signature_page": 1,
        "signature_quote": "2022年6月15日",
        "signature_date": "2022-06-15",
        "signature_date_conflict": True,
        "source_timestamp_ms": 1686803580000,
        "source_timestamp_precision": "minute_label_unverified",
        "review_basis": "synthetic fixture only",
    }


def claim_text(c):
    return (
        "证券代码：688065 公告编号：2023-019 "
        + c["title"]
        + " "
        + c["body_quote"]
        + " "
        + c["signature_quote"]
    )


@pytest.fixture
def announcement_bundle(tmp_path):
    if not Path(TOOL).is_file():
        pytest.skip("Poppler needed for actual synthetic PDF extraction")
    paths = {
        k: tmp_path / k for k in ["pdf", "metadata", "search", "search_metadata", "extraction"]
    }
    c = halt_claim()
    text = claim_text(c)
    make_pdf(paths["pdf"], [text])
    save_json(
        paths["search"],
        {
            "announcements": [
                {
                    "secCode": "688065",
                    "announcementId": "12345",
                    "announcementTitle": c["title"],
                    "adjunctUrl": c["adjunct_url"],
                    "announcementTime": c["source_timestamp_ms"],
                }
            ]
        },
    )
    for role, raw, url, payload in [
        (
            "metadata",
            "pdf",
            "https://static.cninfo.com.cn/" + c["adjunct_url"],
            {"announcement_id": "12345"},
        ),
        (
            "search_metadata",
            "search",
            "https://www.cninfo.com.cn/new/hisAnnouncement/query",
            {"stock": "688065,org"},
        ),
    ]:
        save_json(
            paths[role],
            {
                "complete": True,
                "status": 200,
                "bytes": paths[raw].stat().st_size,
                "sha256": digest(paths[raw]),
                "url": url,
                "payload": payload,
                "content_type": "application/pdf" if raw == "pdf" else "application/json",
                "started_at_utc": "2026-10-03T15:00:00+00:00",
                "completed_at_utc": "2026-10-03T15:00:01+00:00",
            },
        )
    save_json(paths["extraction"], [{"page": 1, "text": text}])
    return paths, {**{k: k for k in paths}, "reviewed_claim": c}


def parse_pdf_bundle(bundle):
    return parse_announcement(*bundle, {"688065.SH"}, date(2023, 1, 1), date(2023, 12, 31), TOOL)


def test_real_pdf_reader_with_synthetic_unicode_and_unknown_availability(announcement_bundle):
    result, tool = parse_pdf_bundle(announcement_bundle)
    assert result["effective_session"] == "afternoon_open"
    assert result["signature_date_conflict"] is True
    assert result["signature_date"] == date(2022, 6, 15)
    assert result["available_time"] is None and result["source_timestamp"] is not None
    assert tool["sha256"] == digest(TOOL)


@pytest.mark.parametrize("change", ["symbol", "date", "session", "signature", "conflict", "page"])
def test_manual_claim_must_match_original_text(change):
    c = halt_claim()
    pages = [claim_text(c)]
    if change == "symbol":
        c["symbol"] = "688066.SH"
    if change == "date":
        c["effective_date"] = "2023-06-16"
    if change == "session":
        c["effective_session"] = "morning_open"
    if change == "signature":
        c["signature_date"] = "2023-06-15"
    if change == "conflict":
        c["signature_date_conflict"] = False
    if change == "page":
        c["body_page"] = 2
    with pytest.raises(ValueError):
        validate_claim(pages, c)


def test_prior_halt_restated_in_resume_notice_is_not_new_halt():
    c = halt_claim()
    c["title"] = "复牌的提示性公告"
    with pytest.raises(ValueError, match="action differs"):
        validate_claim([claim_text(c)], c)


@pytest.mark.parametrize(
    "change",
    [
        "failed_metadata",
        "wrong_source",
        "wrong_stock",
        "wrong_pdf_digest",
        "changed_extraction",
        "changed_raw",
    ],
)
def test_announcement_provenance_and_text_tampering(announcement_bundle, change):
    paths, _ = announcement_bundle
    if change in {"failed_metadata", "wrong_source", "wrong_pdf_digest"}:
        m = read_json(paths["metadata"])
        m[
            {"failed_metadata": "complete", "wrong_source": "url", "wrong_pdf_digest": "sha256"}[
                change
            ]
        ] = {
            "failed_metadata": False,
            "wrong_source": "https://other.invalid",
            "wrong_pdf_digest": "0" * 64,
        }[change]
        save_json(paths["metadata"], m)
    if change == "wrong_stock":
        m = read_json(paths["search"])
        m["announcements"][0]["secCode"] = "000001"
        save_json(paths["search"], m)
        md = read_json(paths["search_metadata"])
        md.update(sha256=digest(paths["search"]), bytes=paths["search"].stat().st_size)
        save_json(paths["search_metadata"], md)
    if change == "changed_extraction":
        save_json(paths["extraction"], [{"page": 1, "text": "fabricated"}])
    if change == "changed_raw":
        paths["pdf"].write_bytes(b"%PDF-invalid")
    with pytest.raises(ValueError):
        parse_pdf_bundle(announcement_bundle)


def test_raw_is_not_pdf(tmp_path):
    p = tmp_path / "x"
    p.write_bytes(b"challenge page")
    if not Path(TOOL).is_file():
        pytest.skip("Poppler executable unavailable")
    with pytest.raises(ValueError, match="not a PDF"):
        pdf_text(p, TOOL)


def test_calendar_rejects_mixed_sources_even_without_overlapping_dates():
    rows = [
        {
            "event_date": date(2023, 1, n),
            "is_open": True,
            "source": "baostock",
            "sdk": "0.9.4",
            "exchange_scope": "SSE_SZSE_provider_joint",
        }
        for n in [1, 2]
    ]
    assert len(calendar_rows(rows, date(2023, 1, 1), date(2023, 1, 2))) == 2
    rows[1]["source"] = "another"
    with pytest.raises(ValueError, match="mixed"):
        calendar_rows(rows, date(2023, 1, 1), date(2023, 1, 2))
    rows[1]["source"] = "baostock"
    with pytest.raises(ValueError, match="missing"):
        calendar_rows(rows[:1], date(2023, 1, 1), date(2023, 1, 2))


def test_pinned_input_digest_tampering_fails_before_parsing(tmp_path):
    raw = tmp_path / "original"
    raw.write_bytes(b"original")
    ref = {**file_ref(raw), "path": "original"}
    config = tmp_path / "config.json"
    save_json(
        config,
        {
            "schema_version": "state-evidence-input-v1",
            "research_eligible": False,
            "start": "2023-01-01",
            "end": "2023-12-31",
            "availability_policy": "exclude_unknown",
            "files": [ref],
        },
    )
    raw.write_bytes(b"modified")
    with pytest.raises(ValueError, match="input bytes/digest"):
        load_inputs(config, tmp_path)


def test_empty_failed_halt_source_never_certifies_normal(tmp_path):
    p = tmp_path / "response"
    save_json(p, {"result": {"data": []}})
    manifest = tmp_path / "manifest"
    save_json(
        manifest,
        {
            "records": [
                {
                    "path": "raw",
                    "bytes": p.stat().st_size,
                    "sha256": digest(p),
                    "url": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_CUSTOM_SUSPEND_DATA_INTERFACE",
                    "status": 200,
                    "date": "2023-06-15",
                    "captured_at_utc": "2026-10-03T00:00:00+00:00",
                }
            ]
        },
    )
    c = {
        "paths": {"raw": p, "manifest": manifest},
        "config": {"rejected_eastmoney": {"manifest": "manifest", "targeted": []}},
        "start": date(2023, 1, 1),
        "end": date(2023, 12, 31),
    }
    result = rejected_state_sources(c)
    assert (
        result[0]["adopted_events"] == 0
        and result[0]["reason"] == "empty_response_not_normal_state"
    )
    save_json(p, {"result": {"data": [{"SUSPEND_START_DATE": "2026-10-01 00:00:00"}]}})
    m = read_json(manifest)
    m["records"][0].update(bytes=p.stat().st_size, sha256=digest(p))
    save_json(manifest, m)
    result = rejected_state_sources(c)
    assert result[0]["adopted_events"] == 0 and result[0]["within_scope_rows"] == 0


def test_provider_failure_raw_cannot_be_promoted_by_candidate(tmp_path):
    p = tmp_path / "raw"
    save_json(
        p,
        {
            "status": "provider_error",
            "error_code": "10001011",
            "source": "baostock",
            "sdk": "0.9.4",
        },
    )
    with pytest.raises(ValueError, match="source/status"):
        verify_daily_raw(p, [])
