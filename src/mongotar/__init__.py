import fnmatch
import logging
import re
import stat
import sys
from enum import StrEnum, auto
from pathlib import Path
from typing import TextIO

from pathspec import GitIgnoreSpec

# --- Constants and Types


class Permission(StrEnum):
    RW = auto()
    RWX = auto()


# Regex of entry names to be skipped during directory traversal
SKIP_NAMES: list[str] = [r"\.(mongotar|mtar|mt)$"]

# Version control system directory names, always excluded during traversal.
VCS_DIR_NAMES: frozenset[str] = frozenset(
    {".git", ".svn", ".hg", ".bzr", ".darcs", "CVS", "_darcs", "$RECYCLE.BIN"}
)

# VCS ignore file respected by `exclude_vcs`. Only `.gitignore` is supported.
IGNORE_FILE_NAME: str = ".gitignore"

# A compiled `.gitignore` layered over a base directory.
#   base
#     - the directory containing the `.gitignore` that produced `spec`
#   spec
#     - the compiled gitignore patterns, interpreted relative to `base`
#
# Explicit input immunity is applied at seed time: patterns in an ancestor
# spec that would ignore the explicitly named input directory itself are
# dropped from `spec` (see `_load_ancestor_gitignores`)
IgnoreSpec = tuple[Path, GitIgnoreSpec]

# Regex for detecting mongotar header format.
HEADER_LINE_RE = re.compile(rf"^--- .+ --- ({Permission.RW}|{Permission.RWX})\s*$")


# --- Internal Functions

logger = logging.getLogger(__name__)


def _get_permission_mode(item_path: Path) -> Permission:
    """Gets the simplified permission mode ('rw' or 'rwx') for the owner of a file path."""
    try:
        st = item_path.stat()
    except OSError as e:
        logger.error(f"Error getting permissions for {item_path}: {e}")
        raise e

    # state.filemode() returns strings like '-rwxrwxrwx'. The range 1:4 is the
    # owner's permissions.
    owner_perms = stat.filemode(st.st_mode)[1:4]

    match owner_perms:
        case "rwx":
            return Permission.RWX
        case "rw-":
            return Permission.RW
        case _:
            logger.warning(
                f"File {item_path} has owner permissions not directly mapped to"
                f" '{Permission.RW}' or '{Permission.RWX}' ({owner_perms})."
                f" Defaulting to '{Permission.RW}'."
            )
            return Permission.RW


def _apply_permissions(filepath: Path, permission_mode_str: Permission) -> None:
    """Applies 'rw' or 'rwx' owner permissions to the file"""
    match permission_mode_str:
        case Permission.RWX:
            target_mode = stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
        case Permission.RW:
            target_mode = stat.S_IRUSR | stat.S_IWUSR

    try:
        logger.debug(f"Applying permissions ({permission_mode_str}) to: {filepath}")
        filepath.chmod(target_mode)
    except Exception as e:
        logger.error(f"Error setting permissions for {filepath}: {e}")
        raise e


def _serialize_file(
    item_path: Path,
    output_file_handle: TextIO,
    root_dir: Path,
) -> None:
    """Serializes a file (not dir) to an open file handle."""

    abs_item_path = item_path.resolve()

    # Get path to show on header (relative path from item root to item)
    # Posix style
    relative_path = abs_item_path.relative_to(root_dir).as_posix()

    # Check for likely binary content (heuristic: look for null byte in first 4KB)
    try:
        with open(item_path, "rb") as fb:
            chunk = fb.read(4096)
            if b"\x00" in chunk:
                logger.warning(f"File '{relative_path}' appears to be binary and will be skipped.")
                return
    except OSError as e:
        logger.warning(f"Could not read {item_path}: {e}")
        raise

    logger.debug(f"Adding: {relative_path}")

    permission_mode = _get_permission_mode(item_path)

    # Read content and check for header-colliding lines before writing.
    try:
        with open(item_path, encoding="utf-8", errors="strict") as infile:
            content = infile.read()
    except OSError as e:
        logger.error(f"Error reading file {item_path}: {e}")
        raise

    for line in content.splitlines(keepends=False):
        if HEADER_LINE_RE.match(line):
            logger.warning(
                f"File '{relative_path}' contains a line that matches the"
                f" archive header format and will be skipped to avoid"
                f" corruption: {line!r}"
            )
            return

    # Write header and content to the file handle
    output_file_handle.write(f"--- {relative_path} --- {permission_mode}\n")
    output_file_handle.write(content)

    # Add separation (two newlines)
    output_file_handle.write("\n\n")


