"""F1 WoWS PR colours for numeric readouts."""

from math import ceil

from PySide6.QtCore import QAbstractAnimation, QEasingCurve, QPointF, QSize, Qt, QVariantAnimation
from PySide6.QtGui import QColor, QFontMetrics, QFontMetricsF, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication, QGraphicsDropShadowEffect, QStyle, QStyledItemDelegate, QStyleOptionViewItem

from ..career_rating import number
from . import academy_palette as tone
from .motion import motion_enabled, subscribe_motion
from .presentation_fonts import load_presentation_fonts
from .statistics_visuals import MetricCard


# F1 WoWS personal_rating.pr_color, in ascending PR order.
PR_BANDS = (
    (300, '#7A6F66'),
    (750, '#A8804E'),
    (1100, '#C7A82E'),
    (1350, '#7CCC42'),
    (1550, '#4F9F38'),
    (1750, '#2B8FB0'),
    (2100, '#9759BC'),
    (2450, '#DA00A6'),
    (float('inf'), '#FFD700'),
)
_RATING_ACCENT_ROLE = Qt.ItemDataRole.UserRole.value + 1


def rating_accent(pr):
    if not number(pr):
        return None
    return QColor(next(color for upper, color in PR_BANDS if pr < upper))


def rating_ink(accent):
    if accent is None:
        return QColor(tone.MUTED)
    # Deepen the original hue for readable text on white and selected pale blue.
    return QColor(round(10 + accent.red() * .40),
                  round(17 + accent.green() * .40),
                  round(28 + accent.blue() * .40))


def rating_halo(accent):
    if accent is None:
        return QColor(Qt.GlobalColor.transparent)
    color = QColor(accent)
    color.setAlpha(96)
    return color


def set_rating_halo(label, accent):
    effect = label.graphicsEffect()
    if accent is None:
        if isinstance(effect, QGraphicsDropShadowEffect):
            effect.setEnabled(False)
        return
    if not isinstance(effect, QGraphicsDropShadowEffect):
        effect = QGraphicsDropShadowEffect(label)
        effect.setOffset(0, 0)
        effect.setBlurRadius(7)
        label.setGraphicsEffect(effect)
    effect.setColor(rating_halo(accent))
    effect.setEnabled(True)


def style_rating_item(item, pr):
    item.setData(_RATING_ACCENT_ROLE, rating_accent(pr))


class _RatingNumberDelegate(QStyledItemDelegate):
    """Paint tier ink over the native cell surface, including selection."""

    def paint(self, painter, option, index):
        accent = index.data(_RATING_ACCENT_ROLE)
        if not isinstance(accent, QColor) or not accent.isValid():
            super().paint(painter, option, index)
            return

        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        font = opt.font
        font.setBold(True)
        opt.font = font
        opt.fontMetrics = QFontMetrics(font)
        style = opt.widget.style() if opt.widget else QApplication.style()
        text_rect = style.subElementRect(QStyle.SubElement.SE_ItemViewItemText, opt, opt.widget)
        margin = style.pixelMetric(QStyle.PixelMetric.PM_FocusFrameHMargin, opt, opt.widget) + 1
        text_rect.adjust(margin, 0, -margin, 0)
        metrics = QFontMetricsF(font)
        shown = metrics.elidedText(opt.text, opt.textElideMode, max(0, text_rect.width()))

        # Qt's selected-row stylesheet overrides item foreground; keep its
        # surface and focus painting, then draw only the numeric glyphs.
        opt.text = ""
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        if not shown:
            return
        measured = QSize(ceil(metrics.horizontalAdvance(shown)), ceil(metrics.height()))
        origin = QStyle.alignedRect(opt.direction, opt.displayAlignment, measured, text_rect)
        glyphs = QPainterPath()
        glyphs.addText(QPointF(origin.x(), origin.y() + metrics.ascent()), font, shown)
        painter.save()
        painter.setClipRect(option.rect, Qt.ClipOperation.IntersectClip)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        halo = rating_halo(accent)
        for width, opacity in ((5., .16), (3., .27), (1.5, .36)):
            color = QColor(halo)
            color.setAlpha(round(halo.alpha() * opacity))
            pen = QPen(color, width)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.strokePath(glyphs, pen)
        painter.fillPath(glyphs, rating_ink(accent))
        painter.restore()


def install_rating_delegate(view, *columns):
    delegate = _RatingNumberDelegate(view)
    for column in columns:
        view.setItemDelegateForColumn(column, delegate)


class RatingMetricCard(MetricCard):
    """Use the account's PR hue for both values and count up the combined power."""

    def __init__(self, label, *, power=False):
        super().__init__(label)
        self._is_power = power
        self._amount = None
        self._rating_accent = None
        self._font_family = load_presentation_fonts()['grade' if power else 'tech']
        self._style_amount()
        self._counter = QVariantAnimation(self)
        self._counter.setDuration(850)
        self._counter.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._counter.valueChanged.connect(self._show_amount)

    def _show_amount(self, amount):
        self.set_value(f'{amount:,}' if amount is not None else '—')

    def _style_amount(self):
        accent = self._rating_accent
        ink = rating_ink(accent).name()
        sizes = 'font-size:36px;font-style:italic;' if self._is_power else 'font-size:29px;'
        self.value.setStyleSheet(f'font-family:"{self._font_family}";font-weight:700;color:{ink};{sizes}')
        set_rating_halo(self.value, accent)

    def _settle_counter(self):
        self._counter.stop()
        self._show_amount(self._amount)

    def _motion_changed(self, enabled):
        if not enabled:
            self._settle_counter()

    def set_rating(self, amount, pr):
        amount = int(round(amount)) if number(amount) else None
        accent = rating_accent(pr) if amount is not None else None
        old_amount = self._amount
        self._amount = amount
        if accent != self._rating_accent:
            self._rating_accent = accent
            self._style_amount()
        if amount != old_amount and amount is not None and self._is_power and self.isVisible() and motion_enabled(self):
            start = self._counter.currentValue() if self._counter.state() == QAbstractAnimation.State.Running else old_amount
            self._counter.stop()
            self._counter.setStartValue(start or 0)
            self._counter.setEndValue(amount)
            self._counter.start()
        elif amount != old_amount:
            self._settle_counter()

    def showEvent(self, event):
        super().showEvent(event)
        subscribe_motion(self, self._motion_changed)

    def hideEvent(self, event):
        self._settle_counter()
        super().hideEvent(event)
