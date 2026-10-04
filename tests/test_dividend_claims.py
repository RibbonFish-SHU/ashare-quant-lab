"""Manual claim semantics and binding tested with synthetic retained source fixtures."""

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from ashare_lab.dividend_evidence import (
    DividendEvidenceError,
    build_reviewed_candidates,
    validate_reviewed_claims,
)
from ashare_lab.dividend_evidence import claims as module
from ashare_lab.dividend_evidence.claims import _signature, validate_fact
from test_dividend_search_audit import append_query, make_bundle, read, ref, row, write


CONSISTENCY = "本次实施的分配方案与公司2022年度股东大会审议通过的分配方案一致。"
CASH = "向全体股东每10股派2.049999元人民币现金（含税；"
REPORTED = "折算出每股现金分红为0.204999元/股（含税）"
DEDUCTION = "除权除息价格=股权登记日收盘价-0.204692元/股。"
DATES = "本次权益分派股权登记日为：2023年6月13日，除权除息日为：2023年6月14日。"


def fact(value, unit, quote, page=1):
    return {
        "value": value,
        "unit": unit,
        "basis": "Explicit synthetic source clause for test",
        "citations": [{"page": page, "quote": quote}],
    }


@pytest.fixture
def manual(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path)
    root, plan, run_path = bundle
    append_query(bundle, [[row(1)]])
    for _ in range(94):
        append_query(bundle, [[]])
    run = read(run_path)
    run["status"] = "complete_selected_queries"
    write(run_path, run)
    pdf, metadata, extraction = [
        root / name for name in ("manual.pdf", "manual.metadata.json", "manual.extraction.json")
    ]
    pdf.write_bytes(
        b"%PDF-1.4\nSynthetic fixture. PDF extraction is substituted in this unit test.\n"
    )
    title = "2022年度权益分派实施公告"
    identity = "证券代码：000001公告编号：2023-001合成公司" + title
    close = "合成公司董事会2023年6月7日"
    pages = [
        identity
        + "。"
        + CASH
        + "扣税说明。"
        + REPORTED
        + "。"
        + DEDUCTION
        + DATES
        + close
        + "\n1\n"
    ]
    tool = {
        "path": str(root / "explicit-test-tool"),
        "bytes": 3,
        "sha256": "1" * 64,
        "version": "synthetic extractor",
        "mode": "UTF-8 default reading order; whitespace-only normalization",
        "stderr": "",
    }
    monkeypatch.setattr(module, "pdf_text", lambda path, executable: (pages, tool))
    write(
        extraction,
        {
            "pdf": {**ref(root, pdf), "path": str(pdf)},
            "pdf_sha256": ref(root, pdf)["sha256"],
            "tool": tool,
            "pages": [{"page": 1, "text": pages[0]}],
        },
    )
    url = "https://static.cninfo.com.cn/finalpage/2023-06-08/1000001.PDF"
    write(
        metadata,
        {
            "url": url,
            "method": "GET",
            "payload": {"announcement_id": "1000001"},
            "started_at_utc": "2026-10-04T02:00:00+00:00",
            "completed_at_utc": "2026-10-04T02:00:01+00:00",
            "complete": True,
            "transfer_complete": True,
            "pdf_header_valid": True,
            "retries": 0,
            "status": 200,
            "final_url": url,
            "content_type": "application/pdf",
            "content_length": str(pdf.stat().st_size),
            **ref(root, pdf),
        },
    )
    claim = {
        "symbol": "000001.SZ",
        "announcement_id": "1000001",
        "title": title,
        "issuer_announcement_number": "2023-001",
        "stage": "implementation",
        "reporting_period": "2022年度",
        "metadata_profile": "standard_v1",
        "search_locator": {"query_symbol": "000001.SZ", "page": 1, "row_index": 0},
        "pdf": "pdf",
        "pdf_metadata": "metadata",
        "extraction": "extraction",
        "pdf_title": title,
        "identity_citations": [{"page": 1, "quote": identity}],
        "signature": {"value": "2023-06-07", "citations": [{"page": 1, "quote": close}]},
        "source_timestamp_ms": 1686153600000,
        "source_timestamp_precision": "date_label",
        "research_eligible": False,
        "available_time": None,
        "review_notes": ["Synthetic mismatch retained"],
        "facts": {
            "gross_cash_per_10_eligible_shares": fact(
                "2.049999", "CNY_per_10_eligible_shares", CASH
            ),
            "reported_cash_per_eligible_share": fact(
                "0.204999", "CNY_per_eligible_share", REPORTED
            ),
            "ex_price_cash_deduction_per_total_share": fact(
                "0.204692", "CNY_per_total_share", DEDUCTION
            ),
            "record_date": fact("2023-06-13", "date", DATES),
            "ex_date": fact("2023-06-14", "date", DATES),
        },
    }
    config = root / "claims.json"
    write(
        config,
        {
            "schema_version": "cninfo-reviewed-dividend-claims-v1",
            "research_eligible": False,
            "available_time": None,
            "availability_policy": "exclude_unknown",
            "files": {
                "pdf": ref(root, pdf),
                "metadata": ref(root, metadata),
                "extraction": ref(root, extraction),
            },
            "claims": [claim],
        },
    )
    return {
        "args": (config, plan, run_path, root, root / "explicit-test-tool"),
        "pages": pages,
        "claim": claim,
        "pdf": pdf,
        "metadata": metadata,
        "extraction": extraction,
        "tool": tool,
    }


