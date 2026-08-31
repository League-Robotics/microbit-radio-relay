#!/usr/bin/env python3
"""Do every implementation of name <-> (channel, group) agree with every other?

The spec's digest proves an implementation matches the SPEC. This proves the
implementations match EACH OTHER -- including the ones that are awkward to run,
like the robot's MakeCode extension -- and when they don't, says which name is
the first to differ.

Usage:
    radio_address_conformance.py [--vectors JSON] [--strict] [TARGET ...]

    TARGET   a repository directory (must contain tools/radio-address-dump),
             a dump file, or "auto" (default): this repository plus every
             sibling directory next to it that has tools/radio-address-dump.
    --vectors JSON
             a radio-address-vectors.json to take the published digests from;
             defaults to this repo's mirror. --no-digest skips that check.

Dump protocol versions are told apart by column count. v2 (5 columns:
name,channel,group,decode(name),reverse(channel,group)) digests to the spec's
conformance_sha256 -- D2, the gate, because it hashes the decoder that turns a
name into its default address. v1 (3 columns) digests to full_space_sha256 --
D1, forward only, kept as a bisector. Dumps of different versions are compared on their
first three columns. A v2 dump equal to the spec's published broken-decode
digest is named as such: that implementation's decoder is little-endian.
    --strict an implementation that reports itself unavailable (no compiler,
             no toolchain) fails the run instead of being noted.

Exit 0 iff every dump that ran is identical and matches the digest.

Standalone: stdlib only, no mbrelay import, so any repo can vendor or invoke
it. The dump protocol is documented in tools/radio-address-dump.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve()
REPO = HERE.parents[1]
DUMP_REL = Path("tools") / "radio-address-dump"
DEFAULT_VECTORS = REPO / "server" / "tests" / "radio-address-vectors.json"
UNAVAILABLE = 3
EXPECTED_LINES = 3125


@dataclass
class Dump:
    label: str                  # "<repo>/<impl>" or a file path
    text: str | None            # None when unavailable or failed
    note: str = ""

    @property
    def sha(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest() if self.text is not None else "-"

    @property
    def version(self) -> int:
        """1 for the three-column form, 2 for five; 0 if it is neither."""
        first = self.text.split("\n", 1)[0] if self.text else ""
        return {3: 1, 5: 2}.get(first.count(",") + 1, 0)

    @property
    def forward(self) -> str:
        """The first three columns -- what v1 and v2 dumps share."""
        return "".join(",".join(line.split(",")[:3]) + "\n"
                       for line in self.text.splitlines()) if self.text else ""


def run_dumper(repo: Path) -> list[Dump]:
    tool = repo / DUMP_REL
    listing = subprocess.run([str(tool), "--list"], capture_output=True, text=True)
    if listing.returncode != 0:
        return [Dump(f"{repo.name}/?", None, f"--list failed: {listing.stderr.strip()[:200]}")]
    dumps = []
    for impl in listing.stdout.split():
        label = f"{repo.name}/{impl}"
        r = subprocess.run([str(tool), impl], capture_output=True, text=True)
        if r.returncode == UNAVAILABLE:
            dumps.append(Dump(label, None, f"unavailable: {r.stderr.strip().splitlines()[-1] if r.stderr.strip() else ''}"))
        elif r.returncode != 0:
            dumps.append(Dump(label, None, f"exit {r.returncode}: {r.stderr.strip()[-200:]}"))
        else:
            dumps.append(Dump(label, r.stdout))
    return dumps


def discover(targets: list[str]) -> list[Dump]:
    out: list[Dump] = []
    for t in targets:
        if t == "auto":
            repos = [REPO] + sorted(p for p in REPO.parent.iterdir()
                                    if p.is_dir() and p != REPO and (p / DUMP_REL).exists())
            for repo in repos:
                out.extend(run_dumper(repo))
        else:
            p = Path(t)
            if p.is_dir():
                if not (p / DUMP_REL).exists():
                    out.append(Dump(f"{p.name}/?", None, f"no {DUMP_REL} in {p}"))
                else:
                    out.extend(run_dumper(p))
            elif p.is_file():
                out.append(Dump(str(p), p.read_text()))
            else:
                out.append(Dump(t, None, "no such file or directory"))
    return out


def first_difference(a: str, b: str) -> str:
    la, lb = a.splitlines(), b.splitlines()
    for i, (x, y) in enumerate(zip(la, lb)):
        if x != y:
            return f"line {i + 1} (n={i}): {x!r} vs {y!r}"
    if len(la) != len(lb):
        return f"line counts differ: {len(la)} vs {len(lb)}"
    return "identical"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("targets", nargs="*", default=["auto"])
    ap.add_argument("--vectors", type=Path, default=DEFAULT_VECTORS)
    ap.add_argument("--no-digest", action="store_true")
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args(argv)

    digests: dict[int, str] = {}        # version -> published digest
    broken_decode = None
    if not args.no_digest:
        try:
            props = json.loads(args.vectors.read_text())["properties"]
            digests[1] = props["full_space_sha256"]
            digests[2] = props.get("conformance_sha256")
            broken_decode = props.get("conformance_sha256_broken_decode", {}).get("digest")
        except (OSError, KeyError, ValueError) as exc:
            print(f"warning: no published digest ({exc}); comparing implementations only")

    dumps = discover(args.targets)
    ran = [d for d in dumps if d.text is not None]
    ok = True

    width = max((len(d.label) for d in dumps), default=10)
    print(f"{'implementation':<{width}}  {'sha256':<16}  lines  v  status")
    for d in dumps:
        if d.text is None:
            status = d.note
            if not d.note.startswith("unavailable") or args.strict:
                ok = False
            version = "-"
        else:
            lines = d.text.count("\n")
            version = d.version
            problems = []
            if lines != EXPECTED_LINES:
                problems.append(f"expected {EXPECTED_LINES} lines")
            if version == 0:
                problems.append("neither 3 nor 5 columns")
            elif broken_decode and version == 2 and d.sha == broken_decode:
                problems.append("DECODER IS LITTLE-ENDIAN (matches the spec's broken-decode digest)")
            elif digests.get(version) and d.sha != digests[version]:
                problems.append(f"does not match the published D{version}")
            elif version == 1 and digests.get(2):
                problems.append("v1 dump: forward only, decoder unverified -- prefer v2")
                ok_note = True
            status = "; ".join(problems) if problems else (f"matches D{version}" if digests.get(version) else "ran")
            ok &= not [p for p in problems if not p.startswith("v1 dump")]
        print(f"{d.label:<{width}}  {d.sha[:16]:<16}  {d.text.count(chr(10)) if d.text else '-':>5}  {version!s:>1}  {status}")

    if len(ran) >= 2:
        ref = ran[0]
        print()
        for other in ran[1:]:
            if ref.version == other.version:
                diff = first_difference(ref.text, other.text)
                how = ""
            else:
                diff = first_difference(ref.forward, other.forward)
                how = " (forward columns only: different protocol versions)"
            print(f"{ref.label} vs {other.label}: {diff}{how}")
            ok &= diff == "identical"
    elif len(ran) == 1:
        print("\nonly one implementation ran; nothing to compare it with")
    else:
        print("\nno implementation ran")
        ok = False

    print("\nRESULT:", "all implementations agree" if ok else "DISAGREEMENT or failure -- see above")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
