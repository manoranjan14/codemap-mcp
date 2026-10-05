"""Regression tests for the TypeScript/JavaScript/Vue indexer
(ts_parser.py), pinned to tests/edge_repo_ts/.

These skip when the optional tree-sitter dependency isn't installed - see
test_optional_tree_sitter.py, which covers that path - so CI must run at
least one job WITH it installed for these to mean anything.
"""
from __future__ import annotations

import pytest

from conftest import connect, edge_rows, edges, node_ids, requires_tree_sitter, run_index

pytestmark = requires_tree_sitter


# --- per-file error isolation ------------------------------------------

def test_broken_files_are_skipped_without_failing_the_run(ts_conn):
    """tree-sitter is error-tolerant and returns a best-effort tree rather
    than raising, so a real syntax error would otherwise produce a silently
    partial graph. It must be surfaced and the file skipped."""
    ids = node_ids(ts_conn)
    assert not any(i.startswith("src/syntax_error.ts") for i in ids)
    assert not any(i.startswith("src/comp/Broken.vue") for i in ids)
    assert "src/main.ts::outer" in ids  # the rest of the repo still indexed


def test_redefinition_in_ts_does_not_abort_the_run(ts_conn):
    assert len([i for i in node_ids(ts_conn) if i.startswith("src/duplicate.ts::foo")]) == 1


# --- definitions and call attribution -----------------------------------

def test_nested_arrow_is_its_own_scope(ts_conn):
    """Same nested-scope attribution rule as Python: helperB is called only
    from `inner`, so `outer` must not claim it."""
    out = edges(ts_conn, "src/main.ts::outer")
    assert "src/utils/helpers.ts::helperA" in out
    assert "src/main.ts::outer.inner" in out
    assert "src/utils/helpers.ts::helperB" not in out
    assert edges(ts_conn, "src/main.ts::outer.inner") == {"src/utils/helpers.ts::helperB"}


def test_class_field_arrow_and_this_call_resolve(ts_conn):
    assert "src/main.ts::Consumer.other" in edges(ts_conn, "src/main.ts::Consumer.method")
    assert "src/main.ts::arrowTop" in edges(ts_conn, "src/main.ts::Consumer.other")


def test_call_bearing_anonymous_callback_gets_a_closure_node_reachable_by_calls(ts_conn):
    """A closure node needs BOTH a defines edge (display) and a calls edge
    (so impacted_by, which only walks calls/imports, reaches through it)."""
    closure = "src/closures.ts::withArrowBlockCallback.<closure:17>"
    assert closure in node_ids(ts_conn)
    assert closure in edges(ts_conn, "src/closures.ts::withArrowBlockCallback")
    assert closure in edges(
        ts_conn, "src/closures.ts::withArrowBlockCallback", types=("defines",)
    )
    assert "src/closures.ts::realWork" in edges(ts_conn, closure)


def test_function_expression_callback_also_gets_a_closure_node(ts_conn):
    closure = "src/closures.ts::withFunctionExprCallback.<closure:24>"
    assert "src/closures.ts::otherWork" in edges(ts_conn, closure)


def test_nested_closures_chain(ts_conn):
    outer_c = "src/closures.ts::withNestedClosure.<closure:37>"
    inner_c = f"{outer_c}.<closure:38>"
    assert inner_c in edges(ts_conn, outer_c)
    assert "src/closures.ts::realWork" in edges(ts_conn, inner_c)


def test_call_free_callback_creates_no_closure_node(ts_conn):
    """`.map(x => x + 1)` has nothing for the graph to resolve through it -
    a node there would be pure bloat."""
    assert not any(
        i.startswith("src/closures.ts::withTrivialCallback.<closure")
        for i in node_ids(ts_conn)
    )


def test_named_concise_body_arrow_is_tracked(ts_conn):
    assert "src/closures.ts::validate" in edges(ts_conn, "src/closures.ts::useValidate")
    assert "src/closures.ts::realWork" in edges(ts_conn, "src/closures.ts::validate")


def test_impacted_by_reaches_through_a_closure(ts_conn):
    """The reason closures get a `calls` edge at all: without it, changing
    realWork would look like it affects nothing in withArrowBlockCallback."""
    import query_lib as ql
    impacted = {n["id"] for n in ql.impacted_by(ts_conn, "src/closures.ts::realWork")["impacted"]}
    assert "src/closures.ts::withArrowBlockCallback" in impacted


# --- imports ------------------------------------------------------------

def test_relative_import_binding_resolves_cross_file(ts_conn):
    assert "src/utils/helpers.ts::helperA" in edges(ts_conn, "src/main.ts::outer")


def test_commonjs_require_bindings_resolve(ts_conn):
    """Namespace (`helpers.helperA()`), destructured, and renamed
    destructured require() bindings all resolve; a bare package require
    stays external."""
    assert "src/utils/helpers.ts::helperA" in edges(ts_conn, "src/legacyRequire.js::useNamespace")
    assert edges(ts_conn, "src/legacyRequire.js::useDestructured") == {
        "src/utils/helpers.ts::helperA", "src/utils/helpers.ts::helperB",
    }
    assert "external:get" in edges(ts_conn, "src/legacyRequire.js::useBarePackage")