def validate(manual):
    return validate_reviewed_claims(*manual["args"])


def test_binding_preserves_conflict_missing_fields_and_nonexecutable_status(manual):
    record = validate(manual)["records"][0]
    assert record["arithmetic_cash_per_eligible_share"] == "0.2049999"
    assert (
        record["reported_vs_arithmetic_conflict"] is True and record["numerical_conflict"] is True
    )
    assert record["facts"]["ex_price_cash_deduction_per_total_share"]["value"] == "0.204692"
    assert record["facts"]["stock_bonus_per_10_old_shares"] is None
    assert record["canonical_cash_per_share"] is None and record["available_time"] is None
    assert record["implementation_eligible"] is False and record["research_eligible"] is False


@pytest.mark.parametrize(
    "case",
    [
        "search_locator",
        "id",
        "stage",
        "year",
        "precision",
        "future_available",
        "identity_number",
        "source_time_bool",
        "signature_date",
        "unknown_field",
    ],
)
def test_claim_cannot_relabel_source_identity_stage_time_or_fields(manual, case):
    config_path = manual["args"][0]
    config = read(config_path)
    claim = config["claims"][0]
    if case == "search_locator":
        claim["search_locator"]["row_index"] = 1
    elif case == "id":
        claim["announcement_id"] = "1000002"
    elif case == "stage":
        claim["stage"] = "proposal"
    elif case == "year":
        claim["reporting_period"] = "2023年度"
    elif case == "precision":
        claim["source_timestamp_precision"] = "timestamp_label_unverified"
    elif case == "future_available":
        claim["available_time"] = "2023-06-08T00:00:00+08:00"
    elif case == "identity_number":
        claim["issuer_announcement_number"] = "2023-002"
    elif case == "source_time_bool":
        claim["source_timestamp_ms"] = True
    elif case == "signature_date":
        claim["signature"]["value"] = "2023-06-08"
    else:
        claim["facts"]["cash_per_share"] = fact("0.204999", "CNY_per_eligible_share", REPORTED)
    write(config_path, config)
    with pytest.raises(DividendEvidenceError):
        validate(manual)


@pytest.mark.parametrize(
    "case",
    [
        "pdf_tamper",
        "metadata_failure",
        "redirect",
        "content_length",
        "legacy_bypass",
        "saved_text",
        "tool_version",
        "extraction_digest",
        "page_bool",
    ],
)
def test_pdf_metadata_extraction_and_original_are_bound_independently(manual, case):
    config_path = manual["args"][0]
    config = read(config_path)
    root = manual["args"][3]
    if case == "pdf_tamper":
        manual["pdf"].write_bytes(b"%PDF-changed")
    elif case == "legacy_bypass":
        config["claims"][0]["metadata_profile"] = "legacy_actions_probe_v1"
    elif case in {"metadata_failure", "redirect", "content_length"}:
        path = manual["metadata"]
        value = read(path)
        if case == "metadata_failure":
            value["failure"] = {"message": "download failed"}
        elif case == "redirect":
            value["final_url"] = "https://other.example/fake.pdf"
        else:
            value["content_length"] = "9999"
        write(path, value)
        config["files"]["metadata"] = ref(root, path)
    else:
        path = manual["extraction"]
        value = read(path)
        if case == "saved_text":
            value["pages"][0]["text"] = value["pages"][0]["text"].replace("0.204999", "0.2")
        elif case == "tool_version":
            value["tool"]["version"] = "another extractor"
        elif case == "extraction_digest":
            value["pdf_sha256"] = "0" * 64
        else:
            value["pages"][0]["page"] = True
        write(path, value)
        config["files"]["extraction"] = ref(root, path)
    write(config_path, config)
    with pytest.raises(DividendEvidenceError):
        validate(manual)


