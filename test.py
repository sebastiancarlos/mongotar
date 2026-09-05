#!/usr/bin/env -S uv run python

import logging
import os
import stat
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from shutil import rmtree

import mongotar as mongotar_lib

_LOGGER = logging.getLogger("mongotar")

# --- Constants ---

ROOT = Path(__file__).resolve().parent

SOURCE_DIR_NAME = "test_source"
PROJECT_DIR_NAME = "project"

# --- Helper functions ---


def _get_simple_permissions(filepath: str | Path) -> str | None:
    """Gets the simplified permission string ('rw' or 'rwx') for the user."""
    try:
        st = Path(filepath).stat()
    except OSError:
        return None
    owner_perms = stat.filemode(st.st_mode)[1:4]
    match owner_perms:
        case "rwx":
            return "rwx"
        case "rw-":
            return "rw"
        case _:
            return "other"


def _make_file(
    source_dir: Path, rel_path: str | Path, content: str, permissions: str | None = None
) -> Path:
    """Creates a file at source_dir / rel_path with optional permissions (rwx/r)."""
    full_path = source_dir / rel_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    with open(full_path, "w", encoding="utf-8") as f:
        f.write(content)
    mode = stat.S_IRUSR | stat.S_IWUSR
    if permissions == "rwx":
        mode |= stat.S_IXUSR
    elif permissions == "r":
        mode = stat.S_IRUSR
    full_path.chmod(mode)
    return full_path


def _cmd(*args: str) -> list[str]:
    # Prefer the project's own console script installed by `uv sync`
    local = ROOT / ".venv" / "bin" / "mongotar"
    if local.is_file():
        return [str(local), *args]
    local_exe = ROOT / ".venv" / "Scripts" / "mongotar.exe"
    if local_exe.is_file():
        return [str(local_exe), *args]
    return ["uv", "run", "mongotar", *args]


IS_WINDOWS = os.name == "nt"

RWX = "rw" if IS_WINDOWS else "rwx"


def _strip_anchor(path: Path) -> Path:
    return path.relative_to(path.anchor)


@contextmanager
def _chdir(path: str | Path):
    """Temporarily changes the working directory, restoring it on exit."""
    orig_cwd = Path.cwd()
    try:
        os.chdir(path)
        yield
    finally:
        os.chdir(orig_cwd)


# ---
# Unit Tests for the Library
# ---


