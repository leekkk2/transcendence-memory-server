#!/usr/bin/env python3
"""生成 GitHub Release 附件中的变更记录与产物清单。"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path


def run_git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def detect_tag(explicit_tag: str | None) -> str:
    if explicit_tag:
        return explicit_tag
    env_tag = os.environ.get("GITHUB_REF_NAME", "").strip()
    if env_tag:
        return env_tag
    raise SystemExit("缺少发布 tag，请通过 --tag 或 GITHUB_REF_NAME 提供。")


def previous_tag(current_tag: str) -> str | None:
    tags = [tag.strip() for tag in run_git("tag", "--sort=-version:refname").splitlines() if tag.strip()]
    for tag in tags:
        if tag != current_tag:
            return tag
    return None


def collect_commits(start_tag: str | None) -> list[dict[str, str]]:
    revision = "HEAD" if not start_tag else f"{start_tag}..HEAD"
    output = run_git("log", "--pretty=format:%h%x09%s", revision)
    commits: list[dict[str, str]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        short_sha, subject = line.split("\t", 1)
        commits.append({"sha": short_sha, "subject": subject})
    return commits


def docker_image_repository() -> str:
    explicit_repo = os.environ.get("DOCKER_IMAGE_REPOSITORY", "").strip()
    if explicit_repo:
        return explicit_repo
    github_repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if "/" in github_repo:
        owner = github_repo.split("/", 1)[0]
        return f"{owner}/transcendence-memory-server"
    return "transcendence-memory-server"


def build_release_notes(
    *,
    tag: str,
    version: str,
    commit_sha: str,
    repository: str,
    image_repository: str,
    previous: str | None,
    distributions: list[str],
    commits: list[dict[str, str]],
) -> str:
    compare_url = ""
    if previous:
        compare_url = f"https://github.com/{repository}/compare/{previous}...{tag}"

    lines = [
        f"# Release {tag}",
        "",
        "## 概览",
        f"- Python package version: `{version}`",
        f"- Git commit: `{commit_sha}`",
        f"- Previous tag: `{previous or 'none'}`",
        f"- Compare: {compare_url or 'N/A'}",
        "",
        "## Docker 镜像",
        f"- `{image_repository}:{version}-lite`",
        f"- `{image_repository}:{version}-full`",
        f"- `{image_repository}:lite`",
        f"- `{image_repository}:full`",
        f"- `{image_repository}:latest` -> lite",
        "",
        "## 附件产物",
    ]

    if distributions:
        lines.extend(f"- `{name}`" for name in distributions)
    else:
        lines.append("- 无 Python 构建产物")

    lines.extend(["", "## 变更记录"])
    if commits:
        lines.extend(f"- `{item['sha']}` {item['subject']}" for item in commits)
    else:
        lines.append("- 无新增提交")

    lines.append("")
    return "\n".join(lines)


def build_docker_notes(*, tag: str, version: str, image_repository: str) -> str:
    return "\n".join(
        [
            f"# Docker Image Tags for {tag}",
            "",
            f"- `{image_repository}:{version}-lite`",
            f"- `{image_repository}:{version}-full`",
            f"- `{image_repository}:lite`",
            f"- `{image_repository}:full`",
            f"- `{image_repository}:latest` -> lite",
            "",
        ]
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate GitHub release asset files.")
    parser.add_argument("--tag", help="Release tag, such as v0.3.1")
    parser.add_argument("--output-dir", default="release-assets", help="Directory to place generated assets in")
    args = parser.parse_args()

    tag = detect_tag(args.tag)
    version = tag[1:] if tag.startswith("v") else tag
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    repository = os.environ.get("GITHUB_REPOSITORY", "leekkk2/transcendence-memory-server").strip()
    image_repository = docker_image_repository()
    commit_sha = os.environ.get("GITHUB_SHA", "").strip() or run_git("rev-parse", "HEAD")
    previous = previous_tag(tag)
    commits = collect_commits(previous)

    distributions = sorted(
        path.name
        for pattern in ("*.whl", "*.tar.gz")
        for path in output_dir.glob(pattern)
        if path.is_file()
    )

    notes_path = output_dir / f"release-notes-{tag}.md"
    docker_path = output_dir / f"docker-images-{tag}.md"
    manifest_path = output_dir / f"release-manifest-{tag}.json"

    notes_path.write_text(
        build_release_notes(
            tag=tag,
            version=version,
            commit_sha=commit_sha,
            repository=repository,
            image_repository=image_repository,
            previous=previous,
            distributions=distributions,
            commits=commits,
        ),
        encoding="utf-8",
    )
    docker_path.write_text(
        build_docker_notes(tag=tag, version=version, image_repository=image_repository),
        encoding="utf-8",
    )
    manifest_path.write_text(
        json.dumps(
            {
                "tag": tag,
                "version": version,
                "commit": commit_sha,
                "previous_tag": previous,
                "repository": repository,
                "compare_url": (
                    f"https://github.com/{repository}/compare/{previous}...{tag}"
                    if previous
                    else None
                ),
                "docker_images": {
                    "version_lite": f"{image_repository}:{version}-lite",
                    "version_full": f"{image_repository}:{version}-full",
                    "channel_lite": f"{image_repository}:lite",
                    "channel_full": f"{image_repository}:full",
                    "latest": f"{image_repository}:latest",
                    "latest_points_to": "lite",
                },
                "python_distributions": distributions,
                "commits": commits,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
