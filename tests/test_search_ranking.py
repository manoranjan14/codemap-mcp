"""`search` ranking and truncation.

Measured on webapp before this: `search("assessment")` matched 1,953
nodes, returned 15, and said nothing about the other 1,938. Ordering was
`ORDER BY (name = ?) DESC` and then whatever SQLite happened to yield, so
all 15 came back as module nodes for route files and not one function or
method surfaced. `search` is the tool the MCP docstring tells a session to
reach for FIRST, so a bad 15 is expensive.

Ranking is now two keys, in order:
  1. match tier   - exact name, then name prefix, then name substring,
                    then qualname-only, then note-text-only
  2. kind weight  - function/method, class, template, closure, module

with shorter qualname and then id as deterministic tiebreaks.
"""
from __future__ import annotations

import pytest

import query_lib as ql
from conftest import connect, run_index


@pytest.fixture(scope="module")
def ranked_repo(tmp_path_factory):
    """Mirrors the real failure shape. On webapp the 15 returned
    results were module nodes like
    `handlers/v1/assessment/[assessmentId]/.../get.ts` - their BASENAME
    ("get.ts") does not contain the term at all; they matched only because
    the term is in the directory path, i.e. in the qualname. Functions whose
    own name contained the term never surfaced."""
    tmp = tmp_path_factory.mktemp("ranked")
    repo = tmp / "repo"
    (repo / "handlers" / "assessment").mkdir(parents=True)

    # Match on PATH only - basenames deliberately free of the term.
    for i in range(12):
        (repo / "handlers" / "assessment" / f"route_{i}.py").write_text(
            f"def handle_{i}():\n    return {i}\n"
        )

    # A module whose own basename is a SUBSTRING match, to test kind weight
    # against a function at the identical tier.
    (repo / "core_assessment_utils.py").write_text("def util_helper():\n    return 0\n")

    # The symbols a developer actually wants back.
    (repo / "core.py").write_text(
        "def assessment():\n"                     # exact
        "    return 1\n\n\n"
        "def assessment_builder():\n"             # prefix
        "    return 2\n\n\n"
        "def build_assessment_payload():\n"       # substring
        "    return 3\n\n\n"
        "class PayloadAssessmentHolder:\n"        # substring, class
        "    def run(self):\n"
        "        return 4\n"
    )
    db = str(tmp / "graph.db")
    run_index(str(repo), db)
    conn = connect(db)
    yield conn
    conn.close()


def ids(res):
    return [r["node"]["id"] for r in res["results"]]


def test_exact_name_ranks_first(ranked_repo):
    assert ids(ql.search(ranked_repo, "assessment"))[0] == "core.py::assessment"


def test_tiers_are_exact_then_prefix_then_substring(ranked_repo):
    ranked = ids(ql.search(ranked_repo, "assessment", limit=50))
    assert ranked.index("core.py::assessment") < ranked.index("core.py::assessment_builder")
    assert ranked.index("core.py::assessment_builder") < ranked.index(
        "core.py::build_assessment_payload")


def test_path_only_module_matches_rank_below_real_symbol_matches(ranked_repo):
    """The measured failure: 12 module nodes matched the term only through
    their directory path and buried every function."""
    ranked = ids(ql.search(ranked_repo, "assessment", limit=50))
    first_module = next(i for i, n in enumerate(ranked) if n.startswith("handlers/"))
    assert ranked.index("core.py::build_assessment_payload") < first_module
    assert ranked.index("core.py::PayloadAssessmentHolder") < first_module


def test_function_outranks_a_module_at_the_identical_tier(ranked_repo):
    """`build_assessment_payload` and `core_assessment_utils.py` are both
    substring matches on their own name - only the kind weight separates
    them, which is the whole point of having one."""
    ranked = ids(ql.search(ranked_repo, "assessment", limit=50))
    assert ranked.index("core.py::build_assessment_payload") < ranked.index(
        "core_assessment_utils.py")


def test_top_results_are_not_all_modules(ranked_repo):
    """The user-visible symptom, asserted directly."""
    top = ids(ql.search(ranked_repo, "assessment"))[:5]
    assert not all(n.startswith("handlers/") for n in top)


def test_class_ranks_below_function_but_above_module(ranked_repo):
    """Both are substring matches, so only the kind weight separates them."""
    ranked = ids(ql.search(ranked_repo, "assessment", limit=50))
    first_module = next(i for i, n in enumerate(ranked) if n.startswith("handlers/"))
    assert ranked.index("core.py::build_assessment_payload") < ranked.index(
        "core.py::PayloadAssessmentHolder") < first_module


