"""Cut-metal achievement mounts, printed stocks and their six naval insignia."""
from functools import lru_cache
from math import cos, radians, sin, tau

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPolygonF, QRadialGradient

from .achievement_rarity import RarityStyle


def expanded_art_width(width):
    """Right margin occupied by the sculpted end, including its text clearance."""
    return round(max(48., min(width * .29, width * .30 - 20)))


def _poly(points):
    return QPolygonF([QPointF(x, y) for x, y in points])


def _path(*points):
    path = QPainterPath()
    path.addPolygon(_poly(points))
    path.closeSubpath()
    return path


def _fill(p, brush, *points):
    p.fillPath(_path(*points), QColor(brush) if isinstance(brush, str) else brush)


def _line(p, ink, weight, *points):
    p.setPen(QPen(QColor(ink), weight))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPolyline(_poly(points))


def _gradient(x1, y1, x2, y2, stops):
    brush = QLinearGradient(x1, y1, x2, y2)
    for t, c in stops:
        brush.setColorAt(t, QColor(c))
    return brush


def _circle(p, x, y, radius, brush, ink=None, width=1):
    p.setBrush(QColor(brush) if isinstance(brush, str) else brush)
    p.setPen(QPen(QColor(ink), width) if ink else Qt.PenStyle.NoPen)
    p.drawEllipse(QPointF(x, y), radius, radius)


_PAPER = (
    ("#ECF2F5", "#F6FAFB", "#CFDCE3", "#7993A3"),
    ("#E0EFE9", "#F2F9F0", "#BBDACD", "#538777"),
    ("#E2F2FF", "#F0F7FC", "#B3D7EB", "#4B86B3"),
    ("#E8E2F6", "#F8F2FD", "#D4C2E9", "#8869A5"),
    ("#EAD9B7", "#FFF8E5", "#E1C28F", "#917147"),
    ("#DDDFEC", "#F2EAF0", "#C6B4C8", "#79536E"),
)


@lru_cache(maxsize=6)
def _grain(rank):
    """Small seamless stock swatch. Paint it as a tiled brush in logical pixels."""
    tile = QImage(96, 72, QImage.Format.Format_ARGB32_Premultiplied)
    tile.fill(Qt.GlobalColor.transparent)
    p = QPainter(tile)
    ink = QColor(_PAPER[rank][3])
    seed = 7919 * (rank + 3)
    # Short parallel fibres keep the imprint continuous at any card width.
    for i in range(104):
        seed = (1664525 * seed + 1013904223) & 0xFFFFFFFF
        x, y = seed % 96, (seed >> 13) % 72
        ink.setAlpha(9 + (i % 5) * 3)
        p.setPen(ink)
        length = 1 + i % (15 if rank == 0 else 5)
        p.drawLine(x, y, x + length, y)
    grid = QColor(_PAPER[rank][3])
    grid.setAlpha(25)
    p.setPen(grid)
    if rank in (1, 2):
        for x in range(0, 96, 24):
            p.drawLine(x, 0, x, 72)
        for y in range(0, 72, 24):
            p.drawLine(0, y, 96, y)
    elif rank == 4:
        for y in range(0, 72, 4):
            p.drawPoint((y * 7) % 96, y)
            p.drawPoint((y * 7 + 48) % 96, y + 2)
    p.end()
    return QBrush(tile)


