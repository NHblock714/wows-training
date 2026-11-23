"""Searchable navigation grouped by the work a player is doing."""
from PySide6.QtCore import Qt, QSize, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QFrame, QLabel, QLineEdit, QPushButton, QScrollArea, QToolButton, QVBoxLayout, QWidget

from .fleet_shell import navigation_icon


REVIEW_PAGE = 9
QUERY_PAGE = 10
HOME_PAGE = 11
_HOME_TERMS = '首页 起始页 开始页 home'
GROUPS = (
    ('回放分析', (
        (0, '录像库', '导入 文件夹 比赛资料 普通rep 随机 排位 人机 活动 录像 筛选'),
        (REVIEW_PAGE, '单局分析', '结算 伤害 击沉 评分 决策 关键时刻 模型 AI'),
        (1, '地图复盘', '时间轴 小地图 炮弹 鱼雷 点亮 航迹'),
    )),
    ('战绩与档案', (
        (QUERY_PAGE, '战绩查询', 'ID 昵称 公会 工会 船仓 对比 同舰 战力 挑战 名片 PR 收藏'),
        (7, '个人作战档案', '公开 账号 REP 关联 生涯 历史'),
        (8, '选手八维图', '选手档案 卡片 评分 图片 播放'),
    )),
    ('赛事与赛训', (
        (4, '赛训工作室', '集合 战术 计划 区域 身份 映射'),
        (6, '选手与阵容', '选船 汇总 胜率 承伤'),
        (5, '双边录像', '合并 多视角 配对'),
        (3, '报告工作台', 'AI 报告 简报 导出 PDF'),
    )),
)


class FeatureIndex(QWidget):
    requested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        self.search = QLineEdit()
        self.search.setObjectName('featureSearch')
        self.search.setPlaceholderText('找功能  Ctrl+K')
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName('搜索功能')
        layout.addWidget(self.search)
        self.buttons = {HOME_PAGE: self._button(HOME_PAGE, '首页')}
        layout.addWidget(self.buttons[HOME_PAGE])
        self.scroll = QScrollArea()
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(self.scroll, 1)
        body = QWidget()
        self.scroll.setWidget(body)
        entries = QVBoxLayout(body)
        entries.setContentsMargins(0, 0, 0, 0)
        entries.setSpacing(5)
        self.headings = []
        self.expanded = [True, True, False]
        for group, (title, pages) in enumerate(GROUPS):
            heading = QToolButton()
            heading.setObjectName('navGroup')
            heading.setText(title)
            heading.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            heading.clicked.connect(lambda _, i=group: self.toggle_group(i))
            self.headings.append(heading)
            entries.addWidget(heading)
            for index, label, _ in pages:
                item = self._button(index, label)
                entries.addWidget(item)
                self.buttons[index] = item
        self.empty = QLabel('没有匹配的功能')
        entries.addWidget(self.empty)
        entries.addStretch()
        self.search.textChanged.connect(self.apply_filter)
        self.search.returnPressed.connect(self.open_first_match)
        self.shortcut = QShortcut(QKeySequence('Ctrl+K'), self)
        self.shortcut.activated.connect(self.focus_search)
        self.clear_shortcut = QShortcut(QKeySequence('Escape'), self.search)
        self.clear_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        self.clear_shortcut.activated.connect(self.search.clear)
        self.apply_filter()

    def _button(self, index, label):
        item = QPushButton(label)
        item.setObjectName('nav')
        item.setAccessibleName(label)
        item.setCheckable(True)
        item.setIcon(navigation_icon(index))
        item.setIconSize(QSize(21, 21))
        item.setFixedHeight(35)
        item.clicked.connect(lambda _: self.requested.emit(index))
        return item

    def focus_search(self):
        self.search.setFocus()
        self.search.selectAll()

    def toggle_group(self, group):
        if self.search.text():
            self.search.clear()
        self.expanded[group] = not self.expanded[group]
        self.apply_filter()

    def apply_filter(self, *_):
        words = self.search.text().casefold().split()
        home_matches = all(word in _HOME_TERMS for word in words)
        self.buttons[HOME_PAGE].setVisible(home_matches)
        count = int(home_matches)
        for group, (title, pages) in enumerate(GROUPS):
            expanded = bool(words) or self.expanded[group]
            matches = 0
            for index, label, aliases in pages:
                text = (title + ' ' + label + ' ' + aliases).casefold()
                matched = all(word in text for word in words)
                matches += int(matched)
                self.buttons[index].setVisible(matched and expanded)
            count += matches
            self.headings[group].setVisible(matches > 0)
            self.headings[group].setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.empty.setVisible(count == 0)

    def open_first_match(self):
        if self.search.text().strip() and not self.buttons[HOME_PAGE].isHidden():
            self.requested.emit(HOME_PAGE)
            return
        for _, pages in GROUPS:
            for index, _, _ in pages:
                if not self.buttons[index].isHidden():
                    self.requested.emit(index)
                    return

    def select(self, index):
        if index != HOME_PAGE:
            group = next(i for i, (_, pages) in enumerate(GROUPS) if any(p[0] == index for p in pages))
            self.expanded[group] = True
        if self.search.text():
            self.search.clear()
        self.apply_filter()
        for key, item in self.buttons.items():
            item.setChecked(key == index)
        if index != HOME_PAGE:
            self.scroll.ensureWidgetVisible(self.buttons[index], 0, 4)
