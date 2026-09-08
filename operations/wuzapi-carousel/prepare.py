#!/usr/bin/env python3
"""Prepare an isolated, pinned WuzAPI source tree; never deploy or pair sessions."""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(archive, output):
    manifest = json.loads((HERE / "manifest.json").read_text())
    if sha256(archive) != manifest["upstream_archive_sha256"]:
        raise ValueError("Upstream archive differs from the inspected pin")
    if output.exists() or output.is_symlink():
        raise ValueError("Output must be a new directory; existing trees are untouched")
    for name in manifest["files"]:
        if not (HERE / "overlay" / name).is_file():
            raise ValueError("Overlay is incomplete")
    with tarfile.open(archive) as bundle:
        members = bundle.getmembers()
        roots = {PurePosixPath(member.name).parts[0] for member in members}
        if len(roots) != 1:
            raise ValueError("Unexpected upstream archive layout")
        for member in members:
            path = PurePosixPath(member.name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or not (member.isdir() or member.isfile())
            ):
                raise ValueError("Unsafe archive member")
        output.mkdir(parents=True, mode=0o700)
        for member in members:
            relative = PurePosixPath(member.name).parts[1:]
            if not relative:
                continue
            destination = output.joinpath(*relative)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with bundle.extractfile(member) as source, destination.open(
                    "xb"
                ) as dest:
                    shutil.copyfileobj(source, dest)
                destination.chmod(0o755 if member.mode & 0o111 else 0o644)
    subprocess.run(
        ["git", "apply", "--check", str(HERE / "routes.patch")],
        cwd=output,
        check=True,
    )
    subprocess.run(["git", "apply", str(HERE / "routes.patch")], cwd=output, check=True)
    for name in manifest["files"]:
        destination = output / name
        if destination.exists():
            raise ValueError("Overlay collides with upstream source")
        shutil.copyfile(HERE / "overlay" / name, destination)
    hashes = {
        path.relative_to(output).as_posix(): sha256(path)
        for path in sorted(output.rglob("*"))
        if path.is_file()
    }
    return {
        "upstream_commit": manifest["upstream_commit"],
        "upstream_archive_sha256": sha256(archive),
        "source_tree_sha256": hashlib.sha256(
            json.dumps(hashes, sort_keys=True).encode()
        ).hexdigest(),
        "files": hashes,
        "experimental": True,
        "deployed": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--download", action="store_true")
    args = parser.parse_args()
    archive = args.archive.absolute()
    output = args.output.absolute()
    if args.download:
        manifest = json.loads((HERE / "manifest.json").read_text())
        archive.parent.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(
            manifest["upstream_archive_url"], headers={"User-Agent": "carousel-lab"}
        )
        with archive.open("xb") as target:
            with urllib.request.urlopen(request, timeout=60) as response:
                shutil.copyfileobj(response, target)
    result = prepare(archive, output)
    evidence = output.with_name(output.name + ".prepared.json")
    with evidence.open("x") as target:
        json.dump(result, target, indent=2, sort_keys=True)
        target.write("\n")
    json.dump({"output": str(output), "evidence": str(evidence)}, sys.stdout)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