def paint_rank_stock(p, bounds, rank):
    """Material common to the folded face and its open mounting plate."""
    w, h = bounds.width(), bounds.height()
    base, glint, shadow, outline = _PAPER[rank]
    gradient = _gradient(0, 0, w * .86, h * 1.4, ((0, base), (.43, glint), (.78, base), (1, shadow)))
    p.fillRect(bounds, gradient)
    tint = QColor(outline)
    tint.setAlpha(18)
    if rank in (0, 2, 3, 5):
        # Lamination seams, with the same broad diagonal facets as the folded card.
        for x, span in ((-.18, .41), (.23, .34), (.53, .30), (.78, .24)):
            _fill(p, tint, (w*x, h), (w*(x+span), 0), (w*(x+span*.69), h))
            _line(p, QColor(255,255,255,104), 1, (w*x, h), (w*(x+span), 0))
        if rank == 3:
            _fill(p, QColor(147,107,181,23), (.42*w,0), (.64*w,.65*h), (.73*w,0))
        if rank == 5:
            _line(p, QColor(162,105,139,36), 1.2, (.37*w,h), (.46*w,.63*h), (.53*w,.52*h), (.65*w,.04*h))
    elif rank == 1:
        # Sounding contours on the navigation chart.
        for i in range(7):
            trail = QPainterPath(QPointF(w * .22 + i * 11, h))
            trail.cubicTo(w * .27 + i * 12, h * .62, w * .54 + i * 9, h * .84, w * .62 + i * 10, h * .49)
            trail.cubicTo(w * .67 + i * 8, h * .21, w * .67 + i * 12, h * .31, w * .74 + i * 11, 0)
            p.setPen(QPen(QColor(81,133,114,40), .8))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(trail)
    else:
        # Engraved latitudes run across the vellum, then meet the compass on the right.
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(141,105,55,44), .85))
        for i in range(7):
            orbit = QRectF(w * .12 - i * 13, -h * (1.5 + i*.07), w * .92 + i*19, h * 2.5 + i*8)
            p.drawArc(orbit, 180*16, 180*16)
    p.fillRect(bounds, _grain(rank))


def _shape(w, h, width, rank):
    a = w - width
    if rank == 0:
        outline = _path((13,7),(a+width*.54,7),(w-4,h*.28),(w-4,h*.72),
                        (a+width*.54,h-7),(13,h-7),(1,h-20),(1,20))
        hole = QPainterPath()
        hole.addEllipse(QPointF(w-width*.19,h*.5), min(8.,width*.064), min(8.,width*.064))
        return outline.subtracted(hole)
    if rank == 1:
        return _path((1,18),(16,5),(a+width*.13,5),(a+width*.25,15),(w-width*.27,15),
                     (w-2,h*.44),(w-2,h*.60),(w-width*.29,h-4),(a+width*.16,h-4),
                     (a+width*.09,h-13),(17,h-13),(1,h-27))
    if rank == 2:
        return _path((1,h*.32),(14,7),(a+width*.06,7),(a+width*.17,1),(w-width*.24,1),
                     (w-1,h*.39),(w-1,h*.60),(w-width*.25,h-1),(a+width*.14,h-1),
                     (a+width*.07,h-8),(17,h-8),(1,h*.66))
    if rank == 3:
        return _path((1,h*.46),(16,17),(w*.27,17),(w*.30,7),(a+width*.28,7),
                     (a+width*.42,1),(w-width*.20,1),(w-1,h*.40),(w-width*.06,h*.65),
                     (w-width*.32,h-1),(a+width*.12,h-1),(a+width*.02,h-9),
                     (w*.45,h-9),(w*.41,h-17),(16,h-17),(1,h*.58))
    if rank == 4:
        # Both shoulders use the same collar and the compass has its own cut-metal bezel.
        upper = ((1, 24), (11, 24), (11, 14), (24, 14), (32, 7), (a + width*.17, 7),
                 (a + width*.27, 13), (w - width*.27, 13), (w - width*.085, h*.23), (w - 2, h*.35))
        return _path(*upper, *((x, h - y) for x, y in reversed(upper)))
    # The broken plate ends diagonally. The eclipse is a separate object across the fracture.
    return _path((1,h*.40),(7,20),(22,13),(w*.17,13),(w*.19,7),(w*.46,7),(w*.49,1),
                 (a+width*.55,1),(a+width*.25,h*.27),(a+width*.33,h*.27),
                 (a+width*.04,h*.60),(a+width*.14,h*.58),(a-width*.04,h*.89),
                 (a+width*.09,h*.87),(a+width*.01,h-4),(w*.32,h-4),(w*.29,h-12),
                 (19,h-12),(5,h-20),(5,h*.64),(1,h*.66))


