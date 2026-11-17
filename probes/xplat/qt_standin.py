"""Probe 2 stand-in (cross-platform Plan A, Task 6): a small Qt 6 dialog shaped like a qtFRED one.

qtFRED's dialogs are .ui files whose widgets carry object names (shipNameEdit, shipClassCombo,
...). This dialog has the widget kinds an editor task touches, with the names Task 6 lists:

    shipNameEdit    QLineEdit, with a QLabel buddy "Ship name:"
    shipClassCombo  QComboBox with three items
    eventTree       QTreeWidget, one item with a child (expanded)
    okButton        QPushButton "OK"; writes values.json and closes the dialog

It talks to a11y_probe.py through files in out_dir: ready.json (pid and Qt version, once shown)
and values.json
(what the widgets held when OK was pressed). It quits by itself after LIFETIME_MS.

    python qt_standin.py <out_dir>
"""
import json
import os
import sys
import time
from pathlib import Path

from PySide6.QtCore import QTimer, qVersion
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
)

LIFETIME_MS = 120_000
REPLACE_TRIES = 10
REPLACE_GAP_S = 0.05
TITLE = "QtStandin"
SHIP_CLASSES = ("GTF Ulysses", "GTF Hercules", "GTB Medusa")
EVENTS = ("Event 1", "Event 1a")          # parent, child


def write_json(path: Path, data: dict) -> None:
    """Write data as JSON through a temporary name, so the probe never reads half a file."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    # On Windows the rename fails while the probe has the file open to read it; a stale file
    # would make a route that took look like one that did not (QA CR-6). A few short retries.
    for tries_left in range(REPLACE_TRIES - 1, -1, -1):       # bounded
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if not tries_left:
                raise
            time.sleep(REPLACE_GAP_S)


def build(out: Path) -> QDialog:
    """Build the dialog; OK writes the widgets' values to out/values.json and closes it."""
    dlg = QDialog()
    dlg.setObjectName("standinDialog")
    dlg.setWindowTitle(TITLE)
    name = QLineEdit(dlg)
    name.setObjectName("shipNameEdit")
    label = QLabel("Ship name:", dlg)
    label.setBuddy(name)
    combo = QComboBox(dlg)
    combo.setObjectName("shipClassCombo")
    combo.addItems(SHIP_CLASSES)
    tree = QTreeWidget(dlg)
    tree.setObjectName("eventTree")
    tree.setHeaderLabel("Events")
    parent = QTreeWidgetItem(tree, [EVENTS[0]])
    QTreeWidgetItem(parent, [EVENTS[1]])
    tree.expandAll()
    ok = QPushButton("OK", dlg)
    ok.setObjectName("okButton")
    form = QFormLayout(dlg)
    form.addRow(label, name)
    form.addRow("Class:", combo)
    form.addRow(tree)
    form.addRow(ok)

    def on_ok():
        """OK pressed: write what the widgets hold to values.json and close."""
        write_json(out / "values.json", {"ship_name": name.text(),
                                         "ship_class": combo.currentText()})
        dlg.accept()

    ok.clicked.connect(on_ok)

    # For qt_combo_routes.py (probe 8), which tries several ways to pick a combo item and needs
    # Qt's own answer after each: state.json is rewritten whenever the combo's item or the
    # focused widget changes. (Each route gets a stand-in of its own, so nothing resets it.)
    def write_state(*_):
        """Write the combo's item and the focused widget to state.json."""
        focus = QApplication.focusWidget()
        write_json(out / "state.json", {"index": combo.currentIndex(),
                                        "ship_class": combo.currentText(),
                                        "focus": focus.objectName() if focus else None})

    combo.currentIndexChanged.connect(write_state)
    QApplication.instance().focusChanged.connect(write_state)
    write_state()
    return dlg


def main() -> int:
    """Show the dialog, announce readiness, and run until OK or LIFETIME_MS."""
    out = Path(sys.argv[1])
    app = QApplication([sys.argv[0]])
    dlg = build(out)
    dlg.finished.connect(app.quit)
    QTimer.singleShot(LIFETIME_MS, app.quit)
    dlg.show()
    # WHY from the event loop: Qt's accessibility bridge may not be registered before the loop
    # runs (QA CR-5), so "ready" written before app.exec() could send the probe walking an
    # empty tree.
    # The probe still retries its first walk (a11y_probe.first_walk).
    QTimer.singleShot(0, lambda: write_json(out / "ready.json", {
        "pid": os.getpid(), "title": TITLE, "qt": qVersion()}))
    return app.exec()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python qt_standin.py <out_dir>")
    sys.exit(main())
