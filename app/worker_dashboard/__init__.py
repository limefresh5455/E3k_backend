"""Worker Dashboard integration.

This package is deliberately isolated from the existing finance cockpit and
Planzer integration. It owns dashboard access scopes, API authentication,
dataset validation, and safe publication. The React frontend lives elsewhere.
"""

from .config import DashboardSettings

__all__ = ["DashboardSettings"]
