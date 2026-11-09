#!/usr/bin/env python3
"""Validate, fetch, stage and publish third-party packages (ORG-008).

Two kinds of entry, both TOML:

  packages/<namespace>/<name>/<version>.toml   mirrored from upstream as published
  recipes/<name>/<version>.toml                built here from pinned upstream source

Every entry becomes one immutable generic package version in CodeArtifact
(domain gizmo-trinket, repository third-party). Standard library only, so it runs
on any runner with Python 3.11+.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
import urllib.request
from pathlib import Path, PurePosixPath

DOMAIN = "gizmo-trinket"
REPOSITORY = "third-party"
OWN_NAMESPACE = "gizmo-trinket"

# ORG-008 §6: these can be mirrored with their license files. Anything else needs a
# recorded `license_decision`.
PERMISSIVE = {
    "MIT", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0", "Zlib", "NCSA", "ISC",
    "BSL-1.0", "Unlicense", "0BSD",
}

NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
VERSION = re.compile(r"^[0-9][0-9A-Za-z.+-]*$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")


class Invalid(Exception):
    pass


# ── Entries ───────────────────────────────────────────────────────────────────


def identify(path: Path) -> dict:
    """Kind, namespace, name and version of an entry, from its path and contents."""
    parts = PurePosixPath(path.as_posix()).parts
    data = tomllib.loads(path.read_text())
    if len(parts) >= 4 and parts[-4] == "packages":
        namespace, name, version = parts[-3], parts[-2], Path(parts[-1]).stem
        kind = "mirror"
    elif len(parts) >= 3 and parts[-3] == "recipes":
        name, version = parts[-2], Path(parts[-1]).stem
        expected = f"{OWN_NAMESPACE}/{name}"
        if data.get("package") != expected:
            raise Invalid(f"{path}: package must be {expected!r}, so a built package never takes upstream's name")
        namespace, kind = OWN_NAMESPACE, "recipe"
    else:
        raise Invalid(f"{path}: not under packages/<namespace>/<name>/ or recipes/<name>/")
    for label, value in (("namespace", namespace), ("name", name)):
        if not NAME.match(value):
            raise Invalid(f"{path}: {label} {value!r} must be lowercase letters, digits, '.', '_' or '-'")
    if not VERSION.match(version):
        raise Invalid(f"{path}: version {version!r} is not a version")
    return {"kind": kind, "namespace": namespace, "name": name, "version": version, "data": data, "path": path}


def validate(entry: dict) -> None:
    d, path = entry["data"], entry["path"]

    license_id = d.get("license")
    if not isinstance(license_id, str) or not license_id:
        raise Invalid(f"{path}: license (an SPDX identifier) is required")
    if license_id not in PERMISSIVE and not str(d.get("license_decision", "")).strip():
        raise Invalid(f"{path}: {license_id} is not on the permissive list; record why we may redistribute it in license_decision (ORG-008 §6)")
    files = d.get("license_files")
    if not isinstance(files, list) or not files:
        raise Invalid(f"{path}: license_files must list the license and notice files to ship with it")

    if entry["kind"] == "mirror":
        assets = d.get("asset")
        if not isinstance(assets, list) or not assets:
            raise Invalid(f"{path}: at least one [[asset]] is required")
        seen = set()
        for a in assets:
            for key in ("platform", "url", "sha256"):
                if not isinstance(a.get(key), str) or not a[key]:
                    raise Invalid(f"{path}: every [[asset]] needs {key}")
            if not a["url"].startswith("https://"):
                raise Invalid(f"{path}: {a['url']} is not https")
            if not SHA256.match(a["sha256"]):
                raise Invalid(f"{path}: {a['platform']} sha256 must be 64 lowercase hex characters")
            if a["platform"] in seen:
                raise Invalid(f"{path}: platform {a['platform']} appears twice")
            seen.add(a["platform"])
        for f in files:
            if not str(f).startswith("https://"):
                raise Invalid(f"{path}: license file {f} must be an https URL")
    else:
        for key in ("source", "tag", "commit"):
            if not isinstance(d.get(key), str) or not d[key]:
                raise Invalid(f"{path}: {key} is required")
        if not COMMIT.match(d["commit"]):
            raise Invalid(f"{path}: commit must be a full 40-character SHA; a tag can move")
        if not (path.parent / "build.sh").is_file():
            raise Invalid(f"{path}: recipes/{entry['name']}/build.sh is missing")
        platforms = d.get("platform")
        if not isinstance(platforms, list) or not platforms:
            raise Invalid(f"{path}: at least one [[platform]] is required")
        seen = set()
        for p in platforms:
            for key in ("name", "runner"):
                if not isinstance(p.get(key), str) or not p[key]:
                    raise Invalid(f"{path}: every [[platform]] needs {key}")
            if not isinstance(p.get("flags", []), list) or not isinstance(p.get("keep"), list) or not p["keep"]:
                raise Invalid(f"{path}: platform {p['name']} needs flags (a list) and keep (a non-empty list)")
            if p["name"] in seen:
                raise Invalid(f"{path}: platform {p['name']} appears twice")
            seen.add(p["name"])


def load(path: str) -> dict:
    entry = identify(Path(path))
    validate(entry)
    return entry


# ── Commands ──────────────────────────────────────────────────────────────────


def cmd_validate(args) -> None:
    failed = False
    for p in args.files:
        try:
            load(p)
            print(f"ok  {p}")
        except (Invalid, tomllib.TOMLDecodeError) as e:
            print(f"::error file={p}::{e}")
            failed = True
    if failed:
        sys.exit(1)


def cmd_changed(args) -> None:
    """Entries added or modified between two commits, as JSON lists."""
    out = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=AM", args.base, args.head, "--", "packages", "recipes"],
        check=True, capture_output=True, text=True,
    ).stdout.split()
    packages = sorted(p for p in out if p.startswith("packages/") and p.endswith(".toml"))
    recipes = sorted(p for p in out if p.startswith("recipes/") and p.endswith(".toml"))
    # A changed build.sh rebuilds the newest version of its recipe.
    for d in sorted({str(Path(p).parent) for p in out if p.startswith("recipes/") and p.endswith("build.sh")}):
        versions = sorted(Path(d).glob("*.toml"))
        if versions and str(versions[-1]) not in recipes:
            recipes.append(str(versions[-1]))
    print(json.dumps({"packages": packages, "recipes": recipes}))


def cmd_matrix(args) -> None:
    rows = []
    for p in args.files:
        e = load(p)
        for plat in e["data"]["platform"]:
            rows.append({"recipe": p, "platform": plat["name"], "runner": plat["runner"]})
    print(json.dumps(rows))


def cmd_flags(args) -> None:
    """A platform's build flags, NUL-separated, for `mapfile -d ''`."""
    e = load(args.file)
    plat = next(p for p in e["data"]["platform"] if p["name"] == args.platform)
    sys.stdout.write("".join(f + "\0" for f in plat.get("flags", [])))


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "gizmo-trinket-third-party-mirror"})
    with urllib.request.urlopen(req, timeout=300) as r, dest.open("wb") as f:
        shutil.copyfileobj(r, f)


