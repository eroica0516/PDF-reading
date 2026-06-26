# PDF Annual Impact Cost Extractor

Extracts **Annual Cost Impact** values, **ISIN codes**, and **Fund Names** from PRIIPs Key Information Document (KID) PDFs — in **English, Dutch, Spanish, Italian, and Swedish** — and exports them to a timestamped CSV file.

---

## Requirements

- **Python 3.10 or later** — [Download here](https://www.python.org/downloads/)
- Windows (the `setup.bat` script is Windows-specific; see [Other Environments](#other-environments) for Linux/macOS)

---

## Quick Start (Windows)

1. Place all input PDF files in the `Input/` folder.
2. Double-click **`setup.bat`**.

That's it. The script will:
- Create a Python virtual environment (`venv/`)
- Install all dependencies from `requirements.txt`
- Run the extractor
- Write results to `Output/Output.csv`

---

## Manual Run

If the virtual environment is already set up:

```bat
# Activate the environment
venv\Scripts\activate

# Run the extractor
python extractor.py
```

### Command-line Options

```
python extractor.py [--input DIR] [--output DIR] [--workers N]
```

| Option | Default | Description |
|---|---|---|
| `--input` | `Input/` | Directory containing input PDF files |
| `--output` | `Output/` | Directory for the output CSV |
| `--workers` | `8` | Number of parallel worker processes |

Example — use 4 workers and a custom input directory:

```bat
python extractor.py --input C:\MyFunds\ --workers 4
```

---

## Output Format

Each run produces a **timestamped CSV** in `Output/` named `output_YYYYMMDD_HHMMSS.csv` (e.g. `output_20260626_121358.csv`).  This prevents previous results from being overwritten and gives you a natural audit trail.

The file has five columns:

| Column | Description | Example |
|---|---|---|
| `ISIN` | Fund identifier (12-char code) | `LU2373783344` |
| `Fund Name` | Full product name from the PDF | `Muzinich European Loans 4 ELTIF SICAV, S.A. - EUR Income R` |
| `Holding Year` | Number of years in the holding period | `1`, `5` |
| `Annual Impact Cost` | Cost as a percentage for that holding period | `3.40%` |
| `input file name` | Source PDF filename | `598902836.pdf` |

Each PDF produces **one row per holding-year column** found in the "Costs over Time" table.  Documents with multiple ISINs (e.g. bearer and registered share classes) produce one row per ISIN per holding year.

If a document has extractable ISINs but no standard cost table (e.g. a long-form fund prospectus), it appears in the output with `N/A` in the cost columns rather than being silently dropped.

---

## Scaling to 800+ PDFs

The extractor uses Python's `ProcessPoolExecutor` to process multiple PDFs simultaneously. On a modern machine with the default 8 workers, 800 PDFs should complete in approximately 2–4 minutes.

To tune the worker count:
```bat
python extractor.py --workers 12
```

---

## Multilingual Support

The extractor recognises KID documents in five languages:

| Language | Holding-year phrase | ACI label |
|---|---|---|
| English (EN) | `If you exit after N Year(s)` | `Annual Cost Impact (*)` |
| Dutch (NL) | `Als u uitstapt na N jaar` | `Effect van de kosten per jaar (*)` |
| Spanish (ES) | `después de N año(s)` | `Incidencia anual de los costes (*)` |
| Italian (IT) | `dopo N anno/anni` | `Impatto sul rendimento (RIY) per anno` |
| Swedish (SV) | `löser in efter N år` | `Årliga kostnadseffekter` |

To process a folder of non-English PDFs:

```bat
python extractor.py --input "Input NonEN"
```

**Known limitation — Swedish KIDs:**  Some Swedish fund KIDs (e.g. Partners Group ELTIF) use a component breakdown table rather than a single consolidated ACI row.  These documents will appear in the output with `N/A` in the cost columns; the ISIN and fund name are still captured.

---

## Error Handling and Logging

All warnings and errors are written to **`extractor.log`** in the project root. Check this file after a batch run to identify any PDFs that failed to parse correctly.

Common warnings:
- `ISIN not found` — the ISIN label was not detected
- `No 'Annual Cost Impact' data found` — cost table uses an unexpected layout; partial rows (N/A) are emitted instead
- `Mismatch: N holding year(s) but M ACI value(s)` — informational; extractor right-aligns to the recommended holding period
- `Page N could not be decoded` — page skipped due to unusual character encoding (common in some Italian prospectuses)
- `Failed to process` — the PDF is corrupt or unreadable

---

## Other Environments

On Linux or macOS, substitute `setup.bat` with:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python extractor.py
```

---

## Project Structure

```
PDF reading/
├── Input/              # Place English input PDFs here
├── Input NonEN/        # Place non-English input PDFs here
├── Output/             # Timestamped CSVs written here (created automatically)
├── venv/               # Python virtual environment (created by setup.bat)
├── extractor.py        # Main extraction script (multilingual)
├── requirements.txt    # Python dependencies
├── setup.bat           # One-click Windows setup and run
├── extractor.log       # Error log (created at runtime)
└── README.md           # This file
```