def _steel(p, t, phase):
    # A pressed Admiralty anchor stamped into the tag's fluted steel.
    steel = _gradient(-45,-65,35,60,((0,"#506878"),(.36,"#DDE8EE"),(.48,"#8FA3B1"),(1,"#566A7A")))
    _circle(p,0,-39,11,steel,"#4C626E",2)
    _circle(p,0,-39,5,"#718390")
    form = QPainterPath(QPointF(-4,-30))
    for x,y in ((4,-30),(4,22),(13,31)):
        form.lineTo(x,y)
    form.quadTo(31,27,34,8)
    for x,y in ((26,11),(38,-5),(44,15),(40,12)):
        form.lineTo(x,y)
    form.quadTo(35,34,13,40)
    form.lineTo(0,51)
    form.lineTo(-13,40)
    form.quadTo(-35,34,-40,12)
    for x,y in ((-44,15),(-38,-5),(-26,11),(-34,8)):
        form.lineTo(x,y)
    form.quadTo(-31,27,-13,31)
    form.lineTo(-4,22)
    form.closeSubpath()
    p.fillPath(form,steel)
    _line(p,"#546B7A",4,(-15,-16),(15,-16))
    _line(p,"#EBF6F9",1,(-14,-18),(14,-18))


def _plotter(p, t, phase):
    glass = _gradient(-44,-56,48,52,((0,"#6CA991"),(.35,"#326E63"),(1,"#123B40")))
    panel = _path((-49,-51),(33,-51),(54,-29),(54,31),(31,53),(-49,53))
    p.fillPath(panel,glass)
    p.save()
    p.setClipPath(panel,Qt.ClipOperation.IntersectClip)
    for i in range(13):
        line = QPainterPath(QPointF(-64,25+i*4))
        line.cubicTo(-26,7+i*5,-9,23+i*4,9,-13+i*3)
        line.quadTo(21,-37+i*4,62,-46+i*4)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor("#80BA99"),.8))
        p.drawPath(line)
    for x in (-40,-20,0,20,40):
        _line(p,QColor(197,239,209,51),.7,(x,-57),(x,59))
    _line(p,"#EEF4BD",2.4,(-36,27),(-15,11),(7,13),(28,-22))
    for x,y in ((-36,27),(-15,11),(7,13)):
        _circle(p,x,y,2.6,"#D7EEB7","#31685A",.8)
    _circle(p,28,-22,5+t,"#E9EDB5","#769B73",1.8)
    p.restore()
    _line(p,"#B3D7BC",1.5,(-45,46),(-45,-46),(29,-46))


def _fleet_radar(p, depth, _phase):
    bezel = _gradient(-45, -48, 43, 51, ((0, "#B4D6E6"), (.28, "#4C7792"),
                                              (.63, "#85B4CC"), (1, "#2D4D6F")))
    _circle(p, 0, 0, 52, bezel, "#25445E", 1.4)
    scope = QRadialGradient(QPointF(-12, -9), 54)
    scope.setColorAt(0, QColor("#37718C"))
    scope.setColorAt(1, QColor("#15334D"))
    _circle(p, 0, 0, 45, scope, "#C6EAF0", 1.1)
    for radius in (14, 28, 40):
        _circle(p, 0, 0, radius, Qt.BrushStyle.NoBrush, QColor(154, 220, 235, 93), .7)
    for index in range(16):
        angle = tau * index / 16
        inner = 46 if index % 4 == 0 else 48
        _line(p, "#D1E6EF", .9, (inner*cos(angle), inner*sin(angle)), (50*cos(angle), 50*sin(angle)))
    sweep_angle = 137 - 103 * depth
    sweep = QPainterPath(QPointF())
    sweep.arcTo(QRectF(-43, -43, 86, 86), sweep_angle, 30)
    sweep.closeSubpath()
    p.fillPath(sweep, QColor(144, 230, 241, 35 + round(72 * depth)))

    # Side-on hull and superstructure remain legible when the radar is reduced to an inlay.
    _fill(p, "#A2CADC", (-38, 5), (-29, 4), (-29, 0), (-20, 0), (-18, -7), (-10, -7), (-10, -14),
          (0, -14), (0, -19), (7, -19), (7, -11), (15, -11), (21, -4), (32, -2), (40, -1), (31, 11),
          (-27, 11), (-33, 8))
    _fill(p, "#E3F5F8", (-36, 5), (-23, 5), (-19, 2), (29, 2), (40, -1), (35, 6), (-31, 8))
    _line(p, "#FFFFFF", .8, (4, -19), (4, -30))
    _line(p, "#BCE6EE", .8, (0, -24), (9, -24))
    for left, right, height in ((-26, 24, 17), (-18, 11, 21), (13, 26, 25)):
        _line(p, QColor(169, 220, 236, 150), .85, (left, height), (right, height))


