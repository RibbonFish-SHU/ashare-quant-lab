"""Strict bounded request identities and non-executing JSON/JSONP parsing."""

from datetime import date
import hashlib
import json
import re
from urllib.parse import parse_qs, urlencode, urlsplit

from ashare_lab.data.queries import symbol

EASTMONEY = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
TENCENT = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
EM_FIELDS1 = "f1,f2,f3,f4,f5,f6"
EM_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116"
EM_UT = "7eea3edcaed734bea9cbfc24409ed989"


class SourceError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def dates(start, end):
    for value in (start, end):
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value or parsed.year != 2023:
            raise ValueError("fallback requests must stay within 2023")
    if start > end:
        raise ValueError("reversed fallback dates")
    return start, end


def eastmoney_request(code, start="2023-01-01", end="2023-12-31"):
    symbol(code)
    dates(start, end)
    market, number = code.split(".")
    return {
        "provider": "eastmoney",
        "parameters": {
            "fields1": EM_FIELDS1,
            "fields2": EM_FIELDS2,
            "ut": EM_UT,
            "klt": "101",
            "fqt": "0",
            "secid": ("1." if market == "sh" else "0.") + number,
            "beg": start.replace("-", ""),
            "end": end.replace("-", ""),
        },
    }


def request_identity(request):
    provider, p = request["provider"], request["parameters"]
    if provider == "eastmoney":
        if set(p) != {"fields1", "fields2", "ut", "klt", "fqt", "secid", "beg", "end"}:
            raise ValueError("unexpected Eastmoney parameters")
        if (p["fields1"], p["fields2"], p["ut"], p["klt"], p["fqt"]) != (
            EM_FIELDS1,
            EM_FIELDS2,
            EM_UT,
            "101",
            "0",
        ):
            raise ValueError("only the reviewed daily unadjusted endpoint is supported")
        if not re.fullmatch(r"[01]\.\d{6}", p["secid"]):
            raise ValueError("invalid Eastmoney security identity")
        market, number = p["secid"].split(".")
        code = ("sh." if market == "1" else "sz.") + number
        if any(not re.fullmatch(r"\d{8}", p[k]) for k in ("beg", "end")):
            raise ValueError("Eastmoney dates must be explicit YYYYMMDD")
        start, end = (date.fromisoformat(p[k]).isoformat() for k in ("beg", "end"))
    elif provider == "tencent":
        if set(p) != {"_var", "param", "r"} or p["_var"] != "kline_day2023":
            raise ValueError("unsupported Tencent capture parameters")
        code, interval, start, end, limit, adjusted = p["param"].split(",")
        if interval != "day" or adjusted != "" or limit != "10":
            raise ValueError("only the reviewed Tencent offline samples are supported")
        code = code[:2] + "." + code[2:]
    else:
        raise ValueError("unsupported fallback provider")
    security = symbol(code)
    dates(start, end)
    identity = {"provider": provider, "parameters": dict(sorted(p.items()))}
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return {**identity, "query_id": key, "symbol": security, "start": start, "end": end}


def request_url(request):
    identity = request_identity(request)
    endpoint = EASTMONEY if identity["provider"] == "eastmoney" else TENCENT
    return endpoint + "?" + urlencode(request["parameters"])


def verify_url(url, request):
    actual, expected = urlsplit(url), urlsplit(request_url(request))
    if (actual.scheme, actual.netloc, actual.path, actual.fragment) != (
        expected.scheme,
        expected.netloc,
        expected.path,
        "",
    ) or parse_qs(actual.query, keep_blank_values=True) != parse_qs(
        expected.query, keep_blank_values=True
    ):
        raise ValueError("HTTP URL differs from explicit provider parameters")


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def constant(value):
        raise ValueError("non-finite JSON constant")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def decode_response(body, request):
    """Return only historical day rows; current quote/name objects stay in raw bytes."""
    identity = request_identity(request)
    text = body.decode("utf-8-sig").strip()
    if text.startswith("<") or re.search(r"captcha|访问频繁|验证码|访问受限", text, re.I):
        raise SourceError("restricted", "HTML/challenge or explicit source restriction")
    if identity["provider"] == "tencent":
        name = re.escape(request["parameters"]["_var"])
        wrapped = re.fullmatch(rf"{name}\s*=\s*(\{{.*\}})\s*;?", text, re.S)
        if wrapped:
            text = wrapped[1]
        elif not text.startswith("{"):
            raise SourceError("protocol_error", "unexpected JSONP assignment; no code execution")
    try:
        payload = strict_json(text)
    except (ValueError, TypeError) as exc:
        raise SourceError("protocol_error", f"invalid complete JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SourceError("protocol_error", "response must be an object")
    if identity["provider"] == "eastmoney":
        if type(payload.get("rc")) is not int or payload["rc"] != 0:
            raise SourceError("provider_error", f"Eastmoney rc={payload.get('rc')}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise SourceError("empty_unverified", "no identified Eastmoney data object")
        number, market = identity["symbol"].split(".")
        if (
            data.get("code") != number
            or type(data.get("market")) is not int
            or data["market"] != (1 if market == "SH" else 0)
        ):
            raise SourceError("protocol_error", "response security differs from request")
        if not isinstance(data.get("klines"), list) or any(
            not isinstance(r, str) for r in data["klines"]
        ):
            raise SourceError("protocol_error", "missing or non-string kline array")
        rows = [row.split(",") for row in data["klines"]]
    else:
        if type(payload.get("code")) is not int or payload["code"] != 0:
            raise SourceError("provider_error", f"Tencent code={payload.get('code')}")
        key = request["parameters"]["param"].split(",")[0]
        data = payload.get("data")
        data = data.get(key) if isinstance(data, dict) else None
        if not isinstance(data, dict) or not isinstance(data.get("day"), list):
            raise SourceError("protocol_error", "missing identified unadjusted day array")
        rows = data["day"]
    if len(rows) > 1000:
        raise SourceError("protocol_error", "bounded daily response exceeded 1000 rows")
    if any(not isinstance(row, list) or len(row) != 11 for row in rows):
        raise SourceError("protocol_error", "unexpected daily row shape")
    return identity, rows
