"""Private bounded transfer child. No Project credentials, redirects or execution.

The parent owns the deadline/process group and private plan/staging directory.
Only a public byte-count/hash receipt or a constant error is printed.
"""

import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import sys
from urllib.parse import urlsplit

MAX_BYTES = 256 * 1024 * 1024
MAX_PARTS = 64
SIGN_HEADER = "X-Meego-File-Sign"


def validate(plan):
    if not isinstance(plan, dict):
        raise ValueError("invalid plan")  # noqa: TRY004 -- protocol rejection
    url, sign = plan.get("download_url"), plan.get("sign")
    if not isinstance(url, str) or not 0 < len(url) <= 16384:
        raise ValueError("invalid URL")
    if not isinstance(sign, str) or not 0 < len(sign) <= 4096:
        raise ValueError("invalid signature")
    unit = plan.get("target_unit_code", "") or ""
    if not isinstance(unit, str) or len(unit) > 4096:
        raise ValueError("invalid routing header")
    if any(ord(c) < 32 or ord(c) == 127 for c in url + sign + unit):
        raise ValueError("invalid controls")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("HTTPS required")
    multi = plan.get("is_multipart")
    if type(multi) is not bool:
        raise ValueError("invalid multipart flag")
    expected = None
    if multi:
        parts = plan.get("multipart")
        if not isinstance(parts, dict):
            raise ValueError("missing parts")
        count, size, need = (
            parts.get("part_count"),
            parts.get("part_size"),
            parts.get("need"),
        )
        if (
            type(count) is not int
            or not 1 <= count <= MAX_PARTS
            or type(size) is not int
            or not 1 <= size <= MAX_BYTES
            or not isinstance(need, list)
            or len(need) != count
            or ":part_number" not in parsed.path + "?" + parsed.query
        ):
            raise ValueError("invalid parts")
        expected, cursor = [], 0
        for index, part in enumerate(need):
            if not isinstance(part, dict) or any(
                type(part.get(k)) is not int
                for k in ("part_index", "start_byte", "end_byte")
            ):
                raise ValueError("invalid range")
            length = part["end_byte"] - part["start_byte"] + 1
            if (
                part["part_index"] != index
                or part["start_byte"] != cursor
                or not 0 < length <= size
                or (index < count - 1 and length != size)
            ):
                raise ValueError("non-contiguous range")
            expected.append(length)
            cursor += length
        if cursor > MAX_BYTES:
            raise ValueError("file budget exceeded")
    return parsed, sign, unit, expected


class PinnedHTTPS(http.client.HTTPSConnection):
    def connect(self):
        # Resolve once and connect only to those addresses; retain hostname for
        # TLS certificate verification/SNI. No second DNS resolution or proxies.
        addresses = socket.getaddrinfo(self.host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(
            not ipaddress.ip_address(a[4][0]).is_global for a in addresses
        ):
            raise ValueError("non-public address")
        for family, socktype, proto, _, address in addresses[:8]:
            sock = socket.socket(family, socktype, proto)
            sock.settimeout(10)
            try:
                sock.connect(address)
                self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
                return
            except OSError:
                sock.close()
        raise OSError("connection unavailable")


def transfer(plan, sink, *, connection_factory=PinnedHTTPS):
    parsed, sign, unit, expected = validate(plan)
    hasher, total = hashlib.sha256(), 0
    for index in range(len(expected) if expected is not None else 1):
        connection = connection_factory(
            parsed.hostname, timeout=10, context=ssl.create_default_context()
        )
        try:
            target = (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
            headers = {SIGN_HEADER: sign, "Accept-Encoding": "identity"}
            if unit:
                headers["x-target-unit"] = unit
            connection.request(
                "GET", target.replace(":part_number", str(index)), headers=headers
            )
            response = connection.getresponse()
            if (
                response.status not in {200, 206}
                or response.getheader(SIGN_HEADER, "").strip().lower() != sign.lower()
            ):
                raise ValueError("unverified response")
            content_range = response.getheader("Content-Range")
            if response.status == 206 or content_range is not None:
                match = re.fullmatch(
                    r"bytes ([0-9]+)-([0-9]+)/([0-9]+)", content_range or ""
                )
                if not match:
                    raise ValueError("invalid response range")
                start, end, total_range = map(int, match.groups())
                if expected is None:
                    if (
                        start != 0
                        or end != total_range - 1
                        or not 0 < total_range <= MAX_BYTES
                    ):
                        raise ValueError("incomplete response range")
                    expected = [total_range]
                offset = sum(expected[:index])
                if (
                    start != offset
                    or end != offset + expected[index] - 1
                    or total_range != sum(expected)
                ):
                    raise ValueError("inconsistent response range")
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise ValueError("unexpected encoding")
            length = response.getheader("Content-Length")
            if length is not None and (
                not length.isascii()
                or not length.isdecimal()
                or int(length) > MAX_BYTES - total
            ):
                raise ValueError("invalid length")
            part_bytes = 0
            while chunk := response.read(64 * 1024):
                total += len(chunk)
                part_bytes += len(chunk)
                if total > MAX_BYTES or (
                    expected is not None and part_bytes > expected[index]
                ):
                    raise ValueError("file budget exceeded")
                sink.write(chunk)
                hasher.update(chunk)
            if (length is not None and part_bytes != int(length)) or (
                expected is not None and part_bytes != expected[index]
            ):
                raise ValueError("incomplete part")
        finally:
            connection.close()
    return {
        "size_bytes": total,
        "sha256": hasher.hexdigest(),
        "parts": len(expected) if expected is not None else 1,
        "server_signature_checked": True,
    }


def main():
    try:
        plan_path, output = sys.argv[1:]
        with open(plan_path, encoding="utf-8") as source:
            plan = json.load(source)
        fd = os.open(
            output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(fd, "wb") as sink:
            receipt = transfer(plan, sink)
            sink.flush()
            os.fsync(sink.fileno())
        print(json.dumps(receipt))
        return 0
    except (OSError, ValueError, TypeError, http.client.HTTPException):
        print(json.dumps({"error": "attachment_transfer_failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
