"""Responsive display for the checked public-career portrait."""
from __future__ import annotations

import re

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

from ..ai_analysis import compact_brief
from ..replay_intel import number
from . import academy_palette as tone
from .harbor_showcase import HarborShowcase
from .layered_panel import panel_path
from .presentation_fonts import load_presentation_fonts


_HEADINGS = ('舰船偏好', '游玩风格', '游戏经验')
_ACCENTS = ('#176CB3', '#A53671', '#167C8C')
_PARENT_MODES = (
    ('随机战', ('pvp',)), ('排位', ('rank_solo',)), ('行动', ('oper_solo', 'oper_div')),
    ('联合', ('pve',)), ('公会', ('club',)),
)
_LEGACY_RATE_NOTE = re.compile(
    r'胜率和\s*PR\s*是单排与分队的合计表现\s*[，,]\s*不能直接视为单排或排位水平[。.!！]?',
    re.IGNORECASE,
)


def _body(row):
    text = row.get('text') if isinstance(row, dict) else None
    return text.strip() if isinstance(text, str) else ''


def _legacy_body(row):
    return _LEGACY_RATE_NOTE.sub('', _body(row)).replace('…〔展开看完整限定〕', '…').strip()


def _rows(value):
    return value if isinstance(value, list) else []


def _chips(request):
    evidence = {row['kind']: row['details'] for row in _rows(request.get('evidence')) if isinstance(row, dict)
                and isinstance(row.get('kind'), str) and isinstance(row.get('details'), dict)}
    chips = []
    summary = evidence.get('career_mode_summaries', {})
    active = {row['mode'] for row in _rows(summary.get('support')) if isinstance(row, dict)
              and isinstance(row.get('mode'), str) and row.get('status') == 'known'
              and number(row.get('count')) and row['count'] > 0}
    modes = [label for label, keys in _PARENT_MODES if active.intersection(keys)]
    if modes:
        chips.append(('参战模式', f'{len(modes)} 类', ' · '.join(modes)))
    elif request.get('report_contract') != 'career_profile_v2':
        random = evidence.get('career_random_summary', {})
        battles = next((row.get('value') for row in _rows(random.get('support'))
                        if isinstance(row, dict) and row.get('metric') == 'battles'), None)
        if number(battles) and battles >= 0 and random.get('mode') == 'pvp':
            chips.append(('随机战场次', f'{battles:,.0f}', ''))
    fleet = evidence.get('career_ship_classes', {})
    classes = [row for row in _rows(fleet.get('support')) if isinstance(row, dict)
               and isinstance(row.get('label'), str) and number(row.get('share')) and 0 < row['share'] <= 1]
    classes.sort(key=lambda row: row['share'], reverse=True)
    if (fleet.get('mode') == 'pvp' and classes and classes[0]['label'] not in ('未知', '舰种未知')
            and (len(classes) == 1 or classes[0]['share'] > classes[1]['share'])):
        lead = classes[0]
        chips.append(('随机战最多使用的舰种', lead['label'], f"逐舰场次占比 {lead['share']:.0%}"))
    comparison = evidence.get('career_server_comparison', {})
    if comparison.get('status') == 'available' and comparison.get('mode') == 'pvp':
        columns = {'avg_damage': ('场均伤害', ',.0f'), 'avg_frags': ('场均击沉', '.2f')}
        for metric in columns:
            entry = next((row for row in _rows(comparison.get('support'))
                          if isinstance(row, dict) and row.get('metric') == metric), None)
            if entry is None:
                continue
            actual, reference = entry.get('value'), entry.get('reference_value')
            matched, total = entry.get('count'), entry.get('denominator')
            if not all(number(value) for value in (actual, reference, matched, total)) or not 0 < matched <= total:
                continue
            label, spec = columns[metric]
            chips.append((f'随机战同舰{label}', format(actual, spec),
                          f'参考 {reference:{spec}} · {matched:,.0f}/{total:,.0f} 场'))
            break
    return chips


