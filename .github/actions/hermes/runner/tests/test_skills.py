from __future__ import annotations

from pathlib import Path

from hermes_runner.skills import copy_skills


def test_no_src_is_noop(tmp_path: Path) -> None:
    src = tmp_path / 'missing'
    dst = tmp_path / 'dst'
    assert copy_skills(src, dst) == 0
    assert not dst.exists()


def test_copies_subdirectories(tmp_path: Path) -> None:
    src = tmp_path / 'skills'
    (src / 'remember').mkdir(parents=True)
    (src / 'remember' / 'SKILL.md').write_text('# remember', encoding='utf-8')
    (src / 'recall').mkdir()
    (src / 'recall' / 'SKILL.md').write_text('# recall', encoding='utf-8')
    (src / 'loose-file.md').write_text('not a skill', encoding='utf-8')

    dst = tmp_path / 'dst'
    assert copy_skills(src, dst) == 2

    assert (dst / 'remember' / 'SKILL.md').read_text(encoding='utf-8') == '# remember'
    assert (dst / 'recall' / 'SKILL.md').read_text(encoding='utf-8') == '# recall'
    assert not (dst / 'loose-file.md').exists()


def test_preserves_existing_destination(tmp_path: Path) -> None:
    src = tmp_path / 'skills'
    (src / 'new-skill').mkdir(parents=True)
    (src / 'new-skill' / 'SKILL.md').write_text('# new', encoding='utf-8')

    dst = tmp_path / 'dst'
    (dst / 'preexisting').mkdir(parents=True)
    (dst / 'preexisting' / 'SKILL.md').write_text('# existing', encoding='utf-8')

    assert copy_skills(src, dst) == 1
    assert (dst / 'preexisting' / 'SKILL.md').read_text(encoding='utf-8') == '# existing'
    assert (dst / 'new-skill' / 'SKILL.md').read_text(encoding='utf-8') == '# new'