def _lens(p, t, phase):
    angles=[radians(a-90) for a in (0,53,98,145,197,245,290)]
    hull=[(61*cos(a),67*sin(a)) for a in angles]
    rim=[(47*cos(a+.17),51*sin(a+.17)) for a in angles]
    drift=sin(tau*phase)*t*.13
    table=[(27*cos(a+drift),30*sin(a+drift)) for a in angles]
    facets=("#483767","#9A7BC3","#D9BAEE","#A377C9","#6F539A","#4C527F","#B3ABDA")
    for i in range(7):
        j=(i+1)%7
        _fill(p,facets[i],hull[i],hull[j],rim[i])
        _fill(p,facets[(i+4)%7],hull[j],rim[j],rim[i])
        _fill(p,facets[(i+2)%7],rim[i],rim[j],table[i])
        _fill(p,facets[(i+5)%7],rim[j],table[j],table[i])
    p.fillPath(_path(*table),_gradient(-20,-29,22,36,((0,"#EEE4FD"),(.5,"#AF9FD2"),(1,"#6D649D"))))
    for i in (0,3,5):
        _line(p,"#E6D9F7",.9,rim[i],table[i],table[(i+1)%7])


def _compass(p,t,phase):
    metal=_gradient(-46,-52,47,59,((0,"#6A4930"),(.18,"#E6BA68"),(.37,"#FFF0AF"),(.48,"#9E743C"),(.77,"#DFBF7B"),(1,"#785834")))
    _circle(p,0,0,50,metal,"#654934",2)
    _circle(p,0,0,44,"#293F43","#F6E2A4",1.5)
    _circle(p,0,0,34,Qt.BrushStyle.NoBrush,"#92A09A",.8)
    for i in range(48):
        a=tau*i/48
        inner=36 if i%4==0 else 39.5
        _line(p,"#E8D8A8",1.3 if i%4==0 else .8,(inner*cos(a),inner*sin(a)),(42*cos(a),42*sin(a)))
    p.save()
    p.rotate(14+t*9*sin(tau*phase))
    for i in range(8):
        r=39 if i%2==0 else 23
        _fill(p,"#F6DF9B",(0,0),(-5,-6),(0,-r))
        _fill(p,"#9C7950",(0,0),(0,-r),(5,-6))
        p.rotate(45)
    p.restore()
    _circle(p,0,0,4,metal,"#6D5739",1)
    for sign in (-1,1):
        p.save()
        p.scale(sign,1)
        stem=QPainterPath(QPointF(2,65))
        stem.cubicTo(38,57,61,26,59,-22)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor("#A77E43"),2))
        p.drawPath(stem)
        for i in range(8):
            a=radians(12+i*12)
            x,y=58*sin(a),61*cos(a)
            p.save()
            p.translate(x,y)
            p.rotate(-i*10)
            leaf=QPainterPath(QPointF())
            leaf.quadTo(1,-8,11,-12)
            leaf.quadTo(9,-3,0,0)
            p.fillPath(leaf,metal)
            _line(p,"#A68148",.8,(0,0),(7,-8))
            p.restore()
        p.restore()
    _fill(p,metal,(-8,62),(0,57),(8,62),(0,68))


def _shard(p,x,y,scale,tilt):
    p.save()
    p.translate(x,y)
    p.rotate(tilt)
    p.scale(scale,scale)
    for ink, points in (
        ("#F8AAAF",((0,-47),(-16,-5),(1,-12))),
        ("#B94B66",((0,-47),(1,-12),(14,-4))),
        ("#661F49",((-16,-5),(-2,51),(1,-12))),
        ("#9E3559",((1,-12),(-2,51),(14,-4))),
        ("#F6D2B8",((0,-47),(-9,-15),(1,-12))),
    ):
        _fill(p,ink,*points)
    p.restore()


