"""Reparse literal XLSX cells; column C is checked as input, never used as history."""

from pathlib import PurePosixPath
import re
from xml.etree import ElementTree as ET
import zipfile

from .common import day, digest, instant, json_text, read_json, require
from .temporal import event

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
SZSE_URL = "https://www.szse.cn/api/report/ShowReport?SHOWTYPE=xlsx&CATALOGID=SSGSGMXX&TABKEY=tab2"


def read_name_cells(path):
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        require(len({i.filename for i in entries}) == len(entries), "duplicate XLSX member")
        require(sum(i.file_size for i in entries) <= 32_000_000, "oversized XLSX")
        for item in entries:
            name = PurePosixPath(item.filename)
            require(
                not name.is_absolute()
                and ".." not in name.parts
                and "\\" not in item.filename
                and ":" not in item.filename,
                "unsafe XLSX member",
            )

        def xml(name):
            body = archive.read(name)
            require(b"<!DOCTYPE" not in body and b"<!ENTITY" not in body, "XML entities rejected")
            return ET.fromstring(body)

        workbook = xml("xl/workbook.xml")
        sheets = workbook.findall("m:sheets/m:sheet", NS)
        require(len(sheets) == 1 and sheets[0].get("name") == "简称变更", "unexpected name sheet")
        relations = xml("xl/_rels/workbook.xml.rels")
        matches = [r for r in relations if r.get("Id") == sheets[0].get(f"{{{REL}}}id")]
        require(
            len(matches) == 1 and matches[0].get("TargetMode") != "External",
            "invalid sheet relationship",
        )
        target = matches[0].get("Target", "")
        require(
            target in {"worksheets/sheet1.xml", "/xl/worksheets/sheet1.xml"},
            "unexpected name sheet target",
        )
        strings = []
        if "xl/sharedStrings.xml" in archive.namelist():
            strings = ["".join(s.itertext()) for s in xml("xl/sharedStrings.xml")]
        rows = []
        for row in xml("xl/worksheets/sheet1.xml").findall("m:sheetData/m:row", NS):
            index, cells = int(row.attrib["r"]), {}
            require(index == len(rows) + 1, "non-contiguous/duplicate name row")
            for cell in row.findall("m:c", NS):
                address = cell.attrib["r"]
                require(
                    re.fullmatch(f"[A-E]{index}", address) and address not in cells,
                    "unexpected/duplicate name cell",
                )
                require(cell.find("m:f", NS) is None, "formula name cell rejected")
                kind = cell.get("t")
                if kind == "inlineStr":
                    value = "".join(cell.find("m:is", NS).itertext())
                elif kind == "s":
                    idx = int(cell.findtext("m:v", namespaces=NS))
                    require(0 <= idx < len(strings), "invalid shared string")
                    value = strings[idx]
                else:
                    raise ValueError("name/date/code cells must be literal strings")
                cells[address] = value
            require(set(cells) == {f"{c}{index}" for c in "ABCDE"}, "incomplete name row")
            rows.append({"row": index, "cells": cells})
    require(
        rows
        and list(rows[0]["cells"].values())
        == ["变更日期", "证券代码", "证券简称", "变更前简称", "变更后简称"],
        "name headers differ",
    )
    return rows


def parse_names(paths, selection, symbols, missing_symbols, start, end):
    raw = paths[selection["xlsx"]]
    meta = read_json(paths[selection["metadata"]])
    require(
        meta.get("url") == meta.get("final_url") == SZSE_URL
        and meta.get("status") == 200
        and meta.get("transfer_complete") is True
        and not meta.get("error")
        and not meta.get("exception"),
        "name source status/identity",
    )
    require(
        meta["sha256"] == digest(raw)
        and meta["bytes"] == raw.stat().st_size
        and int(meta["headers"]["Content-Length"]) == meta["bytes"],
        "name transfer differs",
    )
    observed = instant(meta["captured_at_utc"])
    require(instant(meta["started_at_utc"]) <= observed, "name observation order")
    cells = read_name_cells(raw)
    require(cells == read_json(paths[selection["cells"]]), "saved name cells differ from XLSX")
    require(
        meta["workbook"][0]["cells_sha256"] == digest(paths[selection["cells"]])
        and meta["workbook"][0]["row_count"] == len(cells),
        "name workbook metadata differs",
    )
    selected, ignored_future = [], []
    for item in cells[1:]:
        n, values = item["row"], item["cells"]
        code = values[f"B{n}"]
        require(re.fullmatch(r"\d{6}", code), "invalid XLSX security code")
        symbol, effective = f"{code}.SZ", day(values[f"A{n}"])
        if symbol not in symbols:
            continue
        if effective > end:
            ignored_future.append({"symbol": symbol, "date": effective, "row": n})
            continue
        before, after = values[f"D{n}"], values[f"E{n}"]
        selected.append(
            {
                "symbol": symbol,
                "change_date": effective.isoformat(),
                "before_name": before,
                "after_name": after,
                "before_contains_st": "ST" in before.upper(),
                "after_contains_st": "ST" in after.upper(),
                "row": n,
                "sheet": "简称变更",
                "cells": {
                    "change_date": f"A{n}",
                    "symbol": f"B{n}",
                    "before_name": f"D{n}",
                    "after_name": f"E{n}",
                },
                "among_100_missing_state_codes": symbol in missing_symbols,
                "historical_available_time": None,
            }
        )
    old = read_json(paths[selection["extraction"]])

    def ordered(rows):
        return sorted(rows, key=lambda r: (r["symbol"], r["change_date"], r["row"]))

    require(ordered(selected) == ordered(old["events"]), "selected name extraction differs")
    latest = {}
    for row in ordered(selected):
        if day(row["change_date"]) <= start:
            latest[row["symbol"]] = row
    expected = [
        {
            "symbol": symbol,
            "last_observed_change": latest.get(symbol),
            "continuity_since_change_verified": False,
            "certified_historical_st_state": None,
        }
        for symbol in sorted(s for s in symbols if s.endswith(".SZ"))
    ]
    require(old["start_references"] == expected, "name start reference differs")
    events = [
        event(
            symbol=r["symbol"],
            kind="name_change",
            effective_date=r["change_date"],
            effective_session="date_only",
            before_name=r["before_name"],
            after_name=r["after_name"],
            before_contains_st=r["before_contains_st"],
            after_contains_st=r["after_contains_st"],
            source="szse_name_change_report",
            source_url=SZSE_URL,
            observed_at_utc=observed,
            locator_json=json_text(
                {
                    "raw_path": str(raw),
                    "raw_sha256": digest(raw),
                    "sheet": r["sheet"],
                    "row": r["row"],
                    "cells": r["cells"],
                }
            ),
        )
        for r in ordered(selected)
    ]
    covered = {r["symbol"] for r in events}
    return events, {
        "raw_rows_checked": len(cells) - 1,
        "events": len(events),
        "selected_sz_symbols": sum(s.endswith(".SZ") for s in symbols),
        "symbols_with_name_reference": len(covered),
        "sz_symbols_without_event": sorted(
            s for s in symbols if s.endswith(".SZ") and s not in covered
        ),
        "selected_future_name_events_excluded": ignored_future,
        "observed_at_utc": observed,
        "historical_availability": "unknown",
        "current_name_column": "C excluded from history",
        "continuity_verified": False,
        "certified_st": None,
    }
