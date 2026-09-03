"""What a change review catches mechanically, before a model sees the diff."""

from __future__ import annotations

from daino.review.checks import review_change, summarise
from daino.review.diffs import parse_diff


def _diff(body: str) -> list:
    return parse_diff(body)


def _refs(findings: list) -> set[str]:
    return {item.reference for item in findings}


def _one(findings: list, reference: str):
    return next(item for item in findings if item.reference == reference)


# --------------------------------------------------------------- diff parsing


def test_added_lines_keep_their_number_in_the_new_file() -> None:
    """Every finding points at a line the reviewer can actually open."""
    changes = _diff(
        """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,3 +1,6 @@
 import os
+import sys

 VALUE = 1
+EXTRA = 2
+LAST = 3
"""
    )

    assert [item.path for item in changes] == ["app.py"]
    assert [(line.number, line.text) for line in changes[0].added] == [
        (2, "import sys"),
        (5, "EXTRA = 2"),
        (6, "LAST = 3"),
    ]
    assert changes[0].insertions == 3 and changes[0].deletions == 0


def test_the_parser_recognises_how_a_file_was_touched() -> None:
    changes = {
        item.path: item
        for item in _diff(
            """diff --git a/new.py b/new.py
new file mode 100644
--- /dev/null
+++ b/new.py
@@ -0,0 +1 @@
+x = 1
diff --git a/gone.py b/gone.py
deleted file mode 100644
--- a/gone.py
+++ /dev/null
@@ -1 +0,0 @@
-y = 2
diff --git a/old/name.py b/new/name.py
similarity index 90%
rename from old/name.py
rename to new/name.py
diff --git a/logo.png b/logo.png
Binary files a/logo.png and b/logo.png differ
"""
        )
    }

    assert changes["new.py"].kind == "added"
    assert changes["gone.py"].kind == "deleted"
    assert changes["new/name.py"].kind == "renamed"
    assert changes["new/name.py"].previous_path == "old/name.py"
    assert changes["logo.png"].binary


def test_only_introduced_lines_are_reviewed() -> None:
    """A file someone merely touched must not be blamed for what was already in it."""
    changes = _diff(
        """diff --git a/svc.py b/svc.py
--- a/svc.py
+++ b/svc.py
@@ -1,4 +1,5 @@
 import subprocess

 def old(cmd):
     return subprocess.run(cmd, shell=True)
+VALUE = 1
"""
    )

    # The pre-existing shell=True is context, not an added line.
    assert "py-shell-injection" not in _refs(review_change(changes))


# ------------------------------------------------------------------- syntax


def test_a_file_that_no_longer_parses_is_the_loudest_finding() -> None:
    changes = _diff(
        """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1,2 @@
 x = 1
+def broken(
"""
    )

    findings = review_change(changes, read_file=lambda path: "x = 1\ndef broken(\n")
    syntax = _one(findings, "review-syntax")

    assert syntax.severity == "critical"
    assert "does not parse" in syntax.title


def test_syntax_is_checked_through_a_grammar_for_other_languages() -> None:
    changes = _diff(
        """diff --git a/app.ts b/app.ts
--- a/app.ts
+++ b/app.ts
@@ -1 +1,2 @@
 const a = 1;
+function broken( {
"""
    )

    findings = review_change(changes, read_file=lambda path: "const a = 1;\nfunction broken( {\n")

    assert _one(findings, "review-syntax").severity == "high"


def test_a_malformed_config_file_is_caught_too() -> None:
    changes = _diff(
        """diff --git a/config.json b/config.json
--- a/config.json
+++ b/config.json
@@ -1 +1 @@
-{}
+{"a": }
"""
    )

    findings = review_change(changes, read_file=lambda path: '{"a": }')

    assert _one(findings, "review-syntax").severity == "critical"


