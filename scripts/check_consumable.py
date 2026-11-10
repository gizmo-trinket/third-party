#!/usr/bin/env python3
"""Download every published package the way a consumer does, and check it (ORG-008 §5).

For each entry under packages/ and recipes/, fetch every asset from CodeArtifact
with whatever AWS credentials are in the environment (in CI, the read role), then:

  - check its SHA-256: against the manifest for mirrored packages, against
    CodeArtifact's recorded hash for built ones (whose hash is ours; their
    provenance is checked separately with `gh attestation verify`);
  - open it, by type, to show it is what it claims: an LZMA2 tar lists, a zip
    passes testzip, an AppImage is ELF, a DLL is PE, a shell script parses.

Exit status is non-zero if anything is missing, mismatched or unreadable.
"""

from __future__ import annotations

import hashlib
import io
import json
import lzma
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path

DOMAIN, REPOSITORY = "gizmo-trinket", "third-party"


def aws(*argv: str) -> str:
    return subprocess.run(["aws", "codeartifact", *argv, "--domain", DOMAIN, "--repository", REPOSITORY,
                           "--format", "generic"], check=True, capture_output=True, text=True).stdout


def entries() -> list[tuple[str, str, str, Path, dict]]:
    out = []
    for m in sorted(Path("packages").glob("*/*/*.toml")):
        out.append((m.parts[1], m.parts[2], m.stem, m, tomllib.loads(m.read_text())))
    for m in sorted(Path("recipes").glob("*/*.toml")):
        out.append(("gizmo-trinket", m.parts[1], m.stem, m, tomllib.loads(m.read_text())))
    return out


def opens(path: Path) -> str:
    """What the file turned out to be, or raise if it isn't what its name says."""
    name, head = path.name, path.read_bytes()[:4]
    if name.endswith(".tar.lzma2"):
        d = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=[{"id": lzma.FILTER_LZMA2, "dict_size": 1 << 26}])
        members = tarfile.open(fileobj=io.BytesIO(d.decompress(path.read_bytes()))).getnames()
        return f"LZMA2 tar, {len(members)} entries: {', '.join(members)}"
    if name.endswith(".tar.gz"):
        with tarfile.open(path) as t:
            return f"gzip tar, {len(t.getnames())} entries"
    if name.endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            bad = z.testzip()
            if bad:
                raise ValueError(f"corrupt member {bad}")
            return f"zip, {len(z.namelist())} entries"
    if name.endswith(".AppImage") or name.startswith("AppRun"):
        if head != b"\x7fELF":
            raise ValueError("not an ELF executable")
        return "ELF executable"
    if name.endswith(".dll"):
        if head[:2] != b"MZ":
            raise ValueError("not a PE file")
        return "PE (Windows DLL)"
    if name.endswith(".sh"):
        subprocess.run(["bash", "-n", str(path)], check=True)
        return "shell script, parses"
    if name.startswith("license-") or name.endswith(".toml"):
        if path.stat().st_size == 0:
            raise ValueError("empty")
        return "text"
    return "unchecked type"


def main() -> int:
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        for ns, name, version, manifest, data in entries():
            label = f"{ns}/{name}@{version}"
            coords = ["--namespace", ns, "--package", name, "--package-version", version]
            try:
                listed = json.loads(aws("list-package-version-assets", *coords))["assets"]
            except subprocess.CalledProcessError as e:
                print(f"::error::{label}: not readable from the registry: {e.stderr.strip()}")
                failures += 1
                continue
            recorded = {a["name"]: a["hashes"]["SHA-256"] for a in listed}
            expected = {}
            if manifest.parts[0] == "packages":
                for a in data["asset"]:
                    expected[a.get("filename") or Path(a["url"].split("?")[0]).name] = a["sha256"]
            else:
                for p in data["platform"]:
                    expected[f"{name}-{version}-{p['name']}.tar.gz"] = recorded.get(f"{name}-{version}-{p['name']}.tar.gz", "")
            missing = sorted(set(expected) - set(recorded))
            for m in missing:
                print(f"::error::{label}: asset {m} is not in the registry")
            failures += len(missing)
            for asset in sorted(recorded):
                dest = Path(tmp) / asset
                aws("get-package-version-asset", *coords, "--asset", asset, str(dest))
                got = hashlib.sha256(dest.read_bytes()).hexdigest()
                want = expected.get(asset, recorded[asset])
                if got != want or got != recorded[asset]:
                    print(f"::error::{label}: {asset} downloaded as {got}, expected {want}, registry records {recorded[asset]}")
                    failures += 1
                    continue
                try:
                    kind = opens(dest)
                except Exception as e:  # noqa: BLE001 - every failure is reported, not raised
                    print(f"::error::{label}: {asset} does not open: {e}")
                    failures += 1
                    continue
                print(f"ok  {label}  {asset}  {got[:12]}  {kind}")
                dest.unlink()
    print(f"{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
