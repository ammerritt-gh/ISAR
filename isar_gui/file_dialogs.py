"""Non-native file dialogs, one kept per window and reused.

A static ``QFileDialog.get…`` call with ``DontUseNativeDialog`` builds a dialog and destroys it on
return. Destroying its file-system model makes the GUI thread wait for the model's file-info thread,
which can be stuck on a slow volume (a disconnected network drive took 21 s to stat here): the window
then froze for 1 to 6 s after a dialog closed or as the next one opened (measured 2026-10-27), and
clicks made meanwhile fired late, so *Open…* and *Recent ▾* lagged and fell out of step. One dialog
kept for the window's life is never destroyed mid-session. The signatures follow the static calls'.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QDialog, QFileDialog, QWidget

_ATTR = "_isar_file_dialog"


def _run(parent: QWidget, caption: str, directory: str, file_filter: str, options: QFileDialog.Option,
         accept: QFileDialog.AcceptMode, mode: QFileDialog.FileMode) -> QFileDialog | None:
    """Show the window's dialog (made on first use), set as the static call would set it; the dialog
    if the operator accepted, else ``None``."""
    window = parent.window()
    # Held on the window, not found as its child: PySide's exec() hands the dialog to Python, so
    # without a reference the dialog is deleted on return and the next call builds a fresh one.
    dialog = getattr(window, _ATTR, None)
    if dialog is None:
        dialog = QFileDialog(window)
        setattr(window, _ATTR, dialog)
    dialog.setOptions(options | QFileDialog.Option.DontUseNativeDialog)
    dialog.setAcceptMode(accept)
    dialog.setFileMode(mode)
    dialog.setWindowTitle(caption)
    dialog.setNameFilter(file_filter)
    if directory:
        path = Path(directory)
        if path.is_dir():
            dialog.setDirectory(str(path))
        else:                       # a file or folder to name: start in its parent, with it selected
            if path.is_absolute():
                dialog.setDirectory(str(path.parent))
            dialog.selectFile(path.name)
    if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.selectedFiles():
        return None
    return dialog


def get_existing_directory(parent: QWidget, caption: str, directory: str = "",
                           options: QFileDialog.Option = QFileDialog.Option.ShowDirsOnly) -> str:
    """``QFileDialog.getExistingDirectory`` on the window's reused dialog: the folder, or ``""``."""
    dialog = _run(parent, caption, directory, "", options,
                  QFileDialog.AcceptMode.AcceptOpen, QFileDialog.FileMode.Directory)
    return "" if dialog is None else dialog.selectedFiles()[0]


def get_save_file_name(parent: QWidget, caption: str, directory: str = "", file_filter: str = "",
                       options: QFileDialog.Option = QFileDialog.Option(0)) -> tuple[str, str]:
    """``QFileDialog.getSaveFileName`` on the window's reused dialog: ``(path, filter)``, or ``("", "")``."""
    dialog = _run(parent, caption, directory, file_filter, options,
                  QFileDialog.AcceptMode.AcceptSave, QFileDialog.FileMode.AnyFile)
    return ("", "") if dialog is None else (dialog.selectedFiles()[0], dialog.selectedNameFilter())
