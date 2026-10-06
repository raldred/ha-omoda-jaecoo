#!/usr/bin/env python3
"""Build an offline, deterministic HA component release from a committed Git tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

DOMAIN = "omoda_jaecoo"
COMPONENT = f"custom_components/{DOMAIN}/"
ARTIFACTS = (f"{DOMAIN}.zip", "SHA256SUMS", "release.json")
VERSION_RE = re.compile(
    r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?\Z"
)
FORBIDDEN_PARTS = {
    "analysis",
    "apps",
    "jaecooapps",
    "captures",
    "tests",
    "__pycache__",
    "node_modules",
    "keys",
    "secrets",
    "certs",
    "certificates",
    "private",
}
ESSENTIAL = {
    "__init__.py",
    "manifest.json",
    "config_flow.py",
    "strings.json",
    "translations/en.json",
    "services.yaml",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
}


class ReleaseError(ValueError):
    """The committed tree or release request is not safe to package."""


def git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, check=False
    )
    if result.returncode:
        raise ReleaseError(
            "Git operation failed; check the repository and committed ref"
        )
    return result.stdout


def validate_path(path: str) -> PurePosixPath:
    parts = path.split("/")
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or any(part in {"", ".", ".."} or part.startswith(".") for part in parts)
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
    ):
        raise ReleaseError(f"Unsafe component path: {path!r}")
    if any(part.lower() in FORBIDDEN_PARTS for part in parts):
        raise ReleaseError(f"Forbidden component path: {path!r}")
    safe = PurePosixPath(path)
    if safe.stem.lower() in {
        "secret",
        "secrets",
        "credential",
        "credentials",
        "private_key",
        "privatekey",
        "token",
        "tokens",
        "key",
        "session",
        "sessions",
        "auth_cache",
    }:
        raise ReleaseError(f"Secret-bearing component filename: {path!r}")
    return safe


def collect_files(repo: Path, commit: str) -> dict[str, bytes]:
    files = {}
    for record in git(repo, "ls-tree", "-rz", "--full-tree", commit).split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        # Non-component files are never read, including captures and credentials.
        if not raw_path.startswith(COMPONENT.encode()) and raw_path != b"README.md":
            continue
        try:
            path = raw_path.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ReleaseError("Non-UTF-8 component path") from exc
        mode, kind, oid = metadata.decode("ascii").split()
        if mode not in {"100644", "100755"} or kind != "blob":
            raise ReleaseError(f"Not a regular committed file: {path!r}")
        relative = "README.md" if path == "README.md" else path[len(COMPONENT) :]
        safe = validate_path(relative)
        allowed = (
            relative in {"LICENSE", "THIRD_PARTY_NOTICES.md", "README.md"}
            or safe.suffix in {".py", ".json", ".yaml"}
            or (safe.parent == PurePosixPath("brand") and safe.suffix == ".png")
        )
        if not allowed:
            raise ReleaseError(f"Unexpected component file type: {relative!r}")
        if relative == "README.md" and path != "README.md":
            raise ReleaseError("Component README conflicts with the root README copy")
        data = git(repo, "cat-file", "blob", oid)
        if re.search(rb"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----", data):
            raise ReleaseError(f"Private key material in release file: {relative!r}")
        files[relative] = data
    missing = ESSENTIAL - files.keys()
    if missing:
        raise ReleaseError(
            f"Missing essential component files: {', '.join(sorted(missing))}"
        )
    return files


def build_release(
    repo: Path,
    output: Path,
    ref: str = "HEAD",
    expected_version: str | None = None,
    force: bool = False,
) -> dict:
    if expected_version is not None and not VERSION_RE.fullmatch(expected_version):
        raise ReleaseError(
            "Expected version must be a semantic version (without a v prefix)"
        )
    commit = (
        git(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
        .decode()
        .strip()
    )
    files = collect_files(repo, commit)
    try:
        manifest = json.loads(files["manifest.json"])
    except (ValueError, UnicodeDecodeError) as exc:
        raise ReleaseError("Invalid component manifest JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("domain") != DOMAIN:
        raise ReleaseError(f"Manifest domain must be {DOMAIN}")
    version = manifest.get("version")
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise ReleaseError("Manifest version must be a semantic version")
    if expected_version is not None and version != expected_version:
        raise ReleaseError("Committed manifest version does not match expected version")
    if (
        any(
            (output / name).exists() or (output / name).is_symlink()
            for name in ARTIFACTS
        )
        and not force
    ):
        raise ReleaseError(
            "Output artifacts already exist; use --force to replace them"
        )
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".release-", dir=output) as staging:
        staging_path = Path(staging)
        archive = staging_path / ARTIFACTS[0]
        with zipfile.ZipFile(
            archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as zipped:
            for relative, data in sorted(files.items()):
                info = zipfile.ZipInfo(
                    f"{DOMAIN}/{relative}", date_time=(1980, 1, 1, 0, 0, 0)
                )
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                zipped.writestr(info, data, compresslevel=9)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        release = {
            "git_sha": commit,
            "version": version,
            "filename": ARTIFACTS[0],
            "file_count": len(files),
            "sha256": digest,
        }
        (staging_path / "release.json").write_text(
            json.dumps(release, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        checksums = []
        for name in (ARTIFACTS[0], "release.json"):
            checksum = hashlib.sha256((staging_path / name).read_bytes()).hexdigest()
            checksums.append(f"{checksum}  {name}\n")
        (staging_path / "SHA256SUMS").write_text("".join(checksums), encoding="ascii")
        for name in ARTIFACTS:
            os.replace(staging_path / name, output / name)
    return release


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, default=Path("dist"))
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument("--expected-version", "--version", dest="expected_version")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    try:
        release = build_release(
            args.repo, args.output, args.ref, args.expected_version, args.force
        )
    except (ReleaseError, OSError) as exc:
        parser.exit(1, f"Release build failed: {exc}\n")
    print(
        f"Built {release['filename']} version {release['version']} from {release['git_sha']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
