"""HSN/SAC rates updater.

This script downloads a CSV mapping HSN codes to GST rates from a
trusted source and stores it in the project's data folder.  It
updates the `hsn_rates.csv` file that is used for rate validation
when processing memo.  Invoke it with `python -m app.hsn_updater`.
"""

from __future__ import annotations

import csv
import logging
import os
from pathlib import Path
from typing import Dict

import requests

HSN_DATA_URL = "https://example.com/hsn_rates.csv"  # Placeholder URL
DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent / "data" / "hsn_rates.csv"
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def download_hsn_rates(url: str = HSN_DATA_URL) -> Dict[str, float]:
    """Download the HSN rates CSV and return a mapping.

    Args:
        url: The URL of the CSV file.

    Returns:
        A dictionary mapping HSN codes to GST rates.
    """
    logger.info("Downloading HSN rates from %s", url)
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    lines = resp.text.splitlines()
    reader = csv.DictReader(lines)
    rates: Dict[str, float] = {}
    for row in reader:
        code = row.get("HSN")
        rate = row.get("GST")
        if code and rate:
            try:
                rates[code] = float(rate)
            except ValueError:
                continue
    return rates


def save_hsn_rates(rates: Dict[str, float], path: Path = DEFAULT_OUTPUT) -> None:
    """Save the HSN rates to a CSV file.

    Args:
        rates: Mapping of HSN codes to GST rates.
        path: Output file path.
    """
    os.makedirs(path.parent, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["HSN", "GST"])
        for code, rate in rates.items():
            writer.writerow([code, rate])
    logger.info("Saved %d HSN rates to %s", len(rates), path)


def main() -> None:
    rates = download_hsn_rates()
    save_hsn_rates(rates)


if __name__ == "__main__":
    main()