"""Progress window for building and monitoring G-spread history snapshots."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QProcess, QProcessEnvironment, Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

ROOT = Path(__file__).resolve().parents[1]


class SpreadSnapshotWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Build G-spread history snapshot")
        self.resize(900, 620)
        self.process: QProcess | None = None
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(QLabel(
            "This builds a cleaned TRACE dataset for the selected run, then maps its yields "
            "to the selected Treasury curve vintage. Yield reversals above the threshold are flagged "
            "and excluded from G-spread values. The full history may take several minutes."
        ))
        self.status = QLabel("Ready")
        layout.addWidget(self.status)
        settings = QFormLayout()
        self.yield_threshold = QDoubleSpinBox()
        self.yield_threshold.setRange(1.0, 1000.0)
        self.yield_threshold.setDecimals(1)
        self.yield_threshold.setSingleStep(5.0)
        self.yield_threshold.setSuffix(" bp")
        self.yield_threshold.setValue(50.0)
        settings.addRow("Minimum isolated yield move to flag as reversal", self.yield_threshold)
        layout.addLayout(settings)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        layout.addWidget(self.progress)
        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.output.setPlaceholderText("Build progress and results will appear here.")
        layout.addWidget(self.output, 1)
        self.start_button = QPushButton("Build G-spread snapshot")
        self.start_button.clicked.connect(self._start_build)
        layout.addWidget(self.start_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.setCentralWidget(central)

    def _start_build(self) -> None:
        if self.process and self.process.state() != QProcess.ProcessState.NotRunning:
            return
        self.output.clear()
        self.status.setText("Building cleaned G-spread history. Progress messages will appear below...")
        self.progress.setRange(0, 0)
        self.start_button.setEnabled(False)
        process = QProcess(self)
        process.setWorkingDirectory(str(ROOT))
        process.setProcessEnvironment(QProcessEnvironment.systemEnvironment())
        process.setProgram(sys.executable)
        process.setArguments([
            "-m", "src.spread_snapshot",
            "--yield-spike-threshold-bps", str(self.yield_threshold.value()),
        ])
        process.readyReadStandardOutput.connect(self._read_output)
        process.readyReadStandardError.connect(self._read_output)
        process.finished.connect(self._finished)
        process.errorOccurred.connect(self._error)
        self.process = process
        process.start()

    def _read_output(self) -> None:
        if not self.process:
            return
        text = bytes(self.process.readAllStandardOutput()).decode(errors="replace")
        text += bytes(self.process.readAllStandardError()).decode(errors="replace")
        if text:
            cursor = self.output.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self.output.setTextCursor(cursor)
            self.output.insertPlainText(text)
            self.output.ensureCursorVisible()
            status_by_message = (
                ("Scanning raw field values", "Cleaning and scanning TRACE history..."),
                ("Loaded ", "Loading cleaned observation rows..."),
                ("Mapped ", "Mapping clean yields to Treasury curves..."),
                ("Writing immutable snapshot", "Writing Parquet output and manifest..."),
            )
            for marker, status in status_by_message:
                if marker in text:
                    self.status.setText(status)

    def _finished(self, exit_code: int, _exit_status: QProcess.ExitStatus) -> None:
        self._read_output()
        self.progress.setRange(0, 1)
        self.progress.setValue(1 if exit_code == 0 else 0)
        self.status.setText(
            "G-spread snapshot build complete." if exit_code == 0
            else f"G-spread snapshot build failed (exit code {exit_code})."
        )
        self.start_button.setEnabled(True)

    def _error(self, _error: QProcess.ProcessError) -> None:
        if self.process and self.process.error() == QProcess.ProcessError.FailedToStart:
            self.progress.setRange(0, 1)
            self.progress.setValue(0)
            self.status.setText("Could not start Python. Check the active environment.")
            self.start_button.setEnabled(True)


def main() -> None:
    app = QApplication.instance() or QApplication([])
    window = SpreadSnapshotWindow()
    window.show()
    app.exec()


if __name__ == "__main__":
    main()

