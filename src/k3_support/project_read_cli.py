"""Local operator diagnostics, using only the control account's Project profile.

Does not open/migrate the control database, login, bind Bugs, or dispatch writes.
Raw read output can contain private business data: do not publish it as test logs.
"""

import argparse
import json
from dataclasses import asdict

from .project_read_client import READS, MeegleReadClient, ProjectReadError, parse_json
from .project_read_snapshot import SnapshotReader


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Control-account Project read diagnostics"
    )
    parser.add_argument(
        "--executable",
        required=True,
        help="Protected native Meegle binary (absolute path)",
    )
    parser.add_argument("--sha256", required=True, help="Accepted binary SHA-256")
    parser.add_argument("--profile", required=True)
    parser.add_argument(
        "--host", required=True, help="Expected Project host, without scheme"
    )
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("auth-status")
    decode = sub.add_parser("decode")
    decode.add_argument("--url", required=True)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--command", required=True, choices=sorted(READS))
    read = sub.add_parser("read-page")
    read.add_argument("--command", required=True, choices=sorted(READS))
    read.add_argument(
        "--params", required=True, help="Exact JSON read parameters; no @file expansion"
    )
    item = sub.add_parser(
        "read-item", help="Bounded item/schema pagination; no database changes"
    )
    item.add_argument("--project-key", required=True)
    item.add_argument("--type-key", required=True)
    item.add_argument("--item-id", required=True)
    args = parser.parse_args(argv)
    try:
        client = MeegleReadClient(
            executable=args.executable,
            sha256=args.sha256,
            profile=args.profile,
            host=args.host,
        )
        code = 0
        if args.action == "auth-status":
            status = client.auth_status()
            value = asdict(status)
            code = (
                0
                if status.state == "authenticated"
                else (2 if status.state == "unavailable" else 1)
            )
        elif args.action == "decode":
            value = client.decode_workitem_url(args.url)
        elif args.action == "inspect":
            value = client.inspect(args.command)
        elif args.action == "read-item":
            value = SnapshotReader(client).collect(
                {
                    "host": client.host,
                    "project_key": args.project_key,
                    "type_key": args.type_key,
                    "item_id": args.item_id,
                }
            )
        else:
            if len(args.params.encode("utf-8")) > 65536:
                raise ProjectReadError("invalid_read_request")
            value = client.read_page(args.command, parse_json(args.params))
        print(json.dumps(value, ensure_ascii=False, allow_nan=False))
        return code
    except ProjectReadError as exc:
        print(json.dumps({"error": exc.code}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
