#!/usr/bin/env python3
"""Deploy Angular frontend from GitHub releases with atomic updates."""

import argparse
import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def hash_file(path: Path) -> str:
    """Compute SHA256 hash of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def get_file_hashes(directory: Path) -> dict[str, str]:
    """Get hashes of all files in a directory."""
    hashes = {}
    if directory.exists():
        for path in directory.rglob("*"):
            if path.is_file():
                rel_path = str(path.relative_to(directory))
                hashes[rel_path] = hash_file(path)
    return hashes


def download_release(repo: str, tmp_dir: Path) -> Path:
    """Download latest release zip from GitHub."""
    zip_path = tmp_dir / "build.zip"
    url = f"https://github.com/{repo}/releases/latest/download/build.zip"
    print(f"Downloading from {url}...")
    subprocess.run(["curl", "-sfL", url, "-o", str(zip_path)], check=True)
    return zip_path


def extract_zip(zip_path: Path, extract_to: Path) -> None:
    """Extract zip file to directory."""
    print(f"Extracting to {extract_to}...")
    shutil.unpack_archive(zip_path, extract_to)


def deploy_atomic(src: Path, dest: Path, keep_versions: int = 3, save_version_enabled: bool = True) -> None:
    """
    Deploy files atomically with zero-downtime strategy:
    1. Copy hashed files first (immutable assets)
    2. Copy non-hashed files last (index.html, etc.)
    3. Remove orphaned files from old deployment
    """
    old_hashes = get_file_hashes(dest)
    new_hashes = get_file_hashes(src)

    # Separate hashed (immutable) files from entry points
    # Hashed files contain a hash pattern like -ABC123. or .abc123. before extension
    def is_hashed(filename: str) -> bool:
        import re
        basename = filename.rsplit("/", 1)[-1]
        # Match patterns like: chunk-ABC123.js, main-XYZ789.js, styles-ABC123.css
        return bool(re.search(r"[-.][A-Za-z0-9]{7,}\.[a-z]+$", basename))

    hashed_files = [f for f in new_hashes if is_hashed(f)]
    entry_files = [f for f in new_hashes if not is_hashed(f)]

    dest.mkdir(parents=True, exist_ok=True)

    # Phase 1: Copy hashed/immutable files first
    copied_hashed = 0
    for rel_path in hashed_files:
        src_file = src / rel_path
        dest_file = dest / rel_path
        if old_hashes.get(rel_path) != new_hashes[rel_path]:
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dest_file)
            copied_hashed += 1
    print(f"Copied {copied_hashed}/{len(hashed_files)} hashed assets (skipped {len(hashed_files) - copied_hashed} unchanged)")

    # Phase 2: Copy entry point files (index.html, etc.)
    print(f"Copying {len(entry_files)} entry files...")
    for rel_path in entry_files:
        src_file = src / rel_path
        dest_file = dest / rel_path
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dest_file)

    # Phase 3: Remove orphaned files
    orphaned = set(old_hashes.keys()) - set(new_hashes.keys())
    if orphaned:
        print(f"Removing {len(orphaned)} orphaned files...")
        for rel_path in orphaned:
            orphan_file = dest / rel_path
            if orphan_file.exists():
                orphan_file.unlink()
        # Clean up empty directories
        for dir_path in sorted(dest.rglob("*"), key=lambda p: -len(p.parts)):
            if dir_path.is_dir() and not any(dir_path.iterdir()):
                dir_path.rmdir()

    # Phase 4: Save version for rollback
    if save_version_enabled:
        save_version(src, dest.parent / "versions", keep_versions)


def save_version(src: Path, versions_dir: Path, keep: int) -> None:
    """Save current deployment for rollback, keeping N versions."""
    import time

    versions_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    version_path = versions_dir / timestamp

    print(f"Saving version {timestamp}...")
    shutil.copytree(src, version_path)

    # Cleanup old versions
    versions = sorted(versions_dir.iterdir(), reverse=True)
    for old_version in versions[keep:]:
        print(f"Removing old version {old_version.name}...")
        shutil.rmtree(old_version)


def rollback(dest: Path, versions_dir: Path, version: str | None = None) -> None:
    """Rollback to a previous version."""
    if not versions_dir.exists():
        print("No versions available for rollback", file=sys.stderr)
        sys.exit(1)

    versions = sorted(versions_dir.iterdir(), reverse=True)
    if not versions:
        print("No versions available for rollback", file=sys.stderr)
        sys.exit(1)

    if version:
        version_path = versions_dir / version
        if not version_path.exists():
            print(f"Version {version} not found", file=sys.stderr)
            print(f"Available: {', '.join(v.name for v in versions)}", file=sys.stderr)
            sys.exit(1)
    else:
        version_path = versions[0]

    print(f"Rolling back to {version_path.name}...")
    deploy_atomic(version_path, dest, save_version_enabled=False)
    print("Rollback complete!")


def list_versions(versions_dir: Path) -> None:
    """List available versions for rollback."""
    if not versions_dir.exists():
        print("No versions available")
        return

    versions = sorted(versions_dir.iterdir(), reverse=True)
    if not versions:
        print("No versions available")
        return

    print("Available versions:")
    for v in versions:
        print(f"  {v.name}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Deploy Angular frontend from GitHub releases")
    parser.add_argument("--webroot", "-w", default="/var/www/html", help="Web root directory")
    parser.add_argument("--repo", "-r", default="realworld-apps/angular-realworld-example-app", help="GitHub repo")
    parser.add_argument("--keep-versions", "-k", type=int, default=3, help="Number of versions to keep for rollback")
    parser.add_argument("--rollback", "-R", nargs="?", const="", metavar="VERSION", help="Rollback to previous version")
    parser.add_argument("--list-versions", "-l", action="store_true", help="List available versions")
    parser.add_argument("--dry-run", "-n", action="store_true", help="Show what would be done")

    args = parser.parse_args()
    webroot = Path(args.webroot)
    versions_dir = webroot.parent / "versions"

    if args.list_versions:
        list_versions(versions_dir)
        return

    if args.rollback is not None:
        version = args.rollback if args.rollback else None
        rollback(webroot, versions_dir, version)
        return

    if args.dry_run:
        print(f"Would deploy {args.repo} to {webroot}")
        print(f"Would keep {args.keep_versions} versions in {versions_dir}")
        return

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        zip_path = download_release(args.repo, tmp_dir)
        extract_dir = tmp_dir / "extracted"
        extract_zip(zip_path, extract_dir)

        # Find the actual build directory (might be nested)
        build_dirs = list(extract_dir.rglob("index.html"))
        if not build_dirs:
            print("No index.html found in archive", file=sys.stderr)
            sys.exit(1)
        build_dir = build_dirs[0].parent

        deploy_atomic(build_dir, webroot, args.keep_versions)

    print("Deployment complete!")


if __name__ == "__main__":
    main()
