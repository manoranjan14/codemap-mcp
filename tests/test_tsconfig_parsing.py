"""tsconfig.json parsing (JSONC-tolerant).

A real tsconfig may carry // and /* */ comments, so the raw text is
stripped before json.loads. That stripping was done with regexes that know
nothing about string literals - and TypeScript path aliases are globs:

    "paths":   { "@/*": ["./*"] }
    "include": ["**/*.ts"]

`@/*` opens what the regex reads as a block comment and `**/*.ts` closes
it, so everything between them - the entire paths block - was deleted. The
mangled text then failed json.loads, the exception was swallowed, and
load_tsconfig_aliases returned [] as if the file had no aliases at all.

Measured consequence on a real Next.js repo (marketing-site, 1,555 files):
every `@/...` import went unresolved, resolution fell back to matching by
bare name, and 9,078 call sites - 15% of the repo - came out "ambiguous"
and were dropped. Other repos escaped only by luck: webapp's tsconfig
has `@/*` but no later `*/` to close the phantom comment.
"""
from __future__ import annotations

import json

import ts_parser


def write(tmp_path, text):
    (tmp_path / "tsconfig.json").write_text(text)
    return str(tmp_path)


def test_glob_aliases_survive_comment_stripping(tmp_path):
    """The exact shape that broke: `@/*` paired with a later `**/*.ts`."""
    root = write(tmp_path, """
    {
      "compilerOptions": {
        "paths": { "@/*": ["./*"], "@payload-config": ["./payload.config.ts"] }
      },
      "include": ["**/*.ts", ".next/types/**/*.ts"]
    }
    """)
    aliases = dict(ts_parser.load_tsconfig_aliases(root))
    assert aliases["@/"] == "./"
    assert "@payload-config" in aliases


def test_real_line_comments_are_still_stripped(tmp_path):
    root = write(tmp_path, """
    {
      // the source root
      "compilerOptions": { "paths": { "@/*": ["src/*"] } }
    }
    """)
    assert dict(ts_parser.load_tsconfig_aliases(root))["@/"] == "src/"


def test_real_block_comments_are_still_stripped(tmp_path):
    root = write(tmp_path, """
    {
      /* a block
         comment spanning lines */
      "compilerOptions": { "paths": { "@/*": ["src/*"] } }
    }
    """)
    assert dict(ts_parser.load_tsconfig_aliases(root))["@/"] == "src/"


def test_a_url_inside_a_string_is_not_treated_as_a_comment(tmp_path):
    root = write(tmp_path, """
    {
      "$schema": "https://json.schemastore.org/tsconfig",
      "compilerOptions": { "paths": { "@/*": ["src/*"] } }
    }
    """)
    assert dict(ts_parser.load_tsconfig_aliases(root))["@/"] == "src/"


def test_comment_markers_inside_strings_are_preserved(tmp_path):
    """A string value containing /* or */ is data, not syntax."""
    root = write(tmp_path, """
    {
      "compilerOptions": { "paths": { "@/*": ["src/*"] } },
      "note": "globs look like /* comments */ but are not"
    }
    """)
    assert dict(ts_parser.load_tsconfig_aliases(root))["@/"] == "src/"


def test_trailing_commas_are_tolerated(tmp_path):
    root = write(tmp_path, """
    {
      "compilerOptions": { "paths": { "@/*": ["src/*"], }, },
    }
    """)
    assert dict(ts_parser.load_tsconfig_aliases(root))["@/"] == "src/"


def test_a_comma_inside_a_string_is_not_mistaken_for_a_trailing_comma(tmp_path):
    root = write(tmp_path, """
    {
      "compilerOptions": { "paths": { "@/*": ["src/*"] } },
      "note": "a, ] and a, } live here"
    }
    """)
    assert dict(ts_parser.load_tsconfig_aliases(root))["@/"] == "src/"


def test_longest_prefix_wins(tmp_path):
    root = write(tmp_path, """
    {"compilerOptions": {"paths": {"@/*": ["src/*"], "@/lib/*": ["src/lib/*"]}}}
    """)
    prefixes = [p for p, _ in ts_parser.load_tsconfig_aliases(root)]
    assert prefixes.index("@/lib/") < prefixes.index("@/")


def test_no_tsconfig_returns_no_aliases(tmp_path):
    assert ts_parser.load_tsconfig_aliases(str(tmp_path)) == []


def test_unparseable_tsconfig_returns_no_aliases_rather_than_raising(tmp_path):
    root = write(tmp_path, "{ this is not json at all ")
    assert ts_parser.load_tsconfig_aliases(root) == []


def test_tsconfig_without_paths_returns_no_aliases(tmp_path):
    root = write(tmp_path, '{"compilerOptions": {"strict": true}}')
    assert ts_parser.load_tsconfig_aliases(root) == []


def test_the_real_world_file_that_exposed_this(tmp_path):
    """Verbatim shape of the Next.js tsconfig that returned [] in
    production, reduced to the parts that matter."""
    root = write(tmp_path, json.dumps({
        "compilerOptions": {
            "moduleResolution": "bundler",
            "plugins": [{"name": "next"}],
            "paths": {"@/*": ["./*"], "@payload-config": ["./payload.config.ts"]},
        },
        "include": ["next-env.d.ts", "**/*.ts", ".next/types/**/*.ts"],
        "exclude": ["node_modules", "export"],
    }, indent=2))
    assert dict(ts_parser.load_tsconfig_aliases(root))["@/"] == "./"


# --- alias targets that need normalising --------------------------------

def test_a_dot_slash_alias_target_resolves_to_repo_relative_paths():
    """Next.js writes `"@/*": ["./*"]`, i.e. "the repo root". That produced
    candidates like `./lib/payload.ts`, while every indexed file is keyed
    repo-relative as `lib/payload.ts` - so the candidate never matched and
    the import stayed unresolved even once the alias itself parsed."""
    got = ts_parser._resolve_module_candidates("app/page.tsx", "@/lib/payload", [("@/", "./")])
    assert "lib/payload.ts" in got
    assert not any(c.startswith("./") for c in got), got


def test_a_plain_alias_target_still_resolves():
    got = ts_parser._resolve_module_candidates("src/a.ts", "@/widget", [("@/", "src/")])
    assert "src/widget.ts" in got


def test_an_exact_alias_with_no_glob_resolves():
    got = ts_parser._resolve_module_candidates(
        "app/x.ts", "@payload-config", [("@payload-config", "./payload.config.ts")])
    assert "payload.config.ts" in got


def test_a_bare_package_import_is_still_left_alone():
    assert ts_parser._resolve_module_candidates("a.ts", "lodash", [("@/", "./")]) == []


def test_relative_imports_are_unaffected():
    got = ts_parser._resolve_module_candidates("src/a/b.ts", "../c", [])
    assert "src/c.ts" in got