class TestMongotarLib(unittest.TestCase):
    def setUp(self) -> None:
        # Create a nested test dir structure:
        # - /tmp/mongotar_unit_1234/
        #   - test_source/
        #     - project/
        #   - unit_output.mongotar
        #   - unit_deserialized/
        self.test_dir = Path(tempfile.mkdtemp(prefix="mongotar_unit_")).resolve()
        self.source_base = self.test_dir / SOURCE_DIR_NAME
        self.source_dir = self.source_base / PROJECT_DIR_NAME
        self.source_dir.mkdir(parents=True, exist_ok=True)
        self.output_mongotar = self.test_dir / "unit_output.mongotar"
        self.deserialize_dir = self.test_dir / "unit_deserialized"

    def tearDown(self) -> None:
        if self.test_dir.exists():
            rmtree(self.test_dir, ignore_errors=True)

    def _create_file(
        self, rel_path: str | Path, content: str, permissions: str | None = None
    ) -> Path:
        """Creates a file within self.source_dir"""
        return _make_file(self.source_dir, rel_path, content, permissions)

    def test_unit_basic_serialize_deserialize(self):
        """Tests basic serialization and deserialization via library."""
        file1_rel = Path("subdir") / "file1.txt"
        exec_rel = Path("script.sh")
        file1_content = "Content 1"
        exec_content = "#!/bin/sh\necho test"
        self._create_file(file1_rel, file1_content, "rw")
        self._create_file(exec_rel, exec_content, "rwx")

        source_dir_archive = _strip_anchor(self.source_dir)

        # Expected content - note the path separators are always '/'
        # Order should be deterministic (sorted)
        expected_serialized = (
            f"--- {source_dir_archive.as_posix()}/{exec_rel.as_posix()}"
            f" --- {RWX}\n{exec_content}\n\n"
            f"--- {source_dir_archive.as_posix()}/{file1_rel.as_posix()}"
            f" --- rw\n{file1_content}\n\n"
        )

        # Serialize the source directory
        result = mongotar_lib.serialize([str(self.source_dir)], str(self.output_mongotar))
        self.assertTrue(result)
        self.assertTrue(self.output_mongotar.exists())

        with open(self.output_mongotar, encoding="utf-8") as f:
            actual_serialized = f.read()
        self.assertEqual(actual_serialized, expected_serialized)

        # Deserialize
        result = mongotar_lib.deserialize(str(self.output_mongotar), str(self.deserialize_dir))
        self.assertTrue(result)

        # Verify extracted files
        deser_file1 = self.deserialize_dir / source_dir_archive / file1_rel
        deser_exec = self.deserialize_dir / source_dir_archive / exec_rel

        self.assertTrue(deser_file1.exists())
        self.assertTrue(deser_exec.exists())

        with open(deser_file1, encoding="utf-8") as f:
            # Content includes the final newline from the archive format
            self.assertEqual(f.read().strip(), file1_content)
        with open(deser_exec, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), exec_content)

        # Verify permissions (allow for slight variations if OS adds group bits, focus on user)
        self.assertEqual(_get_simple_permissions(str(deser_file1)), "rw")
        self.assertEqual(_get_simple_permissions(str(deser_exec)), RWX)

    def test_unit_roundtrip_preserves_exact_content(self):
        """Round-trip must reproduce file content byte-for-byte.

        The archive format appends a fixed ``\\n\\n`` separator after every
        entry. Deserialization must strip exactly that separator so the
        extracted files match the originals, including trailing-newline and
        empty-file edge cases.
        """
        cases = [
            ("with_trailing.txt", "a\nb\n"),
            ("no_trailing.txt", "a\nb"),
            ("empty.txt", ""),
            ("single_line.txt", "hello"),
            ("single_line_trailing.txt", "hello\n"),
        ]

        for rel_path, content in cases:
            with self.subTest(content=repr(content)):
                self._create_file(rel_path, content, "rw")

        # Serialize the whole directory and deserialize to a fresh location.
        result = mongotar_lib.serialize([str(self.source_dir)], str(self.output_mongotar))
        self.assertTrue(result)
        result = mongotar_lib.deserialize(str(self.output_mongotar), str(self.deserialize_dir))
        self.assertTrue(result)

        source_dir_archive = _strip_anchor(self.source_dir)

        for rel_path, content in cases:
            with self.subTest(content=repr(content)):
                extracted = self.deserialize_dir / source_dir_archive / rel_path
                self.assertTrue(extracted.exists())
                with open(extracted, encoding="utf-8") as f:
                    self.assertEqual(f.read(), content)

    def test_unit_serialize_archive_layout(self):
        """Test serializing being fine on edge cases, like no new line at end or empty file"""
        cases = [
            ("with_trailing.txt", "a\nb\n"),
            ("no_trailing.txt", "a\nb"),
            ("empty.txt", ""),
        ]

        for rel_path, content in cases:
            self._create_file(rel_path, content, "rw")

        result = mongotar_lib.serialize([str(self.source_dir)], str(self.output_mongotar))
        self.assertTrue(result)

        with open(self.output_mongotar, encoding="utf-8") as f:
            archive = f.read()

        source_dir_archive = _strip_anchor(self.source_dir)

        for rel_path, content in cases:
            with self.subTest(content=repr(content)):
                header = f"--- {source_dir_archive.as_posix()}/{rel_path} --- rw\n"
                self.assertIn(header + content + "\n\n", archive)

    def test_unit_serialize_excludes_vcs_dirs(self):
        """VCS directories (.git, .svn, CVS) are always excluded, without -e."""
        self._create_file(Path(".git") / "config", "git")
        self._create_file(Path(".svn") / "entries" / "wc.db", "db")
        self._create_file(Path("CVS") / "Root", "root")
        self._create_file(Path("data") / "file.txt", "keep")

        with _chdir(self.source_base):
            self.assertTrue(mongotar_lib.serialize([PROJECT_DIR_NAME], str(self.output_mongotar)))
        archive = self.output_mongotar.read_text(encoding="utf-8")
        self.assertNotIn(f"{PROJECT_DIR_NAME}/.git/config", archive)
        self.assertNotIn(f"{PROJECT_DIR_NAME}/.svn/entries/wc.db", archive)
        self.assertNotIn(f"{PROJECT_DIR_NAME}/CVS/Root", archive)
        self.assertIn(f"{PROJECT_DIR_NAME}/data/file.txt", archive)

    def test_unit_serialize_exclude_vcs_respects_gitignore(self):
        """exclude_vcs respects a root .gitignore, including nested dirs."""
        self._create_file(Path(".gitignore"), "*.log\n/build/\n")
        self._create_file(Path("app.log"), "ignore me")
        self._create_file(Path("src") / "deep" / "err.log", "ignore me")
        self._create_file(Path("src") / "deep" / "run.py", "keep")
        self._create_file(Path("build") / "out.bin", "ignore me")
        self._create_file(Path("keep.md"), "keep")

        with _chdir(self.source_base):
            self.assertTrue(
                mongotar_lib.serialize(
                    [PROJECT_DIR_NAME], str(self.output_mongotar), exclude_vcs=True
                )
            )
        archive = self.output_mongotar.read_text(encoding="utf-8")
        self.assertNotIn(f"{PROJECT_DIR_NAME}/app.log", archive)
        self.assertNotIn(f"{PROJECT_DIR_NAME}/src/deep/err.log", archive)
        self.assertNotIn(f"{PROJECT_DIR_NAME}/build", archive)
        self.assertIn(f"{PROJECT_DIR_NAME}/src/deep/run.py", archive)
        self.assertIn(f"{PROJECT_DIR_NAME}/keep.md", archive)

    def test_unit_serialize_exclude_vcs_nested_gitignore(self):
        """exclude_vcs stacks nested .gitignore files per-directory."""
        self._create_file(Path("sub") / ".gitignore", "secret.tmp\n")
        self._create_file(Path("sub") / "secret.tmp", "ignore me")
        self._create_file(Path("sub") / "visible.txt", "keep")
        self._create_file(Path("secret.tmp"), "keep (not under sub)")

        with _chdir(self.source_base):
            self.assertTrue(
                mongotar_lib.serialize(
                    [PROJECT_DIR_NAME], str(self.output_mongotar), exclude_vcs=True
                )
            )
        archive = self.output_mongotar.read_text(encoding="utf-8")
        self.assertNotIn(f"{PROJECT_DIR_NAME}/sub/secret.tmp", archive)
        self.assertIn(f"{PROJECT_DIR_NAME}/sub/visible.txt", archive)
        # Root-level secret.tmp is not excluded by the nested sub/.gitignore.
        self.assertIn(f"{PROJECT_DIR_NAME}/secret.tmp", archive)

    def test_unit_serialize_default_skips_vcs_but_not_gitignore(self):
        "VCS dirs are skipped by default; .gitignore is only applied with -e."
        self._create_file(Path(".svn") / "wc.db", "db")
        self._create_file(Path(".gitignore"), "*.log\n")
        self._create_file(Path("app.log"), "log")

        with _chdir(self.source_base):
            self.assertTrue(mongotar_lib.serialize([PROJECT_DIR_NAME], str(self.output_mongotar)))
        archive = self.output_mongotar.read_text(encoding="utf-8")
        self.assertNotIn(f"{PROJECT_DIR_NAME}/.svn/wc.db", archive)
        # .gitignore is archived, but its rules are not applied without exclude_vcs.
        self.assertIn(f"{PROJECT_DIR_NAME}/.gitignore", archive)
        self.assertIn(f"{PROJECT_DIR_NAME}/app.log", archive)

    def test_unit_serialize_exclude_vcs_ancestor_gitignore_applies(self):
        """Ancestor and project-level .gitignore files apply together."""
        # source_base holds the project; test_dir is the repository root (above it).
        (self.test_dir / ".git").mkdir()
        # A .gitignore at the repo root, above the explicit dir.
        (self.test_dir / ".gitignore").write_text("*.tmp\n", encoding="utf-8")
        # Plus the project's own root .gitignore.
        self._create_file(Path(".gitignore"), "*.log\n")
        self._create_file(Path("keep.txt"), "keep")
        self._create_file(Path("drop.tmp"), "ignore me")
        self._create_file(Path("drop.log"), "ignore me")
        self._create_file(Path("docs") / "keep.txt", "keep")

        with _chdir(self.source_base):
            self.assertTrue(
                mongotar_lib.serialize(
                    [PROJECT_DIR_NAME], str(self.output_mongotar), exclude_vcs=True
                )
            )
        archive = self.output_mongotar.read_text(encoding="utf-8")
        self.assertIn(f"{PROJECT_DIR_NAME}/keep.txt", archive)
        self.assertNotIn(f"{PROJECT_DIR_NAME}/drop.tmp", archive)
        self.assertNotIn(f"{PROJECT_DIR_NAME}/drop.log", archive)
        self.assertIn(f"{PROJECT_DIR_NAME}/docs/keep.txt", archive)

    def test_unit_serialize_exclude_vcs_ancestor_gitignore_not_without_flag(self):
        """Ancestor .gitignore is only consulted under exclude_vcs."""
        (self.test_dir / ".git").mkdir()
        (self.test_dir / ".gitignore").write_text("*.tmp\n", encoding="utf-8")
        self._create_file(Path("drop.tmp"), "ignore me")

        with _chdir(self.source_base):
            self.assertTrue(mongotar_lib.serialize([PROJECT_DIR_NAME], str(self.output_mongotar)))
        archive = self.output_mongotar.read_text(encoding="utf-8")
        self.assertIn(f"{PROJECT_DIR_NAME}/drop.tmp", archive)

    def test_unit_serialize_exclude_vcs_deeper_negation_wins(self):
        """A deeper .gitignore negation overrides a shallower rule (git precedence)."""
        (self.test_dir / ".git").mkdir()
        (self.test_dir / ".gitignore").write_text("*.log\n", encoding="utf-8")
        self._create_file(Path("sub") / ".gitignore", "!keep.log\n")
        self._create_file(Path("sub") / "keep.log", "keep")
        self._create_file(Path("sub") / "other.log", "ignore me")

        with _chdir(self.source_base):
            self.assertTrue(
                mongotar_lib.serialize(
                    [PROJECT_DIR_NAME], str(self.output_mongotar), exclude_vcs=True
                )
            )
        archive = self.output_mongotar.read_text(encoding="utf-8")
        self.assertIn(f"{PROJECT_DIR_NAME}/sub/keep.log", archive)
        self.assertNotIn(f"{PROJECT_DIR_NAME}/sub/other.log", archive)

    def test_unit_serialize_exclude_vcs_explicit_dir_subtree_immune(self):
        """Explicitly naming a dir neutralizes ancestor rules that would drop it."""
        (self.test_dir / ".git").mkdir()
        (self.test_dir / ".gitignore").write_text(f"{PROJECT_DIR_NAME}\n", encoding="utf-8")
        self._create_file(Path("inner") / "file.txt", "keep")

        # The repo-root gitignore tries to drop the project dir itself.
        with _chdir(self.source_base):
            self.assertTrue(
                mongotar_lib.serialize(
                    [PROJECT_DIR_NAME], str(self.output_mongotar), exclude_vcs=True
                )
            )
        archive = self.output_mongotar.read_text(encoding="utf-8")
        self.assertIn(f"{PROJECT_DIR_NAME}/inner/file.txt", archive)

    def test_unit_serialize_exclude_vcs_explicit_dir_trailing_slash_gitignore_immune(self):
        """Trailing-slash .gitignore rule does not exclude an explicit input dir."""
        (self.test_dir / ".git").mkdir()
        (self.test_dir / ".gitignore").write_text(f"{PROJECT_DIR_NAME}/\n", encoding="utf-8")
        self._create_file(Path("inner") / "file.txt", "keep")

        with _chdir(self.source_base):
            self.assertTrue(
                mongotar_lib.serialize(
                    [PROJECT_DIR_NAME], str(self.output_mongotar), exclude_vcs=True
                )
            )
        archive = self.output_mongotar.read_text(encoding="utf-8")
        self.assertIn(f"{PROJECT_DIR_NAME}/inner/file.txt", archive)

    def test_unit_serialize_exclude_vcs_ancestor_spec_other_rules_still_apply(self):
        """An ancestor spec suppressed for an explicit dir still applies elsewhere."""
        # Repo root suppresses the explicit dir itself AND has an unrelated rule.
        (self.test_dir / ".git").mkdir()
        (self.test_dir / ".gitignore").write_text(f"{PROJECT_DIR_NAME}\n*.log\n", encoding="utf-8")
        # A sibling of the explicitly named project.
        (self.source_base / "other").mkdir()
        (self.source_base / "other" / "keep.txt").write_text("keep", encoding="utf-8")
        (self.source_base / "other" / "app.log").write_text("ignore me", encoding="utf-8")
        # Inside the explicitly named project.
        self._create_file(Path("inner") / "file.txt", "keep")
        self._create_file(Path("app.log"), "ignore me")

        with _chdir(self.source_base):
            self.assertTrue(
                mongotar_lib.serialize(
                    [PROJECT_DIR_NAME, "other"], str(self.output_mongotar), exclude_vcs=True
                )
            )
        archive = self.output_mongotar.read_text(encoding="utf-8")
        # The 'project' rule is suppressed under the explicitly named project.
        self.assertIn(f"{PROJECT_DIR_NAME}/inner/file.txt", archive)
        # Only the pattern matching the dir itself is neutralized: '*.log' still
        # applies even inside the explicit dir (and outside it too).
        self.assertNotIn(f"{PROJECT_DIR_NAME}/app.log", archive)
        self.assertIn("other/keep.txt", archive)
        self.assertNotIn("other/app.log", archive)

    def test_unit_serialize_exclude_vcs_explicit_file_included(self):
        """An explicitly named file is never ignored, even if .gitignore matches it."""
        self._create_file(Path(".gitignore"), "*.log\n")
        file_rel = Path("app.log")
        self._create_file(file_rel, "log")

        with _chdir(self.source_base):
            self.assertTrue(
                mongotar_lib.serialize(
                    [f"{PROJECT_DIR_NAME}/{file_rel}"],
                    str(self.output_mongotar),
                    exclude_vcs=True,
                )
            )
        archive = self.output_mongotar.read_text(encoding="utf-8")
        self.assertIn(f"{PROJECT_DIR_NAME}/{file_rel}", archive)

    def test_unit_serialize_exclude_glob(self):
        """--exclude glob patterns hide files, spanning subdirectories."""
        self._create_file(Path("app.log"), "log")
        self._create_file(Path("src") / "deep" / "err.log", "log")
        self._create_file(Path("src") / "deep" / "run.py", "keep")
        self._create_file(Path("keep.md"), "keep")

        with _chdir(self.source_base):
            self.assertTrue(
                mongotar_lib.serialize(
                    [PROJECT_DIR_NAME], str(self.output_mongotar), excludes=["*.log"]
                )
            )
        archive = self.output_mongotar.read_text(encoding="utf-8")
        self.assertNotIn(f"{PROJECT_DIR_NAME}/app.log", archive)
        self.assertNotIn(f"{PROJECT_DIR_NAME}/src/deep/err.log", archive)
        self.assertIn(f"{PROJECT_DIR_NAME}/src/deep/run.py", archive)
        self.assertIn(f"{PROJECT_DIR_NAME}/keep.md", archive)

    def test_unit_serialize_exclude_dir_subtree(self):
        """A pattern matching a directory excludes its whole subtree."""
        self._create_file(Path("build") / "out.o", "keep?")
        self._create_file(Path("build") / "sub" / "more.o", "keep?")
        self._create_file(Path("keep.txt"), "keep")

        with _chdir(self.source_base):
            self.assertTrue(
                mongotar_lib.serialize(
                    [PROJECT_DIR_NAME], str(self.output_mongotar), excludes=["*/build"]
                )
            )
        archive = self.output_mongotar.read_text(encoding="utf-8")
        self.assertNotIn(f"{PROJECT_DIR_NAME}/build", archive)
        self.assertIn(f"{PROJECT_DIR_NAME}/keep.txt", archive)

    def test_unit_serialize_exclude_nested_pattern(self):
        """A pattern with a slash matches only paths below it."""
        self._create_file(Path("src") / "a.tmp", "ignore")
        self._create_file(Path("src") / "sub" / "b.tmp", "ignore")
        self._create_file(Path("top.tmp"), "keep")
        self._create_file(Path("sub") / "c.tmp", "keep")

        with _chdir(self.source_base):
            self.assertTrue(
                mongotar_lib.serialize(
                    [PROJECT_DIR_NAME], str(self.output_mongotar), excludes=["*/src/*.tmp"]
                )
            )
        archive = self.output_mongotar.read_text(encoding="utf-8")
        self.assertNotIn(f"{PROJECT_DIR_NAME}/src/a.tmp", archive)
        self.assertNotIn(f"{PROJECT_DIR_NAME}/src/sub/b.tmp", archive)
        self.assertIn(f"{PROJECT_DIR_NAME}/top.tmp", archive)
        self.assertIn(f"{PROJECT_DIR_NAME}/sub/c.tmp", archive)

    def test_unit_serialize_exclude_anchored_pattern_no_leak(self):
        """A leading-slash-free pattern only matches its own directory level."""
        self._create_file(Path("src") / "a.tmp", "ignore")
        self._create_file(Path("top.tmp"), "keep")

        # 'src/*.tmp' must NOT match 'project/src/a.tmp' (archive path includes
        # the input dir name), matching GNU tar's full-name semantics.
        with _chdir(self.source_base):
            self.assertTrue(
                mongotar_lib.serialize(
                    [PROJECT_DIR_NAME], str(self.output_mongotar), excludes=["src/*.tmp"]
                )
            )
        archive = self.output_mongotar.read_text(encoding="utf-8")
        self.assertIn(f"{PROJECT_DIR_NAME}/src/a.tmp", archive)
        self.assertIn(f"{PROJECT_DIR_NAME}/top.tmp", archive)

    def test_unit_serialize_exclude_multiple_patterns(self):
        """Multiple exclude patterns each apply."""
        self._create_file(Path("a.o"), "obj")
        self._create_file(Path("b.tmp"), "tmp")
        self._create_file(Path("c.txt"), "keep")

        with _chdir(self.source_base):
            self.assertTrue(
                mongotar_lib.serialize(
                    [PROJECT_DIR_NAME],
                    str(self.output_mongotar),
                    excludes=["*.o", "*.tmp"],
                )
            )
        archive = self.output_mongotar.read_text(encoding="utf-8")
        self.assertNotIn(f"{PROJECT_DIR_NAME}/a.o", archive)
        self.assertNotIn(f"{PROJECT_DIR_NAME}/b.tmp", archive)
        self.assertIn(f"{PROJECT_DIR_NAME}/c.txt", archive)

    def test_unit_serialize_exclude_explicit_dir(self):
        """A pattern matching an explicitly named input dir empties the archive."""
        self._create_file(Path("inner") / "file.txt", "keep")

        with _chdir(self.source_base):
            with self.assertLogs(_LOGGER, level=logging.WARNING) as cm:
                self.assertFalse(
                    mongotar_lib.serialize(
                        [PROJECT_DIR_NAME], str(self.output_mongotar), excludes=[PROJECT_DIR_NAME]
                    )
                )
        self.assertIn("No valid input items found to serialize.", "\n".join(cm.output))
        self.assertTrue(self.output_mongotar.exists())

    def test_unit_serialize_exclude_explicit_file(self):
        """A pattern matching an explicitly named input file excludes it."""
        file_rel = Path("app.log")
        self._create_file(file_rel, "log")
        self._create_file(Path("keep.txt"), "keep")

        with _chdir(self.source_base):
            self.assertTrue(
                mongotar_lib.serialize(
                    [f"{PROJECT_DIR_NAME}/{file_rel}", f"{PROJECT_DIR_NAME}/keep.txt"],
                    str(self.output_mongotar),
                    excludes=["*.log"],
                )
            )
        archive = self.output_mongotar.read_text(encoding="utf-8")
        self.assertNotIn(f"{PROJECT_DIR_NAME}/{file_rel}", archive)
        self.assertIn(f"{PROJECT_DIR_NAME}/keep.txt", archive)

    def test_unit_serialize_exclude_logs_hits(self):
        """Verbose (DEBUG) output logs entries skipped by --exclude."""
        self._create_file(Path("keep.txt"), "keep")
        self._create_file(Path("app.log"), "log")
        excluded_abs = (self.source_dir / "app.log").resolve()

        with _chdir(self.source_base):
            with self.assertLogs(_LOGGER, level=logging.DEBUG) as cm:
                mongotar_lib.serialize(
                    [PROJECT_DIR_NAME], str(self.output_mongotar), excludes=["*.log"]
                )

        self.assertIn(f"Skipping excluded entry: {excluded_abs}", "\n".join(cm.output))

    def test_unit_serialize_verbose(self):
        """Per-file detail is logged at DEBUG during serialization."""
        file1_rel = Path("a.txt")
        self._create_file(file1_rel, "content a", "rw")

        source_dir_archive = _strip_anchor(self.source_dir)

        with self.assertLogs(_LOGGER, level=logging.DEBUG) as cm:
            mongotar_lib.serialize([str(self.source_dir)], str(self.output_mongotar))

        self.assertIn(
            f"Adding: {source_dir_archive.as_posix()}/{file1_rel.as_posix()}",
            "\n".join(cm.output),
        )

    def test_unit_serialize_verbose_logs_exclude_hits(self):
        """Verbose (DEBUG) output logs entries skipped by --exclude-vcs."""
        self._create_file(Path("keep.txt"), "keep")
        self._create_file(Path(".gitignore"), "*.log\n")
        self._create_file(Path("app.log"), "ignore me")
        ignored_abs = (self.source_dir / "app.log").resolve()

        with self.assertLogs(_LOGGER, level=logging.DEBUG) as cm:
            mongotar_lib.serialize(
                [str(self.source_dir)], str(self.output_mongotar), exclude_vcs=True
            )

        self.assertIn(f"Skipping ignored entry: {ignored_abs}", "\n".join(cm.output))

    def test_unit_deserialize_verbose(self):
        """Per-file detail is logged at DEBUG during deserialization."""
        file1_rel = Path("b.txt")
        self._create_file(file1_rel, "content b", "rw")
        mongotar_lib.serialize(
            [str(self.source_dir)], str(self.output_mongotar)
        )  # Create archive first

        source_dir_archive = _strip_anchor(self.source_dir)

        with self.assertLogs(_LOGGER, level=logging.DEBUG) as cm:
            mongotar_lib.deserialize(str(self.output_mongotar), str(self.deserialize_dir))

        output = "\n".join(cm.output)

        expected_path_abs = (self.deserialize_dir / source_dir_archive / file1_rel).resolve()
        self.assertIn(f"Extracting: {expected_path_abs}", output)
        if not IS_WINDOWS:
            self.assertIn(f"Applying permissions (rw) to: {expected_path_abs}", output)

    def test_unit_deserialize_no_overwrite_default(self):
        """Tests that deserialization skips existing files by default (library)."""
        file1_rel = Path("file_to_keep.txt")
        original_content = "KEEP THIS CONTENT"
        archive_content = "OVERWRITE CONTENT"

        # Create the archive with 'archive_content'
        archive_source_path = self._create_file(file1_rel, archive_content, "rw")
        mongotar_lib.serialize([str(archive_source_path)], str(self.output_mongotar))
        archive_source_path.unlink()  # Remove the source used for archive creation

        source_archive_path = _strip_anchor(archive_source_path)

        # Create the target file *before* deserializing
        target_file_path = self.deserialize_dir / source_archive_path
        target_file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(target_file_path, "w", encoding="utf-8") as f:
            f.write(original_content)

        # Deserialize without force flag, capture warnings
        with self.assertLogs(_LOGGER, level=logging.WARNING) as cm:
            success = mongotar_lib.deserialize(
                str(self.output_mongotar), str(self.deserialize_dir), force=False
            )

        # Assertions
        self.assertTrue(success, "Deserialization should report success even if files are skipped")
        warning_output = "\n".join(cm.output)
        self.assertIn("already exists. Skipping.", warning_output)
        self.assertIn(str(target_file_path.resolve()), warning_output)

        # Verify the file content was NOT overwritten
        with open(target_file_path, encoding="utf-8") as f:
            final_content = f.read()
        self.assertEqual(final_content, original_content)

    def test_unit_deserialize_force_overwrite(self):
        """Tests that deserialization overwrites with force=True (library)."""
        file1_rel = Path("file_to_overwrite.txt")
        original_content = "ORIGINAL CONTENT"
        archive_content = "NEW CONTENT FROM ARCHIVE"  # Make it distinct

        # Create the archive with 'archive_content'
        archive_source_path = self._create_file(file1_rel, archive_content, "rw")
        mongotar_lib.serialize([str(archive_source_path)], str(self.output_mongotar))
        archive_source_path.unlink()

        source_archive_path = _strip_anchor(archive_source_path)

        # Create the target file *before* deserializing
        target_file_path = self.deserialize_dir / source_archive_path
        target_file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(target_file_path, "w", encoding="utf-8") as f:
            f.write(original_content)

        # Deserialize WITH force flag (DEBUG so the overwrite message is logged)
        with self.assertLogs(_LOGGER, level=logging.DEBUG) as cm:
            success = mongotar_lib.deserialize(
                str(self.output_mongotar),
                str(self.deserialize_dir),
                force=True,
            )

        # Assertions
        self.assertTrue(success)
        all_output = "\n".join(cm.output)
        # No warnings should be emitted when forcing overwrite successfully
        self.assertNotIn("already exists. Skipping.", all_output)
        self.assertIn("Overwriting existing file", all_output)  # Check verbose message

        # Verify the file content WAS overwritten
        with open(target_file_path, encoding="utf-8") as f:
            final_content = f.read()
        # Remember archive adds extra newlines during parsing usually
        self.assertEqual(final_content.strip(), archive_content)

    def test_unit_deserialize_invalid_start(self):
        """Tests that deserialization fails when no valid header is present."""
        invalid_starts = [
            "--- file.txt -- rw\nMissing separator space",
            "--- file.txt --- readwrite\nInvalid permission string",
            "--- --- rw\nEmpty path in header",
            "No header at all, just text.",
            "",  # empty file
            "\n\n\n   \n",  # whitespace only
        ]

        for content in invalid_starts:
            # Use subTest for better reporting of which case failed
            with self.subTest(content_start=content[:30] or "<empty>"):
                invalid_mongotar_path = self.test_dir / "invalid_start.mongotar"
                with open(invalid_mongotar_path, "w", encoding="utf-8") as f:
                    f.write(content)

                # Ensure output directory exists but is empty before the call
                # Important if running multiple subtests
                if self.deserialize_dir.exists():
                    rmtree(self.deserialize_dir)
                self.deserialize_dir.mkdir(parents=True, exist_ok=True)

                with self.assertLogs(_LOGGER, level=logging.ERROR) as cm:
                    # Call the deserialize function
                    success = mongotar_lib.deserialize(
                        str(invalid_mongotar_path), str(self.deserialize_dir)
                    )

                error_output = "\n".join(cm.output)

                # --- Assertions ---
                # 1. Check failure return code
                self.assertFalse(
                    success,
                    f"Deserialize should return False for invalid start"
                    f" '{content[:30] or '<empty>'}...'",
                )

                # 2. Check for the error message
                self.assertIn("does not contain any valid file header", error_output)

                # 3. Verify absolutely no files were created in the output directory
                created_items = list(self.deserialize_dir.iterdir())
                self.assertEqual(
                    len(created_items),
                    0,
                    "Output directory should be empty after failed"
                    f" deserialize. Found: {created_items}",
                )

    def test_unit_deserialize_skips_leading_comment(self):
        """Leading free text before the first header is treated as a comment."""
        archive_content = (
            "This is a project description written by hand.\n"
            "It can span multiple lines.\n"
            "\n"
            "--- subdir/file.txt --- rw\n"
            "line1\n"
            "line2\n"
            "\n"
        )
        archive_path = self.test_dir / "commented.mongotar"
        archive_path.write_text(archive_content, encoding="utf-8")

        result = mongotar_lib.deserialize(str(archive_path), str(self.deserialize_dir))
        self.assertTrue(result)

        extracted = self.deserialize_dir / "subdir" / "file.txt"
        self.assertTrue(extracted.exists())
        with open(extracted, encoding="utf-8") as f:
            self.assertEqual(f.read(), "line1\nline2")

    def test_unit_deserialize_unsafe_output_dir(self):
        """Extraction into the filesystem root '/' is rejected at the library level."""
        valid_archive = self.test_dir / "safe.mongotar"
        with open(valid_archive, "w", encoding="utf-8") as f:
            f.write("--- a.txt --- rw\ncontent\n\n")

        for unsafe_dir in ("/", "/."):
            with self.subTest(output_dir=unsafe_dir):
                with self.assertLogs(_LOGGER, level=logging.ERROR) as cm:
                    result = mongotar_lib.deserialize(str(valid_archive), unsafe_dir)
                self.assertFalse(result)
                self.assertIn(
                    "Invalid or potentially unsafe output directory", "\n".join(cm.output)
                )

    def test_unit_deserialize_into_dot(self):
        """Deserializing into '.' extracts relative to the current directory."""
        archive = self.test_dir / "dot.mongotar"
        with open(archive, "w", encoding="utf-8") as f:
            f.write("--- sub/file.txt --- rw\ncontent\n\n")

        self.deserialize_dir.mkdir(parents=True, exist_ok=True)
        with _chdir(self.deserialize_dir):
            with self.assertLogs(_LOGGER, level=logging.INFO) as cm:
                self.assertTrue(mongotar_lib.deserialize(str(archive), "."))
            self.assertIn("Deserializing", "\n".join(cm.output))

        extracted = self.deserialize_dir / "sub" / "file.txt"
        self.assertTrue(extracted.exists())
        with open(extracted, encoding="utf-8") as f:
            self.assertEqual(f.read(), "content")

    def test_unit_deserialize_into_dot_force_overwrites(self):
        """Deserializing into '.' with force overwrites existing file content."""
        archive = self.test_dir / "dot_force.mongotar"
        with open(archive, "w", encoding="utf-8") as f:
            f.write("--- sub/file.txt --- rw\nNEW CONTENT\n\n")

        self.deserialize_dir.mkdir(parents=True, exist_ok=True)
        target_file = self.deserialize_dir / "sub" / "file.txt"
        target_file.parent.mkdir(parents=True, exist_ok=True)
        with open(target_file, "w", encoding="utf-8") as f:
            f.write("OLD CONTENT")

        with _chdir(self.deserialize_dir):
            with self.assertLogs(_LOGGER, level=logging.DEBUG) as cm:
                self.assertTrue(mongotar_lib.deserialize(str(archive), ".", force=True))
            self.assertIn("Overwriting existing file", "\n".join(cm.output))

        with open(target_file, encoding="utf-8") as f:
            self.assertEqual(f.read(), "NEW CONTENT")

    def test_unit_path_traversal_prevention(self):
        """Tests that paths attempting traversal are skipped (library)."""
        # These paths try to go above the intended output directory
        traversal_archive_content = (
            "--- ../../../etc/passwd --- rw\nroot:x:0:0:root:/root:/bin/bash\n\n"
            "--- ../absolute/path --- rw\nAttempt absolute\n\n"
            "--- ok/nested/file.txt --- rw\nThis one is safe.\n\n"
        )
        traversal_archive_path = self.test_dir / "traversal.mongotar"
        with open(traversal_archive_path, "w", encoding="utf-8") as f:
            f.write(traversal_archive_content)

        with self.assertLogs(_LOGGER, level=logging.WARNING) as cm:
            success = mongotar_lib.deserialize(
                str(traversal_archive_path), str(self.deserialize_dir)
            )

        self.assertTrue(success)  # Should succeed if safe files extracted
        warning_output = "\n".join(cm.output)

        # Check warnings for skipped unsafe paths
        self.assertIn("Skipping potentially unsafe path traversal", warning_output)
        self.assertIn("../../../etc/passwd", warning_output)
        self.assertIn("../absolute/path", warning_output)

        # Verify only the safe file was created
        self.assertTrue((self.deserialize_dir / "ok" / "nested" / "file.txt").exists())
        # Check that the potentially dangerous paths were NOT created relative to the test dir
        self.assertFalse(
            (self.test_dir / "etc" / "passwd").exists()
        )  # Check outside deserialize_dir
        self.assertFalse((self.deserialize_dir / ".." / "absolute").resolve().exists())

    def test_unit_serialize_relative_paths_from_cwd(self):
        """Relative input paths produce archive paths relative to CWD."""
        file_rel = Path("nested") / "f.txt"
        self._create_file(file_rel, "content", "rw")
        orig_cwd = Path.cwd()
        try:
            os.chdir(self.source_base)
            # PROJECT_DIR_NAME is a subdir of CWD (source_base)
            result = mongotar_lib.serialize([PROJECT_DIR_NAME], str(self.output_mongotar))
            self.assertTrue(result)
            with open(self.output_mongotar) as f:
                content = f.read()
            self.assertIn(
                f"--- {PROJECT_DIR_NAME}/{file_rel.as_posix()} --- rw\ncontent\n\n",
                content,
            )
        finally:
            os.chdir(orig_cwd)

    def test_unit_serialize_rejects_path_traversal_outside_cwd(self):
        """Relative paths resolving outside CWD via '..' are rejected."""
        # Create a file in source_base (parent of source_dir)
        outside_rel = "outside.txt"
        outside_abs = self.source_base / outside_rel
        with open(outside_abs, "w") as f:
            f.write("outside")

        orig_cwd = Path.cwd()
        try:
            os.chdir(self.source_dir)
            # "../outside.txt" resolves above CWD (source_dir)
            with self.assertLogs(_LOGGER, level=logging.WARNING) as cm:
                result = mongotar_lib.serialize(["../outside.txt"], str(self.output_mongotar))
            self.assertFalse(result)
            self.assertIn("resolves outside", "\n".join(cm.output))
        finally:
            os.chdir(orig_cwd)
            if outside_abs.exists():
                outside_abs.unlink()

    def test_unit_serialize_output_equals_input_error(self):
        """An explicit input item equal to the output file is an error."""
        duplicate = self.source_dir / "dup.txt"
        self._create_file("dup.txt", "content", "rw")

        with self.assertLogs(_LOGGER, level=logging.ERROR) as cm:
            result = mongotar_lib.serialize([str(duplicate)], str(duplicate))
        self.assertFalse(result)
        self.assertIn("cannot be the same as an input item", "\n".join(cm.output))

    def test_unit_serialize_multiple_relative_inputs(self):
        """Multiple relative inputs each keep their CWD-relative paths."""
        self._create_file(Path("top.txt"), "top", "rw")
        self._create_file(Path("sub") / "bottom.txt", "bottom", "rw")

        orig_cwd = Path.cwd()
        try:
            os.chdir(self.source_base)
            result = mongotar_lib.serialize(
                [
                    str(Path(PROJECT_DIR_NAME) / "top.txt"),
                    str(Path(PROJECT_DIR_NAME) / "sub" / "bottom.txt"),
                ],
                str(self.output_mongotar),
            )
            self.assertTrue(result)
            with open(self.output_mongotar) as f:
                content = f.read()
            self.assertIn(f"--- {PROJECT_DIR_NAME}/top.txt --- rw\ntop\n\n", content)
            self.assertIn(
                f"--- {PROJECT_DIR_NAME}/sub/bottom.txt --- rw\nbottom\n\n",
                content,
            )
        finally:
            os.chdir(orig_cwd)

    def test_unit_serialize_skips_symlink_during_traversal(self):
        """Symlinks encountered during traversal are skipped; target still processed."""
        target_rel = Path("real_target.txt")
        link_rel = Path("link_to_target.txt")
        self._create_file(target_rel, "real content", "rw")
        (self.source_dir / link_rel).symlink_to(self.source_dir / target_rel)

        orig_cwd = Path.cwd()
        try:
            os.chdir(self.source_base)
            with self.assertLogs(_LOGGER, level=logging.DEBUG) as cm:
                result = mongotar_lib.serialize([PROJECT_DIR_NAME], str(self.output_mongotar))
        finally:
            os.chdir(orig_cwd)

        self.assertTrue(result)

        with open(self.output_mongotar) as f:
            content = f.read()

        self.assertNotIn(
            f"--- {PROJECT_DIR_NAME}/{link_rel.as_posix()}",
            content,
        )
        self.assertIn(
            f"--- {PROJECT_DIR_NAME}/{target_rel.as_posix()}",
            content,
        )
        self.assertIn("Skipping link:", "\n".join(cm.output))

    def test_unit_serialize_skips_header_colliding_content(self):
        """File whose content matches the header format is skipped with a warning."""
        # This file's content looks like a mongotar header.
        self._create_file(Path("safe.txt"), "safe content", "rw")
        self._create_file(Path("collider.txt"), "--- other.txt --- rw\npayload\n", "rw")

        with _chdir(self.source_base):
            with self.assertLogs(_LOGGER, level=logging.WARNING) as cm:
                result = mongotar_lib.serialize(
                    [PROJECT_DIR_NAME], str(self.output_mongotar), exclude_vcs=False
                )
        self.assertTrue(result)
        archive = self.output_mongotar.read_text(encoding="utf-8")
        self.assertIn(f"{PROJECT_DIR_NAME}/safe.txt", archive)
        self.assertNotIn(f"{PROJECT_DIR_NAME}/collider.txt", archive)
        self.assertIn("collider.txt", "\n".join(cm.output))
        self.assertIn("matches the archive header format", "\n".join(cm.output))

    def test_unit_deserialize_handles_crlf_line_endings(self):
        """Archives with \\r\\n line endings are parsed correctly."""
        archive_content = "--- subdir/file.txt --- rw\r\nline1\r\nline2\r\n\r\n"
        archive_path = self.test_dir / "crlf.mongotar"
        archive_path.write_bytes(archive_content.encode("utf-8"))

        with self.assertLogs(_LOGGER, level=logging.DEBUG):
            result = mongotar_lib.deserialize(str(archive_path), str(self.deserialize_dir))
        self.assertTrue(result)

        extracted = self.deserialize_dir / "subdir" / "file.txt"
        self.assertTrue(extracted.exists())
        with open(extracted, encoding="utf-8") as f:
            self.assertEqual(f.read(), "line1\nline2")


