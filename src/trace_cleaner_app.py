"""GUI for explicitly cleaning a preprocessed TRACE Parquet dataset."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .trace_cleaner import clean_preprocessed_trace


class CleanerWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, input_path: Path, yield_threshold: float, price_threshold: float, mark_age_days: int) -> None:
        super().__init__()
        self.input_path = input_path
        self.yield_threshold = yield_threshold
        self.price_threshold = price_threshold
        self.mark_age_days = mark_age_days

    def run(self) -> None:
        try:
            report = clean_preprocessed_trace(
                self.input_path,
                yield_spike_threshold_bps=self.yield_threshold,
                price_spike_threshold_points=self.price_threshold,
                max_mark_age_business_days=self.mark_age_days,
            )
        except Exception as error:
            self.failed.emit(str(error))
        else:
            self.succeeded.emit(report)


class TraceCleanerWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Clean preprocessed TRACE history")
        self.resize(700, 390)
        self.input_path: Path | None = None
        self.worker: CleanerWorker | None = None

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.addWidget(QLabel(
            "Select a preprocessed observations.parquet file. Cleaning creates a separate, versioned Parquet output "
            "and diagnostics report; the source dataset and raw history remain unchanged."
        ))
        self.source_label = QLabel("No preprocessed dataset selected")
        self.source_label.setWordWrap(True)
        choose = QPushButton("Choose preprocessed dataset…")
        choose.clicked.connect(self._choose_input)
        layout.addWidget(choose)
        layout.addWidget(self.source_label)

        form = QFormLayout()
        self.yield_threshold = QDoubleSpinBox()
        self.yield_threshold.setRange(1, 1000)
        self.yield_threshold.setSuffix(" bps")
        self.yield_threshold.setValue(50)
        form.addRow("Minimum isolated yield move to check", self.yield_threshold)
        self.price_threshold = QDoubleSpinBox()
        self.price_threshold.setRange(0.1, 100)
        self.price_threshold.setSuffix(" points per 100 par")
        self.price_threshold.setValue(5)
        form.addRow("Minimum isolated price move to check", self.price_threshold)
        self.mark_age_days = QSpinBox()
        self.mark_age_days.setRange(0, 20)
        self.mark_age_days.setValue(3)
        self.mark_age_days.setSuffix(" business days")
        form.addRow("Maximum age for carried marks", self.mark_age_days)
        layout.addLayout(form)

        layout.addWidget(QLabel(
            "Observed cleaned values remain unchanged. Separate mark columns carry the last value forward for short gaps, "
            "and include the mark's age. Large moves that promptly reverse near the prior level are flagged, not revised, "
            "because confirming a reversal requires a later observation. The flag is retrospective; evaluator inputs use "
            "the unchanged observed columns."
        ))
        self.status = QLabel("Ready")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.run_button = QPushButton("Create cleaned dataset")
        self.run_button.setEnabled(False)
        self.run_button.clicked.connect(self._run_cleaning)
        layout.addWidget(self.run_button)
        self.setCentralWidget(root)

    def _choose_input(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select preprocessed TRACE data",
            str(Path.cwd()),
            "Parquet files (*.parquet)",
        )
        if not path:
            return
        self.input_path = Path(path)
        self.source_label.setText(str(self.input_path))
        self.run_button.setEnabled(True)
        self.status.setText("Ready to clean this preprocessed dataset.")

    def _run_cleaning(self) -> None:
        if self.input_path is None:
            return
        self.run_button.setEnabled(False)
        self.status.setText("Cleaning series and writing a new Parquet dataset…")
        self.worker = CleanerWorker(
            self.input_path,
            self.yield_threshold.value(),
            self.price_threshold.value(),
            self.mark_age_days.value(),
        )
        self.worker.succeeded.connect(self._completed)
        self.worker.failed.connect(self._failed)
        self.worker.start()

    def _completed(self, report: dict[str, Any]) -> None:
        self.status.setText(
            f"Created {report['output_rows']:,} rows from {report['input_rows']:,}. "
            f"Flagged reversals: yield {report['reversal_spikes_flagged']['yield']:,}, "
            f"price {report['reversal_spikes_flagged']['price']:,}. "
            f"Carried forward marks: yield {report['business_day_marks_carried_forward']['yield']:,}, "
            f"price {report['business_day_marks_carried_forward']['price']:,}.\n"
            f"Output: {report['output_cleaned_parquet']}\nDiagnostics: {report['diagnostics_report']}"
        )
        self.run_button.setEnabled(True)

    def _failed(self, message: str) -> None:
        self.status.setText("Cleaning failed; the source dataset was not modified.")
        QMessageBox.warning(self, "Could not clean TRACE data", message)
        self.run_button.setEnabled(True)


def main() -> None:
    app = QApplication(sys.argv)
    window = TraceCleanerWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
