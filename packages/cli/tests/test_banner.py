"""Tests for the Memex ASCII art banner."""

import importlib.metadata
from io import StringIO
from unittest.mock import patch

from rich.console import Console

from memex_cli.banner import (
    BANNER_LINES,
    TAGLINE,
    _gradient_color,
    _interpolate_rgb,
    build_banner,
    print_banner,
)


class TestInterpolateRgb:
    def test_t_zero_returns_first_color(self):
        c1 = (100, 50, 200)
        c2 = (200, 100, 50)
        assert _interpolate_rgb(c1, c2, 0.0) == c1

    def test_t_one_returns_second_color(self):
        c1 = (100, 50, 200)
        c2 = (200, 100, 50)
        assert _interpolate_rgb(c1, c2, 1.0) == c2

    def test_t_half_returns_midpoint(self):
        c1 = (0, 0, 0)
        c2 = (200, 100, 50)
        result = _interpolate_rgb(c1, c2, 0.5)
        assert result == (100, 50, 25)


class TestGradientColor:
    def test_t_zero_returns_first_stop(self):
        color = _gradient_color(0.0)
        assert color == '#8a2be2'

    def test_t_one_returns_last_stop(self):
        color = _gradient_color(1.0)
        assert color == '#00bfff'

    def test_clamps_below_zero(self):
        assert _gradient_color(-0.5) == _gradient_color(0.0)

    def test_clamps_above_one(self):
        assert _gradient_color(1.5) == _gradient_color(1.0)

    def test_midpoint_returns_valid_hex(self):
        color = _gradient_color(0.5)
        assert color.startswith('#')
        assert len(color) == 7


class TestBuildBanner:
    def test_contains_all_banner_lines(self):
        banner = build_banner()
        plain = banner.plain
        for line in BANNER_LINES:
            assert line.strip() in plain

    def test_contains_tagline(self):
        banner = build_banner()
        assert TAGLINE in banner.plain

    def test_contains_version_by_default(self):
        banner = build_banner()
        assert '  v' in banner.plain

    def test_no_version_when_disabled(self):
        banner = build_banner(show_version=False)
        assert '  v' not in banner.plain
        assert TAGLINE in banner.plain

    def test_banner_width_under_80_columns(self):
        banner = build_banner(show_version=False)
        for line in banner.plain.split('\n'):
            assert len(line) <= 80, f'Line too wide ({len(line)} chars): {line!r}'

    def test_version_fallback_to_dev(self):
        with patch(
            'memex_cli.banner.importlib.metadata.version',
            side_effect=importlib.metadata.PackageNotFoundError('memex-cli'),
        ):
            banner = build_banner()
            assert 'vdev' in banner.plain


class TestPrintBanner:
    def test_prints_on_terminal(self):
        buf = StringIO()
        console = Console(file=buf, force_terminal=True, color_system='truecolor')
        print_banner(console)
        output = buf.getvalue()
        assert len(output) > 0
        assert 'MEMEX' in output or '|' in output

    def test_suppressed_on_non_terminal(self):
        buf = StringIO()
        console = Console(file=buf, force_terminal=False)
        print_banner(console)
        assert buf.getvalue() == ''
