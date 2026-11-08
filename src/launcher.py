"""Small desktop launcher and diagnostics panel for Parallax."""

from __future__ import annotations

import csv
import importlib.metadata
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from PySide6.QtCore import QProcess, QProcessEnvironment, QThread, Qt, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QGridLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"
VERSIONS_DIR = DATA_DIR / "mapping_versions"
MAPPINGS_PATH = DATA_DIR / "issuer_ticker_map.csv"
DOWNLOAD_STATUS_PATH = DATA_DIR / "reference" / "last_lqd_download.json"
REQUIREMENTS_PATH = ROOT / "requirements.txt"


def _requirement_names() -> list[str]:
    names = []
    for line in REQUIREMENTS_PATH.read_text(encoding="utf-8").splitlines():
        match = re.match(r"\s*([A-Za-z0-9_.-]+)", line)
        if match and not line.lstrip().startswith("#"):
            names.append(match.group(1))
    return names


def _version_key(value: str) -> tuple[tuple[int, int | str], ...]:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.findall(r"\d+|[A-Za-z]+", value)
    )


class DependencyChecker(QThread):
    checked = Signal(object)

    def __init__(self, packages: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.packages = packages

    def run(self) -> None:
        results: dict[str, tuple[str, str]] = {}
        for package in self.packages:
            try:
                installed = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                installed = "Not installed"
            latest = "Unavailable"
            try:
                request = Request(
                    f"https://pypi.org/pypi/{quote(package)}/json",
                    headers={"User-Agent": "Parallax-dependency-check/1.0"},
                )
                with urlopen(request, timeout=8) as response:
                    latest = json.loads(response.read().decode("utf-8"))["info"]["version"]
            except (URLError, TimeoutError, KeyError, ValueError, OSError):
                pass
            results[package] = (installed, latest)
        self.checked.emit(results)


class LauncherWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Parallax")
        self.resize(720, 600)
        self.process: QProcess | None = None
        self.dependency_checker: DependencyChecker | None = None

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(QLabel("LQD research tools"))

        buttons = QGridLayout()
        actions = (
            ("Review issuer mappings", "src.mapping_app"),
            ("Inspect latest holdings", "src.lqd_inspector"),
            ("Refresh issuer candidates", "src.resolve_lqd_tickers"),
            ("Build bond snapshot", "src.build_lqd_snapshot"),
            ("Download TRACE history", "src.trace_history_app"),
            ("Inspect TRACE history", "src.trace_history_inspector"),
        )
        self.buttons: list[QPushButton] = []
        for index, (label, module) in enumerate(actions):
            button = QPushButton(label)
            button.clicked.connect(lambda checked=False, name=module: self._launch(name))
            self.buttons.append(button)
            buttons.addWidget(button, index // 2, index % 2)
        layout.addLayout(buttons)

        self.status = QLabel("Ready")
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status)

        self.tabs = QTabWidget()
        self.diagnostics = QTextEdit()
        self.diagnostics.setReadOnly(True)
        diagnostics_page = QWidget()
        diagnostics_layout = QVBoxLayout(diagnostics_page)
        refresh_diagnostics = QPushButton("Refresh diagnostics")
        refresh_diagnostics.clicked.connect(self._refresh_diagnostics)
        diagnostics_layout.addWidget(refresh_diagnostics, 0, Qt.AlignmentFlag.AlignLeft)
        diagnostics_layout.addWidget(self.diagnostics)
        self.tabs.addTab(diagnostics_page, "Diagnostics")

        dependencies_page = QWidget()
        dependencies_layout = QVBoxLayout(dependencies_page)
        self.dependencies_table = QTableWidget(0, 4)
        self.dependencies_table.setHorizontalHeaderLabels(
            ["Dependency", "Installed", "Latest on PyPI", "Status"]
        )
        for column in range(3):
            self.dependencies_table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        self.dependencies_table.horizontalHeader().setStretchLastSection(True)
        self.dependencies_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        dependencies_layout.addWidget(self.dependencies_table)
        self.dependencies_status = QLabel("Latest versions have not been checked.")
        dependencies_layout.addWidget(self.dependencies_status)
        check_dependencies = QPushButton("Check latest versions")
        check_dependencies.clicked.connect(self._check_dependencies)
        dependencies_layout.addWidget(check_dependencies, 0, Qt.AlignmentFlag.AlignLeft)
        self.tabs.addTab(dependencies_page, "Dependencies")

        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.output.setPlaceholderText("Task output will appear here.")
        self.tabs.addTab(self.output, "Task output")
        layout.addWidget(self.tabs, 1)

        self.setCentralWidget(central)
        self._refresh_diagnostics()
        self._populate_dependencies()

    def _refresh_diagnostics(self) -> None:
        snapshots = sorted(SNAPSHOTS_DIR.rglob("*.csv")) if SNAPSHOTS_DIR.exists() else []
        versions = sorted(VERSIONS_DIR.rglob("*.csv")) if VERSIONS_DIR.exists() else []
        snapshot_path = SNAPSHOTS_DIR / "lqd_bonds.csv"
        source_date = "Not available"
        if snapshot_path.exists():
            try:
                with snapshot_path.open("r", encoding="utf-8-sig", newline="") as handle:
                    source_date = next(csv.DictReader(handle), {}).get("source_as_of_date") or "Not available"
            except (OSError, csv.Error):
                pass

        download = {}
        if DOWNLOAD_STATUS_PATH.exists():
            try:
                download = json.loads(DOWNLOAD_STATUS_PATH.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                download = {}

        lines = ["Most recent successful holdings download"]
        if download:
            lines.extend(
                [
                    f"  Downloaded (UTC): {download.get('downloaded_at_utc', 'Unknown')}",
                    f"  Holdings source date: {download.get('source_as_of_date') or 'Unavailable'}",
                    f"  Rows returned: {download.get('rows_returned', 'Unknown')}",
                    f"  Rows with ISIN: {download.get('rows_with_isin', 'Unknown')}",
                ]
            )
        else:
            lines.append("  No successful download recorded yet.")
        lines.extend(
            [
                "",
                "Local data files",
                f"  Snapshot files: {len(snapshots)}",
                f"  Current snapshot: {snapshot_path.name if snapshot_path.exists() else 'Not built'}",
                f"  Snapshot holdings date: {source_date}",
                f"  Mapping versions: {len(versions)}",
                f"  Current map: {'Available' if MAPPINGS_PATH.exists() else 'Not found'}",
            ]
        )
        if snapshot_path.exists():
            modified = snapshot_path.stat().st_mtime
            last_built = datetime.fromtimestamp(modified).astimezone().isoformat(timespec="minutes")
            lines.append(f"  Snapshot last built: {last_built}")
        self.diagnostics.setPlainText("\n".join(lines))

    def _populate_dependencies(self) -> None:
        packages = _requirement_names()
        self.dependencies_table.setRowCount(len(packages))
        for row, package in enumerate(packages):
            try:
                installed = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                installed = "Not installed"
            self._set_dependency_row(row, package, installed, "Not checked", "")

    def _set_dependency_row(self, row: int, package: str, installed: str, latest: str, status: str) -> None:
        for column, value in enumerate((package, installed, latest, status)):
            self.dependencies_table.setItem(row, column, QTableWidgetItem(value))

    def _check_dependencies(self) -> None:
        if self.dependency_checker and self.dependency_checker.isRunning():
            return
        packages = _requirement_names()
        self.dependencies_status.setText("Checking PyPI...")
        self.dependency_checker = DependencyChecker(packages, self)
        self.dependency_checker.checked.connect(self._show_dependency_results)
        self.dependency_checker.start()

    def _show_dependency_results(self, results: dict[str, tuple[str, str]]) -> None:
        for row in range(self.dependencies_table.rowCount()):
            package_item = self.dependencies_table.item(row, 0)
            if not package_item:
                continue
            package = package_item.text()
            installed, latest = results.get(package, ("Not installed", "Unavailable"))
            if latest == "Unavailable":
                status = "Check failed"
            elif installed == "Not installed":
                status = "Install available"
            elif _version_key(latest) > _version_key(installed):
                status = "Update available"
            else:
                status = "Up to date"
            self._set_dependency_row(row, package, installed, latest, status)
        self.dependencies_status.setText("Version check complete. Latest versions are read from PyPI.")

    def _launch(self, module: str) -> None:
        if self.process and self.process.state() != QProcess.ProcessState.NotRunning:
            return

        environment = QProcessEnvironment.systemEnvironment()
        if module == "src.resolve_lqd_tickers":
            user_agent = os.environ.get("SEC_USER_AGENT", "")
            if "@" not in user_agent:
                user_agent, accepted = QInputDialog.getText(
                    self,
                    "SEC contact email",
                    "Enter a project identifier with a real contact email:",
                )
                if not accepted:
                    return
                user_agent = f"Parallax Observatory research {user_agent.strip()}"
            if "@" not in user_agent:
                QMessageBox.warning(self, "Email required", "Enter a contact email for SEC requests.")
                return
            environment.insert("SEC_USER_AGENT", user_agent)

        self.output.clear()
        self.tabs.setCurrentWidget(self.output)
        self.status.setText(f"Running: {module}")
        for button in self.buttons:
            button.setEnabled(False)

        process = QProcess(self)
        process.setWorkingDirectory(str(ROOT))
        process.setProcessEnvironment(environment)
        process.setProgram(sys.executable)
        process.setArguments(["-m", module])
        process.readyReadStandardOutput.connect(self._read_output)
        process.readyReadStandardError.connect(self._read_output)
        process.finished.connect(self._finished)
        process.errorOccurred.connect(self._process_error)
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

    def _finished(self, exit_code: int, _exit_status: QProcess.ExitStatus) -> None:
        self._read_output()
        self.status.setText("Finished" if exit_code == 0 else f"Finished with errors (exit code {exit_code})")
        for button in self.buttons:
            button.setEnabled(True)
        self._refresh_diagnostics()

    def _process_error(self, _error: QProcess.ProcessError) -> None:
        if self.process and self.process.error() == QProcess.ProcessError.FailedToStart:
            self.status.setText("Could not start Python. Check the active uv environment.")
            for button in self.buttons:
                button.setEnabled(True)


def main() -> None:
    app = QApplication(sys.argv)
    window = LauncherWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
