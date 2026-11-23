"""Read-only visual projections of saved workbench analysis runs."""
from __future__ import annotations

from collections import Counter, defaultdict
from html import escape

from PySide6.QtCore import Qt, QRectF, Signal
from PySide6.QtGui import QColor, QPainter, QFont
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QComboBox, QTabWidget, QScrollArea, QSplitter, QToolTip)
from .chart_motion import ChartReveal
from . import academy_palette as tone
from .layered_panel import LayeredPanel


def player_groups(battles):
    """Merge only explicit identities or identical tournament accounts."""
    result = {}
    for battle in battles:
        people = battle.get('participants', [])
        identities = Counter(p.get('confirmed_player_id') for p in people if p.get('confirmed_player_id'))
        for index, player in enumerate(people):
            identity = player.get('confirmed_player_id')
            confirmed = bool(identity and identities[identity] == 1)
            name = player.get('name')
            key = ('player', identity) if confirmed else ('account', name) if name else ('unknown', battle['battle_id'], index)
            row = result.setdefault(key, {'key': key, 'names': set(), 'confirmed': confirmed,
                'appearances': [], 'ship_counts': Counter(), 'evidence_ids': defaultdict(set)})
            row['names'].add(name or '未知赛号')
            row['appearances'].append(battle['battle_id'])
            ship = str(player.get('ship_id') or 'unknown')
            row['ship_counts'][ship] += 1
            refs = [ref for appearance in battle.get('ship_appearances', [])
                    if str(appearance.get('ship_id')) == ship for ref in appearance.get('evidence_ids', [])]
            row['evidence_ids'][ship].update(refs)
    return sorted(result.values(), key=lambda row: (-len(row['appearances']), sorted(row['names'])))


def lineup_groups(battles, ship_names):
    """Count exact lineups, keeping unknown ships visible rather than guessing."""
    formations = {}
    for battle in battles:
        participants = battle.get('participants') or []
        signature = tuple(sorted(Counter(str(p.get('ship_id') or 'unknown') for p in participants).items()))
        if not signature:
            continue
        row = formations.setdefault(signature, {'count': 0, 'battle_ids': [], 'evidence_ids': set(),
            'label': ' · '.join(f"{ship_names.get(ship, '未知舰船' if ship == 'unknown' else ship)} ×{count}"
                                for ship, count in signature)})
        row['count'] += 1
        row['battle_ids'].append(battle['battle_id'])
        row['evidence_ids'].update(ref for ship in battle.get('ship_appearances', []) for ref in ship.get('evidence_ids', []))
    return sorted(formations.values(), key=lambda row: (-row['count'], row['label']))