def portrait_content(payload):
    """Derive the exact cards from saved validated output and comparability data."""
    request = payload.get('request') or {}
    if request.get('report_contract') == 'career_profile_v2':
        interpretations = _rows(payload.get('interpretations'))
        conclusion = _body(interpretations[0]) if interpretations else ''
        sections = [(title, _body(row)) for title, row in zip(_HEADINGS, _rows(payload.get('observations')))]
    else:
        brief = compact_brief(payload)
        conclusion = _legacy_body(brief['conclusion'])
        findings = '\n\n'.join(text for row in brief['findings'] if (text := _legacy_body(row)))
        sections = [('偏好与游玩特征', findings)]
    sections = [(title, text) for title, text in sections if text]
    return conclusion, sections, _chips(request) if conclusion or sections else []


def _label(text='', *, pixels=None, weight=QFont.Weight.Normal, ink=tone.INK, family='Microsoft YaHei UI', wraps=True):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse |
                                  Qt.TextInteractionFlag.TextSelectableByKeyboard)
    label.setWordWrap(wraps)
    label.setStyleSheet(f'color:{ink}; background:transparent;')
    font = QFont()
    font.setFamilies(list(dict.fromkeys((family, 'Microsoft YaHei', load_presentation_fonts()['chinese_body']))))
    if pixels:
        font.setPixelSize(pixels)
    font.setWeight(weight)
    label.setFont(font)
    if wraps:
        policy = label.sizePolicy()
        policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
        label.setSizePolicy(policy)
    return label


