"""ADS App Manager menu integration."""

from pathlib import Path
import sys

from keysight.ads import de
from keysight.ads.de import app


_owned_session = False
_project_root = None


def setup_addon(addon):
    global _project_root
    _project_root = str(Path(addon.startup_file).resolve().parents[1])


def bridge_module():
    if _project_root is None:
        raise RuntimeError("ADS Bridge Addon has no registered source path.")
    if _project_root not in sys.path:
        sys.path.insert(0, _project_root)
    import ads_addon
    return ads_addon


def show(text):
    from PySide6.QtWidgets import QMessageBox
    QMessageBox.information(app.window.main_pyside_widget(), "ADS Bridge", str(text))


def launch(write):
    global _owned_session
    if write and not de.workspace_is_open():
        show("Open the workspace you want to edit.")
        return
    try:
        workspace = de.active_workspace().path if write else None
        session = bridge_module().start(writable_workspace=workspace)
        _owned_session = True
        show(f"ADS Bridge session {session}")
    except Exception as exc:
        show(exc)


def start_readonly(action, window):
    launch(False)


def start_writable(action, window):
    launch(True)


def stop_bridge(action, window):
    global _owned_session
    bridge_module().stop()
    _owned_session = False


def generate_menu(addon, definition):
    if definition.window_type != app.WindowType.MAIN_WINDOW:
        return
    tools = definition.menubar.find_menu("Tools")
    if tools is None or tools.find_menu("ADS Bridge") is not None:
        return
    bridge = app.Menu("ADS Bridge")
    tools.add_menu(bridge)
    bridge.add_action(app.Action("Start read-only", start_readonly, None))
    bridge.add_action(app.Action("Start with write access to this workspace", start_writable, None))
    bridge.add_action(app.Action("Stop", stop_bridge, None))


def shutdown_addon(addon):
    if _owned_session:
        stop_bridge(None, None)