class FrequencyBars(QWidget):
    selected = Signal(object)
    row_height = 64

    def __init__(self, parent=None, *, multiline=False):
        super().__init__(parent)
        self.data = []
        self.multiline = multiline
        self.row_height = 110 if multiline else 64
        self.setMouseTracking(True)
        self.setMinimumWidth(280)
        self._reveal = ChartReveal(self)

    def set_rows(self, data):
        self.data = data
        self.setMinimumHeight(max(100, len(data) * self.row_height + 8))
        self._reveal.set_content(data if any(isinstance(row.get('ratio'), (int, float)) and row['ratio'] > 0 for row in data) else None)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(event.rect(), QColor(tone.PAPER))
        if not self.data:
            painter.setPen(QColor(tone.MUTED))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, '暂无可展示的观测')
            return
        width = self.width() - 24
        first = max(0, event.rect().top() // self.row_height - 1)
        last = min(len(self.data), event.rect().bottom() // self.row_height + 2)
        for index in range(first, last):
            row = self.data[index]
            y = index * self.row_height + 7
            font = QFont(self.font()); font.setBold(True); painter.setFont(font)
            painter.setPen(QColor(tone.INK))
            if self.multiline:
                label = row['label']
                painter.drawText(QRectF(12, y + 21, width, 53), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap, label)
            else:
                label = painter.fontMetrics().elidedText(row['label'], Qt.TextElideMode.ElideRight, max(80, width - 130))
                painter.drawText(QRectF(12, y, width - 130, 23), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)
            painter.setPen(QColor('#914915' if row.get('warning') else tone.BLUE))
            painter.drawText(QRectF(self.width() - 144, y, 132, 23), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, row['value'])
            bar_y = y + (77 if self.multiline else 29)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(tone.ICE)); painter.drawRoundedRect(QRectF(12, bar_y, width, 7), 1, 1)
            ratio = row.get('ratio')
            if isinstance(ratio, (int, float)):
                painter.setBrush(QColor('#A15423' if row.get('warning') else tone.BLUE))
                if ratio > 0:
                    painter.drawRoundedRect(QRectF(12, bar_y, width * min(1, max(0, ratio)) * self._reveal.progress, 7), 1, 1)
            font.setBold(False); font.setPointSizeF(max(8, self.font().pointSizeF() - 1)); painter.setFont(font)
            painter.setPen(QColor(tone.MUTED))
            caption = painter.fontMetrics().elidedText(row.get('caption', ''), Qt.TextElideMode.ElideRight, int(width))
            painter.drawText(QRectF(12, bar_y + 10, width, 18), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, caption)

    def mouseMoveEvent(self, event):
        index = int(event.position().y()) // self.row_height
        if 0 <= index < len(self.data):
            row = self.data[index]
            self.setCursor(Qt.CursorShape.PointingHandCursor if row.get('evidence_ids') else Qt.CursorShape.ArrowCursor)
            QToolTip.showText(event.globalPosition().toPoint(), row['label'] + '\n' + row.get('caption', ''), self)
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        index = int(event.position().y()) // self.row_height
        if event.button() == Qt.MouseButton.LeftButton and 0 <= index < len(self.data):
            refs = self.data[index].get('evidence_ids') or []
            if refs:
                self.selected.emit(list(refs))
        super().mousePressEvent(event)


def _chart(title, chart, subtitle=''):
    frame = LayeredPanel()
    box = QVBoxLayout(frame); box.setContentsMargins(16, 14, 21, 16)
    label = QLabel(title); label.setStyleSheet('font-size:16px;font-weight:600;color:#12263D;')
    box.addWidget(label)
    if subtitle:
        caption = QLabel(subtitle); caption.setObjectName('subtitle'); caption.setWordWrap(True); box.addWidget(caption)
    scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setWidget(chart); box.addWidget(scroll, 1)
    return frame


class CollectionDashboard(QWidget):
    evidence_requested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.payload = {}
        layout = QVBoxLayout(self); layout.setContentsMargins(2, 8, 2, 2)
        top = QHBoxLayout()
        self.heading = QLabel('阵容与选手'); self.heading.setStyleSheet('font-size:19px;font-weight:600;')
        top.addWidget(self.heading); top.addStretch()
        self.group_combo = QComboBox(); self.group_combo.setObjectName('formationGroup'); self.group_combo.setMinimumWidth(320)
        self.group_combo.currentIndexChanged.connect(self.refresh)
        top.addWidget(self.group_combo); layout.addLayout(top)
        self.scope = QLabel(); self.scope.setObjectName('subtitle'); self.scope.setWordWrap(True); layout.addWidget(self.scope)
        metrics = QHBoxLayout(); self.metrics = []
        for caption in ('纳入比赛', '参赛赛号 / 身份', '使用舰船', '空间观测达标局'):
            frame = LayeredPanel(); box = QVBoxLayout(frame); box.setContentsMargins(16,12,23,16)
            value = QLabel('—'); value.setObjectName('metric'); box.addWidget(value)
            label = QLabel(caption); label.setObjectName('subtitle'); box.addWidget(label)
            metrics.addWidget(frame); self.metrics.append(value)
        layout.addLayout(metrics)
        self.views = QTabWidget(); self.views.setObjectName('formationViews'); layout.addWidget(self.views, 1)
        self.ship_chart, self.lineup_chart = FrequencyBars(), FrequencyBars(multiline=True)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(_chart('舰船偏好', self.ship_chart, '出现局数 / 当前范围局数'))
        split.addWidget(_chart('重复阵容', self.lineup_chart, '相同舰船与数量'))
        split.setSizes([440, 520]); self.views.addTab(split, '阵容构成')
        players = QWidget(); pbox = QVBoxLayout(players)
        line = QHBoxLayout(); line.addWidget(QLabel('选手'))
        self.player_combo = QComboBox(); self.player_combo.setObjectName('formationPlayer'); self.player_combo.currentIndexChanged.connect(self.show_player)
        line.addWidget(self.player_combo, 1); pbox.addLayout(line)
        self.player_summary = QLabel(); self.player_summary.setObjectName('subtitle'); self.player_summary.setWordWrap(True); pbox.addWidget(self.player_summary)
        self.player_chart = FrequencyBars(); pbox.addWidget(_chart('选船分布', self.player_chart, '该舰出场 / 选手参赛'), 1)
        self.views.addTab(players, '选手用船')
        opening = QWidget(); obox = QVBoxLayout(opening)
        self.opening_scope = QLabel(); self.opening_scope.setObjectName('subtitle'); self.opening_scope.setWordWrap(True); obox.addWidget(self.opening_scope)
        self.opening_chart = FrequencyBars(); obox.addWidget(_chart('开局部署规则', self.opening_chart, '满足局 / 有效观测局'), 1)
        self.views.addTab(opening, '开局部署')
        for chart in (self.ship_chart, self.lineup_chart, self.player_chart, self.opening_chart):
            chart.selected.connect(self.evidence_requested)

    def set_payload(self, payload):
        self.payload = payload
        selector = payload.get('team_selector') or {}
        subject = selector.get('clan_tag') or (f"绝对队伍 {selector['team_id']}" if 'team_id' in selector else '所选队伍')
        self.heading.setText(subject + ' · 阵容与选手')
        self.group_combo.blockSignals(True); self.group_combo.clear(); self.group_combo.addItem('全部比赛 · 选船汇总', None)
        for index, group in enumerate(payload.get('groups', []), 1):
            self.group_combo.addItem(f"{index:02d}  {self.group_label(group)} · {len(group['battle_ids'])} 场", group['id'])
            self.group_combo.setItemData(index, self.group_details(group), Qt.ItemDataRole.ToolTipRole)
        self.group_combo.blockSignals(False); self.refresh()

    def group_label(self, group):
        conditions = group.get('conditions') or {}
        maps = (self.payload.get('presentation') or {}).get('maps') or {}
        name = maps.get(conditions.get('map_id'), conditions.get('map_id') or '未知地图')
        side = {'N': '北', 'S': '南', 'W': '西', 'E': '东'}.get(conditions.get('spawn_side'), '未知')
        return f'{name} / {side}侧'

    def group_details(self, group):
        conditions = group.get('conditions') or {}
        mode = conditions.get('mode') or {}
        return ('版本：' + str(conditions.get('version') or '未知') + '\n模式：' + str(mode.get('scenario') or mode.get('gameType') or '未知')
                + '\n占点布局：' + str(conditions.get('layout_id') or '未知') + '\n规则：' + str(self.payload.get('rules_version') or '未知'))

    def refresh(self, *_):
        if not self.payload:
            return
        key = self.group_combo.currentData()
        group = next((row for row in self.payload.get('groups', []) if row['id'] == key), None)
        ids = set(group['battle_ids']) if group else None
        battles = [row for row in self.payload.get('battles', []) if ids is None or row['battle_id'] in ids]
        self.filtered_battles = battles
        names = (self.payload.get('presentation') or {}).get('ships') or {}
        self.ship_names = names
        people = player_groups(battles); self.people = people
        n = len(battles); counts = Counter(); refs = defaultdict(set); slots = Counter()
        for battle in battles:
            for appearance in battle.get('ship_appearances', []):
                ship = str(appearance['ship_id']); counts[ship] += 1; slots[ship] += appearance.get('count', 0)
                refs[ship].update(appearance.get('evidence_ids', []))
        threshold = (self.payload.get('rules') or {}).get('min_coverage', .8)
        eligible = sum((b.get('coverage', {}).get('ratio') or 0) >= threshold for b in battles)
        self.metrics[3].parentWidget().setToolTip(f'位置观测覆盖率 ≥ {threshold:.0%}')
        for widget, value in zip(self.metrics, (n, len(people), len(counts), f'{eligible} / {n}')):
            widget.setText(str(value))
        rules = self.payload.get('rules') or {}
        window = f"{rules.get('window_start_sec', '?')}–{rules.get('window_end_sec', '?')} 秒"
        self.scope.setText((self.group_label(group) if group else f"{len(self.payload.get('groups', []))} 个比较组") + '   |   分析窗口 ' + window)
        self.scope.setToolTip(self.group_details(group) if group else '比较组按地图、版本、模式、占点布局、开局观测方位及规则区分。')
        self.ship_chart.set_rows([{'label': names.get(ship, ship), 'value': f'{count} / {n} 局',
            'ratio': count / n if n else None, 'caption': f'共 {slots[ship]} 席次 · ' + (f'{count/n:.0%} 比赛使用' if n else '未知'),
            'evidence_ids': sorted(refs[ship])} for ship, count in counts.most_common()])
        self.lineup_chart.set_rows([{'label': row['label'], 'value': f"{row['count']} / {n} 局", 'ratio': row['count'] / n if n else None,
            'caption': f"{len(row['battle_ids'])} 场相同舰船组合", 'evidence_ids': sorted(row['evidence_ids'])}
            for row in lineup_groups(battles, names)])
        prior = self.player_combo.currentData(); self.player_combo.blockSignals(True); self.player_combo.clear()
        for row in people:
            self.player_combo.addItem(' / '.join(sorted(row['names'])) + f" · {len(row['appearances'])} 场", row['key'])
        self.player_combo.setCurrentIndex(max(0, self.player_combo.findData(prior))); self.player_combo.blockSignals(False); self.show_player()
        patterns = group.get('patterns', []) if group else []
        self.opening_scope.setText((self.group_label(group) + f" · {n} 场 · " + ('可比条件完整' if group.get('comparable') else '条件不完整，独立保留')) if group else '未选择比较组')
        self.opening_chart.set_rows([{'label': p['label'].replace('（观测规则）', '').replace('（几何规则）', ''),
            'value': f"{p['numerator']} / {p['denominator']} 局" if p.get('denominator') else '未知', 'ratio': p.get('ratio'),
            'warning': p.get('status') != 'observed_pattern',
            'caption': ('达到样本门槛' if p.get('status') == 'observed_pattern' else '样本或观测不足')
                + f" · 例外 {len(p.get('exception_battle_ids', []))} 局 · 未纳入 {len(p.get('excluded_battle_ids', []))} 局",
            'evidence_ids': p.get('evidence_ids', [])} for p in patterns])

    def show_player(self, *_):
        row = next((p for p in getattr(self, 'people', []) if p['key'] == self.player_combo.currentData()), None)
        if not row:
            self.player_chart.set_rows([]); self.player_summary.setText('暂无选手记录'); return
        n = len(row['appearances'])
        self.player_summary.setText(f"{n} 次出场 · {len(row['ship_counts'])} 艘不同舰船 · " + ('已确认身份合并' if row['confirmed'] else '按原始赛事账号统计'))
        self.player_chart.set_rows([{'label': self.ship_names.get(ship, '未知舰船' if ship == 'unknown' else ship),
            'value': f'{count} / {n} 次', 'ratio': count / n, 'caption': f'{count/n:.1%} 选船比例',
            'evidence_ids': sorted(row['evidence_ids'][ship])} for ship, count in row['ship_counts'].most_common()])


def compact_run_html(run, reviews=()):
    """Present bounded prose; complete stored results remain in the detail tab."""
    payload = run.get('payload') or {}
    parts = ['<h2>' + escape(run.get('name') or '分析结果') + '</h2>']
    def paragraph(text):
        parts.append('<p>' + escape(str(text)) + '</p>')
    plans = payload.get('plans') or []
    for index, plan in enumerate(plans[:3], 1):
        basis = plan.get('basis') or {}
        parts.append(f'<h3>方案 {index} · ' + escape(str(basis.get('text') or plan.get('name') or '准备建议')) + '</h3>')
        if basis.get('denominator') is not None:
            paragraph(f"依据：{basis.get('numerator', '未知')} / {basis['denominator']} 场")
        for action in (plan.get('actions') or [])[:2]:
            paragraph('• ' + str(action))
        risks = plan.get('risks') or []
        if risks: paragraph('注意：' + str(risks[0]))
    if plans and len(plans) > 3:
        paragraph(f'另有 {len(plans)-3} 个方案，见完整报告。')
    for label, key in (('观察', 'observations'), ('判断', 'interpretations'), ('建议', 'suggestions'), ('待确认', 'needs_review')):
        values = payload.get(key) or []
        if values:
            parts.append('<h3>' + label + '</h3>')
            for row in values[:3]:
                text = row.get('text') or row.get('summary') or row.get('claim') or row.get('action') if isinstance(row, dict) else row
                if text: paragraph(text)
    results = payload.get('results') or []
    if results:
        counts = Counter(row.get('status') for row in results)
        names = {'unknown': '未知', 'met': '达成', 'arrived': '已到', 'missed': '未达成', 'late': '迟到', 'early': '提前',
                 'on_time': '按时', 'partial': '部分可观测', 'not_observed': '未观测到'}
        paragraph(f"计划目标 {len(results)} 项 · " + ' / '.join(f'{names.get(key,key)}: {value}' for key, value in counts.items()))
        paragraph('逐项目标、时间偏差及判定依据见完整报告。')
    if payload.get('kind') == 'training_comparison':
        groups = payload.get('groups') or []
        paragraph(f"对齐 {len(groups)} 个比较组 · {sum(g.get('status') == 'comparable' for g in groups)} 组可比较")
        paragraph('各组前后样本数、变化量及未纳入原因见完整报告。')
    issues = payload.get('constraint_issues') or []
    if issues:
        parts.append('<h3>需要补齐</h3>')
        for issue in issues[:3]: paragraph(issue)
    if reviews:
        parts.append('<h3>最近批注</h3>'); paragraph(str(reviews[-1].get('comment') or ''))
    return '<html><body style="background:#FFFFFF;color:#12263D;font-family:Microsoft YaHei UI;font-size:14px;">' + ''.join(parts) + '</body></html>'
