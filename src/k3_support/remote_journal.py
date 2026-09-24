"""Standalone private remote receipt storage; no command bodies or credentials.

The caller supplies an administrator-provisioned private directory. A receipt
proves only that this guardian returned; it is not board or repair evidence.
"""

import hashlib
import json
import os
import stat
from uuid import UUID


def read_receipt(directory, request_id, command_digest):
    """Read an exact receipt without creating files or deciding task recovery."""
    if (str(UUID(request_id)) != request_id or not isinstance(command_digest, str)
            or len(command_digest) != 64 or any(c not in "0123456789abcdef" for c in command_digest)):
        raise ValueError("exact receipt binding required")
    if os.path.realpath(directory) != directory:
        raise ValueError("canonical receipt directory required")
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(directory_fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("private receipt directory required")
        def load(suffix):
            try:
                fd = os.open(request_id + suffix, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory_fd)
            except FileNotFoundError:
                return None
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                        or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size > 1024):
                    raise ValueError("invalid receipt file")
                raw = os.read(fd, 1025)
                value = json.loads(raw)
                # This format is deliberately canonical: duplicates, extra text,
                # alternate encodings and incomplete writes never establish exit.
                if json.dumps(value, sort_keys=True, separators=(",", ":")).encode() != raw:
                    raise ValueError("noncanonical receipt")
                return value
            finally:
                os.close(fd)
        expected = {"version": 1, "request_id": request_id, "command_digest": command_digest}
        intent, result = load(".intent"), load(".result")
        if intent is None and result is None:
            return {"state": "unknown"}
        if (not isinstance(intent, dict) or set(intent) != set(expected)
                or any(type(intent[k]) is not type(v) or intent[k] != v for k, v in expected.items())):
            raise ValueError("receipt intent mismatch")
        if result is None:
            return {"state": "unknown"}
        if (not isinstance(result, dict) or set(result) != {*expected, "guard_exit_code"}
                or any(type(result[k]) is not type(v) or result[k] != v for k, v in expected.items())
                or type(result["guard_exit_code"]) is not int or not 0 <= result["guard_exit_code"] <= 255):
            raise ValueError("receipt result mismatch")
        return {"state": "guardian_returned", **result}
    finally:
        os.close(directory_fd)


class RemoteJournal:
    def __init__(self, directory, request_id, command):
        if str(UUID(request_id)) != request_id:
            raise ValueError("canonical request identifier required")
        self.binding = {"version": 1, "request_id": request_id,
                        "command_digest": hashlib.sha256(command.encode()).hexdigest()}
        if os.path.realpath(directory) != directory:
            raise ValueError("canonical receipt directory required")
        self.fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(self.fd)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError("private receipt directory required")
            self._write(request_id + ".intent", self.binding)
        except BaseException:
            os.close(self.fd)
            raise

    def _write(self, name, value):
        # Reserve the final name before writing. A crash may leave an incomplete
        # file, but cannot turn the same request into another execution.
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=self.fd)
        try:
            data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(descriptor)
            os.fsync(self.fd)
        finally:
            os.close(descriptor)

    def finish(self, exit_code):
        if type(exit_code) is not int or not 0 <= exit_code <= 255:
            raise ValueError("guardian exit code required")
        self._write(self.binding["request_id"] + ".result",
                    {**self.binding, "guard_exit_code": exit_code})

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
