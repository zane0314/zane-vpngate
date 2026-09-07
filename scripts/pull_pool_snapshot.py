#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pool_snapshot


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pull and apply a VPNGate pool snapshot")
    parser.add_argument("--source-file", type=Path)
    parser.add_argument("--host", default=os.environ.get("UPSTREAM_SYNC_HOST", ""))
    parser.add_argument("--port", type=int, default=int(os.environ.get("UPSTREAM_SYNC_PORT", "22")))
    parser.add_argument("--user", default=os.environ.get("UPSTREAM_SYNC_USER", "aimili-sync"))
    parser.add_argument("--key", type=Path, default=Path(os.environ.get("UPSTREAM_SYNC_KEY", "/etc/aimilivpn/sync_ed25519")))
    parser.add_argument(
        "--known-hosts",
        type=Path,
        default=Path(os.environ.get("UPSTREAM_SYNC_KNOWN_HOSTS", "/etc/aimilivpn/known_hosts")),
    )
    parser.add_argument(
        "--current",
        type=Path,
        default=Path("/opt/aimilivpn/vpngate_data/upstream-snapshot.json"),
    )
    parser.add_argument(
        "--previous",
        type=Path,
        default=Path("/opt/aimilivpn/vpngate_data/upstream-snapshot.previous.json"),
    )
    parser.add_argument("--max-bytes", type=int, default=int(os.environ.get("UPSTREAM_SNAPSHOT_MAX_BYTES", str(8 * 1024 * 1024))))
    parser.add_argument("--max-nodes", type=int, default=int(os.environ.get("UPSTREAM_SNAPSHOT_MAX_NODES", "300")))
    parser.add_argument("--max-age", type=int, default=int(os.environ.get("UPSTREAM_SNAPSHOT_MAX_AGE_SECONDS", "172800")))
    parser.add_argument("--now", type=float, default=None)
    return parser.parse_args()


def ssh_command(args: argparse.Namespace) -> list[str]:
    return [
        "ssh",
        "-p",
        str(args.port),
        "-i",
        str(args.key),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "KbdInteractiveAuthentication=no",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "PermitLocalCommand=no",
        "-o",
        f"UserKnownHostsFile={args.known_hosts}",
        f"{args.user}@{args.host}",
    ]


def download(args: argparse.Namespace, destination: Path) -> None:
    if args.source_file is not None:
        data = args.source_file.read_bytes()
        if len(data) > args.max_bytes:
            raise pool_snapshot.SnapshotValidationError("snapshot file size exceeds limit")
        destination.write_bytes(data)
        return
    if not args.host:
        raise RuntimeError("UPSTREAM_SYNC_HOST is required")
    with destination.open("wb") as output:
        result = subprocess.run(
            ssh_command(args),
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
    if result.returncode != 0:
        raise RuntimeError(f"restricted SSH snapshot read failed with code {result.returncode}")
    if destination.stat().st_size > args.max_bytes:
        raise pool_snapshot.SnapshotValidationError("snapshot file size exceeds limit")


def main() -> int:
    args = parse_args()
    args.current.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".upstream-download.", dir=args.current.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        download(args, temp_path)
        value = pool_snapshot.apply_snapshot_file(
            temp_path,
            args.current,
            args.previous,
            max_bytes=args.max_bytes,
            now=time.time() if args.now is None else args.now,
            max_age_seconds=args.max_age,
            max_nodes=args.max_nodes,
        )
        print(f"applied sequence={value['sequence']} nodes={len(value['nodes'])}")
        return 0
    except (OSError, RuntimeError, pool_snapshot.SnapshotValidationError) as exc:
        print(f"snapshot sync failed: {exc}", file=sys.stderr)
        return 1
    finally:
        temp_path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
