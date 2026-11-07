"""Desktop interface for resumable FINRA TRACE history downloads."""

from __future__ import annotations

import csv
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Any

from PySide6.QtCore import QDate, QThread, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QDateEdit,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import trace_history

logger = logging.getLogger("parallax.trace_history")


COL_ISIN = 0
COL_ISSUER = 1
COL_CUSIP = 2
COL_STATUS = 3
COL_COUNT = 4
COL_FIRST = 5
COL_LAST = 6
COL_FLAGS = 7
COL_ATTEMPTS = 8
COL_ERROR = 9


class DownloadWorker(QThread):
    job_changed = Signal(object)
    progress_changed = Signal(int, int)
    succeeded = Signal()
    failed = Signal(str)
    batch_started = Signal(int)

    def __init__(
        self,
        run_id: str,
        mode: str,
        isin: str = "",
        batch_size: int = trace_history.DEFAULT_BATCH_SIZE,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.run_id = run_id
        self.mode = mode
        self.isin = isin
        self.batch_size = batch_size

    def run(self) -> None:
        try:
            if self.mode == "fix":
                trace_history.fix_bond(
                    self.run_id, self.isin, on_job=self.job_changed.emit
                )
            else:
                trace_history.download_run(
                    self.run_id,
                    batch_size=self.batch_size,
                    on_job=self.job_changed.emit,
                    on_progress=self.progress_changed.emit,
                    on_batch=self.batch_started.emit,
                )
            self.succeeded.emit()
        except Exception as error:
            logger.exception("GUI worker failed: mode=%s run_id=%s isin=%s", self.mode, self.run_id, self.isin)
            self.failed.emit(str(error))


class TraceHistoryWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("FINRA TRACE Bond History")
        self.resize(1400, 780)
        self.run_id: str | None = None
        self.worker: DownloadWorker | None = None
        self.row_by_isin: dict[str, int] = {}
        self._loading_table = False
        self._close_when_finished = False

        central = QWidget()
        layout = QVBoxLayout(central)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("History window"))
        self.start_date = QDateEdit(calendarPopup=True)
        self.start_date.setDisplayFormat("yyyy-MM-dd")
        self.start_date.setDate(QDate.currentDate().addYears(-5))
        self.end_date = QDateEdit(calendarPopup=True)
        self.end_date.setDisplayFormat("yyyy-MM-dd")
        self.end_date.setDate(QDate.currentDate().addDays(-1))
        controls.addWidget(self.start_date)
        controls.addWidget(QLabel("to"))
        controls.addWidget(self.end_date)
        self.new_run_button = QPushButton("New download run")
        self.new_run_button.clicked.connect(self._new_run)
        self.resume_button = QPushButton("Start / resume")
        self.resume_button.clicked.connect(self._resume_run)
        controls.addWidget(self.new_run_button)
        controls.addWidget(self.resume_button)
        layout.addLayout(controls)

        action_row = QHBoxLayout()
        self.retry_button = QPushButton("Attempt fix for selected bond")
        self.retry_button.clicked.connect(self._retry_selected)
        self.retry_button.setEnabled(False)
        self.export_button = QPushButton("Export CSV")
        self.export_button.clicked.connect(self._export_csv)
        self.export_button.setEnabled(False)
        self.filter = QComboBox()
        self.filter.addItems(["All bonds", "Needs attention", "Pending", "Downloaded"])
        self.filter.currentTextChanged.connect(self._apply_filter)
        self.refresh_button = QPushButton("Refresh list")
        self.refresh_button.clicked.connect(self._refresh_jobs)
        action_row.addWidget(self.retry_button)
        action_row.addWidget(self.export_button)
        action_row.addWidget(self.filter)
        action_row.addStretch(1)
        action_row.addWidget(self.refresh_button)
        layout.addLayout(action_row)

        self.source_label = QLabel()
        self.source_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.source_label)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        self.table = QTableWidget(0, 10)
        self.table.setHorizontalHeaderLabels(
            [
                "ISIN",
                "Issuer",
                "CUSIP (editable)",
                "Status",
                "Rows",
                "First date",
                "Last date",
                "Sanity flags",
                "Attempts",
                "Last error",
            ]
        )
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setWordWrap(False)
        self.table.setEditTriggers(
            QTableWidget.EditTrigger.DoubleClicked | QTableWidget.EditTrigger.EditKeyPressed
        )
        self.table.verticalHeader().setVisible(False)
        self.table.cellChanged.connect(self._cell_changed)
        layout.addWidget(self.table, 1)

        self.setCentralWidget(central)
        self.statusBar().showMessage("Choose a new run or resume a compatible incomplete run.")
        self._load_latest_run()

    def _date_range(self) -> tuple[date, date]:
        return self.start_date.date().toPython(), self.end_date.date().toPython()

    def _snapshot_population_size(self) -> int:
        with trace_history.DEFAULT_SNAPSHOT.open("r", encoding="utf-8-sig", newline="") as handle:
            return sum(1 for row in csv.DictReader(handle) if (row.get("isin") or "").strip())

    def _new_run(self) -> None:
        self._prepare_and_start(resume=False)

    def _resume_run(self) -> None:
        self._prepare_and_start(resume=True)

    def _prepare_and_start(self, resume: bool) -> None:
        if self._worker_running():
            return
        start_date, end_date = self._date_range()
        if start_date > end_date:
            QMessageBox.warning(self, "Invalid date range", "Start date must be on or before end date.")
            return
        try:
            self.run_id, reused = trace_history.prepare_run(
                trace_history.DEFAULT_SNAPSHOT,
                start_date,
                end_date,
                resume=resume,
            )
        except Exception as error:
            QMessageBox.critical(self, "Could not prepare download", str(error))
            return
        self._refresh_jobs()
        label = "Resuming" if reused else "Starting"
        self.statusBar().showMessage(f"{label} run {self.run_id}")
        self._start_worker("download")

    def _retry_selected(self) -> None:
        if not self.run_id or self._worker_running():
            return
        selected = self.table.selectionModel().selectedRows()
        if not selected:
            QMessageBox.information(self, "Select a bond", "Select one bond row to retry.")
            return
        isin = self.table.item(selected[0].row(), COL_ISIN).text()
        self._start_worker("fix", isin)

    def _start_worker(self, mode: str, isin: str = "") -> None:
        if not self.run_id:
            return
        if mode == "download":
            self.progress.setValue(0)
            self.progress.setMaximum(max(1, self._snapshot_population_size()))
        self._set_controls_enabled(False)
        self.worker = DownloadWorker(self.run_id, mode, isin, parent=self)
        self.worker.job_changed.connect(self._update_job)
        self.worker.progress_changed.connect(self._update_progress)
        self.worker.batch_started.connect(self._batch_started)
        self.worker.succeeded.connect(self._worker_succeeded)
        self.worker.failed.connect(self._worker_failed)
        self.worker.start()

    def _worker_running(self) -> bool:
        return bool(self.worker and self.worker.isRunning())

    def _set_controls_enabled(self, enabled: bool) -> None:
        self.new_run_button.setEnabled(enabled)
        self.resume_button.setEnabled(enabled)
        self.refresh_button.setEnabled(enabled)
        self.retry_button.setEnabled(enabled and bool(self.run_id))
        self.export_button.setEnabled(enabled and bool(self.run_id))

    def _update_job(self, job: dict[str, Any]) -> None:
        isin = str(job.get("isin", ""))
        row = self.row_by_isin.get(isin)
        if row is None:
            self._refresh_jobs()
            return
        self._loading_table = True
        values = {
            COL_CUSIP: str(job.get("cusip") or ""),
            COL_STATUS: str(job.get("status") or ""),
            COL_COUNT: str(job.get("observation_count") or 0),
            COL_FIRST: str(job.get("first_date") or ""),
            COL_LAST: str(job.get("last_date") or ""),
            COL_FLAGS: ", ".join(job.get("quality_flags") or []),
            COL_ATTEMPTS: str(job.get("attempts") or 0),
            COL_ERROR: str(job.get("last_error") or ""),
        }
        for column, value in values.items():
            item = self.table.item(row, column)
            if item is None:
                item = QTableWidgetItem()
                self.table.setItem(row, column, item)
            item.setText(value)
        self._style_status_cell(row)
        self._loading_table = False
        self._apply_filter_to_row(row)

    def _batch_started(self, size: int) -> None:
        self.progress.setRange(0, 0)
        self.statusBar().showMessage(
            f"Waiting for FINRA response for {size} bonds. This request may take a few minutes."
        )

    def _style_status_cell(self, row: int) -> None:
        item = self.table.item(row, COL_STATUS)
        status = item.text() if item else ""
        colors = {
            "downloaded": ("#e7f3e8", "#285c32"),
            "review": ("#fff0c2", "#704b00"),
            "failed": ("#f9dada", "#7a2424"),
            "needs_review": ("#f9dada", "#7a2424"),
            "downloading": ("#dceaf7", "#1d4f7a"),
        }
        background, foreground = colors.get(status, ("#ffffff", "#202020"))
        if item:
            item.setBackground(QColor(background))
            item.setForeground(QColor(foreground))

    def _update_progress(self, completed: int, total: int) -> None:
        self.progress.setRange(0, max(1, total))
        self.progress.setMaximum(max(1, total))
        self.progress.setValue(min(completed, total))
        self.statusBar().showMessage(f"Processed {completed:,} of {total:,} bond jobs")

    def _worker_succeeded(self) -> None:
        self._set_controls_enabled(True)
        if self._close_when_finished:
            self.close()
            return
        self._refresh_jobs()
        summary = trace_history.run_summary(self.run_id) if self.run_id else {}
        self.statusBar().showMessage(
            f"Run {summary.get('status', 'finished')}: "
            f"{summary.get('downloaded', 0):,} downloaded, "
            f"{summary.get('review', 0):,} need review, "
            f"{summary.get('failed', 0):,} failed."
        )

    def _worker_failed(self, message: str) -> None:
        self._set_controls_enabled(True)
        if self._close_when_finished:
            self.close()
            return
        self._refresh_jobs()
        self.statusBar().showMessage("Download action failed; the run remains resumable.")
        QMessageBox.warning(self, "TRACE download error", message)

    def _refresh_jobs(self) -> None:
        if not self.run_id:
            return
        try:
            jobs = trace_history.list_jobs(self.run_id)
            summary = trace_history.run_summary(self.run_id)
        except Exception as error:
            self.statusBar().showMessage(f"Could not read run state: {error}")
            return
        self._loading_table = True
        self.row_by_isin = {}
        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(len(jobs))
        for row, job in enumerate(jobs):
            self.row_by_isin[str(job["isin"])] = row
            values = [
                job.get("isin") or "",
                job.get("issuer_name") or "",
                job.get("cusip") or "",
                job.get("status") or "",
                str(job.get("observation_count") or 0),
                str(job.get("first_date") or ""),
                str(job.get("last_date") or ""),
                ", ".join(job.get("quality_flags") or []),
                str(job.get("attempts") or 0),
                job.get("last_error") or "",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column != COL_CUSIP:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if column == COL_CUSIP:
                    item.setToolTip("Double-click to correct the CUSIP, then resume or retry this bond.")
                if column == COL_ERROR and value:
                    item.setToolTip(str(value))
                self.table.setItem(row, column, item)
            self._style_status_cell(row)
        self._loading_table = False
        self.table.setUpdatesEnabled(True)
        self.table.viewport().update()
        self._apply_filter()
        self.source_label.setText(
            f"Run {self.run_id} | Snapshot date {self._snapshot_date() or 'unknown'} | "
            f"{summary['total']:,} bonds | database {trace_history.DATABASE_PATH} | "
            f"log {trace_history.LOG_PATH}"
        )
        self.export_button.setEnabled(bool(jobs))
        self.retry_button.setEnabled(bool(jobs))

    def _snapshot_date(self) -> str:
        latest = trace_history.latest_run()
        return str(latest.get("snapshot_as_of_date", "")) if latest else ""

    def _load_latest_run(self) -> None:
        try:
            latest = trace_history.latest_run()
        except Exception as error:
            self.statusBar().showMessage(f"Could not open DuckDB: {error}")
            return
        if latest:
            self.run_id = str(latest["run_id"])
            self.start_date.setDate(QDate.fromString(str(latest["start_date"]), "yyyy-MM-dd"))
            self.end_date.setDate(QDate.fromString(str(latest["end_date"]), "yyyy-MM-dd"))
            self._refresh_jobs()
            self.statusBar().showMessage(f"Loaded latest run ({latest['status']})")
            self._set_controls_enabled(True)
        else:
            try:
                count = self._snapshot_population_size()
                self.source_label.setText(
                    f"Ready to download {count:,} bonds from {trace_history.DEFAULT_SNAPSHOT} | "
                    f"diagnostic log {trace_history.LOG_PATH}"
                )
                self.statusBar().showMessage(
                    "A new run will seed up to five years of history for the current snapshot population."
                )
            except Exception as error:
                self.source_label.setText(str(error))

    def _cell_changed(self, row: int, column: int) -> None:
        if self._loading_table or column != COL_CUSIP or not self.run_id:
            return
        isin = self.table.item(row, COL_ISIN).text()
        cusip = self.table.item(row, COL_CUSIP).text().strip()
        try:
            trace_history.update_cusip(self.run_id, isin, cusip)
        except Exception as error:
            QMessageBox.warning(self, "Invalid CUSIP", str(error))
            self._refresh_jobs()
            return
        self.statusBar().showMessage(f"Updated CUSIP for {isin}; resume or retry the bond.")
        self._refresh_jobs()

    def _apply_filter(self) -> None:
        for row in range(self.table.rowCount()):
            self._apply_filter_to_row(row)

    def _apply_filter_to_row(self, row: int) -> None:
        selected = self.filter.currentText()
        status_item = self.table.item(row, COL_STATUS)
        status = status_item.text() if status_item else ""
        if selected == "Needs attention":
            visible = status in {"failed", "review", "needs_review"}
        elif selected == "Pending":
            visible = status in {"pending", "downloading"}
        elif selected == "Downloaded":
            visible = status == "downloaded"
        else:
            visible = True
        self.table.setRowHidden(row, not visible)

    def closeEvent(self, event: Any) -> None:
        if self._worker_running():
            answer = QMessageBox.question(
                self,
                "Download in progress",
                "A FINRA request is still running. If you close, this window will finish "
                "the current request and save its results before exiting. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._close_when_finished = True
            self.hide()
            event.ignore()
            return
        event.accept()

    def _export_csv(self) -> None:
        if not self.run_id:
            return
        suggested = trace_history.TRACE_DIR / f"history_{self.run_id}.csv"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export bond history CSV", str(suggested), "CSV files (*.csv)"
        )
        if not path:
            return
        try:
            output = trace_history.export_csv(self.run_id, Path(path))
            self.statusBar().showMessage(f"Exported CSV to {output}", 7000)
        except Exception as error:
            QMessageBox.critical(self, "CSV export failed", str(error))


def main() -> None:
    trace_history.configure_logging()
    app = QApplication(sys.argv)
    window = TraceHistoryWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
