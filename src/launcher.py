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

from .portfolio_archive import ARCHIVE_DIR, load_archive
from .signal_registry import SIGNALS
from .strategy_run import (
    run_saved_strategy, saved_strategy_info, strategy_cache_path,
    strategy_results_path, stable_strategy_id,
)

from PySide6.QtCore import QProcess, QProcessEnvironment, QThread, Qt, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
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
TASK_HISTORY_DIR = DATA_DIR / "task_history"
STRATEGIES_DIR = DATA_DIR / "strategies"


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


class SavedStrategyRunner(QThread):
    progress = Signal(int, int)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, strategy_path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.strategy_path = strategy_path

    def run(self) -> None:
        try:
            self.completed.emit(
                run_saved_strategy(self.strategy_path, on_progress=self.progress.emit)
            )
        except Exception as error:
            self.failed.emit(str(error))


class LauncherWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Parallax")
        self.resize(1500, 760)
        self.process: QProcess | None = None
        self.dependency_checker: DependencyChecker | None = None
        self.current_task_log: Path | None = None

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(QLabel("LQD research tools"))
        self.buttons: list[QPushButton] = []

        self.status = QLabel("Ready")
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status)

        self.tabs = QTabWidget()
        self.tabs.currentChanged.connect(self._tab_changed)
        security_page = QWidget()
        security_layout = QVBoxLayout(security_page)
        security_actions = QGridLayout()
        security_master_tools = (
            ("Review issuer mappings", "src.mapping_app"),
            ("Inspect latest holdings", "src.lqd_inspector"),
            ("Refresh issuer candidates", "src.resolve_lqd_tickers"),
            ("Build bond snapshot", "src.build_lqd_snapshot"),
        )
        for index, (label, module) in enumerate(security_master_tools):
            button = QPushButton(label)
            button.clicked.connect(lambda checked=False, name=module: self._launch(name))
            self.buttons.append(button)
            security_actions.addWidget(button, index // 2, index % 2)
        security_layout.addLayout(security_actions)
        security_layout.addStretch(1)
        self.tabs.addTab(security_page, "Security Master")

        data_page = QWidget()
        data_layout = QVBoxLayout(data_page)
        data_actions = QGridLayout()
        data_tools = (
            ("Download TRACE history", "src.trace_history_app"),
            ("Update Treasury curve", "src.treasury_curves_app"),
            ("Preprocess TRACE history", "src.trace_preprocessor"),
            ("Clean preprocessed TRACE data", "src.trace_cleaner_app"),
            ("Build G-spread snapshot", "src.spread_snapshot_app"),
            ("Inspect TRACE history", "src.trace_history_inspector"),
            ("Analyze bond return series", "src.bond_return_analysis_app"),
        )
        for index, (label, module) in enumerate(data_tools):
            button = QPushButton(label)
            button.clicked.connect(lambda checked=False, name=module: self._launch(name))
            self.buttons.append(button)
            data_actions.addWidget(button, index // 2, index % 2)
        data_layout.addLayout(data_actions)
        data_layout.addStretch(1)
        self.tabs.addTab(data_page, "Data")

        portfolio_page = QWidget()
        portfolio_layout = QVBoxLayout(portfolio_page)
        portfolio_actions = QGridLayout()
        build_portfolio = QPushButton("Build exploratory portfolio")
        build_portfolio.clicked.connect(lambda: self._launch("src.portfolio_builder_app"))
        self.buttons.append(build_portfolio)
        portfolio_actions.addWidget(build_portfolio, 0, 0)
        refresh_archive = QPushButton("Refresh archive")
        refresh_archive.clicked.connect(self._refresh_portfolio_archive)
        portfolio_actions.addWidget(refresh_archive, 0, 1)
        analyze_portfolio = QPushButton("Analyze archived portfolio")
        analyze_portfolio.clicked.connect(lambda: self._launch("src.portfolio_analytics_app"))
        self.buttons.append(analyze_portfolio)
        portfolio_actions.addWidget(analyze_portfolio, 1, 0)
        fit_curves = QPushButton("Explore Nelson-Siegel curves")
        fit_curves.clicked.connect(lambda: self._launch("src.nelson_siegel_app"))
        self.buttons.append(fit_curves)
        portfolio_actions.addWidget(fit_curves, 1, 1)
        portfolio_layout.addLayout(portfolio_actions)
        self.portfolio_archive_table = QTableWidget(0, 5)
        self.portfolio_archive_table.setHorizontalHeaderLabels(
            ["Created", "As of", "Weighting", "Positions", "Archive file"]
        )
        self.portfolio_archive_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.portfolio_archive_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.portfolio_archive_table.horizontalHeader().setStretchLastSection(True)
        self.portfolio_archive_table.currentCellChanged.connect(self._show_portfolio_archive)
        portfolio_layout.addWidget(self.portfolio_archive_table, 2)
        self.portfolio_archive_details = QTextEdit()
        self.portfolio_archive_details.setReadOnly(True)
        self.portfolio_archive_details.setPlaceholderText("Select an archived portfolio to inspect its methodology and positions.")
        portfolio_layout.addWidget(self.portfolio_archive_details, 3)
        self.tabs.addTab(portfolio_page, "Portfolio tooling")

        signal_page = QWidget()
        signal_layout = QVBoxLayout(signal_page)
        signal_layout.addWidget(QLabel(
            "Signal inventory and research status. Prototype signals require walk-forward validation before interpretation as investment alpha."
        ))
        signal_actions = QHBoxLayout()
        evaluate_signal = QPushButton("Evaluate signal components")
        evaluate_signal.setToolTip("Run walk-forward evaluation for an individual signal component.")
        evaluate_signal.clicked.connect(lambda: self._launch("src.signal_evaluation_app"))
        self.buttons.append(evaluate_signal)
        signal_actions.addWidget(evaluate_signal)
        signal_actions.addStretch(1)
        signal_layout.addLayout(signal_actions)
        self.signal_table = QTableWidget(0, 6)
        self.signal_table.setHorizontalHeaderLabels(
            ["Signal", "Family", "Construction", "Inputs", "Status", "Validation"]
        )
        self.signal_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.signal_table.setWordWrap(True)
        self.signal_table.verticalHeader().setVisible(False)
        self.signal_table.verticalHeader().setDefaultSectionSize(68)
        self.signal_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.signal_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.signal_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        self.signal_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        self.signal_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.signal_table.setColumnWidth(2, 430)
        self.signal_table.setColumnWidth(3, 360)
        self.signal_table.setColumnWidth(5, 300)
        self.signal_table.horizontalHeader().setStretchLastSection(True)
        self.signal_table.setRowCount(len(SIGNALS))
        for row, signal in enumerate(SIGNALS):
            for column, value in enumerate((signal.name, signal.family, signal.construction, signal.inputs, signal.status, signal.validation)):
                item = QTableWidgetItem(value)
                if column in {2, 3, 5}:
                    item.setToolTip(value)
                self.signal_table.setItem(row, column, item)
            self.signal_table.setRowHeight(row, 68)
        signal_layout.addWidget(self.signal_table, 1)
        self.tabs.addTab(signal_page, "Signals")

        strategy_page = QWidget()
        self.strategy_page = strategy_page
        strategy_layout = QVBoxLayout(strategy_page)
        strategy_layout.addWidget(QLabel(
            "Select a saved strategy to edit its graph, calculate cached signal inputs, or run the current combination."
        ))
        strategy_list_tools = QHBoxLayout()
        strategy_list_tools.addWidget(QLabel("Saved strategies"))
        strategy_list_tools.addStretch(1)
        refresh_strategies = QPushButton("Refresh list")
        refresh_strategies.clicked.connect(self._refresh_strategies)
        strategy_list_tools.addWidget(refresh_strategies)
        strategy_layout.addLayout(strategy_list_tools)
        self.strategy_list = QListWidget()
        self.strategy_list.currentItemChanged.connect(self._strategy_selection_changed)
        self.strategy_list.itemDoubleClicked.connect(lambda _item: self._launch_selected_strategy("edit"))
        strategy_layout.addWidget(self.strategy_list, 1)
        self.strategy_run_progress = QProgressBar()
        self.strategy_run_progress.setVisible(False)
        strategy_layout.addWidget(self.strategy_run_progress)
        strategy_actions = QHBoxLayout()
        self.edit_selected_strategy = QPushButton("Edit")
        self.calculate_selected_strategy = QPushButton("Calculate inputs")
        self.run_selected_strategy = QPushButton("Run")
        self.clear_strategy_cache = QPushButton("Clear cache")
        self.create_strategy = QPushButton("New strategy")
        self.edit_selected_strategy.clicked.connect(lambda: self._launch_selected_strategy("edit"))
        self.calculate_selected_strategy.clicked.connect(lambda: self._launch_selected_strategy("calculate"))
        self.run_selected_strategy.clicked.connect(self._run_selected_strategy)
        self.clear_strategy_cache.clicked.connect(self._clear_selected_strategy_cache)
        self.create_strategy.clicked.connect(lambda: self._launch("src.strategy_editor_app"))
        for button in (self.edit_selected_strategy, self.calculate_selected_strategy,
                       self.run_selected_strategy, self.clear_strategy_cache, self.create_strategy):
            self.buttons.append(button)
            strategy_actions.addWidget(button)
        strategy_layout.addLayout(strategy_actions)
        self.tabs.addTab(strategy_page, "Strategies")
        self._refresh_strategies()

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

        history_page = QWidget()
        history_layout = QVBoxLayout(history_page)
        self.history_selector = QComboBox()
        self.history_selector.currentIndexChanged.connect(self._show_selected_task)
        history_layout.addWidget(self.history_selector)
        self.history_output = QTextEdit()
        self.history_output.setReadOnly(True)
        self.history_output.setPlaceholderText("Completed task outputs will be kept here.")
        history_layout.addWidget(self.history_output, 1)
        self.tabs.addTab(history_page, "Task history")
        layout.addWidget(self.tabs, 1)

        self.setCentralWidget(central)
        self._load_task_history()
        self._refresh_diagnostics()
        self._populate_dependencies()
        self._refresh_portfolio_archive()

    def _refresh_portfolio_archive(self) -> None:
        records = load_archive()
        table = self.portfolio_archive_table
        table.setRowCount(len(records))
        for row, record in enumerate(records):
            methodology = record.get("methodology", {})
            values = (
                record.get("created_at", ""),
                record.get("as_of_date", ""),
                methodology.get("weighting_method", ""),
                str(methodology.get("position_count", len(record.get("positions", [])))),
                Path(record.get("_path", "")).name,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setData(Qt.ItemDataRole.UserRole, record)
                table.setItem(row, column, item)
        table.resizeColumnsToContents()
        if records:
            table.setCurrentCell(0, 0)
        else:
            self.portfolio_archive_details.setPlainText(f"No archived portfolios yet.\nArchive folder: {ARCHIVE_DIR}")

    def _show_portfolio_archive(
        self, row: int, _column: int, _previous_row: int, _previous_column: int
    ) -> None:
        if row < 0:
            return
        item = self.portfolio_archive_table.item(row, 0)
        if item is None:
            return
        record = item.data(Qt.ItemDataRole.UserRole)
        self.portfolio_archive_details.setPlainText(
            json.dumps({key: value for key, value in record.items() if key != "_path"}, indent=2, ensure_ascii=False)
            + f"\n\nArchive file: {record.get('_path', '')}"
        )

    def _load_task_history(self) -> None:
        TASK_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        paths = sorted(TASK_HISTORY_DIR.glob("*.log"), reverse=True)
        self.history_selector.blockSignals(True)
        self.history_selector.clear()
        for path in paths:
            self.history_selector.addItem(self._task_history_label(path), str(path))
        self.history_selector.blockSignals(False)
        if paths:
            self.history_selector.setCurrentIndex(0)
            self._show_selected_task(0)

    @staticmethod
    def _task_history_label(path: Path) -> str:
        try:
            with path.open("rb") as handle:
                header = handle.read(2048).decode("utf-8", errors="replace").splitlines()[:3]
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 2048))
                tail = handle.read().decode("utf-8", errors="replace").splitlines()
        except OSError:
            header = []
            tail = []
        task = next(
            (line.removeprefix("Task: ") for line in header if line.startswith("Task: ")),
            path.stem,
        )
        started = next(
            (line.removeprefix("Started: ") for line in header if line.startswith("Started: ")),
            "",
        )
        results = [
            line.removeprefix("Result: ")
            for line in (*header, *tail)
            if line.startswith("Result: ")
        ]
        finished = results[-1] if results else "running"
        return f"{started} | {task} | {finished}"

    def _show_selected_task(self, index: int) -> None:
        if index < 0:
            self.history_output.clear()
            return
        path = Path(self.history_selector.itemData(index))
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            content = f"Could not read task output: {error}"
        self.history_output.setPlainText(content)

    def _update_current_history(self) -> None:
        if self.current_task_log is None:
            return
        for index in range(self.history_selector.count()):
            if Path(self.history_selector.itemData(index)) == self.current_task_log:
                self.history_selector.setItemText(index, self._task_history_label(self.current_task_log))
                if self.history_selector.currentIndex() == index:
                    self._show_selected_task(index)
                break

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

    def _refresh_strategies(self) -> None:
        if not hasattr(self, "strategy_list"):
            return
        current_path = self._selected_strategy_path()
        self.strategy_list.clear()
        STRATEGIES_DIR.mkdir(parents=True, exist_ok=True)
        files = sorted(STRATEGIES_DIR.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
        for path in files:
            try:
                info = saved_strategy_info(path)
                name = str(info["name"] or path.stem)
                item = QListWidgetItem(f"{name}  ·  Inputs: {info['cache_status']}  ·  {path.name}")
                item.setData(Qt.ItemDataRole.UserRole, str(path.resolve()))
                item.setToolTip(
                    f"{path.resolve()}\nSignals: {info['signal_count']}\n"
                    f"Cached score rows: {info['cached_rows']:,}\nCache: {info['cache_path']}"
                )
                self.strategy_list.addItem(item)
                if current_path and Path(current_path).resolve() == path.resolve():
                    self.strategy_list.setCurrentItem(item)
            except (OSError, json.JSONDecodeError, TypeError, ValueError, KeyError):
                continue
        if self.strategy_list.count() and self.strategy_list.currentRow() < 0:
            self.strategy_list.setCurrentRow(0)
        self._strategy_selection_changed()

    def _selected_strategy_path(self) -> str | None:
        if not hasattr(self, "strategy_list") or self.strategy_list.currentItem() is None:
            return None
        value = self.strategy_list.currentItem().data(Qt.ItemDataRole.UserRole)
        return str(value) if value else None

    def _strategy_selection_changed(self, *_args: object) -> None:
        selected = self._selected_strategy_path() is not None
        for button in (getattr(self, "edit_selected_strategy", None),
                       getattr(self, "calculate_selected_strategy", None),
                       getattr(self, "run_selected_strategy", None),
                       getattr(self, "clear_strategy_cache", None)):
            if button is not None:
                button.setEnabled(selected)

    def _tab_changed(self, _index: int) -> None:
        if hasattr(self, "strategy_page") and self.tabs.currentWidget() is self.strategy_page:
            self._refresh_strategies()

    def _launch_selected_strategy(self, action: str) -> None:
        path = self._selected_strategy_path()
        if not path:
            return
        self._launch(
            "src.strategy_editor_app",
            ["--strategy", path, "--action", action],
        )

    def _run_selected_strategy(self) -> None:
        path = self._selected_strategy_path()
        if not path:
            return
        if hasattr(self, "strategy_runner") and self.strategy_runner.isRunning():
            return
        self.strategy_run_progress.setRange(0, 0)
        self.strategy_run_progress.setFormat("Running strategy…")
        self.strategy_run_progress.setVisible(True)
        self.status.setText(f"Calculating strategy: {Path(path).name}")
        self.strategy_runner = SavedStrategyRunner(Path(path), self)
        self.strategy_runner.progress.connect(self._strategy_run_progress)
        self.strategy_runner.completed.connect(self._strategy_run_complete)
        self.strategy_runner.failed.connect(self._strategy_run_failed)
        for button in self.buttons:
            button.setEnabled(False)
        self.strategy_runner.start()

    def _strategy_run_progress(self, current: int, total: int) -> None:
        self.strategy_run_progress.setRange(0, max(total, 1))
        self.strategy_run_progress.setFormat("Calculating signal date %v of %m")
        self.strategy_run_progress.setValue(current)

    def _strategy_run_complete(self, result: dict[str, object]) -> None:
        self.strategy_run_progress.setVisible(False)
        for button in self.buttons:
            button.setEnabled(True)
        self._refresh_strategies()
        self._strategy_selection_changed()
        self.status.setText(f"Strategy run complete: {result['strategy_name']} ({result['row_count']:,} bond-date rows)")
        QMessageBox.information(
            self, "Strategy run complete",
            f"Combined {result['row_count']:,} dated bond scores.\nSaved to {result['result_path']}",
        )

    def _strategy_run_failed(self, message: str) -> None:
        self.strategy_run_progress.setVisible(False)
        for button in self.buttons:
            button.setEnabled(True)
        self._strategy_selection_changed()
        self.status.setText("Strategy run failed")
        QMessageBox.warning(
            self, "Could not run strategy",
            f"{message}\n\nIf inputs are missing or stale, choose Calculate inputs first.",
        )

    def _clear_selected_strategy_cache(self) -> None:
        path = self._selected_strategy_path()
        if not path:
            return
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            strategy_id = stable_strategy_id(Path(path), str(payload.get("strategy_id", "")))
        except (OSError, json.JSONDecodeError, TypeError) as error:
            QMessageBox.warning(self, "Could not read strategy", str(error))
            return
        cache_path = strategy_cache_path(strategy_id)
        result_path = strategy_results_path(strategy_id)
        existing = [item for item in (cache_path, result_path) if item.is_file()]
        if not existing:
            QMessageBox.information(self, "No cache", "This strategy has no cached inputs or saved run result.")
            return
        answer = QMessageBox.question(
            self, "Clear strategy cache?",
            f"Delete cached signal inputs and the last combined result for {payload.get('name', Path(path).stem)}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        for item in existing:
            item.unlink()
        self._refresh_strategies()
        self.status.setText("Strategy cache cleared")

    def _launch(self, module: str, arguments: list[str] | None = None) -> None:
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

        TASK_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        started = datetime.now().astimezone().isoformat(timespec="seconds")
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S_%f")
        safe_module = re.sub(r"[^A-Za-z0-9_-]+", "_", module)
        self.current_task_log = TASK_HISTORY_DIR / f"{stamp}_{safe_module}.log"
        self.current_task_log.write_text(
            f"Task: {module}\nStarted: {started}\nResult: running\n\n",
            encoding="utf-8",
        )
        self.history_selector.insertItem(
            0, self._task_history_label(self.current_task_log), str(self.current_task_log)
        )
        self.history_selector.setCurrentIndex(0)
        self.output.clear()
        self.tabs.setCurrentWidget(self.output)
        self.status.setText(f"Running: {module}")
        for button in self.buttons:
            button.setEnabled(False)

        process = QProcess(self)
        process.setWorkingDirectory(str(ROOT))
        process.setProcessEnvironment(environment)
        process.setProgram(sys.executable)
        process.setArguments(["-m", module, *(arguments or [])])
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
            if self.current_task_log is not None:
                with self.current_task_log.open("a", encoding="utf-8") as handle:
                    handle.write(text)
                self._update_current_history()

    def _finished(self, exit_code: int, _exit_status: QProcess.ExitStatus) -> None:
        self._read_output()
        if self.current_task_log is not None:
            result = "success" if exit_code == 0 else f"exit code {exit_code}"
            with self.current_task_log.open("a", encoding="utf-8") as handle:
                handle.write(f"\nResult: {result}\n")
            self._update_current_history()
        self.status.setText("Finished" if exit_code == 0 else f"Finished with errors (exit code {exit_code})")
        for button in self.buttons:
            button.setEnabled(True)
        self._refresh_strategies()
        self._strategy_selection_changed()
        self._refresh_diagnostics()

    def _process_error(self, _error: QProcess.ProcessError) -> None:
        if self.process and self.process.error() == QProcess.ProcessError.FailedToStart:
            if self.current_task_log is not None:
                with self.current_task_log.open("a", encoding="utf-8") as handle:
                    handle.write("\nResult: failed to start Python\n")
                self._update_current_history()
            self.status.setText("Could not start Python. Check the active uv environment.")
            for button in self.buttons:
                button.setEnabled(True)
            self._strategy_selection_changed()


def main() -> None:
    app = QApplication(sys.argv)
    window = LauncherWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
