#!/usr/bin/env python3

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build and deploy micro:bit firmware")

    parser.add_argument("--clean", action="store_true", help="Run clean build")
    parser.add_argument("--verbose", action="store_true", help="Verbose build output")
    parser.add_argument("--parallelism", default=None, help="Build parallelism")

    parser.add_argument("--hex", dest="hex_path", default=None, help="Hex path (default auto-detect)")
    parser.add_argument("--console-url", default=None, help="Console base URL")
    parser.add_argument("--console-key", default=None, help="Console auth key")
    parser.add_argument("--usb-mount", default=None, help="USB mount path")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout seconds")

    return parser.parse_args()


def run_build(args: argparse.Namespace) -> int:
    cmd = [sys.executable, str(SCRIPTS / "build.py")]
    if args.clean:
        cmd.append("--clean")
    if args.verbose:
        cmd.append("--verbose")
    if args.parallelism:
        cmd.extend(["--parallelism", str(args.parallelism)])

    print("==> Running build")
    return subprocess.run(cmd, cwd=ROOT).returncode


def run_deploy(args: argparse.Namespace) -> int:
    cmd = [sys.executable, str(SCRIPTS / "deploy.py")]

    if args.hex_path:
        cmd.extend(["--hex", args.hex_path])
    if args.console_url:
        cmd.extend(["--console-url", args.console_url])
    if args.console_key:
        cmd.extend(["--console-key", args.console_key])
    if args.usb_mount:
        cmd.extend(["--usb-mount", args.usb_mount])
    cmd.extend(["--timeout", str(args.timeout)])

    print("==> Running deploy")
    return subprocess.run(cmd, cwd=ROOT).returncode


def main() -> int:
    args = parse_args()

    build_rc = run_build(args)
    if build_rc != 0:
        print("Build failed. Cannot deploy.", file=sys.stderr)
        return build_rc

    return run_deploy(args)


if __name__ == "__main__":
    raise SystemExit(main())
