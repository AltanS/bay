"""Bay CLI console — output helpers, banners, and theme.

Backward-compatible: ``from bay_cli import console`` then
``console.success(...)``, ``console.console.print(...)`` etc.
"""

from bay_cli.console.banner import banner, show_banner
from bay_cli.console.output import (
    console,
    drain_messages,
    emit_error,
    emit_result,
    error,
    header,
    info,
    is_json_mode,
    is_yes_mode,
    set_json_mode,
    set_yes_mode,
    success,
    warning,
)
from bay_cli.console.theme import BRAND, BRAND_BOLD, BRAND_DIM, SPINNER

__all__ = [
    "BRAND",
    "BRAND_BOLD",
    "BRAND_DIM",
    "SPINNER",
    "banner",
    "console",
    "drain_messages",
    "emit_error",
    "emit_result",
    "error",
    "header",
    "info",
    "is_json_mode",
    "is_yes_mode",
    "set_json_mode",
    "set_yes_mode",
    "show_banner",
    "success",
    "warning",
]
