"""
extractor.py
============
Extracts ISIN, Fund Name, and Annual Cost Impact values from PRIIPs
Key Information Document (KID) PDFs.

Supports documents in English, Dutch (NL), Spanish (ES), Italian (IT),
and Swedish (SV).  Each PDF produces one row per (ISIN × holding-year)
combination found in the "Costs over Time" section.

When a document contains multiple ISINs (e.g. bearer and registered share
classes), one row is emitted per ISIN, duplicating the cost data.

When a document contains no standard cost table (e.g. long-form fund
prospectuses), partial rows are emitted with 'N/A' cost values so the
file is still represented in the output.

All results are combined into a single timestamped CSV:
    Output/output_YYYYMMDD_HHMMSS.csv

with five columns:
    ISIN | Fund Name | Holding Year | Annual Impact Cost | input file name

Usage
-----
    python extractor.py                         # processes all PDFs in Input/
    python extractor.py --workers 4             # override parallel worker count
    python extractor.py --input "Input NonEN"   # process non-English folder
    python extractor.py --output MyOutput       # override output directory

Supports 800+ PDFs via parallel processing (ProcessPoolExecutor).
All warnings and errors are written to extractor.log.
"""

import re
import logging
import argparse
from datetime import datetime
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

import pdfplumber
import pandas as pd
from tqdm import tqdm


# ── Default Paths ─────────────────────────────────────────────────────────────
DEFAULT_INPUT_DIR = Path("Input")
DEFAULT_OUTPUT_DIR = Path("Output")
LOG_FILE = Path("extractor.log")

# Maximum parallel worker processes (tune to CPU core count)
DEFAULT_MAX_WORKERS = 8


# ── ISIN Patterns ─────────────────────────────────────────────────────────────

# Pattern 1a — Simple label: "ISIN LU2373783344" or "ISIN: LU2677621984"
# Covers English, Swedish, Spanish (ISIN: ...) formats.
RE_ISIN_LABELLED = re.compile(r'\bISIN\s*[:\s]\s*([A-Z]{2}[A-Z0-9]{10})\b')

# Pattern 1b — Labelled with intervening word(s) + colon: "ISIN PORTATORE: IT0005431587"
# Covers Italian KIDs where bearer/registered class labels appear between
# 'ISIN' and the colon, e.g. "ISIN PORTATORE: IT..." / "ISIN NOMINATIVO: IT..."
RE_ISIN_LABELLED_EXT = re.compile(r'\bISIN\b[^:\n]*?:\s*([A-Z]{2}[A-Z0-9]{10})\b')

# Pattern 1c — Labelled with intervening word, NO colon: "ISIN Portatore IT0005414823"
# Some Italian KIDs (e.g. Eurizon) omit the colon entirely after the class label.
RE_ISIN_LABELLED_NOCOLON = re.compile(
    r'\bISIN\s+(?:Portatore|Nominativo)\s+([A-Z]{2}[A-Z0-9]{10})\b',
    re.IGNORECASE,
)

# Pattern 2 — Parenthesis: "(LU2523384894)"
# Fallback used for Dutch (NL) KIDs where the ISIN is embedded in the
# share-class line, e.g. "Klasse S Accumulation EUR (LU2523384894)".
# Only applied when no labelled ISINs are found.
RE_ISIN_PARENS = re.compile(r'\(([A-Z]{2}[A-Z0-9]{10})\)')


# ── Fund Name Patterns ────────────────────────────────────────────────────────
# Ordered list of (compiled_pattern, language_code) pairs.
# extract_fund_name() tries each in sequence; the first match is returned.

