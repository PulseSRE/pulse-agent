"""A gate labelled deterministic may not install its grader from a moving ref.

The SRE-Bench sim gate ran `pip install "sre-bench[vertex] @
git+https://github.com/PulseSRE/sre-bench.git@main"`. The grader was therefore
whatever sre-bench's default branch happened to be when the job started — the
23:37 run on the v2.32.0 release commit resolved 96e421b5 — so a fixture change
in another repository could move a release gate here with no commit in this one,
and two runs of the same Pulse commit were not comparable.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WORKFLOWS = sorted((Path(__file__).parent.parent / ".github/workflows").glob("*.y*ml"))
# A 40-character commit SHA, or a released tag. Both name one tree; `main`,
# `HEAD` and a short SHA do not (and pip cannot resolve a short one).
PINNED = re.compile(r"^(?:[0-9a-f]{40}|v\d+\.\d+\.\d+)$")
INSTALL = re.compile(r"sre-bench[^\"']*@\s*git\+https://[^@\"'\s]+@([^\"'\s#]+)")


def _grader_installs() -> list[tuple[Path, int, str]]:
    """Every (workflow, line number, git ref) that installs sre-bench from git."""
    found = []
    for path in WORKFLOWS:
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            match = INSTALL.search(line)
            if match:
                found.append((path, lineno, match.group(1)))
    return found


def test_the_grader_install_is_present_to_be_checked():
    """Guard the guard: an empty scan must not read as a clean one."""
    assert _grader_installs(), "no sre-bench git install found — has the gate moved?"


@pytest.mark.parametrize("path,lineno,ref", _grader_installs(), ids=lambda v: str(v)[:40])
def test_every_sre_bench_install_is_pinned_to_one_tree(path, lineno, ref):
    assert PINNED.match(ref), (
        f"{path.name}:{lineno} installs the sre-bench grader from '{ref}'. A release "
        f"gate cannot depend on a branch that moves without a commit here — pin a "
        f"40-character commit SHA or a vX.Y.Z tag."
    )


@pytest.mark.parametrize("path,lineno,ref", _grader_installs(), ids=lambda v: str(v)[:40])
def test_the_pin_says_how_to_move_it(path, lineno, ref):
    """A pin nobody knows how to bump gets replaced with a branch again."""
    lines = path.read_text().splitlines()
    comment = []
    for line in reversed(lines[: lineno - 1]):
        if not line.strip().startswith("#"):
            break
        comment.append(line)
    assert any("pin" in line.lower() for line in comment), (
        f"{path.name}:{lineno} pins the grader with no comment above it saying why "
        f"it is pinned and how to move the pin."
    )