def _serialize_item(
    item_path: Path,
    output_file_handle: TextIO,
    root_dir: Path,
    processed_paths: set[Path],
    exclude_vcs: bool = False,
    ignore_specs: list[IgnoreSpec] = [],
    excludes: list[str] = [],
) -> None:
    """Serializes a single item (file/dir) to an open file handle."""
    if item_path.is_symlink():
        logger.debug(f"Skipping link: {item_path}")
        return

    abs_item_path = item_path.resolve()

    # Excludes apply to every item: explicitly named inputs and children
    # discovered during traversal alike (like GNU tar's --exclude).
    if _is_excluded(abs_item_path, root_dir, excludes):
        logger.debug(f"Skipping excluded entry: {item_path}")
        return

    if abs_item_path in processed_paths:
        logger.debug(f"Skipping already seen file: {item_path}")
        return
    processed_paths.add(abs_item_path)

    if item_path.is_file():
        _serialize_file(item_path, output_file_handle, root_dir)
    elif item_path.is_dir():
        # Ensure consistent order
        entries = sorted(item_path.iterdir())

        # Build the ignore-file specs for this directory level.
        child_specs: list[IgnoreSpec] = list(ignore_specs)
        if exclude_vcs:
            ignore_file = abs_item_path / IGNORE_FILE_NAME
            if ignore_file.is_file():
                try:
                    with open(ignore_file, encoding="utf-8", errors="ignore") as fh:
                        child_specs.append((abs_item_path, GitIgnoreSpec.from_lines(fh)))
                    logger.debug(f"Applying ignore file: {ignore_file.relative_to(root_dir)}")
                except OSError as e:
                    logger.debug(f"Could not read ignore file {ignore_file}: {e}")

        for entry_path in entries:
            if any(re.search(p, entry_path.name) for p in SKIP_NAMES):
                logger.debug(f"Skipping entry: {entry_path}")
                continue

            # Version-control directories are always excluded
            if entry_path.is_dir() and entry_path.name in VCS_DIR_NAMES:
                logger.debug(f"Skipping version-control directory: {entry_path}")
                continue

            if exclude_vcs and _is_ignored(entry_path, child_specs):
                logger.debug(f"Skipping ignored entry: {entry_path}")
                continue

            # Recursive call
            _serialize_item(
                entry_path,
                output_file_handle,
                root_dir=root_dir,
                processed_paths=processed_paths,
                exclude_vcs=exclude_vcs,
                ignore_specs=child_specs,
                excludes=excludes,
            )
    else:
        # Ignore sockets, etc.
        logger.debug(f"Skipping non-file/non-directory item: {item_path}")


def _is_ignored(entry_path: Path, ignore_specs: list[IgnoreSpec]) -> bool:
    """Returns True if `entry_path` is ignored by the stacked ignore files.

    Precedence follows git: the deepest ignore file that has any opinion about
    the path wins.
    """
    for base_dir, spec in reversed(ignore_specs):
        try:
            rel = entry_path.relative_to(base_dir).as_posix()
        except ValueError:
            continue

        if any(p.match_file(rel) for p in spec.patterns):
            return spec.match_file(rel)
    return False


def _is_excluded(entry_path: Path, root_dir: Path, exclude_patterns: list[str]) -> bool:
    """Returns True if `entry_path` matches any exclude glob pattern.

    Patterns are glob-style (shell wildcards, like GNU tar's `--exclude`):
    `*` spans path separators, `?` matches any single character, and `[...]`
    introduces a character class. Matching is against the archive-relative
    path (so it includes the explicitly named input directories). A pattern
    matching a directory excludes the whole subtree because the caller skips
    the directory without recursing.
    """
    if not exclude_patterns:
        return False
    rel = entry_path.relative_to(root_dir).as_posix()
    return any(fnmatch.fnmatch(rel, p) for p in exclude_patterns)


def _drop_ignore_patterns_for_input(
    spec: GitIgnoreSpec, input_dir: Path, base_dir: Path
) -> GitIgnoreSpec:
    """Drops patterns in `spec` that would ignore `input_dir` itself.

    Only ignore patterns (`include` is True, not `!` negations) that match the
    explicitly named directory are removed, leaving negation rules and all
    unrelated rules (for example `*.log`) intact.
    """
    input_rel = input_dir.relative_to(base_dir).as_posix()
    input_rel_dir = input_rel + "/"
    kept = [
        p
        for p in spec.patterns
        if not (
            getattr(p, "include", True) and (p.match_file(input_rel) or p.match_file(input_rel_dir))
        )
    ]
    return GitIgnoreSpec(kept)


