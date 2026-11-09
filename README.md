# third-party

What third-party binaries we redistribute, at which version, from where and under
which license, and the recipes for the few we have to build ourselves. Each entry
here becomes one immutable generic package in our CodeArtifact registry (domain
`gizmo-trinket`, repository `third-party`, delivery account). The decision is
**ORG-008** in platform-docs.

## Add a mirrored package

Upstream publishes a binary we can use as it is. Add
`packages/<namespace>/<name>/<version>.toml`, where the namespace is the upstream
organisation:

```toml
# packages/microsoft/onnxruntime/1.23.2.toml
license       = "MIT"
license_files = ["https://raw.githubusercontent.com/microsoft/onnxruntime/v1.23.2/LICENSE",
                 "https://raw.githubusercontent.com/microsoft/onnxruntime/v1.23.2/ThirdPartyNotices.txt"]

[[asset]]
platform = "win-x64"
url      = "https://github.com/microsoft/onnxruntime/releases/download/v1.23.2/onnxruntime-win-x64-1.23.2.zip"
sha256   = "<from upstream's published checksums>"
```

- **Take the SHA-256 from upstream** (a checksums file, release notes, a signature),
  not from your own download. The check is only worth something if the hash comes
  from upstream.
- **The license** is an SPDX identifier. MIT, BSD, Apache-2.0, Zlib, NCSA, ISC and a
  few others pass as they are. Anything else needs a `license_decision` saying why we
  may redistribute it (ORG-008 §6). Some things are never mirrored: the VC++ runtime
  stays a download from Microsoft.

The pull request downloads every asset and checks its hash. Merging publishes it.

## Add a built package

Upstream publishes nothing usable, so we build it. Add
`recipes/<name>/<version>.toml` and `recipes/<name>/build.sh`. See
`recipes/onnxruntime-webgpu/` for a complete example.

- `package` must be `gizmo-trinket/<name>`. A built package never takes upstream's
  name.
- `commit` is the full SHA behind `tag`. The build fetches that commit, never the
  tag, because a tag can be moved after the recipe was reviewed.
- Each `[[platform]]` has a GitHub runner, the build flags, and the `keep` patterns
  that pick files from the install tree. `build.sh` gets `SRC_DIR`, `INSTALL_DIR` and
  `FLAGS_FILE` (the flags, NUL-separated).

**The pull request builds every platform** and uploads the result as a CI artifact,
so you can test it before anything is published. **Merging builds again**, records a
build provenance attestation (`gh attestation verify <file> -R gizmo-trinket/third-party`),
and publishes it.

## Versions never change

A published version is never replaced, and publishing an existing version fails. A
change is a new version with build metadata (`1.23.2+r2`), as a new file next to the
old one. That includes rebuilding a recipe with nothing changed, because native
builds aren't byte-for-byte reproducible.

## Fetching a package

CI assumes `arn:aws:iam::185529490620:role/github-actions-third-party-read` (repositories
listed in infra-live `delivery/us-east-2/third-party/iam-read`). Developers use the
`gizmo-shared-services-admin` SSO profile.

```sh
aws codeartifact get-package-version-asset --domain gizmo-trinket --repository third-party \
  --format generic --namespace gizmo-trinket --package onnxruntime-webgpu \
  --package-version 1.23.2 --asset onnxruntime-webgpu-1.23.2-osx-arm64.tar.gz out.tar.gz
echo "<pinned sha256>  out.tar.gz" | shasum -a 256 -c
```

Pin the version **and** the SHA-256, and check it after every download.

## Local checks

```sh
python3 -m unittest discover -s tests
python3 scripts/registry.py validate packages/**/*.toml recipes/*/*.toml
```
