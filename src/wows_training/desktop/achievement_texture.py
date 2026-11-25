"""The folded medal's printed stock, foil hatch and pointer-lit insignia."""
from math import sqrt

from PySide6.QtCore import QLineF, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QRadialGradient

from .achievement_expanded_art import paint_rank_insignia, paint_rank_stock
from .achievement_rarity import RarityStyle


def _triangle(left, base, width, altitude):
    path = QPainterPath(QPointF(left, base))
    path.lineTo(left + width, base)
    path.lineTo(left + width * .5, base + altitude)
    path.closeSubpath()
    return path


def _hatch(painter, triangle, color, spacing, phase):
    bounds = triangle.boundingRect()
    painter.save()
    painter.setClipPath(triangle, Qt.ClipOperation.IntersectClip)
    painter.setPen(QPen(color, 1.1))
    x = bounds.left() - bounds.height() + phase % spacing
    lines = []
    while x <= bounds.right():
        lines.append(QLineF(x, bounds.bottom(), x + bounds.height(), bounds.top()))
        x += spacing
    painter.drawLines(lines)
    painter.restore()


def paint_achievement_texture(
    painter: QPainter, face: QRectF, outline: QPainterPath, style: RarityStyle,
    progress: float, pointer: QPointF | None, *, phase: float = 0.,
):
    """Overlay an already filled face, using the same material and insignia as its open state."""
    if face.isEmpty():
        return
    width, height = face.width(), face.height()
    progress = max(0., min(1., progress))
    aim = pointer if pointer is not None else face.center()
    u = max(0., min(1., (aim.x() - face.left()) / width))
    v = max(0., min(1., (aim.y() - face.top()) / height))
    parallax = (4 + style.rank * 1.25) * progress
    dx, dy = (2 * u - 1) * parallax, (2 * v - 1) * parallax * .65

    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setClipPath(outline, Qt.ClipOperation.IntersectClip)
    painter.translate(face.topLeft())
    bounds = QRectF(0, 0, width, height)
    paint_rank_stock(painter, bounds, style.rank)

    # Folded tinted triangular sheets keep the gallery's pale, layered finish.
    side = min(width * .48, height * 1.09, 195.)
    altitude = side * sqrt(3) / 2
    joint = width * .77 + dx * .58
    seam = height * .52 + dy * .58
    accent = QColor(style.accent)
    accent.setAlpha(40 + round(14 * progress))
    ink = QColor(style.ink)
    ink.setAlpha(25)
    painter.fillPath(_triangle(joint - side / 2, seam - altitude, side, altitude), ink)
    painter.fillPath(_triangle(joint - side / 2, seam + altitude, side, -altitude), accent)
    painter.fillPath(_triangle(joint - side, seam, side, altitude), QColor(255, 255, 255, 74))
    near = _triangle(width * .805 + dx, height * .77 + dy, side * .65, -altitude * .65)
    spacing = max(4.5, min(7., side / 20))
    _hatch(painter, near, QColor(255, 255, 255, 124), spacing, dx * .32)

    if progress:
        light = QRadialGradient(QPointF(u * width, v * height), min(width * .47, height * 1.22))
        light.setColorAt(0, QColor(255, 255, 255, round(100 * progress)))
        accent.setAlpha(round(28 * progress))
        light.setColorAt(.56, accent)
        accent.setAlpha(0)
        light.setColorAt(1, accent)
        painter.fillRect(bounds, light)

    # A ghosted stamping stays light behind wrapped text even during a hover.
    radius = min(49., width * .155, height * .33)
    center = QPointF(width - radius - 9 + dx * .25, height * .54 + dy * .5)
    painter.setOpacity(painter.opacity() * (.17 + .035 * style.rank + .12 * progress))
    paint_rank_insignia(painter, center, radius, style.rank, progress, phase % 1.)
    painter.restore()
