from changed_literals.diff_parser import parse_diff

MODIFIED_DIFF = """\
diff --git a/src/App.tsx b/src/App.tsx
index abc123..def456 100644
--- a/src/App.tsx
+++ b/src/App.tsx
@@ -10 +10 @@ function App() {
-  return <div>Old text</div>;
+  return <div>New text</div>;
@@ -20,0 +21,2 @@ function App() {
+  const extra = "line";
+  const extra2 = "line2";
"""

NEW_FILE_DIFF = """\
diff --git a/src/New.tsx b/src/New.tsx
new file mode 100644
index 0000000..abc123
--- /dev/null
+++ b/src/New.tsx
@@ -0,0 +1,3 @@
+export const New = () => {
+  return <div>Hello</div>;
+};
"""

DELETED_FILE_DIFF = """\
diff --git a/src/Old.tsx b/src/Old.tsx
deleted file mode 100644
index abc123..0000000
--- a/src/Old.tsx
+++ /dev/null
@@ -1,3 +0,0 @@
-export const Old = () => {
-  return <div>Bye</div>;
-};
"""


def test_modified_file_line_ranges():
    files = parse_diff(MODIFIED_DIFF)
    assert len(files) == 1
    f = files[0]
    assert f.path == "src/App.tsx"
    assert not f.is_new_file
    assert not f.is_deleted_file
    assert f.removed_lines == {10}
    assert f.added_lines == {10, 21, 22}


def test_new_file_has_no_removed_lines():
    files = parse_diff(NEW_FILE_DIFF)
    assert len(files) == 1
    f = files[0]
    assert f.is_new_file
    assert f.removed_lines == set()
    assert f.added_lines == {1, 2, 3}


def test_deleted_file_has_no_added_lines():
    files = parse_diff(DELETED_FILE_DIFF)
    assert len(files) == 1
    f = files[0]
    assert f.is_deleted_file
    assert f.added_lines == set()
    assert f.removed_lines == {1, 2, 3}


def test_no_hunks_means_no_files():
    text = "diff --git a/x b/x\nindex a..b 100644\n--- a/x\n+++ b/x\n"
    assert parse_diff(text) == []
