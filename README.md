# `mongotar` (huMONGOus TAR)

**Files to prompt, and back.**

<p align="center">
<img width="400" alt="mongotar logo" src="https://github.com/user-attachments/assets/2da3c48a-1151-4281-99ec-930e093c7678" />
</p>

![Test (Linux, macOS, Windows)](https://github.com/sebastiancarlos/mongotar/actions/workflows/ci.yml/badge.svg)
[![PyPI](https://img.shields.io/pypi/v/mongotar.svg)](https://pypi.org/project/mongotar/)
[![PyPI version](https://img.shields.io/pypi/v/mongotar)](https://pypi.org/project/mongotar/)
[![License: MIT](https://img.shields.io/pypi/l/mongotar)](https://github.com/sebastiancarlos/mongotar/blob/main/LICENSE)

A simple tool for serializing _and deserializing_ files (with permissions and
`.gitignore` exclusion) into a human-readable text file.

Inspired by GNU [`tar`](https://manned.org/man/debian-trixie/tar.1), Go's
[`txtar`](https://pkg.go.dev/golang.org/x/tools/txtar), and
[`files-to-prompt`](https://github.com/simonw/files-to-prompt). `mongotar`
bundles files into a single, human-readable text file. Unlike most "codebase
to prompt" tools, it can reliably **turn that text file back into files**.

To achieve that, and unlike other "plain-text archive" formats, it stores
basic user file permissions (`rw`/`rwx`) together with the file paths.

## `mongotar` 101

```bash
$ mongotar src/ config.txt project.mongotar
  Successfully serialized items to 'project.mongotar'

$ cat project.mongotar
  --- config.txt --- rw
  debug=true

  --- src/main.py --- rw
  print("hello")

$ mongotar -d -f project.mongotar .
  Successfully deserialized 'project.mongotar' to '.'
```

## Why `mongotar`?

`mongotar` is at the intersection of two different tool categories:

1. **Dump a repo into one blob for sending to an LLM.**
   This is the space of players such as:
   [Repomix](https://github.com/yamadashy/repomix),
   [gitingest](https://github.com/cyclotruc/gitingest),
   [files-to-prompt](https://github.com/simonw/files-to-prompt), and
   [code2prompt](https://github.com/mufeedvh/code2prompt). Unlike the first
   two, `mongotar` is _less "full repo serialization"_, and has good UX for
   _hand picking which files to serialize_. None of them _round-trips_ like
   `mongotar`, which can be useful for some LLM workflows (see later).

2. **Plain-text archive formats.** This is the space of tools like `txtar` and
   [`shar`](<https://en.wikipedia.org/wiki/Shar_(file_format)>). `mongotar`'s
   differentiator here is its simultaneous focus on round-tripping,
   human-readability, and `.gitignore` support.

`mongotar` follows UNIX-philosophy, and deliberately has no LLM-specific
features baked in. Those features are to be built on top of this small core
(700 LOC).

What motivated `mongotar` was agentic use cases. Say, a _skill_ composes a
problem description and appends relevant files to it _without_ loading them
into the agent's context, and sends them to another LLM. If the other LLM
**hands back a complete set of modified files, it can be deserialized back
into the file system** (rather than a diff that may not apply cleanly).

Indeed, _diff formats are known to be fragile against LLM output_, so
whole-file output tends to be more reliable.

In short:

| Tool                 | Human readable |   Round-trip | Selective files | AI-friendly |       Permissions | .gitignore |
| -------------------- | -------------: | -----------: | --------------: | ----------: | ----------------: | ---------: |
| `tar`                |             ❌ |           ✅ |              ✅ |          ❌ |                ✅ |         ✅ |
| `txtar`              |             ✅ |           ✅ |         limited |          ✅ |                ❌ |         ❌ |
| repo-to-prompt tools |             ✅ | generally ❌ |          varies |          ✅ |                ❌ |         ✅ |
| **`mongotar`**       |         **✅** |       **✅** |          **✅** |      **✅** | **simplified ✅** |     **✅** |

## Some Features

- **Library:** Besides CLI, `mongotar` can be imported and used directly in
  Python projects.
- **Safe Deserialization:** By default, does not overwrite existing files. Use
  `-f`/`--force` to enable overwriting.
- **VCS Exclusion:** Version-control directories (`.git`, `.svn`, `.hg`,
  `CVS`, ...) are always excluded. Optional `-e`/`--exclude-vcs` additionally
  respects `.gitignore` files, with git precedence.
- **Pattern Exclusion:** `--exclude=PATTERN` skips paths matching a glob-style
  pattern, like GNU tar's `--exclude`.

The default file extension is `.mongotar`, but any extension can be used. You
can even pipe to `stdout`.

## Format

```txt
    Free text before the first file header is an optional comment section.

    --- path/to/file.txt --- rw
    This is the content of the first file.
    It can span multiple lines.

    --- path/to/another/script.sh --- rwx
    #!/bin/bash
    echo "Hello from the script!"

    --- empty_file.txt --- rw

    --- path/to/file2.txt --- rw
    Content of the second file.
```

- **Header format:** `--- <filepath> --- <permission>`
- **Separator:** Two newlines (`\n\n`) separates the content of one file
  from the header of the next.

## Example

Suppose you have a directory structure like this:

```txt
my_project/
├── main.py
├── config.txt
└── scripts/
    └── run.sh       (executable)
```

Running `mongotar my_project my_project.mongotar` produces:

    --- my_project/main.py --- rw
    print("hello")

    --- my_project/config.txt --- rw
    debug=true

    --- my_project/scripts/run.sh --- rwx
    #!/bin/bash
    echo "running"

Running `mongotar -d -f my_project.mongotar .` recreates the same tree, with
`run.sh` restored as executable.

## Installation

Requires Python 3.14+.

```bash
pip install mongotar
```

After installation, the `mongotar` command will be available in your `PATH`.

Or from source:

```bash
git clone https://github.com/sebastiancarlos/mongotar
cd mongotar
uv tool install .
```

## Usage (`mongotar` CLI)

```
mongotar path/to/file_or_directory [path/to/another ...] output.mongotar
```

**`-d`/`--deserialize`**: Deserialize an archive back into a directory.

```bash
mongotar -d my_project.mongotar output_dir/
```

**`-e`/`--exclude-vcs`**: makes `.gitignore` rules apply.

```bash
mongotar -e src/ my_project.mongotar
```

**`--exclude PATTERN`**:

```bash
mongotar --exclude '*.log' --exclude '*/build' src/ my_project.mongotar
```

**Output to stdout**: use `-` as the output path.

```bash
mongotar src/ config.txt - > my_project.mongotar
```

## Limitations

- **Permissions Model:** Only user permissions are stored and restored (`rw`
  or `rwx`). Group/other permissions are ignored during serialization and not
  set during deserialization.
- **Binary Files:** Not designed for binary files. Files with non-UTF-8
  characters will be skipped.
- **Cross-platform:** Windows supported, but everything serializes as `rw`
  (Windows has no concept of executable permissions).
- **No symlink support:** Symlinks are not supported and will be ignored
  during serialization (with a warning).
- **Header collision:** The archive format has no escaping (matching `txtar`
  behavior). Serialization will skip any file whose content contains a line
  matching the header format (`--- path/to/file --- rw`), logging a warning
  (rare case anyway).

## Why not just use Repomix/gitingest/etc?

Those tools tend to encompass the entire pipeline of generating a prompt.
`mongotar`'s output is meant to be a component you embed inside a message, and
which you can turn back into real files.

If you just want to paste your entire repo into ChatGPT once, use Repomix or
gitingest. They're better at that.

`mongotar`'s closest relative is
[`files-to-prompt`](https://github.com/simonw/files-to-prompt), which shares
the simple, "Unix CLI" philosophy. The notable difference is deserialization.

## License

MIT
