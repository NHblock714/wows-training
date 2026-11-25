"""Sculpted naval award at the centre of the gallery's bearing ring."""
from math import cos, radians, sin

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen, QPolygonF


def _polygon(points):
    path = QPainterPath()
    path.addPolygon(QPolygonF([QPointF(x, y) for x, y in points]))
    path.closeSubpath()
    return path


def _metal(x1, y1, x2, y2, stops):
    gradient = QLinearGradient(x1, y1, x2, y2)
    for position, color in stops:
        gradient.setColorAt(position, QColor(color))
    return gradient


def _stroke(painter, path, color, width):
    painter.setPen(QPen(QColor(color), width, Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPath(path)


def _ribbons(painter, foil, edge, earned):
    cloth = _metal(-18, 15, 22, 38, (
        (0, "#31566A"), (.30, "#20465D"), (.49, "#648593"), (.62, "#244A60"), (1, "#17374C"),
    ))
    for sign in (-1, 1):
        painter.save()
        painter.scale(sign, 1)
        ribbon = _polygon([(4, 14), (20, 11), (25, 27), (18, 26), (16, 37), (5, 29)])
        painter.fillPath(ribbon, cloth)
        _stroke(painter, ribbon, "#112939", 1)
        painter.fillPath(_polygon([(13, 18), (16, 18), (21, 26), (18, 26), (16, 31)]), foil)
        painter.fillPath(_polygon([(6, 19), (8, 20), (12, 33), (10, 32)]), QColor(edge))
        stitch = QPainterPath(QPointF(7, 28))
        stitch.lineTo(15, 34)
        _stroke(painter, stitch, "#86ABB7" if earned else "#7D919F", .65)
        painter.restore()


# The domed shield has broad, upward-facing shoulders and a pointed keel.
_PROFILE = ((0, -32), (15, -27), (27, -26), (30, -17), (27, 1),
            (21, 11), (12, 20), (0, 28), (-12, 20), (-21, 11),
            (-27, 1), (-30, -17), (-27, -26), (-15, -27))


def _shield(scale):
    return [(x * scale, -3 + (y + 3) * scale) for x, y in _PROFILE]


def paint_gallery_seal(painter: QPainter, center: QPointF, earned: bool, progress: float = 0.):
    """Paint inside a 42 px radius in the caller's coordinate system."""
    painter.save()
    painter.translate(center)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    if earned:
        deep, dark, middle, light, pearl = "#63432A", "#8C602F", "#D4A751", "#F8D98D", "#FFF2BB"
        glass = ("#466776", "#254756", "#162E3E", "#355663")
    else:
        deep, dark, middle, light, pearl = "#374959", "#536B7E", "#8EA7B6", "#BBD2DC", "#DFEDF0"
        glass = ("#465B68", "#304350", "#223440", "#405562")
    foil = _metal(-22, -27, 22, 29, (
        (0, pearl), (.24, light), (.40, middle), (.52, pearl), (.67, dark), (.81, middle), (1, deep),
    ))
    _ribbons(painter, foil, middle, earned)
    outer = _polygon(_PROFILE)
    painter.fillPath(outer.translated(.7, 2), QColor(7, 20, 31, 192))
    painter.fillPath(outer, foil)

    # Each segment of the die has its own plane and catches the lamp differently.
    inner = _shield(.84)
    planes = (light, middle, dark, deep, dark, middle, light, dark, deep, middle, light, pearl, light, pearl)
    for i, color in enumerate(planes):
        j = (i + 1) % len(_PROFILE)
        painter.fillPath(_polygon([_PROFILE[i], _PROFILE[j], inner[j], inner[i]]), QColor(color))
    _stroke(painter, outer, dark, .85)
    _stroke(painter, _polygon(_shield(.94)), light, .65)
    inset = _polygon(inner)
    painter.fillPath(inset, _metal(-18, -23, 19, 23, (
        (0, glass[0]), (.38, glass[1]), (.66, glass[2]), (1, glass[3]),
    )))
    _stroke(painter, inset, deep, 1.8)
    _stroke(painter, _polygon(_shield(.785)), light, .75)

    # Short engraved rays surround the central device without crossing the check.
    painter.setPen(QPen(QColor(middle), .8))
    for angle in range(0, 360, 15):
        a = radians(angle)
        dx, dy = cos(a), sin(a)
        length = 3 if angle % 45 == 0 else 1.5
        painter.drawLine(QPointF(dx * 16, -4 + dy * 16), QPointF(dx * (16 + length), -4 + dy * (16 + length)))
    for sign in (-1, 1):
        stud = QPointF(sign * 23.2, -21)
        painter.setPen(QPen(QColor(deep), .7))
        painter.setBrush(foil)
        painter.drawEllipse(stud, 1.45, 1.45)

    if earned:
        check = _polygon([(-14, -4), (-10.1, -7.8), (-3.6, -1.2), (10, -16), (14.2, -12.5),
                          (-3.3, 7.4)])
        painter.fillPath(check.translated(.6, 1.8), QColor("#081D2C"))
        painter.fillPath(check, foil)
        _stroke(painter, check, light, .72)
        ridge = QPainterPath(QPointF(-12, -4.1))
        ridge.lineTo(-3.5, 4.3)
        ridge.lineTo(12.1, -12.8)
        _stroke(painter, ridge, pearl, 1.0)
    else:
        cut = _polygon([(0, -14), (10, -4), (0, 7), (-10, -4)])
        _stroke(painter, cut, middle, 2.0)
        _stroke(painter, _polygon([(0, -10), (6, -4), (0, 2), (-6, -4)]), dark, .8)

    progress = max(0., min(1., progress))
    if earned and 0. < progress < 1.:
        # A single reflected highlight crosses the enamel during the existing banner reveal.
        x = -48 + 96 * progress
        sheen = QLinearGradient(x - 7, -10, x + 7, -3)
        sheen.setColorAt(0, QColor(255, 248, 210, 0))
        sheen.setColorAt(.48, QColor(255, 248, 210, round(55 * sin(progress * radians(180)))))
        sheen.setColorAt(1, QColor(255, 248, 210, 0))
        painter.fillPath(inset, sheen)
    painter.restore()
