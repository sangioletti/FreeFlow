# FreeFlow — FCS File Viewer & Gating Tool

A lightweight, open-source, interactive flow cytometry data viewer with gating capabilities.
Only requires **numpy** and **matplotlib** (no scipy, no tkinter needed for the GUI).

## Quick Start

```bash
cd flowcyt
pip install -e .

# Open a file directly
flowcyt -i path/to/sample.fcs

# Launch the GUI (pick a file from the dialog or Open button)
flowcyt

# Print file metadata only (no GUI)
flowcyt --info path/to/sample.fcs
```

Or run without installing:

```bash
cd flowcyt
python -m flowcyt.cli -i path/to/sample.fcs
```

## Features

- **Pure-Python FCS reader** — parses FCS 2.0/3.0/3.1 binary files with no external dependencies beyond numpy
- **Density scatter plots** — coloured by 2D histogram density (the classic flow cytometry look)
- **Polygon gating** — left-click to place vertices, right-click or press Enter to close
- **Rectangle gating** — click and drag a box
- **Live statistics** — event count and percentage for each gate, updated in real time
- **Summary window** — population bar chart + per-channel histograms with gate overlays
- **CSV export** — save gated events with all channel values
- **Save plots** — export summary as PNG/PDF/SVG

## Dependencies

- Python >= 3.9
- numpy >= 1.22
- matplotlib >= 3.5

## Project Structure

```
FreeFlow/
├── flowcyt/
│   ├── __init__.py      # Package init
│   ├── reader.py        # Pure-Python FCS binary file parser
│   ├── gating.py        # Gate definitions (polygon, rectangle) + statistics
│   ├── plotting.py      # Density scatter, gate overlays, summary charts
│   ├── app.py           # Interactive matplotlib GUI
│   └── cli.py           # CLI entry point (argparse)
├── setup.py
├── requirements.txt
└── README.md
```
