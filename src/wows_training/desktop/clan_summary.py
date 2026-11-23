"""Clan totals and coverage alongside the selected radar scope."""
from collections import Counter

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout

from .account_exhibit import InteractiveDonut
from .layered_panel import LayeredPanel
from .player_dossier_panel import count_text
from .presentation_fonts import load_presentation_fonts
from .query_visuals import axis_title


MEMBER_CATEGORIES = {
    'ok': ('有战绩', '#1478BD'),
    'private': ('隐藏', '#BB548C'),
    'unknown': ('资料未知', '#7F8EA5'),
    'not_fetched': ('船仓未载入', '#8466BE'),
    'no_battles': ('无场次', '#399E9A'),
    'no_matches': ('范围内无场次', '#399E9A'),
}


class ClanSummary(LayeredPanel):
    def __init__(self):
        super().__init__(cut=17)
        self.setAccessibleName('公会数据总览')
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        fonts = load_presentation_fonts()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 20, 13)
        layout.setSpacing(5)
        metrics = QHBoxLayout()
        metrics.setSpacing(9)
        self.values = {}
        for key, label in (('power', '公会战力'), ('overall', '维度均分'), ('mean', '人均场次')):
            column = QVBoxLayout()
            column.setSpacing(0)
            caption = QLabel(label)
            caption.setObjectName('subtitle')
            value = QLabel('—')
            value.setStyleSheet(f'font-family:"{fonts["tech"]}";font-size:23px;font-weight:600;color:#134F97;')
            value.setAccessibleName(label)
            column.addWidget(caption)
            column.addWidget(value)
            metrics.addLayout(column, 1)
            self.values[key] = value
        self.values['mean'].setToolTip('当前范围内有战绩成员的平均场次')
        layout.addLayout(metrics)
        self.unloaded = QLabel('成员船仓未载入')
        self.unloaded.setStyleSheet('color:#734685;')
        self.unloaded.hide()
        layout.addWidget(self.unloaded)
        self.coverage = InteractiveDonut('members', heading='成员战绩状态', unit='人',
                                         colors={key: item[1] for key,item in MEMBER_CATEGORIES.items()}, compact=True)
        self.coverage.activated.connect(self.coverage.select)
        layout.addWidget(self.coverage, 1)
        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet('background:#BCD0E4;')
        layout.addWidget(divider)
        axes = QHBoxLayout()
        axes.setSpacing(10)
        self.axis_labels = []
        for heading, color in (('优势维度', '#125DA5'), ('待提升', '#A52769')):
            column = QVBoxLayout()
            column.setSpacing(1)
            label = QLabel(heading)
            label.setStyleSheet(f'font-family:"{fonts["chinese_tech"]}";font-size:15px;color:{color};')
            value = QLabel('—')
            value.setWordWrap(True)
            value.setTextFormat(Qt.TextFormat.PlainText)
            value.setAccessibleName(heading)
            self.axis_labels.append(value)
            column.addWidget(label)
            column.addWidget(value)
            axes.addLayout(column, 1)
        layout.addLayout(axes)

    def set_summary(self, stats, *, filtered, fleets_loaded):
        members = stats['members']
        states = Counter(member['status'] if member['status'] in MEMBER_CATEGORIES else 'unknown'
                         for member in members)
        common = ('ok', 'private', 'unknown')
        keys = (*common, *(key for key in MEMBER_CATEGORIES if key not in common and states[key]))
        rows = [(key, MEMBER_CATEGORIES[key][0], states[key]) for key in keys]
        self.coverage.set_distribution(rows)
        active = [member['battles'] for member in members if member['status'] == 'ok']
        average = sum(active) / len(active) if active else None
        for key, value in (('power', stats['power']), ('overall', stats.get('overall')), ('mean', average)):
            self.values[key].setText('—' if value is None else count_text(value))
        ranked = sorted(stats['scores'].items(), key=lambda row: row[1][1], reverse=True)
        for label, group in zip(self.axis_labels, (ranked[:2], reversed(ranked[-2:]))):
            label.setText('、'.join(axis_title(key) for key, _ in group) or '—')
        self.unloaded.setVisible(filtered and not fleets_loaded)
