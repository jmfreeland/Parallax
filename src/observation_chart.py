"""A compact scatter chart for sparse TRACE price and yield observations."""

from __future__ import annotations

from datetime import date
from typing import Any

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget


class ObservationChart(QWidget):
    """Draw clean price and yield points without interpolating between trades."""

    BACKGROUND = QColor("#0b1220")
    TEXT = QColor("#e2e8f0")
    MUTED = QColor("#94a3b8")
    GRID = QColor("#334155")
    BORDER = QColor("#475569")
    PRICE = QColor("#38bdf8")
    YIELD = QColor("#fbbf24")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.isin = ""
        self.as_of_date: date | None = None
        self.observations: list[dict[str, Any]] = []
        self.message = "Select a bond to view its TRACE observations."
        self.setMinimumWidth(390)
        self.setMinimumHeight(520)

    def set_data(
        self,
        isin: str,
        as_of_date: date,
        observations: list[dict[str, Any]],
    ) -> None:
        self.isin = isin
        self.as_of_date = as_of_date
        self.observations = observations
        self.message = "No valid TRACE observations in this window."
        self.update()

    def set_message(self, message: str) -> None:
        self.observations = []
        self.message = message
        self.update()

    def paintEvent(self, _event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), self.BACKGROUND)

        title_font = QFont(painter.font())
        title_font.setBold(True)
        title_font.setPointSize(title_font.pointSize() + 1)
        painter.setFont(title_font)
        title = f"{self.isin} | {self.as_of_date.isoformat()}" if self.as_of_date else "TRACE observations"
        painter.drawText(QRectF(12, 8, self.width() - 24, 24), Qt.AlignmentFlag.AlignLeft, title)

        painter.setFont(QFont())
        painter.setPen(self.MUTED)
        painter.drawText(
            QRectF(12, 30, self.width() - 24, 34),
            Qt.TextFlag.TextWordWrap,
            "Clean TRACE observations from T-90 to T+90. Points are shown without interpolation; "
            "post-date points are context only and are not used to form the portfolio.",
        )
        if not self.observations:
            painter.drawText(
                QRectF(20, 90, self.width() - 40, 60),
                Qt.AlignmentFlag.AlignCenter,
                self.message,
            )
            painter.end()
            return

        panel_top = 76.0
        panel_gap = 44.0
        panel_height = max(150.0, (self.height() - panel_top - panel_gap - 24.0) / 2.0)
        panels = (
            ("Clean price (per 100 par)", "clean_price", self.PRICE, panel_top),
            ("Last sale yield (%)", "clean_yield_pct", self.YIELD, panel_top + panel_height + panel_gap),
        )
        left = 62.0
        right = float(self.width() - 16)
        for panel_title, key, color, top in panels:
            plot = QRectF(left, top + 21.0, max(20.0, right - left), panel_height - 21.0)
            points = [
                (int(item["day_offset"]), float(item[key]))
                for item in self.observations
                if item.get(key) is not None
            ]
            painter.setPen(self.TEXT)
            painter.drawText(QRectF(12, top, self.width() - 24, 20), Qt.AlignmentFlag.AlignLeft, panel_title)
            if not points:
                painter.setPen(self.MUTED)
                painter.drawText(plot, Qt.AlignmentFlag.AlignCenter, "No valid observations in this window")
                continue

            values = [value for _, value in points]
            low, high = min(values), max(values)
            spread = high - low
            padding = spread * 0.12 if spread else max(abs(low) * 0.03, 0.05)
            low -= padding
            high += padding
            plot_width = plot.width()
            plot_height = plot.height()

            for fraction in (0.0, 0.5, 1.0):
                y = plot.top() + fraction * plot_height
                value = high - fraction * (high - low)
                painter.setPen(QPen(self.GRID, 1))
                painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
                painter.setPen(self.MUTED)
                painter.drawText(
                    QRectF(4, y - 9, left - 10, 18),
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    f"{value:.3f}",
                )

            painter.setPen(QPen(self.BORDER, 1))
            painter.drawRect(plot)
            zero_x = plot.left() + plot_width / 2.0
            painter.setPen(QPen(self.MUTED, 1, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(zero_x, plot.top()), QPointF(zero_x, plot.bottom()))

            painter.setPen(QPen(color, 1))
            painter.setBrush(color)
            for day_offset, value in points:
                x = plot.left() + (day_offset + 90.0) / 180.0 * plot_width
                y = plot.bottom() - (value - low) / (high - low) * plot_height
                painter.drawEllipse(QPointF(x, y), 4.0, 4.0)

        bottom_plot = panel_top + panel_height + panel_gap + panel_height - 21.0
        plot_width = max(20.0, right - left)
        for day in (-90, -60, -30, 0, 30, 60, 90):
            x = left + (day + 90.0) / 180.0 * plot_width
            label = "T" if day == 0 else f"T{day:+d}"
            painter.setPen(self.MUTED)
            painter.drawText(
                QRectF(x - 25, bottom_plot + 4, 50, 20),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                label,
            )
        painter.end()
