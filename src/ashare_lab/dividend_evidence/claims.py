"""Validate selected manual claims against original PDFs and exact reviewed clauses.

Only the reviewed dividend/proposal forms are supported. This is not a general
announcement parser and never creates executable cash or share events.
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import sys

from ashare_lab.state_evidence.announcements import pdf_text
from ashare_lab.state_evidence.common import execution_identity

from .common import EvidenceReader, day, failures, instant, integer, require, text
from .search import audit_search_run


FACT_UNITS = {
    "record_date": "date",
    "ex_date": "date",
    "cash_payment_date": "date",
    "share_listing_date": "date",
    "gross_cash_per_10_eligible_shares": "CNY_per_10_eligible_shares",
    "reported_cash_per_eligible_share": "CNY_per_eligible_share",
    "ex_price_cash_deduction_per_total_share": "CNY_per_total_share",
    "stock_bonus_per_10_old_shares": "shares_per_10_old_shares",
    "capital_reserve_conversion_per_10_old_shares": "shares_per_10_old_shares",
    "after_tax_policy": "text",
    "payment_scope": "text",
    "fractional_share_policy": "text",
    "source_numerical_discrepancy": "text",
    "proposal_status": "text",
}
DATE_TOKEN = r"([0-9]{4})年([0-9]{1,2})月([0-9]{1,2})日"
NUMBER = r"([0-9]+(?:\.[0-9]+)?)"
NUMERIC_PATTERNS = {
    "gross_cash_per_10_eligible_shares": r"每10股派" + NUMBER + r"元人民币现金（含税",
    "reported_cash_per_eligible_share": r"每股现金分红为" + NUMBER + r"元/股（含税）",
    "ex_price_cash_deduction_per_total_share": r"除权除息价格=股权登记日收盘价-" + NUMBER + r"元",
    "stock_bonus_per_10_old_shares": r"每10股送(?:红股)?" + NUMBER + r"股",
    "capital_reserve_conversion_per_10_old_shares": r"每10股转增" + NUMBER + r"股",
}
DATE_PATTERNS = {
    "record_date": r"股权登记日为[：:]?" + DATE_TOKEN,
    "ex_date": r"除权除息日为[：:]?" + DATE_TOKEN,
    "cash_payment_date": r"现金红利将于" + DATE_TOKEN,
    "share_listing_date": r"(?:新增可流通股份上市日为[：:]?|无限售条件流通股的起始交易日为)"
    + DATE_TOKEN,
}
LEGACY_PDF_SHA = "3e9580d1ed9858f7172c98d30b72452dc39458cf381969dccc53a39931a847b1"
LEGACY_META_SHA = "9ee8271b52ba5b502680d6b21da83cb7762f3ae7154d02d823cadf37ca3aca13"


def compact(value):
    return "".join(value.split())


def decimal_value(value):
    require(
        isinstance(value, str) and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value),
        "numeric claim must be a nonnegative decimal string",
    )
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("invalid decimal claim") from exc
    require(result.is_finite() and result >= 0, "non-finite/negative numeric claim")
    return result


def citations(pages, references):
    require(isinstance(references, list) and bool(references), "field needs explicit citations")
    result, seen = [], set()
    for citation in references:
        require(
            isinstance(citation, dict) and set(citation) == {"page", "quote"}, "invalid citation"
        )
        number = integer(citation["page"], "citation page", 1)
        require(number <= len(pages), "citation page outside original")
        quote = compact(text(citation["quote"], "citation quote"))
        require((number, quote) not in seen, "duplicate citation")
        require(
            compact(pages[number - 1]).count(quote) == 1,
            "quote absent or ambiguous on original page",
        )
        seen.add((number, quote))
        result.append(quote)
    return result


def _not_planned(quote):
    require(
        not re.search(r"尚需|拟(?:向|每|于)|预计|原定|暂定|取消|不再实施|延期|若|如果", quote),
        "planned/cancelled clause cannot establish implementation fact",
    )


def validate_fact(name, fact, pages):
    require(name in FACT_UNITS and isinstance(fact, dict), "unknown or invalid fact")
    require(set(fact) == {"value", "unit", "basis", "citations"}, "fact fields differ")
    require(fact["unit"] == FACT_UNITS[name], "fact unit differs from field semantics")
    text(fact["basis"], "fact basis")
    quotes = citations(pages, fact["citations"])
    quote = "".join(quotes)
    value = fact["value"]
    if fact["unit"] == "text":
        require(
            compact(text(value, "text fact")) == quote,
            "text value differs from cited source clauses",
        )
        if name == "proposal_status":
            require(
                "预案" in quote and "尚需" in quote and "股东大会" in quote,
                "proposal evidence must retain pending approval",
            )
        return dict(fact)
    _not_planned(quote)
    for citation, selected in zip(fact["citations"], quotes, strict=True):
        body = compact(pages[citation["page"] - 1])
        prefix = re.split(r"[。；！？]", body[: body.index(selected)])[-1]
        _not_planned(prefix + selected)
    if fact["unit"] == "date":
        expected = day(value, name)
        require(expected.year == 2023, "fact outside reviewed event year")
        actual = [
            day(f"{int(y):04}-{int(m):02}-{int(d):02}", name)
            for y, m, d in re.findall(DATE_PATTERNS[name], quote)
        ]
        require(actual == [expected], "date does not match its specific source role")
    else:
        number = decimal_value(value)
        if number == 0 and name in {
            "stock_bonus_per_10_old_shares",
            "capital_reserve_conversion_per_10_old_shares",
        }:
            negative = (
                r"不送红股|本年度不送股"
                if name == "stock_bonus_per_10_old_shares"
                else r"不以(?:资本公积|公积金)转增股本"
            )
            require(re.search(negative, quote), "zero share fact lacks explicit negative clause")
            require(
                re.search(
                    r"本次实施的[^。；]*方案[^。；]*股东大会[^。；]*审议通过[^。；]*一致", quote
                ),
                "zero share fact lacks implementation/approved-plan consistency citation",
            )
        else:
            actual = [Decimal(v) for v in re.findall(NUMERIC_PATTERNS[name], quote)]
            require(
                actual == [number], "numeric value does not match the exact role-specific token"
            )
    return dict(fact)


def _chinese_number(value):
    digits = {c: str(n) for n, c in enumerate("零一二三四五六七八九")}
    digits["〇"] = "0"
    if "十" in value:
        tens, units = value.split("十")
        return (int(digits[tens]) if tens else 1) * 10 + (int(digits[units]) if units else 0)
    return int("".join(digits[c] for c in value))


def _signature(pages, signature):
    require(
        isinstance(signature, dict) and set(signature) == {"value", "citations"},
        "invalid signature",
    )
    expected = day(signature["value"], "signature date")
    quotes = citations(pages, signature["citations"])
    joined = "".join(quotes)
    require("董事会" in joined, "signature lacks issuer board")
    dates = [
        day(f"{int(y):04}-{int(m):02}-{int(d):02}", "signature date")
        for y, m, d in re.findall(DATE_TOKEN, joined)
    ]
    for y, m, d in re.findall(
        r"([零〇一二三四五六七八九]{4})年([一二三四五六七八九十]{1,3})月([一二三四五六七八九十]{1,3})日",
        joined,
    ):
        dates.append(
            day(
                f"{_chinese_number(y):04}-{_chinese_number(m):02}-{_chinese_number(d):02}",
                "signature date",
            )
        )
    require(dates == [expected], "signature date differs from original closing clause")
    refs = signature["citations"]
    page_numbers = [r["page"] for r in refs]
    require(
        page_numbers == sorted(set(page_numbers)) and page_numbers[-1] == len(pages),
        "signature must end on final PDF page",
    )
    require(
        len(page_numbers) == 1 or page_numbers == list(range(page_numbers[0], len(pages) + 1)),
        "signature skips original pages",
    )
    for citation, quote in zip(refs, quotes, strict=True):
        number = citation["page"]
        body = re.sub(rf"(?m)^[ \t]*{number}[ \t]*\r?$", "", pages[number - 1])
        require(compact(body).endswith(quote), "signature is not the final body text on its page")
    return signature


def _identity(pages, claim, record):
    require(claim.get("stage") in {"implementation", "proposal"}, "unsupported announcement stage")
    pdf_title = compact(text(claim.get("pdf_title"), "PDF title"))
    title = compact(text(claim.get("title"), "search title"))
    require(title == compact(record["announcementTitle"]), "search/claim title differs")
    require(title in pdf_title or pdf_title in title, "unsupported search/PDF title relationship")
    first = compact(pages[0])
    require(pdf_title in first, "PDF title absent from first page")
    header = first.split(pdf_title, 1)[0]
    require(
        re.search(
            r"(?:证券|股票)代码[：:]" + re.escape(claim["symbol"][:6]) + r"(?![0-9])", header
        ),
        "PDF header security code differs",
    )
    number = text(claim.get("issuer_announcement_number"), "issuer announcement number")
    require(
        re.search(r"(?:公告)?编号[：:]" + re.escape(number) + r"(?![0-9-])", header),
        "PDF header announcement number differs",
    )
    identity_quotes = citations(pages, claim.get("identity_citations"))
    require(
        all(c["page"] == 1 for c in claim["identity_citations"])
        and claim["symbol"][:6] in "".join(identity_quotes)
        and pdf_title in "".join(identity_quotes)
        and number in "".join(identity_quotes),
        "identity citations do not bind first-page identity",
    )
    period = text(claim.get("reporting_period"), "reporting period")
    match = re.fullmatch(r"(20[0-9]{2})(半?)年度", period)
    require(
        match and re.search(match[1] + r"年?" + match[2] + r"年度", pdf_title),
        "reporting period not established by selected PDF title",
    )
    if claim["stage"] == "implementation":
        require(
            pdf_title.endswith("权益分派实施公告") and "预案" not in pdf_title,
            "proposal title cannot establish implementation",
        )
    else:
        require("权益分派预案" in pdf_title, "proposal stage differs from PDF title")


def _pdf_metadata(reader, pdf_ref, meta_ref, claim, record):
    raw, meta = reader.reference(pdf_ref), reader.json(meta_ref)
    require(raw.startswith(b"%PDF-"), "PDF original lacks PDF header")
    require(
        isinstance(meta, dict)
        and meta.get("complete") is True
        and meta.get("status") == 200
        and type(meta.get("status")) is int
        and not failures(meta),
        "PDF source capture failed",
    )
    require(
        integer(meta.get("bytes"), "PDF bytes") == len(raw)
        and meta.get("sha256") == pdf_ref["sha256"],
        "PDF metadata digest/size differs",
    )
    url = "https://static.cninfo.com.cn/" + record["adjunctUrl"]
    require(
        meta.get("url") == url and meta.get("content_type") == "application/pdf",
        "PDF source URL/type differs",
    )
    profile = claim.get("metadata_profile")
    if profile == "standard_v1":
        require(
            meta.get("method") == "GET"
            and meta.get("payload") == {"announcement_id": claim["announcement_id"]}
            and meta.get("final_url") == url
            and meta.get("transfer_complete") is True
            and meta.get("pdf_header_valid") is True,
            "PDF transport/identity differs",
        )
        require(integer(meta.get("retries"), "PDF retries") == 0, "PDF capture was retried")
        require(
            reader.path(meta.get("path", "")) == reader.path(pdf_ref["path"]),
            "PDF raw path differs",
        )
        length = meta.get("content_length")
        require("content_length" in meta, "PDF Content-Length observation absent")
        if length is not None:
            require(
                isinstance(length, str)
                and re.fullmatch(r"[0-9]+", length)
                and int(length) == len(raw),
                "PDF Content-Length differs",
            )
        begin, end = meta.get("started_at_utc"), meta.get("completed_at_utc")
        limitations = []
    else:
        require(
            profile == "legacy_actions_probe_v1"
            and claim["announcement_id"] == "1216935954"
            and pdf_ref["sha256"] == LEGACY_PDF_SHA
            and meta_ref["sha256"] == LEGACY_META_SHA,
            "legacy exception is only the previously reviewed exact original",
        )
        require(
            meta.get("announcement_id") == claim["announcement_id"], "legacy PDF identity differs"
        )
        begin, end = meta.get("started_at"), meta.get("completed_at")
        limitations = [
            "Legacy capture has no final_url, Content-Length or transfer_complete observations; none are invented."
        ]
    require(
        instant(begin, "PDF capture start") <= instant(end, "PDF capture end"),
        "PDF capture time reversed",
    )
    return {
        "observed_at_utc": instant(end, "PDF observation").isoformat(),
        "metadata_profile": profile,
        "limitations": limitations,
    }


def _original_pages(reader, pdf_ref, extraction_ref, executable):
    saved = reader.json(extraction_ref)
    require(
        isinstance(saved, dict) and saved.get("pdf_sha256") == pdf_ref["sha256"],
        "extraction PDF digest differs",
    )
    linked = saved.get("pdf", {})
    require(
        isinstance(linked, dict)
        and reader.path(linked.get("path", "")) == reader.path(pdf_ref["path"])
        and integer(linked.get("bytes"), "extraction PDF bytes") == pdf_ref["bytes"]
        and linked.get("sha256") == pdf_ref["sha256"],
        "extraction PDF reference differs",
    )
    pages, tool = pdf_text(reader.path(pdf_ref["path"]), executable)
    prior_tool = saved.get("tool", {})
    require(
        isinstance(prior_tool, dict)
        and all(prior_tool.get(k) == tool[k] for k in ("bytes", "sha256", "version", "mode")),
        "extraction tool/version differs from explicit local extractor",
    )
    originals = saved.get("pages")
    require(
        isinstance(originals, list) and len(originals) == len(pages),
        "extraction page count differs",
    )
    for number, (original, actual) in enumerate(zip(originals, pages, strict=True), 1):
        require(
            isinstance(original, dict)
            and integer(original.get("page"), "extraction page", 1) == number
            and isinstance(original.get("text"), str)
            and compact(original["text"]) == compact(actual),
            "saved extraction differs from freshly read PDF",
        )
    return pages, tool


def _claim(reader, files, claim, search, executable):
    require(
        isinstance(claim, dict)
        and claim.get("research_eligible") is False
        and "available_time" in claim
        and claim["available_time"] is None,
        "claim research gate differs",
    )
    symbol, identity = (
        text(claim.get("symbol"), "claim symbol"),
        text(claim.get("announcement_id"), "claim ID"),
    )
    locator = claim.get("search_locator")
    require(
        isinstance(locator, dict)
        and set(locator) == {"query_symbol", "page", "row_index"}
        and locator["query_symbol"] == symbol,
        "invalid search locator",
    )
    page, index = (
        integer(locator["page"], "search page", 1),
        integer(locator["row_index"], "search row"),
    )
    matching = [
        a
        for q in search["queries"]
        if q["symbol"] == symbol
        for a in q["announcements"]
        if a["page"] == page and a["row_index"] == index
    ]
    require(len(matching) == 1, "selected search row absent or ambiguous")
    source = matching[0]
    record = source["record"]
    require(record["announcementId"] == identity, "search locator points to another announcement")
    require(
        integer(claim.get("source_timestamp_ms"), "claimed timestamp") == record["announcementTime"]
        and claim.get("source_timestamp_precision") == source["source_timestamp_precision"],
        "source timestamp/precision differs from selected raw",
    )
    for key in ("pdf", "pdf_metadata", "extraction"):
        require(
            isinstance(claim.get(key), str) and claim[key] in files, "missing explicit file alias"
        )
    pdf_ref, meta_ref, extraction_ref = [
        files[claim[key]] for key in ("pdf", "pdf_metadata", "extraction")
    ]
    observation = _pdf_metadata(reader, pdf_ref, meta_ref, claim, record)
    pages, tool = _original_pages(reader, pdf_ref, extraction_ref, executable)
    _identity(pages, claim, record)
    signature = _signature(pages, claim.get("signature"))
    facts = claim.get("facts")
    require(isinstance(facts, dict) and facts, "claim facts absent")
    if claim["stage"] == "proposal":
        require(set(facts) == {"proposal_status"}, "proposal cannot contain implementation facts")
    else:
        require("proposal_status" not in facts, "implementation cannot contain proposal status")
    values = {key: None for key in FACT_UNITS}
    for name, fact in facts.items():
        values[name] = validate_fact(name, fact, pages)
    ordered_dates = [
        values[name]["value"]
        for name in ("record_date", "ex_date", "cash_payment_date")
        if values[name]
    ]
    require(ordered_dates == sorted(ordered_dates), "record/ex/payment dates reversed")
    if values["share_listing_date"] and values["ex_date"]:
        require(
            values["share_listing_date"]["value"] >= values["ex_date"]["value"],
            "shares listed before ex date",
        )
    per10 = values["gross_cash_per_10_eligible_shares"]
    arithmetic = decimal_value(per10["value"]) / Decimal("10") if per10 else None
    reported = values["reported_cash_per_eligible_share"]
    mismatch = bool(
        arithmetic is not None and reported and arithmetic != decimal_value(reported["value"])
    )
    notes = claim.get("review_notes")
    require(
        isinstance(notes, list) and all(isinstance(v, str) and v.strip() for v in notes),
        "invalid review notes",
    )
    return {
        "symbol": symbol,
        "announcement_id": identity,
        "title": claim["title"],
        "pdf_title": claim["pdf_title"],
        "issuer_announcement_number": claim["issuer_announcement_number"],
        "stage": claim["stage"],
        "reporting_period": claim["reporting_period"],
        "status": "reviewed_" + claim["stage"],
        "implementation_eligible": False,
        "research_eligible": False,
        "available_time": None,
        "source_timestamp_ms": record["announcementTime"],
        "source_timestamp_precision": source["source_timestamp_precision"],
        "signature": signature,
        **observation,
        "facts": values,
        "arithmetic_cash_per_eligible_share": str(arithmetic) if arithmetic is not None else None,
        "reported_vs_arithmetic_conflict": mismatch,
        "source_reports_other_numerical_discrepancy": values["source_numerical_discrepancy"]
        is not None,
        "numerical_conflict": mismatch or values["source_numerical_discrepancy"] is not None,
        "canonical_cash_per_share": None,
        "review_notes": notes,
        "identity_citations": claim["identity_citations"],
        "source_locator": {
            "search": source,
            "pdf": pdf_ref,
            "pdf_metadata": meta_ref,
            "extraction": extraction_ref,
            "pdf_pages": len(pages),
            "extractor": tool,
        },
    }


def validate_reviewed_claims(config_path, plan_path, manifest_path, evidence_root, pdftotext):
    """Re-extract originals and validate manual facts; never infer absent/zero facts."""
    search = audit_search_run(plan_path, manifest_path, evidence_root)
    require(
        search["selected_queries_complete"],
        "reviewed build requires verified complete selected searches",
    )
    reader = EvidenceReader(evidence_root)
    for reference in search["inputs"]:
        reader.reference(reference)
    config = reader.json(config_path)
    require(
        isinstance(config, dict)
        and config.get("schema_version") == "cninfo-reviewed-dividend-claims-v1"
        and config.get("research_eligible") is False
        and "available_time" in config
        and config["available_time"] is None
        and config.get("availability_policy") == "exclude_unknown",
        "invalid reviewed claim configuration",
    )
    files, claims = config.get("files"), config.get("claims")
    require(
        isinstance(files, dict) and isinstance(claims, list) and 1 <= len(claims) <= 7,
        "expected selected manual claim batch of at most seven originals",
    )
    for reference in files.values():
        reader.reference(reference)
    used = [
        c.get(k)
        for c in claims
        if isinstance(c, dict)
        for k in ("pdf", "pdf_metadata", "extraction")
    ]
    require(
        len(used) == 3 * len(claims) and len(set(used)) == len(used) and set(used) == set(files),
        "unbound/reused file aliases or unsupported claim shape",
    )
    records = [_claim(reader, files, claim, search, pdftotext) for claim in claims]
    require(
        len({r["announcement_id"] for r in records}) == len(records),
        "duplicate reviewed announcement",
    )
    reader.unchanged()
    return {
        "schema_version": "cninfo-reviewed-dividend-candidates-v1",
        "status": "reviewed_candidates_research_blocked",
        "claims_validated": True,
        "research_eligible": False,
        "available_time": None,
        "formal_action_coverage_certified": False,
        "network_requests": 0,
        "counts": {
            "claims": len(records),
            "implementation": sum(r["stage"] == "implementation" for r in records),
            "proposal": sum(r["stage"] == "proposal" for r in records),
            "numerical_conflicts": sum(r["numerical_conflict"] for r in records),
        },
        "records": records,
        "search_audit": search,
        "inputs": list(reader.references.values()),
        "module_file": str(Path(__file__).resolve()),
        "interpreter": sys.executable,
        "limits": [
            "Manual fact candidates only; no executable event, PIT availability or annual coverage is certified.",
            "Absent fields remain null; text tax/payment scope and share-listing dates are not substituted.",
            "All canonical_cash_per_share and available_time values remain null, including numerically consistent cases.",
            "The parser supports only reviewed clause forms; unknown dates, stages or semantics fail closed.",
        ],
    }


def build_reviewed_candidates(
    config_path, plan_path, manifest_path, evidence_root, pdftotext, output
):
    """Write a new JSON candidate artifact only after all originals/claims validate."""
    output = Path(output).resolve()
    require(not output.exists(), "candidate output already exists")
    source_root = Path(__file__).resolve().parents[3]
    source = execution_identity(source_root)
    result = validate_reviewed_claims(
        config_path, plan_path, manifest_path, evidence_root, pdftotext
    )
    require(execution_identity(source_root) == source, "source identity changed during validation")
    output.mkdir(parents=True, exist_ok=False)
    for name, value in (
        ("candidates.json", result["records"]),
        ("search_audit.json", result["search_audit"]),
    ):
        with (output / name).open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    manifest = {
        key: value for key, value in result.items() if key not in {"records", "search_audit"}
    }
    manifest["created_at_utc"] = datetime.now(timezone.utc).isoformat()
    manifest["source"] = {"root": str(source_root), **source}
    manifest["files"] = {}
    for name in ("candidates.json", "search_audit.json"):
        raw = (output / name).read_bytes()
        manifest["files"][name] = {
            "path": name,
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    with (output / "manifest.json").open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    try:
        require(execution_identity(source_root) == source, "source identity changed during build")
    except (ValueError, RuntimeError) as exc:
        manifest.update(
            status="failed_source_identity_changed",
            claims_validated=False,
            failure={"type": type(exc).__name__, "message": str(exc)},
        )
        (output / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        raise
    return manifest
