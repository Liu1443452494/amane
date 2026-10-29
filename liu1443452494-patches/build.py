"""自用构建的版本校验、补丁应用与逻辑验证入口。"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class BuildError(RuntimeError):
    """需要立即终止构建的错误。"""


def git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode:
        raise BuildError(result.stderr.strip() or result.stdout.strip() or "Git 命令执行失败")
    return result.stdout.strip()


def validate_version(value: str, root: Path) -> str:
    if len(value) > 128 or re.fullmatch(r"v[0-9]+(?:\.[0-9]+)*(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?", value) is None:
        raise BuildError("版本号必须以小写 v 和数字开头，例如 v1.0.0；不允许空格或路径字符")
    git(root, "check-ref-format", f"refs/tags/{value}")
    return value


def append_summary(text: str) -> None:
    filename = os.environ.get("GITHUB_STEP_SUMMARY")
    if filename:
        with Path(filename).open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(text + "\n")


def output(name: str, value: str) -> None:
    filename = os.environ.get("GITHUB_OUTPUT")
    if filename:
        with Path(filename).open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(f"{name}={value}\n")


def prepare(root: Path, directory: Path, version: str) -> None:
    patches = (
        "0001-library-images.patch",
        "0002-nfo.patch",
        "0003-actresses.patch",
        "0004-actor-mapping.patch",
    )
    actual = {path.name for path in directory.glob("*.patch")}
    if actual != set(patches):
        missing = sorted(set(patches) - actual)
        unexpected = sorted(actual - set(patches))
        raise BuildError(f"补丁清单不一致；缺少: {missing}；未登记: {unexpected}")
    source_sha = git(root, "rev-parse", "HEAD")
    append_summary(f"## 自用构建 {version}\n\n- 源码提交：`{source_sha}`\n- 软件内部版本：沿用上游\n")
    for filename in patches:
        path = directory / filename
        if not path.is_file():
            raise BuildError(f"补丁不是普通文件: {filename}")
        sys.stdout.write(f"检查并应用补丁: {filename}\n")
        sys.stdout.flush()
        try:
            git(root, "apply", "--check", "--whitespace=error", "--", str(path))
            git(root, "apply", "--whitespace=error", "--", str(path))
        except BuildError as exc:
            raise BuildError(f"补丁 {filename} 应用失败，已停止构建。请检查上游源码变化。\n{exc}") from exc
        append_summary(f"- 已应用：`{filename}`")
    output("version", version)
    output("docker_tag", version[1:])
    output("source_sha", source_sha)


def check_release(version: str) -> None:
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GH_TOKEN", "")
    if not repository or not token:
        raise BuildError("检查发布版本需要 GITHUB_REPOSITORY 和 Actions 自带的 GH_TOKEN")
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    for suffix in (f"git/ref/tags/{quote(version, safe='')}", f"releases/tags/{quote(version, safe='')}"):
        request = Request(
            f"{api}/repos/{repository}/{suffix}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        )
        try:
            with urlopen(request, timeout=30):
                raise BuildError(f"版本 {version} 已存在标签或发布记录，请填写新的版本号")
        except HTTPError as exc:
            if exc.code != 404:
                raise BuildError(f"无法检查发布版本，GitHub 返回 HTTP {exc.code}") from exc
        except URLError as exc:
            raise BuildError("无法连接 GitHub 检查发布版本") from exc


def run_tests(root: Path) -> None:
    cases = (
        "tests/api/test_tasks.py::TestSubmitTask::test_submit_payloads_and_rejects",
        "tests/handlers/test_file_ops.py",
        "tests/handlers/test_liu1443452494_library_images.py",
        "tests/handlers/test_organize.py",
        "tests/media/test_nfo_write.py",
        "tests/media/test_liu1443452494_nfo.py",
        "tests/organize/test_path_templates.py::test_from_metadata_actress_placeholders",
        "tests/organize/test_path_templates.py::test_actress_names",
        "tests/handlers/test_liu1443452494_actor_mapping.py",
        "tests/handlers/test_scraper.py",
        "tests/handlers/test_auto_organize.py",
        "tests/handlers/test_actor_scrape.py",
    )
    temp_root = Path(os.environ.get("BUILD_TEST_TEMP", str(Path(tempfile.gettempdir()) / "amane-custom-tests")))
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *cases, "-q", "--basetemp", str(temp_root)],
        cwd=root,
    )
    if result.returncode:
        raise BuildError("补丁相关逻辑测试失败，已停止构建")
    append_summary("\n补丁相关逻辑测试通过。")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "check-release", "test"))
    parser.add_argument("--version", default=os.environ.get("BUILD_VERSION", ""), help="以 v 开头的构建版本")
    arguments = parser.parse_args()
    directory = Path(__file__).resolve().parent
    root = directory.parent
    try:
        if arguments.command == "test":
            run_tests(root)
        else:
            version = validate_version(arguments.version, root)
            if arguments.command == "prepare":
                prepare(root, directory, version)
            else:
                check_release(version)
    except BuildError as exc:
        message = str(exc)
        escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        sys.stderr.write(f"::error title=自用构建失败::{escaped}\n")
        append_summary(f"\n### 构建已停止\n\n```text\n{message}\n```")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
