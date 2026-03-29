"""GitHub repository download and caching utilities."""

from __future__ import annotations

import logging
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

import httpx
import platformdirs

logger = logging.getLogger(__name__)

_GITHUB_URL_RE = re.compile(
    r'^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?(?:/tree/(?P<branch>.+))?$'
)


def parse_github_url(url: str) -> tuple[str, str, str]:
    """Parse a GitHub URL into (owner, repo, branch).

    Accepts URLs of the form:
        https://github.com/USER/REPO[.git][/tree/BRANCH]

    Branch defaults to 'main' when /tree/... is absent.
    Branch names with slashes (e.g. feature/foo) are supported.

    Raises:
        ValueError: If the URL cannot be parsed as a GitHub repository URL.
    """
    match = _GITHUB_URL_RE.match(url.strip())
    if not match:
        raise ValueError(f'Cannot parse GitHub URL: {url}')
    owner = match.group('owner')
    repo = match.group('repo')
    branch = match.group('branch') or 'main'
    return owner, repo, branch


def download_and_cache_github_repo(
    url: str,
    *,
    cache_dir: Path | None = None,
) -> Path:
    """Download a GitHub repository archive and cache it locally.

    Returns the path to the cached repository directory. If the repo has
    already been downloaded, returns the cached path without re-downloading.

    Args:
        url: GitHub repository URL.
        cache_dir: Override the default cache directory.

    Returns:
        Path to the extracted repository root inside the cache.

    Raises:
        ValueError: If the URL cannot be parsed.
        httpx.HTTPStatusError: If the download fails.
        RuntimeError: If extraction fails unexpectedly.
    """
    owner, repo, branch = parse_github_url(url)

    if cache_dir is None:
        cache_dir = Path(platformdirs.user_cache_dir('memex')) / 'github_repos'

    # Sanitize branch slashes to match GitHub's zip naming convention
    # (e.g. feature/foo -> feature-foo) and avoid creating nested dirs.
    safe_branch = branch.replace('/', '-')
    repo_dir = cache_dir / owner / repo / f'{repo}-{safe_branch}'
    if repo_dir.exists():
        logger.debug('Cache hit for %s/%s@%s', owner, repo, branch)
        return repo_dir

    zip_url = f'https://github.com/{owner}/{repo}/archive/refs/heads/{branch}.zip'
    logger.info('Downloading %s', zip_url)

    response = httpx.get(zip_url, follow_redirects=True, timeout=30)
    response.raise_for_status()

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            zip_path = tmp_path / 'repo.zip'
            zip_path.write_bytes(response.content)

            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(tmp_path / 'extracted')

            # GitHub sanitizes branch slashes in the zip's top-level dir name,
            # so we glob for the single directory instead of constructing its name.
            extracted_dirs = list((tmp_path / 'extracted').iterdir())
            if len(extracted_dirs) != 1 or not extracted_dirs[0].is_dir():
                raise RuntimeError(
                    f'Expected exactly one top-level directory in archive, '
                    f'found: {[d.name for d in extracted_dirs]}'
                )

            inner_dir = extracted_dirs[0]
            repo_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(inner_dir), str(repo_dir))

    except Exception:
        # Clean up partial cache on failure
        if repo_dir.exists():
            shutil.rmtree(repo_dir, ignore_errors=True)
        raise

    logger.info('Cached %s/%s@%s at %s', owner, repo, branch, repo_dir)
    return repo_dir


def resolve_template_in_repo(repo_dir: Path, relative_path: str) -> Path:
    """Resolve a relative path within a cached repository directory.

    Args:
        repo_dir: Root of the cached repository.
        relative_path: Path to the template file relative to repo root.

    Returns:
        Resolved absolute path to the template file.

    Raises:
        ValueError: If the resolved path escapes the repository directory.
        FileNotFoundError: If the resolved file does not exist.
    """
    resolved = (repo_dir / relative_path).resolve()
    repo_resolved = repo_dir.resolve()

    if not str(resolved).startswith(str(repo_resolved) + '/') and resolved != repo_resolved:
        raise ValueError(
            f'Path traversal detected: {relative_path!r} resolves outside repo directory'
        )

    if not resolved.is_file():
        raise FileNotFoundError(f'Template not found: {resolved}')

    return resolved