FUND_NAME_PATTERNS: list[tuple[re.Pattern, str]] = [
    # EN — "Product: Muzinich European Loans 4 ELTIF SICAV"
    (re.compile(r'^\s*Product:\s*(.+?)\s*$', re.MULTILINE), 'EN'),

    # SV (Swedish) — two-column PDF layout places the product name on the line
    # BEFORE the line containing 'Produktnamn':
    #   Line N:   "PartnersGroupDirectEquityIIELTIFSICAVC-RDR(USD)"
    #   Line N+1: "produktensegenskaper,...  Produktnamn (\"produkten\")"
    # We match the line preceding 'Produktnamn'.
    (re.compile(r'^(.+)\n[^\n]*Produktnamn\b', re.MULTILINE | re.IGNORECASE), 'SV'),

    # NL (Dutch) — product name on the line BEFORE "een subfonds van"
    # e.g. "Private Equity ELTIF 2023\neen subfonds van Schroders Capital"
    (re.compile(r'^(.+?)\s*\n\s*een subfonds van\b', re.MULTILINE | re.IGNORECASE), 'NL'),

    # ES (Spanish) — "Producto\n<name>" (Spanish KIDs use 'Producto' not
    # 'Nombre del producto' as the label before the fund name)
    (re.compile(r'^\s*Producto\s*\n\s*(.+?)\s*$', re.MULTILINE | re.IGNORECASE), 'ES'),

    # IT (Italian) — two patterns tried in sequence:
    # a) "<name>\nQuote di Classe D" — Anthilia-style KIDs
    (re.compile(r'^(.+?)\s*\n\s*Quote di Classe\b', re.MULTILINE | re.IGNORECASE), 'IT_a'),
    # b) "<FundName>, Classe D" on a single line — Eurizon-style KIDs
    (re.compile(r'^(.+?),?\s+Classe\s+\w+\s*$', re.MULTILINE | re.IGNORECASE), 'IT_b'),
]


# ── Holding Year Column Patterns ──────────────────────────────────────────────
# Ordered list of (compiled_pattern, language_code) pairs.
# The first pattern that yields at least one match is used; others are skipped
# to avoid cross-language noise.
# Decimal separators may be either '.' or ',' in European documents.

HOLD_YEAR_PATTERNS: list[tuple[re.Pattern, str]] = [
    # EN — "If you exit after 1 Year" / "If you exit after 5.0 Years"
    (re.compile(r'If you exit after (\d+(?:[.,]\d+)?)\s+Years?', re.IGNORECASE), 'EN'),

    # NL (Dutch) — "Als u uitstapt na 1 jaar" / "na 8 jaar"
    (re.compile(r'Als u uitstapt na (\d+(?:[.,]\d+)?)\s+jaar', re.IGNORECASE), 'NL'),

    # ES (Spanish) — "después de 1 año" / "después de 6 años"
    # The 'En caso de salida' prefix appears separately in the layout, so we
    # match the shorter 'después de N año' substring which is more reliable.
    (re.compile(r'despu[eé]s de (\d+(?:[.,]\d+)?)\s+a[ñn]', re.IGNORECASE), 'ES'),

    # IT (Italian) — Italian KIDs may have 2 or 3 holding periods.
    # Two sub-strategies, tried in sequence:
    #
    # Strategy A — ACI line has values for ALL periods (e.g. Anthilia):
    #   "dopo N anno/anni" column headers appear in the cost table.
    #   Collect them in document order to match the ACI row left-to-right.
    #
    # Strategy B — ACI line has a value for the RECOMMENDED period only
    #   (e.g. Eurizon, where earlier periods show dashes):
    #   "SCENARIO ... N anni (periodo di detenzione)" gives the period whose
    #   ACI value is the only one on the line.
    #
    # Both cases are handled by the same alternation; groups are flattened
    # and de-duplicated in extract_cost_records (Step 1).
    (re.compile(
        r'\bdopo\s+(\d+(?:[.,]\d+)?)\s+anni?'
        r'|SCENARIO[^\n]*?(\d+)\s+anni?\s*\(periodo'
        r'|Scenari\s+(\d+)\s+anno?\s+(\d+)\s+anni?',
        re.IGNORECASE,
    ), 'IT_COMBO'),  # see extract_cost_records for special handling

    # SV (Swedish) — two-column layout causes words to be concatenated;
    # pattern matches both spaced and space-free variants:
    # Spaced:      "Om du löser in efter 1 år"
    # Concatenated: "Omduläserinefter1år"
    (re.compile(
        r'(?:l[\u00f6o]ser\s*in\s*efter|l[\u00f6o]serinefter)\s*(\d+(?:[.,]\d+)?)',
        re.IGNORECASE,
    ), 'SV'),
]


# ── Annual Cost Impact / RIY Line Patterns ────────────────────────────────────
# Ordered list of (compiled_pattern, language_code) pairs.
# The first pattern that matches is used; all percentage values on that line
# are then extracted with RE_PERCENT (see below).
#
# Observed ACI line formats per language:
#   EN  — "Annual Cost Impact (*) 3.40% 1.80%"
#   NL  — "Effect van de kosten per jaar (*) 2.9% 2.9% per jaar"
#   ES  — "Incidencia anual de los costes (*) 3,6% 2,5 % cada año"
#   IT  — "Impatto sul rendimento (RIY) per anno 4,24% 2,82% 2,54%"
#   SV  — "Kostnadseffekten per år ..." (with percentage values on same line)

