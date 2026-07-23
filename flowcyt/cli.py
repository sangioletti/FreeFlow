"""
cli.py - Command-line interface for FlowCyt.

Usage:
    flowcyt -i sample.fcs          # open a file directly
    flowcyt                        # launch GUI, open file dialog
    flowcyt --info sample.fcs      # print file metadata to stdout

Compensation comes from a settings .xml in the same folder as the .fcs
files; raw files are auto-compensated into *_compensated.fcs. If a folder
has no .xml and no *_compensated.fcs, the GUI warns and exits. See the
"Compensation" section of the README.
"""

import argparse
import logging
import sys


def _setup_logging():
    """Configure logging to file (flowcyt.log) and console.

    The file handler uses append mode and flushes after every line
    so that log output survives even if the process crashes (e.g. segfault).
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

    # File handler — append mode.
    # StreamHandler.emit() already calls flush() after every record,
    # pushing data to the OS so it survives even a segfault.
    fh = logging.FileHandler("flowcyt.log", mode="a")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # Console handler — same format, INFO level
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)
    root.addHandler(sh)


def main():
    parser = argparse.ArgumentParser(
        prog="flowcyt",
        description="FlowCyt — FCS file viewer and gating tool",
    )
    parser.add_argument(
        "-i", "--input",
        metavar="FILE",
        help="Path to an .fcs file to open immediately",
    )
    parser.add_argument(
        "--info",
        metavar="FILE",
        help="Print FCS file metadata and exit (no GUI)",
    )
    args = parser.parse_args()

    _setup_logging()

    # Info-only mode (no GUI)
    if args.info:
        from .reader import FCSData
        try:
            fcs = FCSData(args.info)
            print(fcs.summary())
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        return

    # GUI mode
    from .app import FlowCytApp
    app = FlowCytApp(filepath=args.input)
    app.run()


if __name__ == "__main__":
    main()