def _load_ancestor_gitignores(item: Path) -> list[IgnoreSpec]:
    """Loads `.gitignore` ancestor files for an explicitly named directory.

    Walks up from `item`'s parent toward the filesystem root and loads every
    `.gitignore` encountered, stopping at the nearest directory that contains a
    `.git` marker.

    For each spec, any ignore pattern that would itself ignore the explicitly
    named `item` is dropped (see `_drop_ignore_patterns_for_input`), so an
    explicitly named directory cannot be excluded by an ancestor rule while the
    rest of that spec's rules still apply.

    If no `.git` marker is found above `item`, no ancestor ignore files apply.
    """
    specs: list[IgnoreSpec] = []
    resolved_item = item.resolve()

    current = resolved_item.parent
    found_repo_root = False

    # Walk up until the filesystem root
    while current != Path(current.anchor):
        ignore_file = current / IGNORE_FILE_NAME
        if ignore_file.is_file():
            try:
                with open(ignore_file, encoding="utf-8", errors="ignore") as fh:
                    spec = GitIgnoreSpec.from_lines(fh)
                    spec = _drop_ignore_patterns_for_input(spec, resolved_item, current)
                    specs.append((current, spec))
                logger.debug(f"Applying ancestor ignore file: {ignore_file}")
            except OSError as e:
                logger.debug(f"Could not read ignore file {ignore_file}: {e}")
        # Stop at the repository root
        if (current / ".git").exists():
            found_repo_root = True
            break
        current = current.parent

    # If no repository root was found above the input, no ancestor ignore files
    # apply
    if not found_repo_root:
        return []

    # we search deepest-first via `reversed(...)`, so specs must be reversed
    specs.reverse()
    return specs


# --- Public functions


def serialize(
    input_items: list[str],
    output_file: str,
    exclude_vcs: bool = False,
    excludes: list[str] = [],
) -> bool:
    """Serializes multiple folders and/or files to a text file."""

    to_stdout = output_file == "-"
    output_path: Path | None

    # obtain absolute output path (if not stdout)
    if not to_stdout:
        output_path = Path(output_file).resolve()

        # also, sanity check that output path is not one of the inputs
        for item in input_items:
            if Path(item).resolve() == output_path:
                logger.error(
                    f"Output file '{output_file}' cannot be the same as an input item '{item}'."
                )
                return False
    else:
        output_path = None

    target: TextIO
    if to_stdout:
        target = sys.stdout
    else:
        assert output_path is not None
        target = open(output_path, "w", encoding="utf-8")

    # Keep list of (absolute) processed paths.
    # Ensure the output file is never processed.
    processed_paths: set[Path] = set()
    if output_path is not None:
        processed_paths.add(output_path)

    wrote_any = False
    with target as f:
        for item in input_items:
            abs_item = Path(item).resolve()
            if not abs_item.exists():
                logger.warning(f"Input item '{item}' not found. Skipping.")
                continue

            before_item_count = len(processed_paths)

            # Determine root like `tar` does:
            # - Relative paths: root is CWD
            #   - archive paths will be relative to CWD
            # - Absolute paths: root is "/"
            #   - this will result on the leading "/" being stripped on the
            #     archive path, matching what `tar` does for security on
            #     extraction.
            if item.startswith("/"):
                root_for_item = Path("/")
                logger.debug(f"Note: Stripping leading '/' from absolute path: '{item}'")
            else:
                root_for_item = Path.cwd()

            # Validate the archive path doesn't traverse outside the root
            if not abs_item.is_relative_to(root_for_item):
                logger.warning(
                    f"Skipping '{item}' as it resolves outside the current working directory."
                )
                continue

            # Load ancestor `.gitignore` files (item's parent up to the nearest
            # `.git` root) for an explicitly named directory.
            seed_specs: list[IgnoreSpec] = []
            if exclude_vcs and abs_item.is_dir():
                seed_specs = _load_ancestor_gitignores(abs_item)

            _serialize_item(
                abs_item,
                f,
                root_dir=root_for_item,
                processed_paths=processed_paths,
                exclude_vcs=exclude_vcs,
                ignore_specs=seed_specs,
                excludes=excludes,
            )
            after_item_count = len(processed_paths)

            # Check if we actually processed anything new
            if after_item_count > before_item_count:
                wrote_any = True

    # Do final checks *after* closing the file handle
    if not wrote_any:
        logger.warning("No valid input items found to serialize.")
        return False

    if not to_stdout:
        assert output_path is not None
        if not (output_path.exists() and output_path.stat().st_size > 0):
            logger.warning(
                f"Output file '{output_file}' is empty or contains no file content"
                " despite processing inputs. Check input files and permissions."
            )
    logger.info(f"Successfully serialized items to '{output_file}'")
    return True


