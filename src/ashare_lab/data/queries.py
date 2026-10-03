"""An explicit, bounded subset of BaoStock; dates never default to today."""

from datetime import date
import hashlib
import json
import re

SDK_VERSION = "0.9.4"
SDK_SHA256 = "0bf71c6069ab5890ff3596632f9c3f8f1fbc6bfcac582c2f9d6a5c11ab2cfaf8"
BAR_FIELDS = "date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,tradestatus,isST"
API_KINDS = {
    "query_hs300_stocks": "membership_snapshots",
    "query_trade_dates": "calendar",
    "query_history_k_data_plus": "bars",
    "query_stock_basic": "securities",
    "query_dividend_data": "dividends",
    "query_adjust_factor": "factors",
    "query_st_stocks": "st_snapshots",
    "query_suspended_stocks": "suspension_snapshots",
}
DOCS = {
    "membership_snapshots": "hs300Stock.md",
    "calendar": "StockBasicInfoAPI.md",
    "bars": "stockKData.md",
    "securities": "stockBasic.md",
    "dividends": "dividInfo.md",
    "factors": "factorInfo.md",
    "st_snapshots": "pythonAPI.md",
    "suspension_snapshots": "pythonAPI.md",
}


def symbol(code):
    if not isinstance(code, str) or not re.fullmatch(
        r"(?:sh\.(?:60[0135]|68[89])\d{3}|sz\.(?:00[0-3]|30[01])\d{3})", code
    ):
        raise ValueError("expected a Shanghai/Shenzhen A-share code, not an index or fund")
    market, number = code.split(".")
    return number + "." + market.upper()


def validate_query(query):
    api, p = query["api"], query["parameters"]
    if api not in API_KINDS or not isinstance(p, dict):
        raise ValueError("unsupported provider query")
    allowed = {
        "membership_snapshots": {"date"},
        "calendar": {"start_date", "end_date"},
        "bars": {"code", "fields", "start_date", "end_date", "frequency", "adjustflag"},
        "securities": {"code"},
        "dividends": {"code", "year", "yearType"},
        "factors": {"code", "start_date", "end_date"},
        "st_snapshots": {"date"},
        "suspension_snapshots": {"date"},
    }[API_KINDS[api]]
    if set(p) != allowed or any(not isinstance(v, str) or not v for v in p.values()):
        raise ValueError("all query parameters must be explicit, nonempty strings")
    if "code" in p:
        symbol(p["code"])
    for name in ("date", "start_date", "end_date"):
        if name in p:
            parsed = date.fromisoformat(p[name])
            if parsed.isoformat() != p[name] or not date(2010, 1, 1) <= parsed < date(2024, 1, 1):
                raise ValueError(
                    "query dates must be ISO dates within the authorized 2010-2023 range"
                )
    if "start_date" in p and p["start_date"] > p["end_date"]:
        raise ValueError("reversed query interval")
    if "year" in p and (
        not re.fullmatch(r"\d{4}", p["year"]) or not 2010 <= int(p["year"]) <= 2023
    ):
        raise ValueError("year outside development range")
    if "yearType" in p and p["yearType"] != "operate":
        raise ValueError("use the explicit action-year query, not default report year")
    if api == "query_history_k_data_plus":
        if p["frequency"] != "d" or p["adjustflag"] != "3":
            raise ValueError("only daily unadjusted bars are authorized")
        fields = p["fields"].split(",")
        if not set(fields) <= set(BAR_FIELDS.split(",")) or len(set(fields)) != len(fields):
            raise ValueError("unsupported or repeated bar fields")
        if not {
            "date",
            "code",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
            "tradestatus",
            "isST",
        } <= set(fields):
            raise ValueError("missing essential bar fields")
    return {"api": api, "parameters": dict(sorted(p.items()))}


def query_id(query):
    identity = {"source": "baostock", "sdk": SDK_VERSION, **validate_query(query)}
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def source_url(api):
    return "https://www.baostock.com/mainContent?file=" + DOCS[API_KINDS[api]]
