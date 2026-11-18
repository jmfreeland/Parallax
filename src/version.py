"""Human-readable development version derived from the Git history."""

from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def version_label() -> str:
    """Return v0.<reachable commit count> plus the identifying Git revision."""
    try:
        count = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            check=True,
            text=True,
            timeout=2,
        ).stdout.strip()
        revision = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            check=True,
            text=True,
            timeout=2,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=ROOT,
            capture_output=True,
            check=True,
            text=True,
            timeout=2,
        ).stdout.strip()
        label = f"v0.{count} - {revision}"
        return f"{label} - uncommitted changes" if dirty else label
    except (OSError, subprocess.SubprocessError):
        return "v0.0 - Git revision unavailable"