def _eclipse(p,t,phase):
    # The light sits behind the occluder. Shards move across the crescent in opposite directions.
    lift=t*sin(tau*phase)
    afterglow=QRadialGradient(QPointF(-6,-1),61+t*3*(1-cos(tau*phase)))
    for pos,color in ((0,"#E44467"),(.56,"#E44467"),(.69,"#B84768"),(.80,"#793951"),(1,"#25293D")):
        afterglow.setColorAt(pos,QColor(color))
    _circle(p,-6,-1,62,afterglow)
    # Charged filaments sweep away from the lit limb; there is no mechanical dial.
    for i in range(13):
        a = radians(92 + i * 9)
        r = 42 + (i % 3) * 2
        outer = r + 5 + ((i * 7) % 9) + lift * (i % 3)
        _line(p, QColor(255, 167, 151, 97), .55, (-5 + r*cos(a), r*sin(a)),
              (-5 + outer*cos(a+.09), outer*sin(a+.09)))
    body=QRadialGradient(QPointF(11,-15),53)
    body.setColorAt(0,QColor("#372D47"))
    body.setColorAt(1,QColor("#172233"))
    _circle(p,-4,-1,38,"#FFDBAA")
    _circle(p,1,-5+lift*1.7,39,body)
    # Sparse fixed stars on the night glass.
    for x,y,r in ((-39,-31,1),(-57,13,1.1),(34,38,1.2),(19,-46,1),(40,-23,1.5),(-26,46,1)):
        _circle(p,x,y,r,"#F1C8BF")
    _shard(p,4,-2+lift*4,.82,-22)
    _shard(p,-35,32-lift*3,.45,-38)
    _shard(p,39,-30-lift*4,.44,-20)
    _shard(p,31,32+lift*2,.24,21)


_INSIGNIA=(_steel,_plotter,_fleet_radar,_lens,_compass,_eclipse)


def paint_rank_insignia(p, center, radius, rank, depth, phase):
    p.save()
    p.translate(center)
    p.scale(radius/68,radius/68)
    _INSIGNIA[rank](p,depth,phase)
    p.restore()


def _engrave_mount(p, w, h, b, rank):
    """Cast and etched margins are confined to the lip above and below the label area."""
    a = w - b
    if rank == 0:
        _line(p, "#AFBFCA", .9, (10, 23), (19, 14), (a + b*.52, 14))
        _line(p, "#F4F8FA", 1.1, (12, h-22), (18, h-15), (a+b*.49, h-15))
    elif rank == 1:
        for i in range(5):
            x = 18 + 7*i
            _line(p, "#7FA699", 1.2, (x, 11), (x+4, 11))
        _line(p, "#8FAE9D", .9, (59, 11), (a*.70, 11), (a*.70+7, 16), (a, 16))
    elif rank == 2:
        for i in range(20):
            x = 21 + i*7
            _line(p, "#74A1BB", .9, (x, 10), (x-1.8, 17 if i%5 == 0 else 13))
        _line(p, "#6C97B1", 1, (a*.54, 12), (a+b*.04, 12), (a+b*.17, 4))
        for i in range(4):
            x = a+b*.32 + i*5
            _line(p, "#ACD1DC", 1, (x+3, h*.23), (x-3, h*.30))
    elif rank == 3:
        _line(p, "#B298CB", 1, (8, h*.43), (20, 22), (.27*w, 22), (.30*w, 12), (a, 12))
        for i in range(4):
            x = a*.56+10*i
            _fill(p, "#B39AC9", (x-4, 14), (x, 11), (x+4, 14), (x, 17))
    elif rank == 4:
        for sign in (-1, 1):
            p.save()
            if sign == -1:
                p.translate(0, h)
                p.scale(1, -1)
            _line(p, "#B89863", 1, (6, 29), (17, 26), (18, 18), (28, 18), (35, 12), (a, 12))
            for i in range(5):
                x = 27 + 8*i
                _fill(p, "#B49152", (x-5, 18-i*.6), (x, 14-i*.45), (x+7, 13-i*.44), (x+3, 16-i*.5))
            _line(p, "#FBE8B6", 1, (a*.35, 11), (a*.84, 11))
            p.restore()
    else:
        _line(p, "#B77E95", .85, (23, 19), (.17*w, 19), (.19*w, 13), (.32*w, 14))
        _line(p, "#939FB0", .9, (.37*w, h-12), (.58*w, h-12), (.61*w, h-18), (a, h-18))
        for i in range(4):
            x = .53*w + i*11
            _fill(p, "#9D6079", (x, 9), (x+8, 5), (x+12, 5), (x+5, 9))


