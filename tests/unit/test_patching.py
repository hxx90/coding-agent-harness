from __future__ import annotations

import pytest

from agent_harness.errors import ToolExecutionError
from agent_harness.patching import apply_file_patch, parse_unified_diff


def test_applies_standard_unified_diff():
    original = "one\ntwo\nthree\n"
    patch = """--- a/src/demo.py
+++ b/src/demo.py
@@ -1,3 +1,3 @@
 one
-two
+changed
 three
"""
    parsed = parse_unified_diff(patch)
    assert len(parsed) == 1
    assert apply_file_patch(original, parsed[0]) == "one\nchanged\nthree\n"


def test_supports_new_file():
    patch = """--- /dev/null
+++ b/src/new.py
@@ -0,0 +1,2 @@
+VALUE = 1
+NAME = "demo"
"""
    parsed = parse_unified_diff(patch)
    assert parsed[0].is_new
    assert apply_file_patch("", parsed[0]) == 'VALUE = 1\nNAME = "demo"\n'


def test_rejects_delete_and_context_conflict():
    delete_patch = """--- a/src/demo.py
+++ /dev/null
@@ -1 +0,0 @@
-value = 1
"""
    with pytest.raises(ToolExecutionError, match="不允许删除"):
        parse_unified_diff(delete_patch)

    conflict_patch = """--- a/src/demo.py
+++ b/src/demo.py
@@ -1 +1 @@
-different
+changed
"""
    with pytest.raises(ToolExecutionError, match="上下文不匹配"):
        apply_file_patch("actual\n", parse_unified_diff(conflict_patch)[0])


def test_tolerates_wrong_hunk_counts_when_context_is_exact():
    patch = '''--- a/src/order.py
+++ b/src/order.py
@@ -1,99 +1,42 @@
 """Order domain helpers."""
+
+def calculate_discount(subtotal: float) -> float:
+    return 0.0
'''
    parsed = parse_unified_diff(patch)
    assert apply_file_patch('"""Order domain helpers."""\n', parsed[0]) == (
        '"""Order domain helpers."""\n\n'
        'def calculate_discount(subtotal: float) -> float:\n'
        '    return 0.0\n'
    )