# ---
# E2E Tests for the 'mongotar' CLI
# ---


class TestMongotarCLI(unittest.TestCase):
    def setUp(self) -> None:
        self.test_dir = Path(tempfile.mkdtemp(prefix="mongotar_cli_")).resolve()
        self.source_dir = self.test_dir / SOURCE_DIR_NAME
        self.archive_file = self.test_dir / "cli_archive.mongotar"
        self.dest_dir = self.test_dir / "cli_dest"
        self.source_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        if self.test_dir.exists():
            rmtree(self.test_dir, ignore_errors=True)

    def _run_mongotar(
        self, args: list[str], cwd: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Runs the mongotar CLI and returns the result."""

        command = _cmd(*args)

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            cwd=cwd,
        )
        return result

    def _create_file(
        self, rel_path: str | Path, content: str, permissions: str | None = None
    ) -> Path:
        """Creates a file within self.source_dir for CLI tests."""
        return _make_file(self.source_dir, rel_path, content, permissions)

    def test_cli_serialize_deserialize_verbose(self):
        """Tests CLI serialize/deserialize with verbose flag."""
        file1_rel = Path("data") / "file1.txt"
        exec_rel = Path("run.sh")
        self._create_file(file1_rel, "Data 1", "rw")
        self._create_file(exec_rel, "echo run", "rwx")

        source_dir_archive = _strip_anchor(self.source_dir)

        # Serialize Verbose
        result_ser = self._run_mongotar(["-v", str(self.source_dir), str(self.archive_file)])
        self.assertEqual(result_ser.returncode, 0, f"CLI serialize failed:\n{result_ser.stderr}")
        self.assertIn(
            f"Adding: {source_dir_archive.as_posix()}/{file1_rel.as_posix()}",
            result_ser.stderr,
        )
        self.assertIn(
            f"Adding: {source_dir_archive.as_posix()}/{exec_rel.as_posix()}",
            result_ser.stderr,
        )
        self.assertIn("Successfully serialized", result_ser.stderr)

        # Deserialize Verbose
        result_des = self._run_mongotar(["-d", "-v", str(self.archive_file), str(self.dest_dir)])
        self.assertEqual(result_des.returncode, 0, f"CLI deserialize failed:\n{result_des.stderr}")

        abs_dest_file1 = (self.dest_dir / source_dir_archive / file1_rel).resolve()
        abs_dest_exec = (self.dest_dir / source_dir_archive / exec_rel).resolve()

        self.assertTrue(abs_dest_file1.exists())
        self.assertTrue(abs_dest_exec.exists())
        # Check for key verbose messages on stderr (diagnostics)
        self.assertIn(f"Extracting: {abs_dest_file1}", result_des.stderr)
        if not IS_WINDOWS:
            self.assertIn(f"Applying permissions (rw) to: {abs_dest_file1}", result_des.stderr)
        self.assertIn(f"Extracting: {abs_dest_exec}", result_des.stderr)
        if not IS_WINDOWS:
            self.assertIn(f"Applying permissions (rwx) to: {abs_dest_exec}", result_des.stderr)
        self.assertIn("Successfully deserialized", result_des.stderr)

    def test_cli_deserialize_no_overwrite_default(self):
        """Tests CLI deserialize skips existing file by default."""
        file_rel = Path("config.ini")
        archive_content = "[settings]\nvalue=new"
        existing_content = "[settings]\nvalue=old"

        # Create archive
        abs_source_file = self.source_dir / file_rel
        self._create_file(file_rel, archive_content, "rw")
        result_ser = self._run_mongotar([str(abs_source_file), str(self.archive_file)])
        self.assertEqual(result_ser.returncode, 0)
        abs_source_file.unlink()  # Clean up source

        source_archive_path = _strip_anchor(abs_source_file)

        # Create existing file in destination
        dest_file_path = self.dest_dir / source_archive_path
        dest_file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(dest_file_path, "w", encoding="utf-8") as f:
            f.write(existing_content)

        # Run deserialize without -f
        result_des = self._run_mongotar(["-d", str(self.archive_file), str(self.dest_dir)])
        self.assertEqual(
            result_des.returncode,
            0,
            f"Deserialize should succeed even if skipping:\n{result_des.stderr}",
        )  # Should still be exit code 0

        # Check stderr for warning
        self.assertIn("already exists. Skipping.", result_des.stderr)
        self.assertIn(str(dest_file_path.resolve()), result_des.stderr)
        self.assertNotIn("forcing overwrite", result_des.stdout)  # Ensure no force message

        # Verify content was NOT overwritten
        with open(dest_file_path, encoding="utf-8") as f:
            content_after = f.read()
        self.assertEqual(content_after, existing_content)

    def test_cli_deserialize_force_overwrite(self):
        """Tests CLI deserialize overwrites with -f."""
        file_rel = Path("config.ini")
        archive_content = "[settings]\nvalue=new"
        existing_content = "[settings]\nvalue=old"

        # Create archive
        abs_source_file = self.source_dir / file_rel
        self._create_file(file_rel, archive_content, "rw")
        result_ser = self._run_mongotar([str(abs_source_file), str(self.archive_file)])
        self.assertEqual(result_ser.returncode, 0)
        abs_source_file.unlink()  # Clean up source

        source_archive_path = _strip_anchor(abs_source_file)

        # Create existing file in destination
        dest_file_path = self.dest_dir / source_archive_path
        dest_file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(dest_file_path, "w", encoding="utf-8") as f:
            f.write(existing_content)

        # Run deserialize WITH -f
        result_des = self._run_mongotar(["-d", "-f", str(self.archive_file), str(self.dest_dir)])
        self.assertEqual(
            result_des.returncode,
            0,
            f"Deserialize failed with -f:\n{result_des.stderr}",
        )

        # No skipping warning should be emitted when forcing overwrite
        self.assertNotIn("already exists. Skipping.", result_des.stderr)
        self.assertIn("forcing overwrite", result_des.stderr)  # Check for force message

        # Verify content WAS overwritten
        with open(dest_file_path, encoding="utf-8") as f:
            content_after = f.read()
        # Strip trailing newline added by archive read/write cycle
        self.assertEqual(content_after.strip(), archive_content)

    def test_cli_force_flag_with_deserialize_only(self):
        """Tests that using -f without -d (i.e. serialize) is an error."""
        result = self._run_mongotar(["-f", str(self.source_dir), str(self.archive_file)])
        self.assertNotEqual(result.returncode, 0)
        # Argparse error goes to stderr
        self.assertIn("usage:", result.stderr)
        self.assertIn("The --force flag is only applicable during deserialization", result.stderr)

    def test_cli_serialize_exclude_vcs_flag(self):
        """Tests that --exclude-vcs respects .gitignore."""
        self._create_file(Path(".gitignore"), "*.log\n")
        self._create_file(Path("app.log"), "ignore me")
        self._create_file(Path("keep.txt"), "keep")

        result = self._run_mongotar(["--exclude-vcs", str(self.source_dir), str(self.archive_file)])
        self.assertEqual(result.returncode, 0, f"CLI failed:\n{result.stderr}")

        archive = self.archive_file.read_text(encoding="utf-8")
        self.assertNotIn("app.log", archive)
        self.assertIn("keep.txt", archive)

    def test_cli_serialize_exclude_vcs_applies_only_to_serialize(self):
        """--exclude-vcs is rejected during deserialization."""
        self._create_file("keep.txt", "keep")
        res_ser = self._run_mongotar([str(self.source_dir), str(self.archive_file)])
        self.assertEqual(res_ser.returncode, 0)

        result = self._run_mongotar(
            ["-d", "--exclude-vcs", str(self.archive_file), str(self.dest_dir)]
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "The --exclude-vcs flag is only applicable during serialization", result.stderr
        )

    def test_cli_serialize_exclude_flag(self):
        """Tests that --exclude hides matching paths from the archive."""
        self._create_file("build/out.o", "obj")
        self._create_file("readme.md", "keep")
        self._create_file("run.log", "ignore me")

        result = self._run_mongotar(
            [
                "--exclude",
                "*.log",
                "--exclude",
                "*/build",
                str(self.source_dir),
                str(self.archive_file),
            ]
        )
        self.assertEqual(result.returncode, 0, f"CLI failed:\n{result.stderr}")

        archive = self.archive_file.read_text(encoding="utf-8")
        self.assertNotIn("run.log", archive)
        self.assertNotIn("build", archive)
        self.assertIn("readme.md", archive)

    def test_cli_serialize_exclude_flag_explicit_input(self):
        """A pattern matching an explicitly named CLI input excludes it."""
        self._create_file("app.log", "log")
        self._create_file("keep.txt", "keep")

        result = self._run_mongotar(
            [
                "--exclude",
                "*.log",
                str(self.source_dir / "app.log"),
                str(self.source_dir / "keep.txt"),
                str(self.archive_file),
            ]
        )
        self.assertEqual(result.returncode, 0, f"CLI failed:\n{result.stderr}")

        archive = self.archive_file.read_text(encoding="utf-8")
        self.assertNotIn("app.log", archive)
        self.assertIn("keep.txt", archive)

    def test_cli_serialize_exclude_applies_only_to_serialize(self):
        """--exclude is rejected during deserialization."""
        self._create_file("keep.txt", "keep")
        res_ser = self._run_mongotar([str(self.source_dir), str(self.archive_file)])
        self.assertEqual(res_ser.returncode, 0)

        result = self._run_mongotar(
            ["-d", "--exclude", "*.log", str(self.archive_file), str(self.dest_dir)]
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("The --exclude flag is only applicable during serialization", result.stderr)

    def test_cli_serialize_input_equals_output_error(self):
        """Tests CLI error when input item is the same as output file."""
        dummy_input = self.source_dir / "dummy.txt"
        self._create_file("dummy.txt", "content", "rw")
        output_archive_path = dummy_input  # Make output same as input

        result = self._run_mongotar([str(dummy_input), str(output_archive_path)])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Output file", result.stderr)
        self.assertIn("cannot be the same as an input item", result.stderr)

    def test_cli_deserialize_unsafe_output_dir(self):
        """Tests CLI error for unsafe output directories like '/'."""
        # Create a dummy archive first
        self._create_file("safe.txt", "safe", "rw")
        res_ser = self._run_mongotar([str(self.source_dir), str(self.archive_file)])
        self.assertEqual(res_ser.returncode, 0)

        for unsafe_dir in ("/", "/."):
            result = self._run_mongotar(["-d", str(self.archive_file), unsafe_dir])
            self.assertNotEqual(result.returncode, 0, f"Deserialize to '{unsafe_dir}' should fail.")
            self.assertIn("Invalid or potentially unsafe output directory", result.stderr)

    def test_cli_deserialize_into_dot(self):
        """Deserializing into '.' works and extracts relative to CWD."""
        self._create_file("sub/file.txt", "content", "rw")
        res_ser = self._run_mongotar([str(self.source_dir), str(self.archive_file)])
        self.assertEqual(res_ser.returncode, 0, f"CLI failed:\n{res_ser.stderr}")

        source_dir_archive = _strip_anchor(self.source_dir)
        self.dest_dir.mkdir(parents=True, exist_ok=True)
        result = self._run_mongotar(["-d", str(self.archive_file), "."], cwd=str(self.dest_dir))
        self.assertEqual(result.returncode, 0, f"CLI deserialize failed:\n{result.stderr}")
        self.assertIn("Successfully deserialized", result.stderr)
        self.assertTrue((self.dest_dir / source_dir_archive / "sub" / "file.txt").exists())

    def test_cli_serialize_binary_file_warning(self):
        """Tests CLI serialize warns about non-UTF-8/binary files."""
        text_file_rel = Path("config.txt")
        binary_file_rel = Path("image.dat")
        text_content = "Valid UTF-8 content."
        # Create invalid UTF-8 bytes (\x00 is the null byte, binary heuristic)
        binary_content = b"Some valid text then \x00 invalid byte."

        # Create text file normally
        self._create_file(text_file_rel, text_content, "rw")

        # Create binary file by writing bytes
        binary_file_path = self.source_dir / binary_file_rel
        binary_file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(binary_file_path, "wb") as f:  # Open in binary write mode
            f.write(binary_content)
        # Set standard read/write permissions for the user
        binary_file_path.chmod(stat.S_IRUSR | stat.S_IWUSR)

        abs_text_file = self.source_dir / text_file_rel
        text_archive_path = _strip_anchor(abs_text_file)
        binary_archive_path = _strip_anchor(binary_file_path)

        # Run serialize, include both files
        result_ser = self._run_mongotar(
            [
                "-v",  # Use verbose for stdout checks
                str(abs_text_file),
                str(binary_file_path),  # Input the binary file path
                str(self.archive_file),
            ]
        )

        # Assertions
        self.assertEqual(
            result_ser.returncode,
            0,
            "CLI serialize should succeed even with binary warning:"
            f"\nSTDERR:\n{result_ser.stderr}"
            f"\nSTDOUT:\n{result_ser.stdout}",
        )

        # Check stderr for the specific warning about the binary file
        # Check for the binary file's archive path (relative to "/")
        self.assertIn(f"'{binary_archive_path.as_posix()}'", result_ser.stderr)
        self.assertIn("appears to be binary", result_ser.stderr)

        # Check verbose "Adding:" message on stderr (diagnostics)
        self.assertIn(f"Adding: {text_archive_path.as_posix()}", result_ser.stderr)
        self.assertIn("Successfully serialized", result_ser.stderr)

        # Optional: Check archive content
        with open(self.archive_file, encoding="utf-8") as f:
            archive_data = f.read()
        # Check that the text file is present and correct
        self.assertIn(
            f"--- {text_archive_path.as_posix()} --- rw\n{text_content}",
            archive_data,
        )
        # Check that the binary file header is absent
        self.assertNotIn(
            f"--- {binary_archive_path.as_posix()}",
            archive_data,
        )

    def test_cli_serialize_relative_paths_from_cwd(self):
        """CLI serialize with relative paths stores paths relative to CWD."""
        file_rel = Path("docs") / "readme.txt"
        self._create_file(file_rel, "hello", "rw")
        orig_cwd = Path.cwd()
        try:
            os.chdir(self.test_dir)
            # SOURCE_DIR_NAME is a subdir of test_dir
            result = self._run_mongotar(
                ["-v", SOURCE_DIR_NAME, "cli_archive.mongotar"],
                cwd=str(self.test_dir),
            )
            self.assertEqual(result.returncode, 0)
            self.assertIn(
                f"Adding: {SOURCE_DIR_NAME}/{file_rel.as_posix()}",
                result.stderr,
            )
        finally:
            os.chdir(orig_cwd)
            archive_in_test = self.test_dir / "cli_archive.mongotar"
            if archive_in_test.exists():
                archive_in_test.unlink()

    def test_cli_serialize_is_default_mode(self):
        """Serialize is assumed when no mode flag is passed."""
        file_rel = Path("data") / "file1.txt"
        self._create_file(file_rel, "Data 1", "rw")

        result = self._run_mongotar([str(self.source_dir), str(self.archive_file)])
        self.assertEqual(result.returncode, 0, f"CLI serialize failed:\n{result.stderr}")
        self.assertTrue(self.archive_file.exists())
        with open(self.archive_file, encoding="utf-8") as f:
            archive = f.read()
        self.assertIn("--- ", archive)

    def test_cli_serialize_to_stdout(self):
        """Serialize output of '-' writes the archive to stdout, messages to stderr."""
        file_rel = Path("data") / "file1.txt"
        self._create_file(file_rel, "Data 1", "rw")

        result = self._run_mongotar([str(self.source_dir), "-"])
        self.assertEqual(result.returncode, 0, f"CLI serialize failed:\n{result.stderr}")

        # Archive content on stdout, diagnostics/status on stderr
        self.assertIn("--- ", result.stdout)
        self.assertIn("Successfully serialized", result.stderr)
        self.assertNotIn("Successfully serialized", result.stdout)

    def test_cli_serialize_to_stdout_preserves_content(self):
        """The archive written to stdout round-trips back to the same content."""
        contents = {
            "trailing.txt": "a\nb\n",
            "no_trailing.txt": "a\nb",
            "empty.txt": "",
        }
        for rel, content in contents.items():
            self._create_file(rel, content, "rw")

        result = self._run_mongotar([str(self.source_dir), "-"])
        self.assertEqual(result.returncode, 0, f"CLI serialize failed:\n{result.stderr}")

        stdout_archive = self.test_dir / "stdout_archive.mongotar"
        with open(stdout_archive, "w", encoding="utf-8") as f:
            f.write(result.stdout)

        res_des = self._run_mongotar(["-d", str(stdout_archive), str(self.dest_dir)])
        self.assertEqual(res_des.returncode, 0, f"CLI deserialize failed:\n{res_des.stderr}")

        source_dir_archive = _strip_anchor(self.source_dir)
        for rel, content in contents.items():
            extracted = self.dest_dir / source_dir_archive / rel
            with open(extracted, encoding="utf-8") as f:
                self.assertEqual(f.read(), content)


if __name__ == "__main__":
    unittest.main(verbosity=1)
