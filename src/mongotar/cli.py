import argparse
import logging
import sys
import traceback

from . import deserialize, serialize


def get_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Serialize/deserialize directory structures with basic permissions"
            " (rw/rwx for user) to/from a text file (mongotar format)."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
        usage="%(prog)s [-h] [-d] [-v] [-f] input [input ...] output",
    )

    # --- Mode Selection ---
    # Serialize is the default; -d opts into deserialize.

    parser.add_argument(
        "-d",
        "--deserialize",
        action="store_true",
        help="Deserialize a mongotar archive to a directory (default is serialize).",
    )

    # --- Options ---

    parser.add_argument("-v", "--verbose", action="store_true", help="Show verbose (DEBUG) detail.")
    parser.add_argument(
        "-e",
        "--exclude-vcs",
        action="store_true",
        help=("Serialization only: respect .gitignore files."),
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="PATTERN",
        help=(
            "Serialization only: exclude paths matching PATTERN, a glob-style"
            " wildcard pattern (may be given multiple times)."
        ),
    )
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Force overwrite existing files during deserialization. Use with caution.",
    )

    # --- Positional Arguments ---

    # Help text clarifies the role based on the mode (-s or -d)
    parser.add_argument(
        "input",
        nargs="+",
        help=(
            "Serialization: one or more input files/dirs."
            " Deserialization: the input .mongotar file."
        ),
    )
    parser.add_argument(
        "output",
        help=(
            "Serialization: the output .mongotar file, or '-' for stdout."
            " Deserialization: the output directory."
        ),
    )

    # --- Initial Argument Validation ---

    # Show help if run with no arguments
    if len(sys.argv) == 1:
        parser.print_help(sys.stderr)
        sys.exit(1)

    args = parser.parse_args()

    # --- Mode-Specific Argument Validation ---

    if args.deserialize and len(args.input) != 1:
        parser.error("Deserialization (-d) requires exactly one input .mongotar file.")
    if args.force and not args.deserialize:
        parser.error("The --force flag is only applicable during deserialization (-d).")
    if args.exclude_vcs and args.deserialize:
        parser.error("The --exclude-vcs flag is only applicable during serialization.")
    if args.exclude and args.deserialize:
        parser.error("The --exclude flag is only applicable during serialization.")
    if args.deserialize and args.output == "-":
        parser.error("Deserialization output cannot be stdout ('-').")

    return args


def cli_serialize(args: argparse.Namespace) -> None:
    if not serialize(args.input, args.output, exclude_vcs=args.exclude_vcs, excludes=args.exclude):
        sys.exit(1)


def cli_deserialize(args: argparse.Namespace) -> None:
    if not deserialize(args.input[0], args.output, force=args.force):
        sys.exit(1)


def main() -> None:
    args = get_args()

    # All logging goes to stderr. Default shows status (INFO) and diagnostics;
    # -v additionally shows verbose detail (DEBUG).
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    try:
        if args.deserialize:
            cli_deserialize(args)
        else:
            cli_serialize(args)
    except Exception as e:
        if args.verbose:
            traceback.print_exc()
        else:
            print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
