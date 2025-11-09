"""GST verification adapters.

This module provides pluggable adapters for verifying Indian GSTINs.
Two implementations are included:

* **OfficialGSTAdapter**: Simulates an integration with the official
  GSTN API.  In this prototype it merely checks a hard‑coded list
  of valid GSTINs loaded from environment or a YAML file.
* **FallbackGSTAdapter**: A fallback adapter using a generic HTTP
  service.  Here it returns 'unknown' for any GSTIN not in the
  sample list.

Adapters should return one of `verified`, `unverified`, or `unknown`.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Literal


GSTStatus = Literal["verified", "unverified", "unknown"]


class BaseGSTAdapter(ABC):
    """Abstract base class for GST verification adapters."""

    @abstractmethod
    def verify_gstin(self, gstin: str) -> GSTStatus:
        """Verify a GSTIN.

        Args:
            gstin: GST identification number.

        Returns:
            A status string: 'verified', 'unverified', or 'unknown'.
        """


class OfficialGSTAdapter(BaseGSTAdapter):
    """Simulated official GST verification adapter."""

    def __init__(self) -> None:
        # In a real implementation this would load credentials and set up
        # signing keys for the GSTN API.  For this prototype we
        # read a comma‑separated list of verified GSTINs from an
        # environment variable for demonstration.
        verified = os.environ.get("VALID_GSTINS", "".strip())
        self.verified_gstins = set(g.strip() for g in verified.split(",") if g.strip())

    def verify_gstin(self, gstin: str) -> GSTStatus:
        if gstin in self.verified_gstins:
            return "verified"
        # Simple format check: GSTIN should be 15 characters
        if len(gstin) == 15:
            return "unverified"
        return "unknown"


class FallbackGSTAdapter(BaseGSTAdapter):
    """Fallback adapter returning unknown for all GSTINs."""

    def verify_gstin(self, gstin: str) -> GSTStatus:
        # A real fallback would call a third‑party API.
        return "unknown"


def get_gst_adapter(preferred: bool = True) -> BaseGSTAdapter:
    """Return a configured GST adapter.

    If `preferred` is True, attempt to use the official adapter
    first; otherwise fall back.
    """
    if preferred:
        return OfficialGSTAdapter()
    return FallbackGSTAdapter()