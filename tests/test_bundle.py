"""The bundle of prompts and testbed guidance."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from cloud_conversations.bundle import Bundle, BundleError

COMMITTED = Bundle(Path("bundle"), {"chameleon": "Chameleon"})

# Each prompt the code reads, with values for its placeholders.
_PROMPTS = {
    ("build", "instructions"): {},
    ("build", "knowledge"): {},
    ("describe", "instructions"): {"now": "2026-10-19T12:00:00+00:00"},
    ("describe", "knowledge"): {},
    ("route", "instructions"): {},
    ("route", "versions"): {},
    ("route", "rules"): {},
    ("route", "no-description"): {},
    ("inventory", "instructions"): {},
    ("answer", "instructions"): {"primary_section": "P", "supplementary_section": "S"},
}

_PLACEHOLDER = re.compile(r"\{(testbed|about|now|primary_section|supplementary_section)\}")


@pytest.mark.parametrize(("agent", "name"), sorted(_PROMPTS))
def test_each_committed_prompt_fills_every_placeholder(agent, name):
    text = COMMITTED.prompt("chameleon", agent, name, **_PROMPTS[(agent, name)])

    assert text.strip()
    assert _PLACEHOLDER.search(text) is None


def test_a_prompt_names_the_testbed_by_its_label(tmp_path):
    (tmp_path / "agents" / "route").mkdir(parents=True)
    (tmp_path / "agents" / "route" / "instructions.md").write_text("about {testbed}")
    (tmp_path / "testbeds" / "chameleon").mkdir(parents=True)
    (tmp_path / "testbeds" / "chameleon" / "about.md").write_text("A testbed.")

    text = Bundle(tmp_path, {"chameleon": "Chameleon"}).prompt("chameleon", "route", "instructions")

    assert text == "about Chameleon"


def test_a_missing_file_is_named(tmp_path):
    with pytest.raises(BundleError) as raised:
        Bundle(tmp_path).prompt("chameleon", "build", "instructions")
    assert str(raised.value) == f"the bundle at {tmp_path} has no agents/build/instructions.md"


def test_the_digest_changes_when_a_file_does(tmp_path):
    (tmp_path / "agents").mkdir()
    prompt = tmp_path / "agents" / "route.md"
    prompt.write_text("route it")
    before = Bundle(tmp_path).digest()

    prompt.write_text("route me")

    assert Bundle(tmp_path).digest() != before