def test_a_language_with_no_grammar_gets_no_opinion() -> None:
    """Silence is required here: claiming "syntax OK" for an unparsed file is worse."""
    changes = _diff(
        """diff --git a/main.zig b/main.zig
--- a/main.zig
+++ b/main.zig
@@ -1 +1,2 @@
 const a = 1;
+fn broken( {
"""
    )

    findings = review_change(changes, read_file=lambda path: "fn broken( {")

    assert "review-syntax" not in _refs(findings)


# ------------------------------------------------------------ what got left in


def test_a_conflict_marker_is_critical() -> None:
    changes = _diff(
        """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1,4 @@
 x = 1
+<<<<<<< HEAD
+y = 2
+>>>>>>> other
"""
    )

    finding = _one(review_change(changes), "review-conflict-marker")

    assert finding.severity == "critical"
    assert finding.line == 2


def test_debugging_left_behind_is_reported() -> None:
    changes = _diff(
        """diff --git a/app.js b/app.js
--- a/app.js
+++ b/app.js
@@ -1 +1,4 @@
 const a = 1;
+console.log(a);
+debugger;
+it.only("focused", () => {});
"""
    )

    findings = review_change(changes)
    titles = " ".join(item.title for item in findings)

    assert "console logging" in titles
    assert "debugger statement" in titles
    assert "focused test" in titles


def test_a_commented_out_debug_line_is_not_a_finding() -> None:
    changes = _diff(
        """diff --git a/app.js b/app.js
--- a/app.js
+++ b/app.js
@@ -1 +1,2 @@
 const a = 1;
+// console.log(a);
"""
    )

    assert "review-debug-leftover" not in _refs(review_change(changes))


def test_a_print_is_not_treated_as_debugging() -> None:
    """A command-line tool prints for a living; flagging it would be noise."""
    changes = _diff(
        """diff --git a/cli.py b/cli.py
--- a/cli.py
+++ b/cli.py
@@ -1 +1,2 @@
 import sys
+print("done")
"""
    )

    assert "review-debug-leftover" not in _refs(review_change(changes))


def test_source_that_reads_differently_from_how_it_runs_is_critical() -> None:
    """Trojan Source: a bidi override makes the line display in another order."""
    changes = _diff(
        """diff --git a/auth.py b/auth.py
--- a/auth.py
+++ b/auth.py
@@ -1 +1,2 @@
 x = 1
+if user.is_admin:  # ‮ gnitset rof ‬
"""
    )

    finding = _one(review_change(changes), "review-bidi-control")

    assert finding.severity == "critical"
    assert finding.cwe == "CWE-451"


def test_a_secret_added_by_the_change_is_found_at_the_right_line() -> None:
    """The repository audit's rules, applied to the diff and remapped."""
    changes = _diff(
        """diff --git a/settings.py b/settings.py
--- a/settings.py
+++ b/settings.py
@@ -10,2 +10,4 @@
 import os

+AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
+DEBUG = False
"""
    )

    finding = _one(review_change(changes), "secret-aws-access-key")

    assert finding.severity == "critical"
    assert finding.line == 12
    assert finding.source == "change review"


# ------------------------------------------------------------------- gaps


def test_source_changed_without_a_test_is_a_gap() -> None:
    findings = review_change(
        _diff(
            """diff --git a/svc.py b/svc.py
--- a/svc.py
+++ b/svc.py
@@ -1 +1,2 @@
 x = 1
+def added(): pass
"""
        )
    )

    assert _one(findings, "review-no-tests").category == "tests"


def test_touching_a_test_clears_the_gap() -> None:
    findings = review_change(
        _diff(
            """diff --git a/svc.py b/svc.py
--- a/svc.py
+++ b/svc.py
@@ -1 +1,2 @@
 x = 1
+def added(): pass
diff --git a/tests/test_svc.py b/tests/test_svc.py
--- a/tests/test_svc.py
+++ b/tests/test_svc.py
@@ -1 +1,2 @@
 import svc
+def test_added(): assert svc.added() is None
"""
        )
    )

    assert "review-no-tests" not in _refs(findings)