@pytest.mark.parametrize("value", [True, 0.204999, "NaN", "Infinity", "-0.2", "0.204", "0.2049999"])
def test_numeric_claim_needs_exact_role_token_not_substring(value):
    with pytest.raises(DividendEvidenceError):
        validate_fact(
            "reported_cash_per_eligible_share",
            fact(value, "CNY_per_eligible_share", REPORTED),
            [REPORTED],
        )


def test_equivalent_decimals_and_cross_page_gross_tax_clause_are_supported():
    gross = "向全体股东每10股派0.70元人民币现金（含"
    value = fact("0.7", "CNY_per_10_eligible_shares", gross)
    value["citations"].append({"page": 2, "quote": "税；扣税后"})
    assert (
        validate_fact("gross_cash_per_10_eligible_shares", value, [gross, "税；扣税后"])["value"]
        == "0.7"
    )


def test_date_roles_cannot_be_swapped_inside_a_shared_quote():
    with pytest.raises(DividendEvidenceError, match="specific source role"):
        validate_fact("record_date", fact("2023-06-14", "date", DATES), [DATES])


def test_zero_share_fact_requires_explicit_negative_and_implementation_confirmation():
    negative = "不送红股，不以资本公积转增股本。"
    value = fact("0", "shares_per_10_old_shares", negative)
    with pytest.raises(DividendEvidenceError, match="consistency"):
        validate_fact("stock_bonus_per_10_old_shares", value, [negative, CONSISTENCY])
    value["citations"].append({"page": 2, "quote": CONSISTENCY})
    assert (
        validate_fact("stock_bonus_per_10_old_shares", value, [negative, CONSISTENCY])["value"]
        == "0"
    )
    value["citations"][0]["quote"] = "本次每10股派0.7元。"
    with pytest.raises(DividendEvidenceError, match="negative clause"):
        validate_fact("stock_bonus_per_10_old_shares", value, ["本次每10股派0.7元。", CONSISTENCY])


def test_conditional_context_cannot_be_removed_by_shortening_selected_quote():
    with pytest.raises(DividendEvidenceError, match="planned"):
        validate_fact(
            "gross_cash_per_10_eligible_shares",
            fact("2.049999", "CNY_per_10_eligible_shares", CASH),
            ["若本预案通过，" + CASH],
        )


def test_text_discrepancy_preserves_two_original_clauses_without_recalculation():
    quotes = ["总额为173,778,232.00元。", "实际总额173,778,142.9元。"]
    value = fact("".join(quotes), "text", quotes[0])
    value["citations"].append({"page": 2, "quote": quotes[1]})
    assert validate_fact("source_numerical_discrepancy", value, quotes)["value"] == "".join(quotes)
    value["value"] = "Corrected amount"
    with pytest.raises(DividendEvidenceError):
        validate_fact("source_numerical_discrepancy", value, quotes)


def test_chinese_signature_can_span_last_two_pages_but_not_skip_or_precede_text():
    pages = ["合成公司董事会\n4\n", "二零二三年九月十四日\n5\n"]
    signature = {
        "value": "2023-09-14",
        "citations": [
            {"page": 1, "quote": "合成公司董事会"},
            {"page": 2, "quote": "二零二三年九月十四日"},
        ],
    }
    # Original page numbers in this two-page fixture must match the source page positions.
    pages = [p.replace("4", "1").replace("5", "2") for p in pages]
    assert _signature(pages, signature)["value"] == "2023-09-14"
    with pytest.raises(DividendEvidenceError):
        _signature([pages[0], pages[1] + "追加正文"], signature)


def test_proposal_control_keeps_implementation_fields_null(manual):
    config_path = manual["args"][0]
    config = read(config_path)
    root = manual["args"][3]
    claim = config["claims"][0]
    old_title = claim["title"]
    new_title = "关于2022年度权益分派预案的公告"
    proposal = "本次权益分派预案尚需提交公司股东大会审议批准后方能实施。"
    claim.update(
        title=new_title,
        pdf_title=new_title,
        stage="proposal",
        facts={"proposal_status": fact(proposal, "text", proposal)},
    )
    claim["identity_citations"][0]["quote"] = claim["identity_citations"][0]["quote"].replace(
        old_title, new_title
    )
    manual["pages"][0] = (
        manual["pages"][0].replace(old_title, new_title).replace(CASH, proposal + CASH)
    )
    saved = read(manual["extraction"])
    saved["pages"][0]["text"] = manual["pages"][0]
    write(manual["extraction"], saved)
    config["files"]["extraction"] = ref(root, manual["extraction"])
    run_path = manual["args"][2]
    run = read(run_path)
    receipt = run["entries"][0]["receipts"][0]
    raw = root / receipt["raw"]["path"]
    data = read(raw)
    data["announcements"][0]["announcementTitle"] = new_title
    write(raw, data)
    from test_dividend_search_audit import refresh

    refresh((root, manual["args"][1], run_path))
    write(config_path, config)
    record = validate(manual)["records"][0]
    assert record["status"] == "reviewed_proposal" and record["implementation_eligible"] is False
    assert (
        record["facts"]["record_date"] is None
        and record["arithmetic_cash_per_eligible_share"] is None
    )
    config["claims"][0]["facts"]["record_date"] = fact("2023-06-13", "date", DATES)
    write(config_path, config)
    with pytest.raises(DividendEvidenceError, match="proposal cannot"):
        validate(manual)


