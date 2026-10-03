"""Reproduce the Jan 2023 baseline contradiction using CSI announcement 14497."""

import argparse
from html.parser import HTMLParser
import json
from pathlib import Path

from ashare_lab.data.pipeline import load_run
from ashare_lab.data.queries import symbol
from ashare_lab.data.raw import digest, save_json, utc_now


class Tables(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables = []
        self.rows = self.cells = self.value = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.rows = []
        elif tag == "tr" and self.rows is not None:
            self.cells = []
        elif tag in {"td", "th"} and self.cells is not None:
            self.value = ""

    def handle_data(self, data):
        if self.value is not None:
            self.value += data

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.value is not None:
            self.cells.append(self.value.strip())
            self.value = None
        elif tag == "tr" and self.cells is not None:
            self.rows.append(self.cells)
            self.cells = None
        elif tag == "table" and self.rows is not None:
            self.tables.append(self.rows)
            self.rows = None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--announcement", type=Path, required=True)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--original-plan", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("evidence is immutable")
    events = json.loads(args.events.read_text(encoding="utf-8"))["events"]
    event = next(e for e in events if e["id"] == "csi-14497")
    assert digest(args.announcement) == event["source_sha256"]
    doc = json.loads(args.announcement.read_text(encoding="utf-8"))["data"]
    assert doc["id"] == 14497 and doc["publishDate"] == event["publish_date"]
    assert "沪深300" in doc["title"]
    tables = Tables()
    tables.feed(doc["content"])
    pairs = [r for r in tables.tables[0] if len(r) == 4 and len(r[0]) == 6 and r[0].isdigit()]
    assert len(pairs) == 15

    def as_symbol(number):
        return symbol(("sh." if number.startswith("6") else "sz.") + number)

    removed, added = {as_symbol(r[0]) for r in pairs}, {as_symbol(r[2]) for r in pairs}
    assert removed == set(event["removed"]) and added == set(event["added"])
    records, _, _ = load_run(args.run, original_plans=args.original_plan)
    record = next(
        r
        for r in records
        if r["api"] == "query_hs300_stocks" and r["parameters"]["date"] == "2023-01-03"
    )
    field = record["fields"].index("code")
    actual = {symbol(row[field]) for row in record["rows"]}
    retained, missing = sorted(removed & actual), sorted(added - actual)
    result = {
        "verified_at_utc": utc_now(),
        "announcement": event,
        "announcement_path": str(args.announcement.resolve()),
        "raw_locator": record["raw_locator"],
        "query_id": record["query_id"],
        "query_date": "2023-01-03",
        "rows": len(record["rows"]),
        "unique_codes": len(actual),
        "source_update_dates": sorted(
            {r[record["fields"].index("updateDate")] for r in record["rows"]}
        ),
        "missing_added": missing,
        "retained_removed": retained,
        "status": "fail" if retained or missing else "pass_selected_event_only",
        "research_eligible": False,
    }
    save_json(args.output, result)
    print(
        json.dumps(
            {
                "status": result["status"],
                "missing_added": len(missing),
                "retained_removed": len(retained),
            }
        )
    )
    return 2 if result["status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