def deserialize(input_file: str, output_folder: str, force: bool = False) -> bool:
    """Deserializes a folder structure from a mongotar text file."""

    # Prevent extraction directly into the filesystem root.
    abs_output_folder = Path(output_folder).resolve()
    if abs_output_folder == Path("/"):
        logger.error(
            f"Invalid or potentially unsafe output directory specified: '{output_folder}'."
        )
        return False

    logger.info(
        f"Deserializing '{input_file}' to '{output_folder}'"
        + (" (forcing overwrite)" if force else "")
        + "..."
    )

    header_re = re.compile(rf"^--- (.+) --- ({Permission.RW}|{Permission.RWX})\s*$")

    files_extracted_count = 0
    files_skipped_count = 0

    Path(output_folder).mkdir(parents=True, exist_ok=True)

    with open(input_file, encoding="utf-8") as f:
        current_line = f.readline()
        line_num = 1

        # --- Skip optional leading comment section

        comment_lines: list[str] = []
        while current_line and not header_re.match(current_line):
            comment_lines.append(current_line)
            current_line = f.readline()
            line_num += 1

        # --- Validate that the archive contains at least one header

        if not current_line:
            logger.error(
                f"Invalid mongotar format.\n"
                f"Input file '{input_file}' does not contain any valid file"
                f" header (for example, '--- path/to/file --- {Permission.RW}')."
            )
            return False

        # --- Iterate on headers

        def skip_to_next_header():
            nonlocal current_line, line_num
            while current_line and not header_re.match(current_line):
                current_line = f.readline()
                line_num += 1

        while current_line:
            # extract header info
            match = header_re.match(current_line)
            if not match:
                raise ValueError(f"Expected header, got: {current_line!r}")
            output_path_rel, output_perms = match.group(1).strip(), match.group(2)
            current_filepath_abs = (abs_output_folder / output_path_rel).resolve()

            # go to next line
            line_num += 1
            current_line = f.readline()

            # Security check: prevent path traversal
            if not current_filepath_abs.is_relative_to(abs_output_folder):
                logger.warning(
                    f"Skipping potentially unsafe path traversal on line {line_num}:"
                    f" '{output_path_rel}' resolves outside output directory"
                    f" '{output_folder}'"
                )
                skip_to_next_header()
                continue

            logger.debug(f"Extracting: {current_filepath_abs}")

            # --- Check for existing file before creating directories/opening

            if current_filepath_abs.exists():
                if current_filepath_abs.is_dir():
                    logger.warning(
                        f"Cannot create file '{current_filepath_abs}', a directory"
                        " already exists with that name. Skipping."
                    )
                    files_skipped_count += 1
                    skip_to_next_header()
                    continue
                elif not force:
                    logger.warning(
                        f"File '{current_filepath_abs}' already exists. Skipping."
                        " Use -f to overwrite."
                    )
                    files_skipped_count += 1
                    skip_to_next_header()
                    continue
                else:
                    logger.debug(f"Overwriting existing file: {current_filepath_abs}")

            # --- Proceed to create directories and file

            try:
                current_filepath_abs.parent.mkdir(parents=True, exist_ok=True)
                files_extracted_count += 1
            except OSError as e:
                logger.error(
                    f"Error creating directory or opening file for '{current_filepath_abs}': {e}"
                )
                files_skipped_count += 1
                skip_to_next_header()
                continue

            # Write lines
            with open(current_filepath_abs, "w", encoding="utf-8") as o:
                # The format appends a fixed "\n\n" separator after every entry, so
                # the last two newlines we read belong to that separator (not the
                # content) and must be dropped. So we 'held' some content
                held = ""
                while current_line and not header_re.match(current_line):
                    if current_line == "\n":
                        held += "\n"
                    else:
                        # Every non-blank line read here ends with "\n", which
                        # is ambiduous. It may be content or the start of the
                        # separator. So we move it to 'held'.
                        o.write(held + current_line[:-1])
                        held = "\n"
                    current_line = f.readline()
                    line_num += 1
                if held:
                    o.write(held[:-2])

            _apply_permissions(current_filepath_abs, Permission(output_perms))

    if files_extracted_count == 0 and files_skipped_count == 0:
        logger.warning(
            f"Input archive '{input_file}' appears to be empty or contained"
            " no processable file entries."
        )
    elif files_extracted_count == 0 and files_skipped_count > 0:
        logger.warning(
            f"No files were extracted. {files_skipped_count} file(s) existed"
            " and were skipped (use -f to overwrite)."
        )

    logger.info(f"Successfully deserialized '{input_file}' to '{output_folder}'")
    return True
