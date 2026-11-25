"""Install or remove the ADS Bridge menu addon from inside the ADS Python Console (or inspect status from CLI)."""

from __future__ import annotations

from pathlib import Path
import sys

try:
    from keysight.ads.de import app
except (ImportError, RuntimeError):
    app = None  # type: ignore[assignment]


NAME = "ADS Bridge"


def _require_gui_app() -> None:
    if app is None:
        addon_file = Path(__file__).resolve().with_name("addon_entry") / "__init__.py"
        raise RuntimeError(
            "install_addon.install() requires the live ADS GUI Python Console (Tools -> Python Console).\n"
            f"Addon source path: {addon_file}\n"
            "In ADS Python Console run:\n"
            f"  import sys; sys.path.insert(0, r'{Path(__file__).resolve().parent}'); import install_addon; install_addon.install()"
        )


def install(persistent=True):
    _require_gui_app()
    existing = app.find_addon(NAME)
    if existing:
        raise RuntimeError("ADS Bridge is already registered. Remove the current registration first.")
    location = app.AddonLocale.USER if persistent else app.AddonLocale.MEMORY
    file = Path(__file__).resolve().with_name("addon_entry") / "__init__.py"
    entry = app.Addon(NAME, str(file), True, location)
    entry.root_directory = str(file.parent)
    register = app.add_user_addon if persistent else app.add_memory_addon
    register(entry)
    return app.find_addon(NAME)


def uninstall():
    _require_gui_app()
    entry = app.find_addon(NAME)
    if entry is None:
        return False
    if entry.location == app.AddonLocale.USER:
        app.remove_user_addon(entry)
    elif entry.location == app.AddonLocale.MEMORY:
        app.remove_memory_addon(entry)
    else:
        raise RuntimeError("ADS Bridge is registered outside the current user's addon locations.")
    return True


if __name__ == "__main__":
    if app is not None:
        try:
            res = install(persistent=True)
            print(f"Registered ADS Bridge addon: {res}")
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(1)
    else:
        addon_file = Path(__file__).resolve().with_name("addon_entry") / "__init__.py"
        print(f"ADS Bridge addon source ready at: {addon_file}")
        print("Note: Persistent registration runs inside ADS GUI Tools -> Python Console (or via bridge.gui_launch).")