def test_candidate_build_is_immutable_and_manifest_hashes_output(manual, tmp_path, monkeypatch):
    monkeypatch.setattr(
        module, "execution_identity", lambda root: {"commit": "1" * 40, "dirty": False}
    )
    target = tmp_path / "candidate-output"
    result = build_reviewed_candidates(*manual["args"], target)
    assert result["research_eligible"] is False and result["source"]["commit"]
    for name, expected in result["files"].items():
        assert hashlib.sha256((target / name).read_bytes()).hexdigest() == expected["sha256"]
    before = (target / "manifest.json").read_bytes()
    with pytest.raises(DividendEvidenceError, match="already exists"):
        build_reviewed_candidates(*manual["args"], target)
    assert (target / "manifest.json").read_bytes() == before


def test_invalid_claim_writes_no_candidate_directory(manual, tmp_path, monkeypatch):
    monkeypatch.setattr(
        module, "execution_identity", lambda root: {"commit": "1" * 40, "dirty": False}
    )
    path = manual["args"][0]
    config = read(path)
    config["claims"][0]["facts"]["record_date"]["value"] = "2023-06-14"
    write(path, config)
    target = tmp_path / "must-not-exist"
    with pytest.raises(DividendEvidenceError):
        build_reviewed_candidates(*manual["args"], target)
    assert not target.exists()


@pytest.mark.parametrize("case", ["dirty", "mixed_import"])
def test_actual_source_guard_rejects_dirty_commit_and_mixed_module_in_child_process(tmp_path, case):
    source_root = Path(module.__file__).resolve().parents[3]
    checkout = tmp_path / "isolated-source"
    shutil.copytree(
        source_root / "src", checkout / "src", ignore=shutil.ignore_patterns("__pycache__")
    )
    (checkout / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    for args in (
        ("init",),
        ("config", "core.autocrlf", "false"),
        ("add", "."),
        (
            "-c",
            "user.name=Evidence tests",
            "-c",
            "user.email=tests@example.invalid",
            "commit",
            "-m",
            "Synthetic source guard fixture",
        ),
    ):
        subprocess.run(
            ["git", "-C", str(checkout), *args], capture_output=True, check=True, timeout=30
        )
    if case == "dirty":
        with (checkout / "src/ashare_lab/dividend_evidence/claims.py").open(
            "a", encoding="utf-8"
        ) as stream:
            stream.write("\n# Deliberately uncommitted source mutation.\n")
    code = """
import importlib.util
from pathlib import Path
import sys
from ashare_lab.dividend_evidence import build_reviewed_candidates
if sys.argv[1] == 'mixed_import':
    spec = importlib.util.spec_from_file_location('ashare_lab.other_checkout_runtime', sys.argv[2])
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    spec.loader.exec_module(loaded)
try:
    build_reviewed_candidates('absent', 'absent', 'absent', '.', 'absent', 'rejected-output')
except ValueError as exc:
    print(type(exc).__name__ + ': ' + str(exc))
else:
    raise AssertionError('source guard was bypassed')
assert not Path('rejected-output').exists()
"""
    env = {
        **os.environ,
        "PYTHONPATH": str(checkout / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    result = subprocess.run(
        [sys.executable, "-c", code, case, str(source_root / "src/ashare_lab/runtime.py")],
        cwd=checkout,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert (
        "clean source commit" if case == "dirty" else "imported module outside"
    ) in result.stdout


def test_source_change_during_validation_writes_no_candidate(manual, tmp_path, monkeypatch):
    snapshots = iter([{"commit": "1" * 40}, {"commit": "2" * 40}])
    monkeypatch.setattr(module, "execution_identity", lambda root: next(snapshots))
    target = tmp_path / "changed-source-output"
    with pytest.raises(DividendEvidenceError, match="changed during validation"):
        build_reviewed_candidates(*manual["args"], target)
    assert not target.exists()