class _PortraitPanel(QWidget):
    def __init__(self, accent, *, featured=False):
        super().__init__()
        self.accent = accent
        self.featured = featured
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self._harbor = HarborShowcase() if featured else None
        self._background_enabled = True
        if featured:
            self.setProperty('backgroundEffect', True)

    def set_background_enabled(self, enabled):
        self._background_enabled = bool(enabled)
        self.update()

    def showEvent(self, event):
        super().showEvent(event)
        if self.featured:
            choices = getattr(self.window(), 'client_preferences', {})
            self.set_background_enabled(choices.get('background_enabled', self._background_enabled))

    def paintEvent(self, _event):
        bounds = QRectF(self.rect()).adjusted(1, 1, -4, -4)
        frame = panel_path(bounds, 22 if self.featured else 13)
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.fillPath(frame.translated(2, 2), QColor(123, 163, 190, 75))
            wash = QLinearGradient(bounds.topLeft(), bounds.bottomRight())
            wash.setColorAt(0, QColor('#FFFFFF'))
            wash.setColorAt(1, QColor('#DDECF7' if self.featured else '#EFF5FA'))
            p.fillPath(frame, wash)
            if self._harbor is not None and self._background_enabled and self._harbor.paint(p, bounds, frame):
                veil = QLinearGradient(bounds.topLeft(), bounds.topRight())
                veil.setColorAt(0, QColor(255, 255, 255, 225))
                veil.setColorAt(.66, QColor(245, 251, 255, 152))
                veil.setColorAt(1, QColor(245, 251, 255, 12))
                p.fillPath(frame, veil)
            p.setPen(QPen(QColor('#ABC0D4'), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(frame)
            p.fillRect(QRectF(17, 1, 79 if self.featured else 43, 3), QColor(self.accent))
        finally:
            p.end()


class PortraitView(QScrollArea):
    """Present one report with a reading order that reflows under a narrow window."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAccessibleName('公开战绩 AI 画像')
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._page = QWidget()
        self._page.setObjectName('careerPortraitPage')
        self._page.setStyleSheet('QWidget#careerPortraitPage { background:#EDF4FA; }')
        self.setWidget(self._page)
        box = QVBoxLayout(self._page)
        box.setContentsMargins(10, 10, 12, 12)
        box.setSpacing(12)
        self.message = _label(ink=tone.MUTED, pixels=14)
        box.addWidget(self.message)

        self.hero = _PortraitPanel(_ACCENTS[0], featured=True)
        hero_layout = QVBoxLayout(self.hero)
        hero_layout.setContentsMargins(22, 15, 26, 19)
        hero_layout.setSpacing(8)
        hero_layout.addWidget(_label('AI 战绩画像', pixels=26, weight=QFont.Weight.DemiBold))
        self.conclusion = _label(pixels=17, weight=QFont.Weight.Medium)
        hero_layout.addWidget(self.conclusion)
        self.metrics = QWidget()
        self._metric_layout = QHBoxLayout(self.metrics)
        self._metric_layout.setContentsMargins(0, 5, 0, 0)
        self._metric_layout.setSpacing(8)
        hero_layout.addWidget(self.metrics)
        box.addWidget(self.hero)

        self._cards = QWidget()
        self._grid = QGridLayout(self._cards)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(12)
        self._panels = []
        for accent in _ACCENTS:
            panel = _PortraitPanel(accent)
            card = QVBoxLayout(panel)
            card.setContentsMargins(18, 14, 23, 16)
            card.setSpacing(7)
            title = _label(pixels=18, weight=QFont.Weight.DemiBold, ink=accent)
            text = _label(pixels=15)
            card.addWidget(title)
            card.addWidget(text)
            card.addStretch(1)
            self._panels.append((panel, title, text))
        box.addWidget(self._cards)
        box.addStretch(1)
        self._columns = 0
        self._section_count = 0
        self._reflow()
        self.set_message('打开一份公开玩家战绩以生成画像。')

    def set_message(self, text):
        self.message.setText(text)
        self.message.show()
        self.hero.hide()
        self._cards.hide()
        self.verticalScrollBar().setValue(0)

    def show_report(self, payload):
        conclusion, sections, chips = portrait_content(payload)
        if not conclusion and not sections:
            self.set_message('当前回复缺少可展示的画像正文。')
            return
        self.message.hide()
        self.conclusion.setText(conclusion)
        self.conclusion.setVisible(bool(conclusion))
        self._show_chips(chips)
        self.hero.show()
        self._section_count = len(sections)
        for index, (panel, title, text) in enumerate(self._panels):
            if index < self._section_count:
                label, content = sections[index]
                title.setText(label)
                text.setText(content)
            panel.setVisible(index < self._section_count)
        self._cards.setVisible(bool(sections))
        self._reflow()
        self.verticalScrollBar().setValue(0)

    def _show_chips(self, chips):
        while self._metric_layout.count():
            old = self._metric_layout.takeAt(0)
            old.widget().deleteLater()
        fonts = load_presentation_fonts()
        for label, value, note in chips:
            chip = QFrame()
            chip.setObjectName('careerPortraitChip')
            chip.setStyleSheet('QFrame#careerPortraitChip { background:#F3F9FE; border:1px solid #AAC7DB; border-radius:5px; }')
            group = QVBoxLayout(chip)
            group.setContentsMargins(10, 6, 10, 7)
            group.setSpacing(2)
            group.addWidget(_label(label, pixels=12, ink=tone.MUTED))
            group.addWidget(_label(value, pixels=21, weight=QFont.Weight.DemiBold, ink='#164F78',
                                   family=fonts['tech'] if value.isascii() else 'Microsoft YaHei UI'))
            if note:
                group.addWidget(_label(note, pixels=12, ink=tone.MUTED))
            self._metric_layout.addWidget(chip, 1)
        self.metrics.setVisible(bool(chips))

    def _reflow(self):
        columns = 2 if self.viewport().width() >= 760 and self._section_count > 1 else 1
        if columns == self._columns:
            return
        self._columns = columns
        for index, (panel, _, _) in enumerate(self._panels):
            self._grid.removeWidget(panel)
            if columns == 2:
                self._grid.addWidget(panel, index // 2, index % 2, 1, 2 if index == 2 else 1)
            else:
                self._grid.addWidget(panel, index, 0)
        self._grid.setColumnStretch(0, 1)
        self._grid.setColumnStretch(1, int(columns == 2))
        self._cards.updateGeometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reflow()
