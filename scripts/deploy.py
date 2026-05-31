#!/usr/bin/env python3

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv
import os


ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deploy a built micro:bit hex file")
    parser.add_argument("--hex", dest="hex_path", default=None, help="Hex path (default auto-detect)")
    parser.add_argument("--console-url", default=None, help="Console base URL")
    parser.add_argument("--console-key", default=None, help="Console auth key")
    parser.add_argument("--usb-mount", default=None, help="USB mount path (default: /Volumes/MICROBIT)")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout seconds")
    return parser.parse_args()


def resolve_hex_path(explicit_hex: str | None) -> Path:
    if explicit_hex:
        path = Path(explicit_hex).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"Hex file not found: {path}")
        return path

    candidates = [
        ROOT / "MICROBIT.hex",
        ROOT / "build" / "MICROBIT.hex",
        ROOT / "built" / "binary.hex",
    ]

    for path in candidates:
        if path.exists():
            return path

    raise FileNotFoundError(
        "No hex file found. Expected one of: MICROBIT.hex, build/MICROBIT.hex, built/binary.hex"
    )


def deploy_console(console_url: str, console_key: str, hex_path: Path, timeout: int) -> None:
    endpoint = f"{console_url.rstrip('/')}/api/hex"
    data = hex_path.read_bytes()

    response = requests.post(
        endpoint,
        data=data,
        headers={
            "Authorization": console_key,
            "Content-Type": "application/octet-stream",
        },
        timeout=timeout,
    )

    if response.status_code < 200 or response.status_code >= 300:
        raise RuntimeError(f"Console returned HTTP {response.status_code}: {response.text}")

    print(f"Console responded: HTTP {response.status_code}")
    if response.text:
        print(response.text)


def deploy_usb(hex_path: Path, usb_mount: str | None) -> None:
    mount_path = Path(usb_mount or os.environ.get("MICROBIT_MOUNT", "/Volumes/MICROBIT")).expanduser()
    if not mount_path.exists() or not mount_path.is_dir():
        raise FileNotFoundError(
            f"USB mount not found: {mount_path}. Connect micro:bit and verify mount path or set --usb-mount"
        )

    destination = mount_path / hex_path.name
    shutil.copy2(hex_path, destination)
    print(f"Copied {hex_path} -> {destination}")


def main() -> int:
    args = parse_args()

    load_dotenv(ROOT / ".env")

    console_url = args.console_url or os.environ.get("CONSOLE_URL")
    console_key = args.console_key or os.environ.get("CONSOLE_KEY")

    try:
        hex_path = resolve_hex_path(args.hex_path)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if console_url and console_key:
        print(f"Deploying via console: {console_url}")
        try:
            deploy_console(console_url, console_key, hex_path, args.timeout)
            print("Deploy path: console (HTTP POST)")
            return 0
        except Exception as exc:
            print(f"Console deploy failed: {exc}", file=sys.stderr)
            return 1

    print("CONSOLE_URL and/or CONSOLE_KEY not set. Using local USB deploy.")
    try:
        deploy_usb(hex_path, args.usb_mount)
        print("Deploy path: local USB copy")
        return 0
    except Exception as exc:
        print(f"Local deploy failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
