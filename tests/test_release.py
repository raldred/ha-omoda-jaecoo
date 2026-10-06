"""Offline packaging tests: all repository contents are synthetic temporary Git trees."""

import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from scripts.build_release import ARTIFACTS, ReleaseError, build_release, validate_path


@pytest.fixture
def repo(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()
    run_git(repository, "init", "-q")
    run_git(repository, "config", "user.name", "Release Test")
    run_git(repository, "config", "user.email", "release@example.invalid")
    component = repository / "custom_components/omoda_jaecoo"
    component.mkdir(parents=True)
    files = {
        "manifest.json": json.dumps({"domain": "omoda_jaecoo", "version": "0.6.0"}),
        "__init__.py": "# synthetic integration\n",
        "config_flow.py": "# synthetic flow\n",
        "strings.json": "{}",
        "services.yaml": "refresh_status: {}\n",
        "translations/en.json": "{}",
        "brand/icon.png": b"synthetic image",
        "LICENSE": "Synthetic license\n",
        "THIRD_PARTY_NOTICES.md": "Synthetic notices\n",
    }
    for name, content in files.items():
        target = component / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content if isinstance(content, bytes) else content.encode())
    (repository / "README.md").write_text("Synthetic readme\n")
    for name in (
        "analysis/private.json",
        "captures/private.key",
        "JaecooApps/app.apk",
        ".env",
        "tests/private.py",
    ):
        target = repository / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("not for distribution\n")
    commit(repository)
    return repository


def run_git(repo, *args):
    return (
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        .stdout.decode()
        .strip()
    )


def commit(repo):
    run_git(repo, "add", "--all")
    run_git(repo, "commit", "-qm", "Synthetic fixture")
    return run_git(repo, "rev-parse", "HEAD")


