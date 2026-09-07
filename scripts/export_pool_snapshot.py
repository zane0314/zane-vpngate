#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pool_snapshot


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return default


def next_sequence(path: Path) -> int:
    value = read_json(path, {})
    try:
        return max(1, int(value.get("sequence", 0)) + 1)
    except (AttributeError, TypeError, ValueError):
        return 1


def atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export authoritative VPNGate pools")
    parser.add_argument("--nodes-file", type=Path, default=Path("/opt/aimilivpn/vpngate_data/nodes.json"))
    parser.add_argument("--state-file", type=Path, default=Path("/opt/aimilivpn/vpngate_data/state.json"))
    parser.add_argument("--output", type=Path, default=Path("/var/lib/aimilivpn-export/pool-snapshot.json"))
    parser.add_argument("--source-instance", default="tokyo")
    parser.add_argument("--now", type=float, default=None)
    parser.add_argument("--max-nodes", type=int, default=300)
    parser.add_argument("--max-bytes", type=int, default=8 * 1024 * 1024)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    nodes = read_json(args.nodes_file, [])
    state = read_json(args.state_file, {})
    if not isinstance(nodes, list) or not isinstance(state, dict):
        print("invalid nodes or state JSON", file=sys.stderr)
        return 1
    now = time.time() if args.now is None else args.now
    value = pool_snapshot.build_snapshot(
        nodes,
        state,
        sequence=next_sequence(args.output),
        generated_at=now,
        source_instance=args.source_instance,
        max_nodes=args.max_nodes,
    )
    pool_snapshot.validate_snapshot(
        value, now=now, max_age_seconds=172800, max_nodes=args.max_nodes
    )
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    if len(payload) > args.max_bytes:
        print("snapshot file size exceeds limit", file=sys.stderr)
        return 1
    atomic_write(args.output, payload)
    print(f"exported sequence={value['sequence']} nodes={len(value['nodes'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
