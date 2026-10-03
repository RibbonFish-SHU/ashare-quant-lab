"""Bind reviewed event clauses to PDF text, exact search records and source metadata."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import subprocess

from .common import day, digest, file_ref, instant, json_text, read_json, require
from .temporal import event

SEARCH_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
DATE = r"(\d{4})年(\d{1,2})月(\d{1,2})日"
CLAUSE = re.compile(
    r"本公司股票将于" + DATE + r"（星期[一二三四五六日]）(上午|下午)开市起(停牌|复牌)"
)


def compact(text):
    return "".join(text.split())


def pdf_text(path, executable):
    tool = Path(executable).resolve()
    require(tool.is_file(), "explicit local pdftotext executable required")
    require(Path(path).read_bytes().startswith(b"%PDF-"), "not a PDF original")
    version = subprocess.run([str(tool), "-v"], capture_output=True, timeout=15, check=True)
    result = subprocess.run(
        [str(tool), "-enc", "UTF-8", str(path), "-"], capture_output=True, timeout=30, check=True
    )
    pages = [t for t in result.stdout.decode("utf-8").split("\f") if t.strip()]
    require(bool(pages), "empty PDF text")
    return pages, {
        **file_ref(tool),
        "version": (version.stdout + version.stderr).decode("utf-8").strip(),
        "mode": "UTF-8 default reading order; whitespace-only normalization",
        "stderr": result.stderr.decode("utf-8"),
    }


def metadata(path, raw, *, url):
    value = read_json(path)
    require(
        value.get("complete") is True
        and value.get("status") == 200
        and not value.get("error")
        and not value.get("exception")
        and value.get("url") == url,
        "announcement source status/identity",
    )
    require(
        value["sha256"] == digest(raw) and value["bytes"] == raw.stat().st_size,
        "announcement raw binding differs",
    )
    require(
        instant(value["started_at_utc"]) <= instant(value["completed_at_utc"]),
        "announcement observation order",
    )
    return value


def validate_claim(pages, claim):
    """A reviewed quote must describe this event, not a prior halt restated in a resume notice."""
    require(claim["kind"] in {"halt", "resume"}, "invalid announcement event kind")
    require(
        {"halt": "停牌", "resume": "复牌"}[claim["kind"]] in claim["title"],
        "reviewed action differs from announcement title",
    )
    require(
        1 <= claim["body_page"] <= len(pages) and 1 <= claim["signature_page"] == len(pages),
        "invalid announcement page",
    )
    normalized = [compact(p) for p in pages]
    require(
        f"证券代码：{claim['symbol'][:6]}" in normalized[0]
        and f"公告编号：{claim['announcement_number']}" in normalized[0]
        and compact(claim["title"]) in normalized[0],
        "PDF security/title/number mismatch",
    )
    quote = compact(claim["body_quote"])
    require(
        quote in normalized[claim["body_page"] - 1], "reviewed event quote absent from PDF page"
    )
    match = CLAUSE.fullmatch(quote)
    require(match is not None, "reviewed event quote has unsupported date/session syntax")
    year, month, d, session, action = match.groups()
    effective = day(f"{int(year):04}-{int(month):02}-{int(d):02}")
    require(
        effective == day(claim["effective_date"]), "body event date differs from reviewed claim"
    )
    require(
        claim["effective_session"] == {"上午": "morning_open", "下午": "afternoon_open"}[session]
        and claim["kind"] == {"停牌": "halt", "复牌": "resume"}[action],
        "body event action/session differs from reviewed claim",
    )
    signature = compact(claim["signature_quote"])
    require(normalized[-1].endswith(signature), "reviewed signature is not PDF final date")
    dates = re.findall(DATE, signature)
    require(len(dates) == 1, "ambiguous signature date")
    y, m, d = map(int, dates[0])
    signature_day = day(f"{y:04}-{m:02}-{d:02}")
    require(signature_day == day(claim["signature_date"]), "signature date differs")
    conflict = signature_day.year != effective.year
    require(claim["signature_date_conflict"] is conflict, "signature year conflict hidden")
    return effective, signature_day, conflict


def parse_announcement(paths, selection, symbols, start, end, executable):
    claim = selection["reviewed_claim"]
    require(claim["symbol"] in symbols, "announcement outside original scope")
    require(start <= day(claim["effective_date"]) <= end, "announcement outside requested year")
    raw, search = paths[selection["pdf"]], paths[selection["search"]]
    announcement_id = claim["announcement_id"]
    url = f"https://static.cninfo.com.cn/{claim['adjunct_url']}"
    require(
        re.fullmatch(
            r"finalpage/\d{4}-\d{2}-\d{2}/" + announcement_id + r"\.PDF", claim["adjunct_url"]
        ),
        "invalid announcement asset URL",
    )
    meta = metadata(paths[selection["metadata"]], raw, url=url)
    require(
        meta.get("content_type") == "application/pdf"
        and meta.get("payload", {}).get("announcement_id") == announcement_id,
        "PDF metadata identity differs",
    )
    search_meta = metadata(paths[selection["search_metadata"]], search, url=SEARCH_URL)
    require(
        search_meta["payload"]["stock"].split(",")[0] == claim["symbol"][:6],
        "search requested stock differs",
    )
    rows = read_json(search)["announcements"]
    matches = [r for r in rows if r["announcementId"] == announcement_id]
    require(len(matches) == 1, "missing/duplicate selected search announcement")
    record = matches[0]
    require(
        record["secCode"] == claim["symbol"][:6]
        and record["announcementTitle"] == claim["title"]
        and record["adjunctUrl"] == claim["adjunct_url"]
        and record["announcementTime"] == claim["source_timestamp_ms"],
        "search/PDF announcement identity differs",
    )
    pages, tool = pdf_text(raw, executable)
    extracted = read_json(paths[selection["extraction"]])
    if isinstance(extracted, dict):
        require(extracted["pdf_sha256"] == digest(raw), "saved PDF extraction digest differs")
        extracted = extracted["pages"]
    require(len(extracted) == len(pages), "PDF extraction page count differs")
    require(
        all(
            r["page"] == i and compact(r["text"]) == compact(text)
            for i, (r, text) in enumerate(zip(extracted, pages), 1)
        ),
        "saved PDF extraction differs from original",
    )
    effective, signature, conflict = validate_claim(pages, claim)
    timestamp = datetime.fromtimestamp(record["announcementTime"] / 1000, timezone.utc)
    precision = claim["source_timestamp_precision"]
    require(
        precision in {"minute_label_unverified", "date_label"}, "unreviewed source time precision"
    )
    require(
        precision != "date_label"
        or timestamp.astimezone(timezone(timedelta(hours=8))).time().isoformat() == "00:00:00",
        "date label is not at source midnight",
    )
    result = event(
        symbol=claim["symbol"],
        kind=claim["kind"],
        effective_date=effective,
        effective_session=claim["effective_session"],
        source="cninfo_issuer_announcement",
        source_url=url,
        observed_at_utc=meta["completed_at_utc"],
        signature_date=signature,
        signature_date_conflict=conflict,
        source_timestamp=timestamp.isoformat(),
        source_timestamp_precision=precision,
        locator_json=json_text(
            {
                "raw_path": str(raw),
                "raw_sha256": digest(raw),
                "body_page": claim["body_page"],
                "body_quote": claim["body_quote"],
                "signature_page": claim["signature_page"],
                "signature_quote": claim["signature_quote"],
                "search_path": str(search),
                "search_sha256": digest(search),
                "search_record": record,
                "source_timestamp_ms": record["announcementTime"],
                "search_observed_at_utc": search_meta["completed_at_utc"],
                "review": claim["review_basis"],
            }
        ),
    )
    return result, tool