ACI_LINE_PATTERNS: list[tuple[re.Pattern, str]] = [
    # EN
    (re.compile(r'Annual Cost Impact\b[^\n]*', re.IGNORECASE), 'EN'),

    # NL — "Effect van de kosten per jaar (*) 2.9% 2.9% per jaar"
    (re.compile(r'Effect van de kosten per jaar\b[^\n]*', re.IGNORECASE), 'NL'),

    # ES — "Incidencia anual de los costes (*) 3,6% 2,5 % cada año"
    (re.compile(r'Incidencia anual de los costes\b[^\n]*', re.IGNORECASE), 'ES'),

    # IT — "Impatto sul rendimento (RIY) per anno 4,24% 2,82% 2,54%"
    (re.compile(r'Impatto sul rendimento\b[^\n]*', re.IGNORECASE), 'IT'),

    # SV — "Årliga kostnadseffekter" section header followed by cost data.
    # The Swedish KID uses a component breakdown table rather than a single
    # unified ACI row.  We look for the annual return percentage that appears
    # in the line summarising costs vs returns (e.g. "13.8% efter kostnader").
    # If that too is unavailable, the doc will be captured via partial extraction.
    (re.compile(r'efter\s*kostnader[^\n]*', re.IGNORECASE), 'SV'),

    # Generic RIY fallback — catches any line that contains "RIY" and a '%'
    # Useful for docs that use the RIY abbreviation without a full language label.
    (re.compile(r'\bRIY\b[^\n]*%[^\n]*', re.IGNORECASE), 'ANY'),
]

# Extracts ALL percentage values from a line.
# Handles both period (3.40%) and comma (3,40%) decimal separators.
RE_PERCENT = re.compile(r'\d+[.,]\d+\s*%|\d+\s*%')


# ── Logging Setup ─────────────────────────────────────────────────────────────

