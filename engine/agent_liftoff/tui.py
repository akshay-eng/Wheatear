"""Small terminal helpers shared by the wizard and the pre-flight screens."""

from __future__ import annotations

import sys


def flush_input() -> None:
    """Drop anything typed while a slow, non-interactive step was running.

    Several steps shell out and block for seconds at a time -- `dotnet
    --version`, `pac --version` (a .NET app, slow to even start), the IAM
    token exchange. Keystrokes pressed during those land in the terminal's
    input buffer and are then handed to the *next* prompt all at once, so a
    few impatient Enters silently answer several menus in a row. The user
    sees a screen that stopped offering choices and appears to have looped
    on itself -- which is exactly what a queued Enter on a "Refresh" row
    does, over and over.

    Called right before each prompt is drawn, so a menu always waits for a
    keystroke made *after* it was on screen.
    """
    try:
        if not sys.stdin.isatty():
            return
    except (AttributeError, ValueError):  # detached / closed stdin
        return

    try:
        import termios
    except ImportError:  # Windows
        try:
            import msvcrt
        except ImportError:
            return
        while msvcrt.kbhit():
            msvcrt.getwch()
        return

    try:
        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except (termios.error, OSError, ValueError):
        pass
