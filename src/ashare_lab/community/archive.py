"""Bounded, non-extracting reader for the retained Qlib float32 archive."""

from dataclasses import dataclass
from datetime import date
import gzip
import hashlib
import math
from pathlib import PurePosixPath
import re
import struct
import tarfile

FIELDS = (
    "open",
    "high",
    "low",
    "close",
    "factor",
    "volume",
    "amount",
    "adjclose",
    "change",
    "vwap",
)
OHLC = ("open", "high", "low", "close")
FEATURE = re.compile(r"qlib_bin/features/((?:sh|sz|bj)\d{6})/([a-z]+)\.day\.bin")


@dataclass(frozen=True)
class Limits:
    members: int = 70000
    member_bytes: int = 16 * 1024 * 1024
    total_bytes: int = 600 * 1024 * 1024
    stream_bytes: int = 800 * 1024 * 1024


def sha256_file(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def iso_day(text):
    result = date.fromisoformat(text)
    if result.isoformat() != text:
        raise ValueError("date is not canonical ISO")
    return result


def safe_member(member):
    name = member.name
    parts = name.split("/")
    if (
        not name
        or len(name) > 240
        or PurePosixPath(name).is_absolute()
        or any(p in {"", ".", ".."} or p.endswith((".", " ")) for p in parts)
        or any(c in name for c in "\\:\x00")
        or parts[0] != "qlib_bin"
        or not (member.isdir() or member.isreg())
        or member.issparse()
        or member.pax_headers
        or member.linkname
        or member.size < 0
        or (member.isdir() and member.size != 0)
    ):
        raise ValueError(f"unsafe archive member: {name!r}")
    return name


class BoundedStream:
    def __init__(self, stream, limit):
        self.stream, self.limit, self.count = stream, limit, 0

    def read(self, size):
        value = self.stream.read(size)
        self.count += len(value)
        if self.count > self.limit:
            raise ValueError("decompressed archive exceeds stream limit")
        return value


def parse_calendar(body, latest):
    lines = body.decode("utf-8").splitlines()
    days = [iso_day(line) for line in lines]
    if not days or days != sorted(set(days)) or days[-1] > latest:
        raise ValueError("calendar empty, repeated, unordered or beyond historical release")
    return days


def parse_intervals(body, latest, *, allow_unmapped=False):
    rows, seen = [], set()
    for number, line in enumerate(body.decode("utf-8").splitlines(), 1):
        parts = line.split("\t")
        if len(parts) != 3:
            raise ValueError("invalid instrument interval")
        code, first, last = parts
        canonical = re.fullmatch(r"(?:SH|SZ|BJ)\d{6}", code) is not None
        if not canonical and not (allow_unmapped and re.fullmatch(r"[A-Z][A-Z0-9]{1,31}", code)):
            raise ValueError("invalid instrument interval")
        start, end = iso_day(first), iso_day(last)
        if start > end or end > latest or tuple(parts) in seen:
            raise ValueError("duplicate, reversed or future instrument interval")
        seen.add(tuple(parts))
        rows.append(
            {
                "source_code": code,
                "symbol": code[2:] + "." + code[:2] if canonical else None,
                "start": start,
                "end": end,
                "line_number": number,
            }
        )
    return rows


def read_archive(path, symbols, *, latest=date(2023, 12, 31), limits=Limits()):
    """Validate every member/header; retain only selected feature bodies and text references."""
    names, inventory, headers, selected, texts = {}, [], [], {}, {}
    parent_names = set()
    total = 0
    with gzip.open(path, "rb") as gz:
        bounded = BoundedStream(gz, limits.stream_bytes)
        with tarfile.open(fileobj=bounded, mode="r|", ignore_zeros=True) as archive:
            for member in archive:
                name = safe_member(member)
                folded = name.casefold()
                if folded in names:
                    raise ValueError(f"duplicate archive member: {name}")
                for parent in PurePosixPath(name).parents:
                    if names.get(str(parent).casefold()) == "file":
                        raise ValueError("archive member has a file as parent")
                if member.isreg() and folded in parent_names:
                    raise ValueError("archive file collides with existing descendants")
                parent_names.update(str(p).casefold() for p in PurePosixPath(name).parents)
                names[folded] = "directory" if member.isdir() else "file"
                total += member.size
                if (
                    len(names) > limits.members
                    or member.size > limits.member_bytes
                    or total > limits.total_bytes
                ):
                    raise ValueError("archive member/count/total size limit exceeded")
                inventory.append({"name": name, "bytes": member.size, "type": names[folded]})
                if member.isdir():
                    continue
                stream = archive.extractfile(member)
                match = FEATURE.fullmatch(name)
                if match:
                    code, field = match.groups()
                    if field not in FIELDS or member.size < 8 or member.size % 4:
                        raise ValueError("invalid float32 feature size or field")
                    header = stream.read(4)
                    if len(header) != 4:
                        raise ValueError("truncated feature header")
                    offset = struct.unpack("<f", header)[0]
                    if not math.isfinite(offset) or offset < 0 or not offset.is_integer():
                        raise ValueError("invalid float32 calendar offset")
                    count = member.size // 4 - 1
                    headers.append((name, int(offset), count))
                    symbol = code[2:] + "." + code[:2].upper()
                    if symbol in symbols:
                        body = header + stream.read()
                        if len(body) != member.size:
                            raise ValueError("truncated feature body")
                        selected[(symbol, field)] = {
                            "member": name,
                            "sha256": hashlib.sha256(body).hexdigest(),
                            "bytes": member.size,
                            "start_index": int(offset),
                            "count": count,
                            "body": body,
                        }
                elif name.startswith("qlib_bin/calendars/") or name.startswith(
                    "qlib_bin/instruments/"
                ):
                    body = stream.read()
                    if len(body) != member.size:
                        raise ValueError("truncated text member")
                    texts[name] = body
                else:
                    raise ValueError(f"unsupported regular archive member: {name}")
        # gzip.open validates CRC and trailer; consuming EOF also rejects truncated gzip streams.
        while bounded.read(10240):
            pass
    day_name = "qlib_bin/calendars/day.txt"
    if day_name not in texts:
        raise ValueError("missing daily calendar")
    calendar = parse_calendar(texts[day_name], latest)
    for name, body in texts.items():
        if "/calendars/" in name and parse_calendar(body, latest) != calendar:
            raise ValueError("archive calendars disagree")
        if "/instruments/" in name:
            parse_intervals(body, latest, allow_unmapped=name != "qlib_bin/instruments/all.txt")
    for name, offset, count in headers:
        if offset + count > len(calendar):
            raise ValueError(f"feature extends beyond calendar: {name}")
    if set(selected) != {(symbol, field) for symbol in symbols for field in FIELDS}:
        raise ValueError("missing planned security feature members")
    for name in ("all", "csi300"):
        if f"qlib_bin/instruments/{name}.txt" not in texts:
            raise ValueError(f"missing {name} instrument reference")
    all_rows = parse_intervals(texts["qlib_bin/instruments/all.txt"], latest)
    if not symbols <= {r["symbol"] for r in all_rows}:
        raise ValueError("planned security absent from all instruments")
    return {
        "calendar": calendar,
        "selected": selected,
        "texts": texts,
        "inventory": inventory,
        "feature_headers_checked": len(headers),
        "uncompressed_regular_bytes": total,
        "stream_bytes": bounded.count,
    }


def feature_value(feature, calendar_index):
    index = calendar_index - feature["start_index"]
    locator = {
        "member": feature["member"],
        "member_sha256": feature["sha256"],
        "value_index": None,
        "byte_offset": None,
        "raw_bits": None,
        "value_state": "outside_member_span",
    }
    if not 0 <= index < feature["count"]:
        return None, locator
    position = 4 * (index + 1)
    value = struct.unpack_from("<f", feature["body"], position)[0]
    bits = struct.unpack_from("<I", feature["body"], position)[0]
    locator.update(
        value_index=index,
        byte_offset=position,
        raw_bits=bits,
        value_state="nan" if math.isnan(value) else "finite",
    )
    if math.isinf(value):
        raise ValueError(f"infinite feature value: {feature['member']} at {index}")
    return (None if math.isnan(value) else value), locator


def float32_rounding_bound(value):
    """Conservative half-ULP bound on each retained float32 input, not on upstream methodology."""
    exponent = math.frexp(abs(value))[1] if value else -125
    return math.ldexp(1.0, max(exponent - 24, -149) - 1)