def test_archive_is_component_only_deterministic_and_checksummed(repo, tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    metadata = build_release(repo, first, expected_version="0.6.0")
    build_release(repo, second, expected_version="0.6.0")
    for name in ARTIFACTS:
        assert (first / name).read_bytes() == (second / name).read_bytes()
    assert metadata == json.loads((first / "release.json").read_text())
    assert metadata["git_sha"] == run_git(repo, "rev-parse", "HEAD")
    assert (
        metadata["sha256"]
        == hashlib.sha256((first / "omoda_jaecoo.zip").read_bytes()).hexdigest()
    )
    for line in (first / "SHA256SUMS").read_text().splitlines():
        checksum, filename = line.split("  ")
        assert hashlib.sha256((first / filename).read_bytes()).hexdigest() == checksum
    with zipfile.ZipFile(first / "omoda_jaecoo.zip") as archive:
        names = archive.namelist()
        assert names == sorted(names)
        assert len(names) == metadata["file_count"] == 10
        assert all(name.startswith("omoda_jaecoo/") for name in names)
        assert "omoda_jaecoo/services.yaml" in names
        assert "omoda_jaecoo/brand/icon.png" in names
        assert archive.read("omoda_jaecoo/README.md") == b"Synthetic readme\n"
        for entry in archive.infolist():
            assert entry.date_time == (1980, 1, 1, 0, 0, 0)
            assert entry.external_attr >> 16 == 0o100644
        assert not any("private" in name or "analysis" in name for name in names)


def test_only_committed_ref_is_packaged(repo, tmp_path):
    old = run_git(repo, "rev-parse", "HEAD")
    manifest = repo / "custom_components/omoda_jaecoo/manifest.json"
    manifest.write_text(json.dumps({"domain": "omoda_jaecoo", "version": "0.7.0"}))
    (repo / "custom_components/omoda_jaecoo/ignored.py").write_text("untracked\n")
    build_release(repo, tmp_path / "dirty", expected_version="0.6.0")
    commit(repo)
    build_release(repo, tmp_path / "old", ref=old, expected_version="0.6.0")
    assert (tmp_path / "dirty/omoda_jaecoo.zip").read_bytes() == (
        tmp_path / "old/omoda_jaecoo.zip"
    ).read_bytes()
    with pytest.raises(ReleaseError, match="does not match"):
        build_release(repo, tmp_path / "mismatch", expected_version="0.6.0")


@pytest.mark.parametrize(
    "version", ["v0.6.0", "../0.6.0", "0.6", "01.6.0", "0.6.0;echo unsafe"]
)
def test_expected_version_is_validated(repo, tmp_path, version):
    with pytest.raises(ReleaseError, match="semantic version"):
        build_release(repo, tmp_path / "dist", expected_version=version)
    assert not (tmp_path / "dist").exists()


@pytest.mark.parametrize(
    "path",
    [
        "../manifest.json",
        "/manifest.json",
        "a/../b.py",
        "a\\b.py",
        "a//b.py",
        ".env",
        "a/\nfile.py",
        "captures/file.json",
        "__pycache__/file.py",
        "keys/key.json",
    ],
)
def test_unsafe_paths_are_rejected(path):
    with pytest.raises(ReleaseError):
        validate_path(path)


@pytest.mark.parametrize(
    "name",
    [
        "private.pem",
        "tokens.txt",
        "credentials.json",
        "secrets.yaml",
        "analysis/notes.json",
        "captures/data.json",
        "__pycache__/code.py",
        ".env",
        "certs/store.json",
        "private/data.json",
        "session.json",
        "auth_cache.json",
    ],
)
def test_forbidden_committed_component_files_fail_closed(repo, tmp_path, name):
    target = repo / "custom_components/omoda_jaecoo" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("synthetic sensitive data")
    commit(repo)
    with pytest.raises(ReleaseError):
        build_release(repo, tmp_path / "dist")
    assert not (tmp_path / "dist").exists()


def test_symlink_rejected(repo, tmp_path):
    (repo / "custom_components/omoda_jaecoo/link.py").symlink_to("__init__.py")
    commit(repo)
    with pytest.raises(ReleaseError, match="regular committed file"):
        build_release(repo, tmp_path / "dist")


def test_private_key_content_rejected(repo, tmp_path):
    (repo / "custom_components/omoda_jaecoo/fixtures.py").write_text(
        'KEY = "-----BEGIN RSA PRIVATE KEY-----"\n'
    )
    commit(repo)
    with pytest.raises(ReleaseError, match="Private key"):
        build_release(repo, tmp_path / "dist")


@pytest.mark.parametrize(
    "manifest",
    [
        {"domain": "other", "version": "0.6.0"},
        {"domain": "omoda_jaecoo", "version": "latest"},
        [],
        "invalid json",
    ],
)
def test_invalid_manifest_rejected(repo, tmp_path, manifest):
    target = repo / "custom_components/omoda_jaecoo/manifest.json"
    target.write_text(manifest if isinstance(manifest, str) else json.dumps(manifest))
    commit(repo)
    with pytest.raises(ReleaseError, match="manifest|Manifest"):
        build_release(repo, tmp_path / "dist")


def test_missing_essential_file_rejected(repo, tmp_path):
    (repo / "custom_components/omoda_jaecoo/LICENSE").unlink()
    commit(repo)
    with pytest.raises(ReleaseError, match="Missing essential"):
        build_release(repo, tmp_path / "dist")


def test_ref_must_resolve_to_commit(repo, tmp_path):
    with pytest.raises(ReleaseError, match="Git operation failed"):
        build_release(repo, tmp_path / "dist", ref="missing-ref")


def test_overwrite_requires_force(repo, tmp_path):
    output = tmp_path / "dist"
    build_release(repo, output)
    with pytest.raises(ReleaseError, match="already exist"):
        build_release(repo, output)
    before = {name: (output / name).read_bytes() for name in ARTIFACTS}
    build_release(repo, output, force=True)
    assert before == {name: (output / name).read_bytes() for name in ARTIFACTS}


def test_cli_version_alias(repo, tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts/build_release.py"
    import sys

    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--repo",
            str(repo),
            "--output",
            str(tmp_path / "dist"),
            "--version",
            "0.6.0",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "version 0.6.0" in result.stdout