def setup_logging() -> None:
    """Configure root logger to write INFO+ to console and WARNING+ to file."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Console: show INFO and above
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    root.addHandler(ch)

    # File: capture WARNING and above for post-run review
    fh = logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8")
    fh.setLevel(logging.WARNING)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    root.addHandler(fh)


# ── Helper: Percentage Normalisation ─────────────────────────────────────────

def _normalise_percent(value: str) -> str:
    """Normalise European comma-decimal percentages to period format.

    Removes internal spaces and converts comma decimal separator to period.

    Examples:
        '4,24%'   → '4.24%'
        '2,5 %'   → '2.5%'
        '3.40%'   → '3.40%'   (unchanged)
    """
    return value.replace(" ", "").replace(",", ".")


# ── Core Extraction Functions ─────────────────────────────────────────────────

def extract_full_text(pdf: pdfplumber.PDF) -> str:
    """
    Concatenate raw text from every page of an open pdfplumber PDF object.

    Pages are joined with a newline so that cross-page regex searches work
    correctly.  Pages that raise encoding or decoding errors are skipped
    with a WARNING log entry — this handles documents that mix charsets or
    contain non-standard glyphs (e.g. the 40-page Italian prospectus).

    Returns an empty string if the PDF contains no extractable text.
    """
    pages = []
    for page_num, page in enumerate(pdf.pages, start=1):
        try:
            text = page.extract_text()
            if text:
                pages.append(text)
        except Exception as exc:  # pylint: disable=broad-except
            logging.warning(
                "Page %d could not be decoded (%s: %s). Skipping page.",
                page_num, type(exc).__name__, exc,
            )
    return "\n".join(pages)


def extract_isins(full_text: str) -> list[str]:
    """
    Extract all ISIN codes from the full document text.

    Two strategies, applied in order:

    1. Label-based (most reliable):
       Matches "ISIN LU2373783344" or "ISIN PORTATORE: IT0005431587".
       Used for English, Swedish, and Italian documents.

    2. Parenthesis fallback (only when strategy 1 yields nothing):
       Matches "(LU2523384894)" — the format used in Dutch (NL) KIDs
       where the ISIN is embedded in the share-class line.
       Skipped when labelled ISINs are found to minimise false positives.

    Duplicates are removed while preserving document order.

    Returns a list of ISIN strings.  Returns ['UNKNOWN'] if none are found.
    """
    found: list[str] = []
    seen: set[str] = set()

    # Strategy 1a: simple label — "ISIN LU..." or "ISIN: LU..."
    for match in RE_ISIN_LABELLED.finditer(full_text):
        isin = match.group(1)
        if isin not in seen:
            seen.add(isin)
            found.append(isin)

    # Strategy 1b: extended label with colon — "ISIN PORTATORE: IT..." (Italian KIDs)
    for match in RE_ISIN_LABELLED_EXT.finditer(full_text):
        isin = match.group(1)
        if isin not in seen:
            seen.add(isin)
            found.append(isin)

    # Strategy 1c: extended label without colon — "ISIN Portatore IT..." (Eurizon-style)
    for match in RE_ISIN_LABELLED_NOCOLON.finditer(full_text):
        isin = match.group(1)
        if isin not in seen:
            seen.add(isin)
            found.append(isin)

    # Strategy 2: parenthesis — fallback only when no labelled ISINs found
    if not found:
        for match in RE_ISIN_PARENS.finditer(full_text):
            isin = match.group(1)
            if isin not in seen:
                seen.add(isin)
                found.append(isin)

    return found if found else ["UNKNOWN"]


def extract_fund_name(full_text: str) -> str:
    """
    Extract the fund's product name using multilingual pattern matching.

    Tries FUND_NAME_PATTERNS in order (EN → SV → NL → ES → IT) and
    returns the first successful match.

    Returns 'UNKNOWN' if no pattern matches.
    """
    for pattern, _lang in FUND_NAME_PATTERNS:
        match = pattern.search(full_text)
        if match:
            return match.group(1).strip()
    return "UNKNOWN"


# Maps internal pattern language codes to human-readable column values.
_LANG_DISPLAY: dict[str, str] = {
    "EN":       "English",
    "NL":       "Dutch",
    "ES":       "Spanish",
    "IT_COMBO": "Italian",
    "SV":       "Swedish",
}


def detect_language(full_text: str) -> str:
    """
    Detect the document language by trying HOLD_YEAR_PATTERNS in order.

    Holding-year phrases are the most language-specific signals in PRIIPs KIDs
    and are therefore the most reliable language discriminator.  The first
    pattern that yields at least one match wins.

    Returns a human-readable language name (e.g. 'English', 'Italian'), or
    'Unknown' if no holding-year pattern matches (e.g. Swedish KIDs that have
    no standard ACI row and whose column text is concatenated by the PDF
    renderer).
    """
    for pattern, lang in HOLD_YEAR_PATTERNS:
        if lang == "IT_COMBO":
            matches = pattern.findall(full_text)
            flat = [g for tup in matches for g in tup if g]
            if flat:
                return _LANG_DISPLAY.get(lang, lang)
        else:
            if pattern.search(full_text):
                return _LANG_DISPLAY.get(lang, lang)
    return "Unknown"


def extract_cost_records(full_text: str) -> list[dict]:
    """
    Parse holding-year column headers and ACI/RIY values from the
    'Costs over Time' section, supporting EN, NL, ES, IT, and SV documents.

    Strategy:
        1. Try HOLD_YEAR_PATTERNS in order; stop at the first language that
           yields matches (prevents cross-language noise).
        2. Try ACI_LINE_PATTERNS in order; stop at the first match.
        3. Extract ALL percentage values from the matched ACI line.
           Comma-decimal values (e.g. '3,40%') are normalised to '3.40%'.
        4. Zip holding years ↔ ACI values to produce one record each.

    Holding-year values are normalised: '5.0' and '5,0' both become '5'.

    Returns a list of dicts:
        [
            {'holding_year': '1', 'annual_impact_cost': '3.40%'},
            {'holding_year': '5', 'annual_impact_cost': '1.80%'},
        ]
    Returns an empty list if the section cannot be parsed.
    """
    # ── Step 1: Extract holding years ─────────────────────────────────────
    raw_years: list[str] = []
    for pattern, lang in HOLD_YEAR_PATTERNS:
        if lang == 'IT_COMBO':
            # Special case: Italian SCENARIO header (group 1) + dopo lines
            # (group 2) in a single alternation regex — collect non-empty groups.
            matches = pattern.findall(full_text)
            flat = [g for tup in matches for g in tup if g]
            if flat:
                raw_years = flat
                break
        else:
            matches = pattern.findall(full_text)
            if matches:
                raw_years = matches
                break  # Use only the first matching language; stop here.

    if not raw_years:
        return []

    # Normalise decimal separator and convert whole-number floats to ints
    holding_years: list[str] = []
    for y in raw_years:
        y_norm = y.replace(",", ".")
        try:
            val = float(y_norm)
            holding_years.append(str(int(val)) if val == int(val) else y_norm)
        except ValueError:
            holding_years.append(y)

    # Remove duplicates while preserving order (e.g. repeated column headers)
    seen: set[str] = set()
    unique_years: list[str] = []
    for y in holding_years:
        if y not in seen:
            seen.add(y)
            unique_years.append(y)
    holding_years = unique_years

    # ── Step 2: Find the ACI / RIY line ───────────────────────────────────
    aci_line: str | None = None
    for pattern, _lang in ACI_LINE_PATTERNS:
        match = pattern.search(full_text)
        if match:
            aci_line = match.group(0)
            break

    if not aci_line:
        return []

    # ── Step 3: Extract all percentages from the ACI line ─────────────────
    raw_percents = RE_PERCENT.findall(aci_line)
    if not raw_percents:
        return []

    aci_values = [_normalise_percent(p) for p in raw_percents]

    # ── Step 4: Zip holding years ↔ ACI values ────────────────────────────
    n_years = len(holding_years)
    n_aci   = len(aci_values)

    if n_years != n_aci:
        logging.warning(
            "Mismatch: %d holding year(s) but %d ACI value(s). "
            "Pairing by position; extras will be skipped.",
            n_years, n_aci,
        )

    if n_aci < n_years:
        # Fewer ACI values than years — the ACI line has dashes for earlier
        # periods (e.g. "- - - 2,6%").  The available values correspond to
        # the LAST N columns, so right-align by trimming earlier years.
        holding_years = holding_years[n_years - n_aci:]
    elif n_aci > n_years:
        # More ACI values than years — truncate the surplus from the right.
        aci_values = aci_values[:n_years]

    records: list[dict] = []
    for year, cost in zip(holding_years, aci_values):
        records.append({
            "holding_year": year,
            "annual_impact_cost": cost,
        })

    return records



def process_single_pdf(pdf_path: Path) -> list[dict]:
    """
    Open one PDF file and extract all required fields.

    This function is designed to run in a separate worker process.
    It catches all exceptions internally so a single bad file never
    crashes the entire batch.

    Multiple ISINs:
        Documents declaring more than one ISIN (e.g. bearer and registered
        share classes) produce one output row per ISIN per holding year,
        duplicating the cost data to preserve the full association.

    Partial extraction:
        Documents that contain extractable ISINs but no standard cost table
        (e.g. long-form prospectus files) produce one row per ISIN with
        'N/A' values for Holding Year and Annual Impact Cost, so the file
        is still represented in the output rather than being silently lost.

    Returns a list of row-dicts, each containing:
        ISIN | Fund Name | Holding Year | Annual Impact Cost | input file name

    Returns an empty list only on a hard failure (unreadable file).
    """
    try:
        with pdfplumber.open(pdf_path) as pdf:
            full_text = extract_full_text(pdf)

        if not full_text.strip():
            logging.warning(
                "No extractable text found in '%s'. Skipping.", pdf_path.name
            )
            return []

        isins        = extract_isins(full_text)
        fund_name    = extract_fund_name(full_text)
        language     = detect_language(full_text)
        cost_records = extract_cost_records(full_text)

        if isins == ["UNKNOWN"]:
            logging.warning("No ISIN found in '%s'.", pdf_path.name)

        if fund_name == "UNKNOWN":
            logging.warning("Fund name not found in '%s'.", pdf_path.name)

        rows: list[dict] = []

        if not cost_records:
            # Partial extraction: include the file with N/A cost values
            logging.warning(
                "No 'Annual Cost Impact' data found in '%s'. "
                "Emitting partial row(s) with N/A cost values.",
                pdf_path.name,
            )
            for isin in isins:
                rows.append({
                    "ISIN":               isin,
                    "Fund Name":          fund_name,
                    "Holding Year":       "N/A",
                    "Annual Impact Cost": "N/A",
                    "Language":           language,
                    "input file name":    pdf_path.name,
                })
        else:
            # Full extraction: one row per ISIN × holding year
            for isin in isins:
                for record in cost_records:
                    rows.append({
                        "ISIN":               isin,
                        "Fund Name":          fund_name,
                        "Holding Year":       record["holding_year"],
                        "Annual Impact Cost": record["annual_impact_cost"],
                        "Language":           language,
                        "input file name":    pdf_path.name,
                    })

        return rows

    except Exception as exc:  # pylint: disable=broad-except
        logging.error(
            "Failed to process '%s': %s: %s",
            pdf_path.name, type(exc).__name__, exc,
        )
        return []


# ── Main Orchestration ────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    """Parse command-line arguments for flexible invocation."""
    parser = argparse.ArgumentParser(
        description=(
            "Extract Annual Cost Impact values from PRIIPs KID PDFs. "
            "Supports English, Dutch, Spanish, Italian, and Swedish documents."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        metavar="DIR",
        help="Directory containing input PDF files.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        metavar="DIR",
        help="Directory for the output CSV file.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_MAX_WORKERS,
        metavar="N",
        help="Number of parallel worker processes.",
    )
    return parser.parse_args()


def main() -> None:
    """
    Main entry point.

    1. Discover all PDF files in the input directory.
    2. Process them in parallel using ProcessPoolExecutor.
    3. Combine all results into a single pandas DataFrame.
    4. Write the DataFrame to Output/output_YYYYMMDD_HHMMSS.csv.
       The timestamp reflects the moment the job finishes.
    """
    setup_logging()
    args = parse_args()

    input_dir: Path  = args.input
    output_dir: Path = args.output
    max_workers: int = args.workers

    # ── Validate input directory ───────────────────────────────────────────
    if not input_dir.is_dir():
        logging.error("Input directory '%s' does not exist.", input_dir)
        raise SystemExit(1)

    pdf_files = sorted(input_dir.glob("*.pdf"))
    if not pdf_files:
        logging.error("No PDF files found in '%s'.", input_dir)
        raise SystemExit(1)

    logging.info("Found %d PDF file(s) in '%s'.", len(pdf_files), input_dir)

    # ── Create output directory ────────────────────────────────────────────
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── Process PDFs in parallel ───────────────────────────────────────────
    all_rows: list[dict] = []
    failed_files: list[str] = []

    # ProcessPoolExecutor spawns separate Python processes for true parallelism,
    # bypassing the GIL — well-suited for CPU-bound PDF parsing at scale.
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        future_to_path = {
            executor.submit(process_single_pdf, pdf_path): pdf_path
            for pdf_path in pdf_files
        }

        with tqdm(total=len(pdf_files), desc="Processing PDFs", unit="file") as pbar:
            for future in as_completed(future_to_path):
                pdf_path = future_to_path[future]
                try:
                    rows = future.result()
                    all_rows.extend(rows)
                    if not rows:
                        failed_files.append(pdf_path.name)
                except Exception as exc:  # pylint: disable=broad-except
                    logging.error(
                        "Unexpected error for '%s': %s", pdf_path.name, exc
                    )
                    failed_files.append(pdf_path.name)
                finally:
                    pbar.update(1)

    # ── Assemble and export DataFrame ──────────────────────────────────────
    if not all_rows:
        logging.error(
            "No data was successfully extracted. "
            "Check '%s' for details.",
            LOG_FILE,
        )
        raise SystemExit(1)

    df = pd.DataFrame(all_rows, columns=[
        "ISIN",
        "Fund Name",
        "Holding Year",
        "Annual Impact Cost",
        "Language",
        "input file name",
    ])

    # Sort for a clean, predictable output: by fund name, then holding year
    df = df.sort_values(["Fund Name", "Holding Year"]).reset_index(drop=True)

    # Timestamp reflects job completion time; prevents output files being
    # overwritten on repeated runs and provides a natural audit trail.
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_csv = output_dir / f"output_{timestamp}.csv"

    df.to_csv(output_csv, index=False, encoding="utf-8-sig")

    # ── Summary report ─────────────────────────────────────────────────────
    logging.info("─" * 60)
    logging.info("Extraction complete.")
    logging.info("  PDFs processed     : %d", len(pdf_files))
    logging.info("  Rows extracted     : %d", len(df))
    logging.info("  Files with no data : %d", len(failed_files))
    logging.info("  Output written to  : %s", output_csv.resolve())

    if failed_files:
        logging.warning(
            "%d file(s) yielded no data. See '%s' for details.",
            len(failed_files),
            LOG_FILE,
        )
        logging.info("Files with no data:")
        for name in failed_files:
            logging.info("    %s", name)

    logging.info("─" * 60)


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    main()
