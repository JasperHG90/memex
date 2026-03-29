"""Tests for memex_common.github_cache module."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from memex_common.github_cache import (
    download_and_cache_github_repo,
    parse_github_url,
    resolve_template_in_repo,
)


class TestParseGithubUrl:
    def test_full_url_with_branch(self) -> None:
        owner, repo, branch = parse_github_url('https://github.com/user/repo/tree/main')
        assert (owner, repo, branch) == ('user', 'repo', 'main')

    def test_url_without_branch_defaults_to_main(self) -> None:
        owner, repo, branch = parse_github_url('https://github.com/user/repo')
        assert (owner, repo, branch) == ('user', 'repo', 'main')

    def test_branch_with_slashes(self) -> None:
        owner, repo, branch = parse_github_url(
            'https://github.com/user/repo/tree/feature/my-branch'
        )
        assert branch == 'feature/my-branch'

    def test_strips_dot_git(self) -> None:
        owner, repo, branch = parse_github_url('https://github.com/user/repo.git')
        assert repo == 'repo'

    def test_invalid_url_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match='Cannot parse GitHub URL'):
            parse_github_url('https://gitlab.com/user/repo')

    def test_empty_string_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match='Cannot parse GitHub URL'):
            parse_github_url('')

    def test_garbage_string_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match='Cannot parse GitHub URL'):
            parse_github_url('not-a-url-at-all')


def _make_repo_zip(top_dir_name: str, files: dict[str, str]) -> bytes:
    """Create an in-memory zip archive mimicking GitHub's repo download format."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for rel_path, content in files.items():
            zf.writestr(f'{top_dir_name}/{rel_path}', content)
    return buf.getvalue()


class TestDownloadAndCacheGithubRepo:
    def test_downloads_and_extracts(self, tmp_path: Path) -> None:
        zip_bytes = _make_repo_zip('repo-main', {'templates/test.toml': '[template]\nname="t"'})

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.content = zip_bytes
        mock_response.raise_for_status = MagicMock()

        with patch('memex_common.github_cache.httpx') as mock_httpx:
            mock_httpx.get.return_value = mock_response

            result = download_and_cache_github_repo(
                'https://github.com/user/repo/tree/main',
                cache_dir=tmp_path,
            )

        assert result == tmp_path / 'user' / 'repo' / 'repo-main'
        assert result.is_dir()
        assert (result / 'templates' / 'test.toml').is_file()
        mock_httpx.get.assert_called_once()

    def test_cache_hit_skips_download(self, tmp_path: Path) -> None:
        zip_bytes = _make_repo_zip('repo-main', {'README.md': '# hello'})

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.content = zip_bytes
        mock_response.raise_for_status = MagicMock()

        with patch('memex_common.github_cache.httpx') as mock_httpx:
            mock_httpx.get.return_value = mock_response

            # First call: downloads
            result1 = download_and_cache_github_repo(
                'https://github.com/user/repo/tree/main',
                cache_dir=tmp_path,
            )
            assert mock_httpx.get.call_count == 1

            # Second call: cache hit, no download
            result2 = download_and_cache_github_repo(
                'https://github.com/user/repo/tree/main',
                cache_dir=tmp_path,
            )
            assert mock_httpx.get.call_count == 1
            assert result1 == result2

    def test_branch_with_slashes_creates_flat_cache_dir(self, tmp_path: Path) -> None:
        """Branch names with slashes should produce flat cache dirs, not nested."""
        zip_bytes = _make_repo_zip('repo-feature-foo', {'README.md': '# hello'})

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.content = zip_bytes
        mock_response.raise_for_status = MagicMock()

        with patch('memex_common.github_cache.httpx') as mock_httpx:
            mock_httpx.get.return_value = mock_response

            result = download_and_cache_github_repo(
                'https://github.com/user/repo/tree/feature/foo',
                cache_dir=tmp_path,
            )

        # Should be flat 'repo-feature-foo', NOT nested 'repo-feature/foo'
        assert result == tmp_path / 'user' / 'repo' / 'repo-feature-foo'
        assert result.is_dir()

    def test_http_error_raises(self, tmp_path: Path) -> None:
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 404
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            '404 Not Found',
            request=MagicMock(spec=httpx.Request),
            response=mock_response,
        )

        with patch('memex_common.github_cache.httpx') as mock_httpx:
            mock_httpx.get.return_value = mock_response
            mock_httpx.HTTPStatusError = httpx.HTTPStatusError

            with pytest.raises(httpx.HTTPStatusError):
                download_and_cache_github_repo(
                    'https://github.com/user/repo/tree/main',
                    cache_dir=tmp_path,
                )


class TestResolveTemplateInRepo:
    def test_valid_relative_path(self, tmp_path: Path) -> None:
        template = tmp_path / 'templates' / 'note.toml'
        template.parent.mkdir(parents=True)
        template.write_text('[template]')

        result = resolve_template_in_repo(tmp_path, 'templates/note.toml')
        assert result == template.resolve()
        assert result.is_absolute()

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            resolve_template_in_repo(tmp_path, 'nonexistent.toml')

    def test_path_traversal_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match='Path traversal detected'):
            resolve_template_in_repo(tmp_path, '../../../etc/passwd')

    def test_absolute_path_outside_repo_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match='Path traversal detected'):
            resolve_template_in_repo(tmp_path, '/etc/passwd')
