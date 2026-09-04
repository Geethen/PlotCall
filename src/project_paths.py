"""Filesystem defaults, so the builders run from any checkout."""
from __future__ import annotations

import os
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def project_data_dir(*parts: str) -> Path:
    """Where derived tables live. ``PLOTCALL_DATA`` moves it off the checkout."""
    root = os.environ.get("PLOTCALL_DATA")
    base = Path(root) if root else REPO_ROOT / "data"
    return base.joinpath(*parts)