def cmd_fetch(args) -> None:
    """Download a mirrored entry's assets and license files, and verify every hash."""
    e = load(args.file)
    if e["kind"] != "mirror":
        raise Invalid(f"{args.file}: only mirrored packages are fetched; recipes are built")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for a in e["data"]["asset"]:
        name = a.get("filename") or Path(a["url"].split("?")[0]).name
        dest = out / name
        download(a["url"], dest)
        got = sha256_of(dest)
        if got != a["sha256"]:
            dest.unlink()
            raise Invalid(f"{args.file}: {name} has sha256 {got}, manifest says {a['sha256']}")
        print(f"ok  {name}  {got}")
    for url in e["data"]["license_files"]:
        download(url, out / Path(url).name)
        print(f"ok  {Path(url).name} (license)")
    shutil.copy(args.file, out / f"{e['name']}-{e['version']}.toml")


def matches(rel: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(rel, p.replace("**", "*")) for p in patterns)


def cmd_stage(args) -> None:
    """Archive the kept files of a recipe build, with its licenses and the recipe."""
    e = load(args.file)
    plat = next(p for p in e["data"]["platform"] if p["name"] == args.platform)
    install, src, out = Path(args.install), Path(args.source), Path(args.out)
    staging = out / "staging"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)

    kept = 0
    for root, dirs, files in os.walk(install):
        for f in files + [d for d in dirs if (Path(root) / d).is_symlink()]:
            p = Path(root) / f
            rel = p.relative_to(install).as_posix()
            if not matches(rel, plat["keep"]):
                continue
            dest = staging / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if p.is_symlink():
                os.symlink(os.readlink(p), dest)
            else:
                shutil.copy2(p, dest)
            kept += 1
    for pattern in plat["keep"]:
        if not any(matches(q.relative_to(staging).as_posix(), [pattern]) for q in staging.rglob("*")):
            raise Invalid(f"{args.file}: keep pattern {pattern!r} matched nothing in the install tree")

    for lf in e["data"]["license_files"]:
        shutil.copy(src / lf, staging / Path(lf).name)
    shutil.copy(args.file, staging / "recipe.toml")

    archive = out / f"{e['name']}-{e['version']}-{plat['name']}.tar.gz"

    def normalise(t: tarfile.TarInfo) -> tarfile.TarInfo:
        t.uid = t.gid = 0
        t.uname = t.gname = ""
        t.mtime = 0
        return t

    with tarfile.open(archive, "w:gz") as tar:
        for q in sorted(staging.rglob("*")):
            tar.add(q, arcname=q.relative_to(staging).as_posix(), recursive=False, filter=normalise)
    shutil.rmtree(staging)
    print(f"staged {archive.name}: {kept} files kept, sha256 {sha256_of(archive)}")


