"""Tests for the LongMemEval CLI variant default (M8).

The canonical variant is ``s`` (LongMemEval-S) — 500 questions, ~40
sessions / ~115k tokens per question — matching agentmemory's
baseline so scores are directly comparable. These tests pin that
default on every subcommand that takes ``--variant``.
"""

from __future__ import annotations

from typer.testing import CliRunner

from memex_eval.cli import app


def test_longmemeval_ingest_help_defaults_to_s() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ['longmemeval', 'ingest', '--help'])
    assert result.exit_code == 0
    # Typer renders ``--variant`` default as ``[default: s]``.
    assert 'default: s' in result.stdout.replace('\n', ' ').lower()


def test_longmemeval_answer_help_defaults_to_s() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ['longmemeval', 'answer', '--help'])
    assert result.exit_code == 0
    assert 'default: s' in result.stdout.replace('\n', ' ').lower()


def test_longmemeval_judge_help_defaults_to_s() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ['longmemeval', 'judge', '--help'])
    assert result.exit_code == 0
    assert 'default: s' in result.stdout.replace('\n', ' ').lower()


def test_longmemeval_report_help_defaults_to_s() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ['longmemeval', 'report', '--help'])
    assert result.exit_code == 0
    assert 'default: s' in result.stdout.replace('\n', ' ').lower()


def test_longmemeval_run_help_defaults_to_s() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ['longmemeval', 'run', '--help'])
    assert result.exit_code == 0
    assert 'default: s' in result.stdout.replace('\n', ' ').lower()
