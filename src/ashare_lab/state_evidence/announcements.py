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
CONTINUOUS_HALT = re.compile(r"公司A股股票自" + DATE + r"开市起连续停牌")
OPEN_RESUME = re.compile(r"公司A股股票将于" + DATE + r"开市起复牌")
RIGHTS_HALT = re.compile(
    DATE
    + r"至"
    + DATE
    + r"（T\+1日至T\+5日）为本次A股配股缴款期，缴款期公司A股股票全天停牌；"
    + DATE
    + r"（T\+6日）为登记公司网上清算期，公司A股股票继续停牌一天"
)
RIGHTS_RESUME = re.compile(r"自" + DATE + r"开市起，([^，。；]+)A股股票复牌并恢复交易")


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


def parsed_day(parts):
    y, m, d = map(int, parts)
    return day(f"{y:04}-{m:02}-{d:02}")


def body_event(quote, claim, pdf_title):
    """Recognize reviewed issuer clauses, never an estimated resumption or a dividend."""
    form = claim.get("clause_format", "session_notice")
    patterns = {
        "session_notice": CLAUSE,
        "request_rights_continuous_halt": CONTINUOUS_HALT,
        "open_resume_notice": OPEN_RESUME,
        "rights_subscription_halt": RIGHTS_HALT,
        "rights_result_resume": RIGHTS_RESUME,
    }
    require(form in patterns, "unsupported announcement clause format")
    if form == "request_rights_continuous_halt":
        title_ok = claim["kind"] == "halt" and pdf_title.endswith("异议股东收购请求权申报公告")
    elif form == "rights_result_resume":
        title_ok = claim["kind"] == "resume" and pdf_title.endswith("A股配股发行结果公告")
    elif form == "open_resume_notice":
        title_ok = claim["kind"] == "resume" and pdf_title.endswith("关于公司股票复牌的提示性公告")
    else:
        title_ok = {"halt": "停牌", "resume": "复牌"}[claim["kind"]] in pdf_title
        # The combined title “停复牌” does not contain the uninterrupted word “停牌”.
        if form == "rights_subscription_halt":
            title_ok = claim["kind"] == "halt" and pdf_title.endswith("A股股票停复牌提示性公告")
    require(title_ok, "reviewed action differs from announcement title")
    match = patterns[form].fullmatch(quote)
    require(match is not None, "reviewed event quote has unsupported date/session syntax")
    values = match.groups()
    effective = parsed_day(values[:3])
    session = "morning_open"
    if form == "session_notice":
        session = {"上午": "morning_open", "下午": "afternoon_open"}[values[3]]
        kind = {"停牌": "halt", "复牌": "resume"}[values[4]]
    elif form in {"request_rights_continuous_halt", "rights_subscription_halt"}:
        kind = "halt"
        if form == "rights_subscription_halt":
            require(
                effective <= parsed_day(values[3:6]) < parsed_day(values[6:9]),
                "invalid rights subscription/clearing dates",
            )
            # This records only the start. A separate confirmed notice must close the halt.
    else:
        kind = "resume"
        if form == "rights_result_resume":
            require(values[3] == claim.get("security_name"), "resume issuer differs")
    require(
        effective == day(claim["effective_date"]), "body event date differs from reviewed claim"
    )
    require(
        claim["effective_session"] == session and claim["kind"] == kind,
        "body event action/session differs from reviewed claim",
    )
    return effective


def without_page_number(text, number):
    """Only strip the exact standalone printed page number, not other trailing digits."""
    return re.sub(rf"(?m)^[ \t]*{number}[ \t]*\r?$", "", text)


def validate_claim(pages, claim):
    """Bind the event, title and signature; appendices must be explicit no-body stamp pages."""
    require(claim["kind"] in {"halt", "resume"}, "invalid announcement event kind")
    signature_page = claim["signature_page"]
    require(
        1 <= claim["body_page"] <= signature_page <= len(pages),
        "invalid announcement page",
    )
    normalized = [compact(p) for p in pages]
    pdf_title = compact(claim.get("pdf_title", claim["title"]))
    short_name = compact(claim.get("security_name", ""))
    title = compact(claim["title"])
    require(
        bool(pdf_title) and title in {pdf_title, short_name + pdf_title},
        "search/PDF title prefix differs",
    )
    # Restrict identity checks to the header, so a cited prior notice cannot supply them.
    header = normalized[0].split(pdf_title, 1)[0]
    require(
        re.search(r"(?:证券|股票)代码：" + re.escape(claim["symbol"][:6]) + r"(?!\d)", header)
        and re.search(
            r"(?:公告)?编号：" + re.escape(claim["announcement_number"]) + r"(?![\d-])", header
        )
        and pdf_title in normalized[0],
        "PDF security/title/number mismatch",
    )
    if short_name:
        require(
            any(f"{label}简称：{short_name}" in header for label in ("证券", "股票")),
            "PDF security short name differs",
        )
    quote = compact(claim["body_quote"])
    require(
        quote in normalized[claim["body_page"] - 1], "reviewed event quote absent from PDF page"
    )
    body = normalized[claim["body_page"] - 1]
    require(body.count(quote) == 1, "ambiguous reviewed event quote")
    location = body.index(quote)
    prefix = re.split(r"[。；！？]", body[:location])[-1]
    require(
        not re.search(r"预计|原定|拟定|计划于|暂拟|暂定", prefix + quote),
        "estimated/planned clause cannot confirm an event",
    )
    if claim["kind"] == "resume":
        # These reviewed resumption forms end an affirmative sentence. Do not accept
        # a fragment before a conditional, cancelled or postponed suffix.
        suffix = body[location + len(quote) :]
        require(not suffix or suffix.startswith("。"), "resume quote must end the sentence")
        require(
            not re.search(r"取消|不再|不予|未能|延期|可能|有望|争取|待|如|若|计划", prefix),
            "conditional/cancelled context cannot confirm resumption",
        )
    effective = body_event(quote, claim, pdf_title)
    signature = compact(claim["signature_quote"])
    require(
        compact(without_page_number(pages[signature_page - 1], signature_page)).endswith(signature),
        "reviewed signature is not PDF final body date",
    )
    stamp_page = re.compile(
        r"（(?:本|此)页无正文，为《" + re.escape(pdf_title) + r"》之盖章页）"
        r"(?:发行人|保荐人（联席主承销商）|联席主承销商)："
        r"[\u4e00-\u9fff（）()]+(?:有限公司|有限责任公司)年月日"
    )
    for number in range(signature_page + 1, len(pages) + 1):
        require(
            stamp_page.fullmatch(compact(without_page_number(pages[number - 1], number)))
            is not None,
            "unsupported text after signature page; only no-body stamp pages allowed",
        )
    dates = re.findall(DATE, signature)
    require(len(dates) == 1, "ambiguous signature date")
    signature_day = parsed_day(dates[0])
    require(signature_day == day(claim["signature_date"]), "signature date differs")
    if claim.get("clause_format") in {"rights_result_resume", "open_resume_notice"}:
        require(signature_day == effective, "selected notice must confirm same-day resume")
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
    if claim.get("security_name"):
        require(record.get("secName") == claim["security_name"], "search security name differs")
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
                "clause_format": claim.get("clause_format", "session_notice"),
                "pdf_title": claim.get("pdf_title", claim["title"]),
                "trailing_stamp_pages": list(range(claim["signature_page"] + 1, len(pages) + 1)),
            }
        ),
    )
    return result, tool