def test_a_test_that_loses_assertions_is_flagged() -> None:
    """The failure this catches: making a test pass by asking it less."""
    findings = review_change(
        _diff(
            """diff --git a/tests/test_svc.py b/tests/test_svc.py
--- a/tests/test_svc.py
+++ b/tests/test_svc.py
@@ -1,5 +1,3 @@
 def test_it():
-    assert result.ok
-    assert result.count == 3
-    assert result.name == "x"
+    assert result is not None
"""
        )
    )

    finding = _one(findings, "review-weakened-test")

    assert finding.severity == "high"
    assert "2 more assertion" in finding.title


def test_a_manifest_without_its_lockfile_is_flagged() -> None:
    findings = review_change(
        _diff(
            """diff --git a/package.json b/package.json
--- a/package.json
+++ b/package.json
@@ -1 +1,2 @@
 {
+  "dependencies": {"left-pad": "1.0.0"},
"""
        )
    )

    assert _one(findings, "review-lockfile-drift").category == "dependencies"


def test_a_manifest_with_its_lockfile_is_not() -> None:
    findings = review_change(
        _diff(
            """diff --git a/package.json b/package.json
--- a/package.json
+++ b/package.json
@@ -1 +1,2 @@
 {
+  "dependencies": {"left-pad": "1.0.0"},
diff --git a/package-lock.json b/package-lock.json
--- a/package-lock.json
+++ b/package-lock.json
@@ -1 +1,2 @@
 {
+  "left-pad": {},
"""
        )
    )

    assert "review-lockfile-drift" not in _refs(findings)


def test_a_schema_change_with_no_migration_is_flagged() -> None:
    findings = review_change(
        _diff(
            """diff --git a/app/models.py b/app/models.py
--- a/app/models.py
+++ b/app/models.py
@@ -1 +1,2 @@
 class User(Base):
+    email = Column(String)
"""
        )
    )

    assert "review-migration-gap" in _refs(findings)


def test_a_migration_alongside_the_schema_clears_it() -> None:
    findings = review_change(
        _diff(
            """diff --git a/app/models.py b/app/models.py
--- a/app/models.py
+++ b/app/models.py
@@ -1 +1,2 @@
 class User(Base):
+    email = Column(String)
diff --git a/alembic/versions/0007_email.py b/alembic/versions/0007_email.py
new file mode 100644
--- /dev/null
+++ b/alembic/versions/0007_email.py
@@ -0,0 +1 @@
+revision = "0007"
"""
        )
    )

    assert "review-migration-gap" not in _refs(findings)


def test_a_removed_public_definition_is_worth_confirming() -> None:
    findings = review_change(
        _diff(
            """diff --git a/api.py b/api.py
--- a/api.py
+++ b/api.py
@@ -1,6 +1,3 @@
-def public_helper():
-    pass
-
-def _private():
+def kept():
     pass
"""
        )
    )

    finding = _one(findings, "review-removed-symbol")

    assert "public_helper" in finding.detail
    # A leading underscore is not part of the module's surface.
    assert "_private" not in finding.detail


# ------------------------------------------------------------------- shape


def test_the_summary_describes_the_shape_of_the_change() -> None:
    stats = summarise(
        _diff(
            """diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -1,2 +1,2 @@
 x = 1
-y = 2
+y = 3
diff --git a/tests/test_a.py b/tests/test_a.py
--- a/tests/test_a.py
+++ b/tests/test_a.py
@@ -1 +1,2 @@
 import a
+def test_a(): assert a
"""
        )
    )

    assert (stats.files, stats.insertions, stats.deletions) == (2, 2, 1)
    assert stats.tests_touched
    assert stats.areas == ("src", "tests")


def test_a_deleted_file_produces_no_per_file_findings() -> None:
    findings = review_change(
        _diff(
            """diff --git a/gone.py b/gone.py
deleted file mode 100644
--- a/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-console.log("x")
-<<<<<<< HEAD
"""
        )
    )

    assert not _refs(findings) & {"review-debug-leftover", "review-conflict-marker"}