def _mount(p,w,h,b,rank,t,phase,decorated):
    a=w-b
    colors=("#657D8F","#376D60","#346583","#705984","#947342","#6D3651")
    c=colors[rank]
    if rank==0:
        _fill(p,"#9BADB6",(a+b*.04,7),(a+b*.35,7),(a+b*.19,h-7),(a-b*.02,h-7))
        for i in range(6):
            x=a+b*.08+2.7*i
            _line(p,"#D0DBDB",1,(x,h-10),(x+b*.13,10))
        center=QPointF(a+b*.51,h*.50)
    elif rank==1:
        enamel = _gradient(a,0,w,h,((0,"#91BBA5"),(.48,"#629C8B"),(.53,"#99BDA4"),(1,"#5E877E")))
        _fill(p,enamel,(a+b*.10,3),(a+b*.29,14),(w,14),(w,h),(a,h),(a+b*.13,h*.72))
        _line(p,"#D2E5BC",2,(a+b*.06,h*.93),(a+b*.20,h*.70),(a+b*.17,h*.37),(a+b*.28,18),(w,18))
        center=QPointF(a+b*.55,h*.50)
    elif rank==2:
        _fill(p,"#729BB8",(a+b*.16,1),(w,1),(w,h),(a+b*.08,h),(a+b*.28,h*.50))
        enamel = _gradient(a,0,w,h,((0,"#678CA5"),(.32,"#4B7A97"),(.55,"#80A9C0"),(1,"#4B7893")))
        _fill(p,enamel,(a+b*.36,0),(w,0),(w,h),(a+b*.30,h),(a+b*.47,h*.5))
        _line(p,"#D5E6EF",2,(a+b*.20,5),(a+b*.04,h*.50),(a+b*.17,h-5))
        center=QPointF(a+b*.62,h*.5)
    elif rank==3:
        for ink, points in (
            ("#A28BB8",((a,11),(a+b*.52,0),(a+b*.3,h*.48))),
            ("#745F91",((a+b*.52,0),(w,0),(a+b*.30,h*.48))),
            ("#BDADCE",((a+b*.30,h*.48),(w,0),(w,h*.67))),
            ("#937CA9",((a+b*.30,h*.48),(w,h*.67),(a+b*.4,h))),
            ("#67577C",((a+b*.30,h*.48),(a+b*.4,h),(a,h))),
        ):
            _fill(p,ink,*points)
        center=QPointF(a+b*.57,h*.49)
    elif rank==4:
        # The bevel follows the clipped end of the card, leaving a continuous seam beside the text.
        brass=_gradient(a,0,w,h,((0,"#937446"),(.22,"#E5C382"),(.50,"#F3DA9B"),(.75,"#BF9D64"),(1,"#806342")))
        _fill(p,brass,(a+b*.17,7),(w,7),(w,h-7),(a+b*.17,h-7),(a+b*.24,h*.69),(a+b*.24,h*.31))
        _line(p,"#FFF0BE",1.2,(a+b*.31,19),(w-b*.28,19),(w-b*.11,h*.25),(w-8,h*.37))
        _line(p,"#986F3C",1.2,(a+b*.31,h-19),(w-b*.28,h-19),(w-b*.11,h*.75),(w-8,h*.63))
        _line(p,"#CEA76A",1.1,(a+b*.12,14),(a+b*.19,h*.32),(a+b*.19,h*.68),(a+b*.12,h-14))
        center=QPointF(a+b*.61,h*.50)
    else:
        # The detached block is a separate piece of the mount. Its throat follows the fractured paper edge.
        fault=_path((a+b*.62,2),(w-b*.16,8),(w-b*.18,h*.17),(w-b*.04,h*.12),(w,h*.55),
                    (w-b*.10,h*.62),(w-b*.04,h*.71),(w-b*.24,h*.96),(a+b*.20,h),
                    (a+b*.25,h*.86),(a+b*.07,h*.90),(a+b*.23,h*.64),(a+b*.15,h*.67),
                    (a+b*.40,h*.36),(a+b*.32,h*.39))
        p.fillPath(fault,_gradient(a,0,w,h,((0,"#352A47"),(.46,"#202B40"),(.8,"#4D3045"),(1,"#783C57"))))
        p.save()
        p.setClipPath(fault,Qt.ClipOperation.IntersectClip)
        for i in range(6):
            _line(p,QColor(204,126,157,86),.9,(a+b*(.49+.055*i),-2),(a+b*(.12+.051*i),h))
        center=QPointF(a+b*.58,h*.51)
        if decorated:
            paint_rank_insignia(p,center,min(b*.44,h*.45),rank,t,phase)
        p.restore()
        _fill(p,"#6A3854",(w-b*.13,0),(w-2,4),(w-b*.045,h*.085),(w-b*.16,h*.13))
        _fill(p,"#C3697F",(w-b*.13,0),(w-2,4),(w-b*.12,h*.058))
        _fill(p,"#92516D",(a+b*.09,h*.97),(a+b*.17,h*.93),(a+b*.14,h))
        _line(p,"#C58A9F",1.2,(a+b*.62,3),(a+b*.35,h*.37))
        return
    if rank>0:
        # Long exposed hardware ties the small stamp at the left to the sculpted end.
        _line(p,c,1.1,(19,h-19),(a*.40,h-19),(a*.43,h-15),(a,h-15))
    if decorated:
        vertical_fit = .42 if rank == 4 else .46
        paint_rank_insignia(p,center,min(b*.39,h*vertical_fit),rank,t,phase)


