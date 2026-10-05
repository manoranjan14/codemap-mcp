"""Module-level (top-level) call attribution.

A call written outside any function or class is a real call - it runs at
import time, and in a composition-API Vue/TS codebase it is where most
composable wiring lives. These calls used to be dropped entirely: calls
were only collected while visiting a function/method/closure node, so
nothing ever attributed them to anything.

Measured on webapp before the fix: 38,302 top-level call expressions,
13.0% of all 294,319 call expressions in the repo, produced zero edges -
every one of the 12,447 module-level `calls` edges in that graph pointed at
a closure, never at a real function.

They are attributed to the file's MODULE node, which already exists and is
already what `defines` edges hang off.
"""
from __future__ import annotations

from conftest import connect, edges, node_ids, requires_tree_sitter, run_index


# --- Python -------------------------------------------------------------

def test_module_level_call_to_a_same_file_function(py_conn):
    assert "module_level.py::top_target" in edges(py_conn, "module_level.py")


def test_module_level_call_to_an_imported_symbol(py_conn):
    assert "repo_helper.py::Helper" in edges(py_conn, "module_level.py")


def test_module_level_chained_call_resolves_through_the_constructor(py_conn):
    """`Helper().assist()` is two calls at module scope, and BOTH resolve.

    This test used to assert `.assist()` stayed external, on the reasoning
    that it needed RETURN-type inference. That reasoning was wrong:
    `Helper()` is a construction, so the receiver's type is written in the
    expression and only needs looking up. An adversarial review found the
    same shape silently dropping real calls in production code, which is
    what prompted the correction - see test_adversarial_findings.py."""
    out = edges(py_conn, "module_level.py")
    assert "repo_helper.py::Helper" in out
    assert "repo_helper.py::Helper.assist" in out


def test_a_call_on_a_function_result_still_stays_external(tmp_path):
    """The case that genuinely DOES need return-type inference, and still
    is not guessed: the receiver is a function's result, not a written
    type."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "m.py").write_text(
        "class Helper:\n    def assist(self):\n        return 1\n\n\n"
        "def get_helper():\n    return Helper()\n\n\n"
        "RESULT = get_helper().assist()\n"
    )
    db = str(tmp_path / "graph.db")
    run_index(str(repo), db)
    conn = connect(db)
    out = edges(conn, "m.py")
    assert "m.py::get_helper" in out
    assert "external:assist" in out
    assert "m.py::Helper.assist" not in out
    conn.close()


def test_calls_inside_a_top_level_function_are_not_also_attributed_to_the_module(py_conn):
    """The nested-scope rule has to hold in both directions - fixing the
    module case must not resurrect the double-attribution bug."""
    assert "module_level.py::nested_only_target" not in edges(py_conn, "module_level.py")
    assert "module_level.py::nested_only_target" in edges(py_conn, "module_level.py::wrapper")


def test_module_node_is_reachable_as_a_caller_by_impacted_by(py_conn):
    """The point of the fix: `who uses top_target` must now include the
    module that calls it at import time."""
    import query_lib as ql
    impacted = {n["id"] for n in ql.impacted_by(py_conn, "module_level.py::top_target")["impacted"]}
    assert "module_level.py" in impacted


# --- TypeScript ---------------------------------------------------------

@requires_tree_sitter
def test_ts_module_level_call_to_a_same_file_function(ts_conn):
    assert "src/moduleLevel.ts::tsTopTarget" in edges(ts_conn, "src/moduleLevel.ts")


@requires_tree_sitter
def test_ts_module_level_destructured_and_bare_calls_to_an_import(ts_conn):
    """`const { x } = helperA()` and a bare `helperA()` statement are both
    module-scope calls. The destructured form is the one that dominates
    real composable usage."""
    assert "src/utils/helpers.ts::helperA" in edges(ts_conn, "src/moduleLevel.ts")


@requires_tree_sitter
def test_ts_calls_inside_functions_and_named_arrows_stay_in_their_own_scope(ts_conn):
    module_out = edges(ts_conn, "src/moduleLevel.ts")
    assert "src/moduleLevel.ts::nestedOnlyTarget" not in module_out
    assert "src/moduleLevel.ts::nestedOnlyTarget" in edges(ts_conn, "src/moduleLevel.ts::tsWrapper")
    assert "src/moduleLevel.ts::nestedOnlyTarget" in edges(ts_conn, "src/moduleLevel.ts::arrowOwner")


# --- Vue <script setup> -------------------------------------------------

@requires_tree_sitter
def test_vue_script_setup_top_level_call_to_a_local_function(ts_conn):
    assert "src/comp/TopLevel.vue::vueTopTarget" in edges(ts_conn, "src/comp/TopLevel.vue")


@requires_tree_sitter
def test_vue_script_setup_top_level_composable_call_resolves_cross_file(ts_conn):
    """The exact miss found on webapp: `const { x } = useThing()` at
    the top of <script setup> left the component out of useThing's caller
    list entirely."""
    assert "src/utils/helpers.ts::helperA" in edges(ts_conn, "src/comp/TopLevel.vue")


@requires_tree_sitter
def test_vue_template_edges_still_work_alongside_module_level_calls(ts_conn):
    """Regression guard: the <template> node and the module node are
    different callers of the same method and must not clobber each other."""
    assert "src/comp/TopLevel.vue::vueTopTarget" in edges(
        ts_conn, "src/comp/TopLevel.vue::<template>")


@requires_tree_sitter
def test_impacted_by_now_reaches_a_vue_component_through_a_top_level_call(ts_conn):
    import query_lib as ql
    impacted = {n["id"] for n in ql.impacted_by(ts_conn, "src/utils/helpers.ts::helperA")["impacted"]}
    assert "src/comp/TopLevel.vue" in impacted
    assert "src/moduleLevel.ts" in impacted


@requires_tree_sitter
def test_both_script_blocks_of_a_dual_script_vue_file_are_recorded(ts_conn):
    """A .vue file can have a plain <script> AND a <script setup>. Hooking
    the module-level collector outside the per-block loop silently records
    only the last block - and raises NameError on a file with no script
    block at all."""
    out = edges(ts_conn, "src/comp/DualScript.vue")
    assert "src/utils/helpers.ts::helperA" in out   # plain <script>
    assert "src/utils/helpers.ts::helperB" in out   # <script setup>