def aws(*argv: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["aws", "codeartifact", *argv], check=check, capture_output=True, text=True)


def cmd_publish(args) -> None:
    """Publish every file in a directory as the assets of one new package version."""
    e = identify(Path(args.file))
    files = sorted(p for p in Path(args.dir).iterdir() if p.is_file())
    if not files:
        raise Invalid(f"{args.dir} is empty")
    coords = ["--domain", DOMAIN, "--repository", REPOSITORY, "--format", "generic",
              "--namespace", e["namespace"], "--package", e["name"]]

    existing = aws("describe-package-version", *coords, "--package-version", e["version"], check=False)
    if existing.returncode == 0:
        raise Invalid(f"{e['namespace']}/{e['name']}@{e['version']} is already published; a change is a new version (+rN)")
    if "ResourceNotFoundException" not in existing.stderr:
        raise Invalid(f"could not check for an existing version: {existing.stderr.strip()}")

    for i, f in enumerate(files):
        last = i == len(files) - 1
        argv = [*coords, "--package-version", e["version"], "--asset-content", str(f),
                "--asset-name", f.name, "--asset-sha256", sha256_of(f)]
        if not last:
            argv.append("--unfinished")
        r = aws("publish-package-version", *argv, check=False)
        if r.returncode != 0:
            raise Invalid(f"publishing {f.name} failed: {r.stderr.strip()}")
        print(f"published {e['namespace']}/{e['name']}@{e['version']}  {f.name}{'' if last else '  (unfinished)'}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(required=True)

    p = sub.add_parser("validate"); p.add_argument("files", nargs="+"); p.set_defaults(fn=cmd_validate)
    p = sub.add_parser("changed"); p.add_argument("base"); p.add_argument("head"); p.set_defaults(fn=cmd_changed)
    p = sub.add_parser("matrix"); p.add_argument("files", nargs="+"); p.set_defaults(fn=cmd_matrix)
    p = sub.add_parser("flags"); p.add_argument("file"); p.add_argument("platform"); p.set_defaults(fn=cmd_flags)
    p = sub.add_parser("fetch"); p.add_argument("file"); p.add_argument("out"); p.set_defaults(fn=cmd_fetch)
    p = sub.add_parser("stage")
    p.add_argument("file"); p.add_argument("platform"); p.add_argument("--install", required=True)
    p.add_argument("--source", required=True); p.add_argument("--out", required=True); p.set_defaults(fn=cmd_stage)
    p = sub.add_parser("publish"); p.add_argument("file"); p.add_argument("dir"); p.set_defaults(fn=cmd_publish)

    args = ap.parse_args()
    try:
        args.fn(args)
    except Invalid as e:
        print(f"::error::{e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