def paint_expanded_achievement(painter: QPainter, rect: QRectF, style: RarityStyle,
                               progress=0., phase=0., background_enabled=True):
    rect=QRectF(rect).adjusted(2,2,-3,-4)
    if rect.width()<68 or rect.height()<62:
        return
    rank=style.rank
    w,h=rect.width(),rect.height()
    b=expanded_art_width(rect.width())
    t=max(0.,min(1.,progress)) if background_enabled else 0.
    phase%=1.
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.translate(rect.topLeft())
    plate=_shape(w,h,b,rank)
    painter.fillPath(plate.translated(.8,1.8),QColor(31,44,58,55))
    painter.save()
    painter.setClipPath(plate,Qt.ClipOperation.IntersectClip)
    bounds=QRectF(0,0,w,h)
    if background_enabled:
        paint_rank_stock(painter,bounds,rank)
    else:
        painter.fillRect(bounds,QColor(_PAPER[rank][1]))
    if rank!=5:
        _mount(painter,w,h,b,rank,t,phase,background_enabled)
    else:
        # Inked crystalline edges across the stock emphasise the physical break.
        a=w-b
        for ink, points in (
            ("#343347",((w*.19,7),(w*.46,7),(w*.49,1),(a+b*.55,1),(a+b*.38,10),(w*.50,10),(w*.47,15))),
            ("#97576F",((a+b*.55,1),(a+b*.19,h*.42),(a+b*.30,h*.28),(a+b*.08,h*.48))),
            ("#664059",((w*.28,h-12),(a,h-15),(a+b*.14,h*.58),(a+b*.05,h),(w*.32,h))),
            ("#C47787",((a-b*.04,h*.89),(a+b*.03,h*.82),(a,h*.94),(a+b*.09,h*.87),(a,h))),
        ):
            _fill(painter,ink,*points)
    _engrave_mount(painter,w,h,b,rank)
    painter.restore()
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(QColor(style.ink),1.3 if rank<3 else 1.65))
    painter.drawPath(plate)
    if rank==5:
        _mount(painter,w,h,b,rank,t,phase,background_enabled)
    painter.restore()