def test_a_prefix_match_beats_a_substring_match_even_across_kinds(ranked_repo):
    """`assessment_builder` (prefix, function) must beat
    `PayloadAssessmentHolder` (substring, class) - tier is the primary key,
    kind only breaks ties within a tier."""
    ranked = ids(ql.search(ranked_repo, "assessment", limit=50))
    assert ranked.index("core.py::assessment_builder") < ranked.index(
        "core.py::PayloadAssessmentHolder")


def test_total_and_truncated_are_reported(ranked_repo):
    capped = ql.search(ranked_repo, "assessment", limit=3)
    assert len(capped["results"]) == 3
    assert capped["truncated"] is True
    assert capped["total"] > 3
    assert "raise `limit`" in capped["truncation_note"]

    full = ql.search(ranked_repo, "assessment", limit=500)
    assert full["truncated"] is False
    assert full["total"] == len(full["results"])


def test_ranking_is_deterministic(ranked_repo):
    assert ids(ql.search(ranked_repo, "assessment", limit=50)) == \
           ids(ql.search(ranked_repo, "assessment", limit=50))


def test_case_insensitive_exact_match_still_counts_as_exact(ranked_repo):
    """LIKE matching is already ASCII case-insensitive, so the tiers must
    be too - otherwise 'Assessment' silently drops to a lower tier than
    'assessment' for the same symbol."""
    assert ids(ql.search(ranked_repo, "ASSESSMENT"))[0] == "core.py::assessment"


def test_note_only_matches_still_surface_but_rank_below_code_matches(ranked_repo):
    ql.add_note(ranked_repo, "handlers/assessment/route_0.py::handle_0",
                "assessment quota logic lives here", "s1")
    ranked = ids(ql.search(ranked_repo, "quota", limit=50))
    assert "handlers/assessment/route_0.py::handle_0" in ranked

    both = ids(ql.search(ranked_repo, "assessment", limit=50))
    assert both[0] == "core.py::assessment"


def test_notes_are_still_attached_to_their_node(ranked_repo):
    ql.add_note(ranked_repo, "core.py::assessment", "note on the exact match", "s1")
    top = ql.search(ranked_repo, "assessment")["results"][0]
    assert "note on the exact match" in [n["note"] for n in top["notes"]]


# --- `total` accounting --------------------------------------------------

@pytest.fixture
def counting_repo(tmp_path):
    """Five nodes matching by name, one of which ALSO has a matching note."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "m.py").write_text(
        "".join(f"def foo{i}():\n    return {i}\n\n\n" for i in range(5))
    )
    db = str(tmp_path / "graph.db")
    run_index(str(repo), db)
    conn = connect(db)
    ql.add_note(conn, "m.py::foo4", "a note mentioning foo", "s1")
    yield conn
    conn.close()


def test_total_counts_each_node_once(counting_repo):
    """`total` summed code matches and note matches independently, but the
    note-only set only excluded the rows actually RETURNED - so a node
    matching by BOTH name and note that fell below `limit` was counted
    twice. The truncation_note then promised matches that don't exist,
    which is exactly the dishonesty the signal was added to remove."""
    real = counting_repo.execute(
        "SELECT COUNT(*) FROM nodes WHERE name LIKE '%foo%' OR qualname LIKE '%foo%'"
    ).fetchone()[0]
    assert ql.search(counting_repo, "foo", limit=2)["total"] == real
    assert ql.search(counting_repo, "foo", limit=500)["total"] == real


def test_total_is_stable_across_limits(counting_repo):
    totals = {ql.search(counting_repo, "foo", limit=n)["total"] for n in (1, 2, 3, 500)}
    assert len(totals) == 1


def test_a_low_ranked_code_match_does_not_reappear_via_the_note_tail(counting_repo):
    """A node that matches by name is a CODE match, however low it ranks.
    Letting it back in through the note-only tail would jump it ahead of
    better-ranked code matches that were cut by the same limit."""
    res = ql.search(counting_repo, "foo", limit=2)
    assert len(res["results"]) == 2
    ranked = [r["node"]["id"] for r in ql.search(counting_repo, "foo", limit=500)["results"]]
    assert [r["node"]["id"] for r in res["results"]] == ranked[:2]


def test_a_genuine_note_only_match_is_still_counted_and_returned(counting_repo):
    """The tail must still work: a node whose NAME doesn't match at all,
    found only through its note text."""
    ql.add_note(counting_repo, "m.py::foo0", "mentions zebra", "s1")
    res = ql.search(counting_repo, "zebra", limit=10)
    assert [r["node"]["id"] for r in res["results"]] == ["m.py::foo0"]
    assert res["total"] == 1
