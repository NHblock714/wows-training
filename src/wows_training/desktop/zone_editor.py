"""Draw user-defined tactical polygons over the verified replay map."""
from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from .replay_panel import _MapCanvas
from wows_training.contracts import parse_version


class ZoneCanvas(_MapCanvas):
    points_changed = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.points = []
        self.setObjectName("zoneCanvas")

    def set_report(self, report):
        try:
            supported = parse_version(report.get("replay_version") or "") == (15, 9, 0, 13357625)
        except (TypeError, ValueError):
            supported = False
        self.configure(report, {}, supported)
        self.tracks = {}
        self.points = []
        self.points_changed.emit([])
        self.update()

    def clear_points(self):
        self.points = []
        self.points_changed.emit([])
        self.update()

    def undo_point(self):
        if self.points:
            self.points.pop()
            self.points_changed.emit(list(self.points))
            self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.projection is not None:
            canvas = self._canvas_rect()
            if canvas.contains(event.position()):
                bounds = self.projection["viewport_bounds_engine"]
                x = bounds["min_x"] + (event.position().x()-canvas.left())/canvas.width()*(bounds["max_x"]-bounds["min_x"])
                z = bounds["max_z"] - (event.position().y()-canvas.top())/canvas.height()*(bounds["max_z"]-bounds["min_z"])
                self.points.append([round(x, 3), round(z, 3)])
                self.points_changed.emit(list(self.points))
                self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.projection is None or not self.points:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        canvas = self._canvas_rect()
        painter.setClipRect(canvas)
        polygon = QPolygonF([self._world_point(x, z, canvas) for x, z in self.points])
        painter.setPen(QPen(QColor("#f8d17b"), 2))
        painter.setBrush(QColor(248, 209, 123, 45))
        if len(polygon) > 2:
            painter.drawPolygon(polygon)
        else:
            painter.drawPolyline(polygon)
        for i, point in enumerate(polygon):
            painter.drawEllipse(point, 4, 4)
            painter.drawText(point+QPointF(6, -6), str(i+1))
        painter.end()
