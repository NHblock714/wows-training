"""Responsive display for the checked public-career portrait."""
from __future__ import annotations

import re

from PySide6.QtCore import QLineF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QProgressBar, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

from ..ai_analysis import compact_brief
from ..replay_intel import number
from . import academy_palette as tone
from .harbor_showcase import HarborShowcase
from .layered_panel import panel_path
from .presentation_fonts import load_presentation_fonts


_HEADINGS = ('舰船偏好', '游玩风格', '游戏经验')
_ACCENTS = ('#176CB3', '#A53671', '#167C8C')
_MIX_COLORS = ('#377CAD', '#CC7B9B', '#47A5B3', '#A8BDC9')
_PARENT_MODES = (
    ('随机战', ('pvp',)), ('排位', ('rank_solo',)), ('行动', ('oper_solo', 'oper_div')),
    ('联合', ('pve',)), ('公会', ('club',)),
)
_LEGACY_RATE_NOTE = re.compile(
    r'胜率和\s*PR\s*是单排与分队的合计表现\s*[，,]\s*不能直接视为单排或排位水平[。.!！]?',
    re.IGNORECASE,
)
_COMBAT_READINGS = (
    (('main_battery_hit_rate', '主炮命中率'), ('torpedo_hit_rate', '鱼雷命中率'),
     ('second_battery_hit_rate', '副炮命中率'), ('aircraft_hit_rate', '舰载机命中率')),
    (('damage_received_to_dealt_ratio', '实际承伤 ÷ 造伤'), ('survival_rate', '存活率')),
    (('avg_scouting_damage', '场均侦察伤害'), ('avg_dropped_capture_points', '场均防守点数'),
     ('avg_capture_points', '场均占领点数')),
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


def _combat_readings(request):
    """One weapon, one exchange or survival measure and one team contribution."""
    row = next((item for item in _rows(request.get('evidence')) if isinstance(item, dict)
                and item.get('kind') == 'career_combat_style'), {})
    details = row.get('details')
    if not isinstance(details, dict):
        return '', []
    parent_modes = {mode for _, modes in _PARENT_MODES for mode in modes}
    modes = [item for item in _rows(details.get('support')) if isinstance(item, dict)
             and isinstance(item.get('mode'), str) and isinstance(item.get('label'), str)
             and number(item.get('count')) and item['count'] > 0
             and _rows(item.get('support'))]
    modes.sort(key=lambda item: (item.get('mode') in parent_modes, item['count']), reverse=True)
    for mode in modes:
        by_metric = {entry['metric']: entry for entry in mode['support'] if isinstance(entry, dict)
                     and isinstance(entry.get('metric'), str) and number(entry.get('value')) and entry['value'] >= 0}
        readings = []
        for group in _COMBAT_READINGS:
            for key, label in group:
                entry = by_metric.get(key)
                if entry is None:
                    continue
                value = entry['value']
                rate = key.endswith('_rate')
                if rate and value > 1:
                    continue
                formatted = (f'{value:.1%}' if rate else
                             f'{value:.2f}' if key == 'damage_received_to_dealt_ratio' else f'{value:,.1f}'.removesuffix('.0'))
                coverage = entry.get('coverage_ratio')
                if number(coverage) and 0 < coverage < 1:
                    label += '（部分样本）'
                readings.append((label, formatted, value if rate else None))
                break
        if readings:
            return mode['label'], readings
    return '', []


def _fleet_mix(request):
    row = next((item for item in _rows(request.get('evidence')) if isinstance(item, dict)
                and item.get('kind') == 'career_mode_fleets'), {})
    details = row.get('details')
    if not isinstance(details, dict):
        return '', []
    fleets = [item for item in _rows(details.get('support')) if isinstance(item, dict)
              and isinstance(item.get('label'), str) and number(item.get('denominator'))
              and item['denominator'] > 0 and item.get('status') == 'available']
    if not fleets:
        return '', []
    fleet = max(fleets, key=lambda item: item['denominator'])
    bands = []
    for key, title in (('class_distribution', '舰种占比'), ('tier_distribution', '等级占比')):
        shares = [(entry['label'], entry['share']) for entry in _rows(fleet.get(key))
                  if isinstance(entry, dict) and isinstance(entry.get('label'), str)
                  and number(entry.get('share')) and 0 < entry['share'] <= 1]
        shares.sort(key=lambda entry: entry[1], reverse=True)
        total = sum(share for _, share in shares)
        if shares and .99 <= total <= 1.01:
            tail = sum(share for _, share in shares[3:])
            bands.append((title, shares[:3] + ([('其余', tail)] if tail else [])))
    return fleet['label'], bands


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
            p.setPen(QPen(QColor(self.accent), 2 if self.featured else 1))
            p.drawLine(QLineF(bounds.right() - 36, bounds.bottom() - 9, bounds.right() - 19, bounds.bottom() - 9))
            p.drawLine(QLineF(bounds.right() - 19, bounds.bottom() - 9, bounds.right() - 13, bounds.bottom() - 15))
        finally:
            p.end()


class _MixBand(QWidget):
    def __init__(self, shares):
        super().__init__()
        self.shares = shares
        self.setFixedHeight(7)
        self.setAccessibleName(' · '.join(f'{label} {share:.0%}' for label, share in shares))

    def paintEvent(self, _event):
        painter = QPainter(self)
        try:
            x = 0.
            total = sum(share for _, share in self.shares)
            for (_, share), color in zip(self.shares, _MIX_COLORS):
                width = self.width() * share / total
                painter.fillRect(QRectF(x, 0, max(0, width - 1), self.height()), QColor(color))
                x += width
        finally:
            painter.end()


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
        for index, accent in enumerate(_ACCENTS):
            panel = _PortraitPanel(accent)
            card = QVBoxLayout(panel)
            card.setContentsMargins(18, 14, 23, 16)
            card.setSpacing(7)
            title = _label(pixels=18, weight=QFont.Weight.DemiBold, ink=accent)
            text = _label(pixels=15)
            card.addWidget(title)
            card.addWidget(text)
            if index == 0:
                self._distribution = QWidget()
                self._distribution_layout = QVBoxLayout(self._distribution)
                self._distribution_layout.setContentsMargins(0, 7, 0, 0)
                self._distribution_layout.setSpacing(4)
                card.addWidget(self._distribution)
            elif index == 1:
                self._signals = QWidget()
                self._signals_layout = QGridLayout(self._signals)
                self._signals_layout.setContentsMargins(0, 7, 0, 0)
                self._signals_layout.setSpacing(6)
                self._signals_layout.setColumnStretch(0, 1)
                self._signals_layout.setColumnStretch(1, 1)
                card.addWidget(self._signals)
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
        request = payload.get('request')
        request = request if isinstance(request, dict) else {}
        self._show_mix(*_fleet_mix(request if sections and sections[0][0] == _HEADINGS[0] else {}))
        show_style = len(sections) > 1 and sections[1][0] == _HEADINGS[1]
        self._show_readings(*_combat_readings(request if show_style else {}))
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

    def _show_mix(self, mode, bands):
        while self._distribution_layout.count():
            item = self._distribution_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self._distribution.setVisible(bool(bands))
        for index, (title, shares) in enumerate(bands):
            if index:
                self._distribution_layout.addSpacing(5)
            self._distribution_layout.addWidget(_label(f'{mode} · {title}', pixels=12, ink=tone.MUTED))
            self._distribution_layout.addWidget(_MixBand(shares))
            legend = '　'.join(f'{label} {share:.0%}' for label, share in shares)
            self._distribution_layout.addWidget(_label(legend, pixels=12, ink=tone.INK))

    def _show_readings(self, mode, readings):
        while self._signals_layout.count():
            self._signals_layout.takeAt(0).widget().deleteLater()
        self._signals.setVisible(bool(readings))
        if not readings:
            return
        self._signals_layout.addWidget(_label(mode, pixels=12, ink=tone.MUTED), 0, 0, 1, 2)
        for index, (label, value, fraction) in enumerate(readings):
            chip = QFrame()
            chip.setObjectName('careerCombatReading')
            chip.setStyleSheet('QFrame#careerCombatReading { background:#F5F9FD; border:1px solid #B6CDD9; border-radius:5px; }')
            content = QVBoxLayout(chip)
            content.setContentsMargins(9, 6, 9, 7)
            content.setSpacing(2)
            content.addWidget(_label(label, pixels=12, ink=tone.MUTED))
            content.addWidget(_label(value, pixels=17, weight=QFont.Weight.DemiBold, ink='#185679'))
            if fraction is not None:
                line = QProgressBar()
                line.setFixedHeight(4)
                line.setTextVisible(False)
                line.setRange(0, 1000)
                line.setValue(round(fraction * 1000))
                line.setAccessibleName(label)
                line.setStyleSheet('QProgressBar { border:0; background:#DCEAF4; }'
                                   'QProgressBar::chunk { border:0; background:#3C9EC5; }')
                content.addWidget(line)
            span = 2 if index == len(readings) - 1 and index % 2 == 0 else 1
            self._signals_layout.addWidget(chip, 1 + index // 2, index % 2, 1, span)

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
