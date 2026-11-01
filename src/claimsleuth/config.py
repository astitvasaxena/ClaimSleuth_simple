"""Paths and fixed business limits shared by every service.

Paths can be overridden with environment variables so tests and deployments never
write into the source tree.
"""
import os
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

DATA_DIR = Path(os.environ.get("CLAIMSLEUTH_DATA_DIR", PROJECT_ROOT / "data"))
DB_PATH = Path(os.environ.get("CLAIMSLEUTH_DB_PATH", DATA_DIR / "claimsleuth.db"))
RUNTIME_DIR = Path(os.environ.get("CLAIMSLEUTH_RUNTIME_DIR", PROJECT_ROOT / "runtime"))


def claims_dir(data_dir: Path = DATA_DIR) -> Path:
    return data_dir / "claims"


def policies_file(data_dir: Path = DATA_DIR) -> Path:
    return data_dir / "policies.json"


def ground_truth_file(data_dir: Path = DATA_DIR) -> Path:
    # Labels live outside the claims folder and are never given to an agent or tool.
    return data_dir / "ground_truth.json"


# All money is Indian Rupees (INR). Approved payouts at or above this amount need a human sign-off.
CURRENCY = "INR"
HUMAN_APPROVAL_THRESHOLD = Decimal("15000.00")

# Claim-history rules: this many earlier claims on the same serial inside the window is a repeat pattern.
REPEAT_CLAIM_WINDOW_DAYS = 365
REPEAT_CLAIM_LIMIT = 2