def test_bare_package_import_stays_external(ts_conn):
    assert "external:debounce" in edges(ts_conn, "src/main.ts::Consumer.method")


def test_tsconfig_path_alias_resolves_a_call(tmp_path):
    """281 files in the real repo this was built for import via `@/`, so an
    unresolved alias makes import-aware resolution nearly useless there.
    The shipped fixture only imports a TYPE via `@/`, which produces no
    call edge - so build a repo that actually calls through the alias."""
    repo = tmp_path / "aliased"
    (repo / "src" / "deep").mkdir(parents=True)
    (repo / "tsconfig.json").write_text('{"compilerOptions":{"paths":{"@/*":["src/*"]}}}')
    (repo / "src" / "deep" / "target.ts").write_text("export function aliasTarget() { return 1; }\n")
    (repo / "src" / "caller.ts").write_text(
        'import { aliasTarget } from "@/deep/target";\n'
        "export function useAlias() { return aliasTarget(); }\n"
    )
    db = str(tmp_path / "graph.db")
    run_index(str(repo), db)

    conn = connect(db)
    assert "src/deep/target.ts::aliasTarget" in edges(conn, "src/caller.ts::useAlias")
    conn.close()


# --- .vue Single File Components ----------------------------------------

def test_vue_template_is_recorded_as_a_caller_of_its_own_methods(ts_conn):
    """The specific gap this feature closed: a Vue method invoked only from
    the template (`@click="save"`) used to have no caller at all, so
    impacted_by said nothing depended on it."""
    assert "src/comp/Widget.vue::<template>" in node_ids(ts_conn)
    assert "src/comp/Widget.vue::save" in edges(ts_conn, "src/comp/Widget.vue::<template>")


def test_vue_script_setup_calls_resolve_within_and_across_files(ts_conn):
    assert edges(ts_conn, "src/comp/Widget.vue::save") == {
        "src/comp/Widget.vue::validate", "src/utils/helpers.ts::helperA",
    }


def test_vue_options_api_method_is_found_from_a_mustache_call(ts_conn):
    assert "src/comp/Legacy.vue::greet" in edges(ts_conn, "src/comp/Legacy.vue::<template>")


def test_vue_slot_shorthand_and_dynamic_directives_are_parsed(ts_conn):
    """`#default`, `:[dynamicProp]` and `@[dynamicEvent]` are non-standard
    enough to have broken the attribute regex once - one odd directive must
    not blank out every other call in the same template."""
    assert edges(ts_conn, "src/comp/SlotShorthand.vue::<template>") == {
        "src/comp/SlotShorthand.vue::formatRow",
        "src/comp/SlotShorthand.vue::emptyMessage",
        "src/comp/SlotShorthand.vue::computeValue",
        "src/comp/SlotShorthand.vue::handleDynamic",
    }


def test_template_only_vue_file_indexes_without_a_script_block(ts_conn):
    ids = node_ids(ts_conn)
    assert "src/comp/TemplateOnly.vue" in ids
    assert "src/comp/TemplateOnly.vue::<template>" in ids


def test_impacted_by_surfaces_the_template_as_a_caller(ts_conn):
    import query_lib as ql
    impacted = {n["id"] for n in ql.impacted_by(ts_conn, "src/comp/Widget.vue::validate")["impacted"]}
    assert "src/comp/Widget.vue::save" in impacted
    assert "src/comp/Widget.vue::<template>" in impacted


# --- self / instance-method type inference (TS side) --------------------

def test_this_call_resolves_to_the_owning_class(ts_conn):
    assert edges(ts_conn, "src/selfResolution.ts::Worker.dispatch") == {
        "src/selfResolution.ts::Worker.run"
    }


def test_extends_resolves_same_file_and_cross_file(ts_conn):
    assert "src/selfResolution.ts::BaseThing.shared" in edges(
        ts_conn, "src/selfResolution.ts::Derived.useInherited")
    assert "src/remoteBase.ts::RemoteBase.remoteMethod" in edges(
        ts_conn, "src/selfResolution.ts::RemoteUser.useRemote")


@pytest.mark.parametrize("method", [
    "src/selfResolution.ts::ComposedField.useField",        # field type annotation
    "src/selfResolution.ts::ComposedParamProp.useParamProp",  # constructor param property
    "src/selfResolution.ts::ComposedAssign.doWork",         # this.x = new X()
    "src/selfResolution.ts::ComposedAssign.doLocal",        # const local = new X()
])
def test_attribute_and_local_types_are_inferred(ts_conn, method):
    assert "src/helper.ts::Helper.assist" in edges(ts_conn, method)


def test_unknown_attribute_type_stays_external(ts_conn):
    out = edges(ts_conn, "src/selfResolution.ts::ComposedAssign.doUnknownAttr")
    assert "external:assist" in out
    assert "src/helper.ts::Helper.assist" not in out


# --- graph integrity ----------------------------------------------------

def test_no_dangling_edges(ts_conn):
    ids = node_ids(ts_conn)
    dangling = [
        (src, etype, dst) for src, etype, dst in edge_rows(ts_conn)
        if not dst.startswith(("external:", "ambiguous:")) and dst not in ids
    ]
    assert dangling == []
