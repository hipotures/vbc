#!/usr/bin/env python3
"""Publish the next VBC image release tag (vbc-X.Y.Z) from a clean, pushed HEAD.

Copied from HomeStack (scripts/tag-image.py). CI builds and publishes
ghcr.io/hipotures/vbc:X.Y.Z from the tag. Never pick versions by hand.
"""

import argparse
import re
import subprocess
import sys


IMAGE_INPUTS = (
    "vbc/__init__.py", "vbc/version.py", "vbc/config", "vbc/contracts",
    "vbc/domain", "vbc/infrastructure", "vbc/maintenance", "vbc/pipeline",
    "vbc/service", "vbc/utils", "pyproject.toml", "uv.lock", ".dockerignore",
    "conf/vbc.yaml.example", "conf/exiftool.conf", "conf/demo.yaml",
    "deploy/container/Containerfile", "deploy/container/entrypoint.sh",
    "deploy/container/make-default-config.py",
)


def git(*args):
    return subprocess.run(["git", *args], check=True, text=True, capture_output=True).stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", choices=["vbc"], default="vbc")
    parser.add_argument("--dry-run", action="store_true", help="fetch tags and show the next release without publishing")
    args = parser.parse_args()
    version_pattern = re.compile(rf"{args.service}-(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", re.ASCII)
    try:
        branch = git("symbolic-ref", "--short", "HEAD")
        remote = git("config", f"branch.{branch}.remote")
        if remote == ".":
            raise ValueError("The branch must track a remote repository")
        if git("status", "--porcelain"):
            raise ValueError("Commit all changes before tagging an image release")
        git("fetch", remote, "--tags")
        if git("rev-parse", "HEAD") != git("rev-parse", "@{upstream}"):
            raise ValueError("Push the branch and resolve any upstream changes before tagging")
        tags = git("tag", "--list").splitlines()
        if any(version_pattern.fullmatch(tag) for tag in git("tag", "--points-at", "HEAD").splitlines()):
            raise ValueError(f"HEAD already has a {args.service} image release")
        versions = [tuple(map(int, match.groups())) for tag in tags if (match := version_pattern.fullmatch(tag))]
        latest = max(versions) if versions else None
        if latest:
            previous_tag = f"{args.service}-" + ".".join(map(str, latest))
            if not git("diff", "--name-only", previous_tag, "HEAD", "--", *IMAGE_INPUTS):
                raise ValueError(f"No server image inputs changed since {previous_tag}; no image release needed")
        version = f"{latest[0]}.{latest[1]}.{latest[2] + 1}" if latest else "0.1.0"
        tag = f"{args.service}-{version}"
        if not args.dry_run:
            git("tag", "-a", tag, "-m", f"{args.service} image release {version}")
            git("push", remote, f"refs/tags/{tag}")
        print(tag)
        return 0
    except (subprocess.CalledProcessError, ValueError) as error:
        detail = error.stderr.strip() if isinstance(error, subprocess.CalledProcessError) else str(error)
        print(f"tag-image: {detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
