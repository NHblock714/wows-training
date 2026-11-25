"""Account and clan lookup, ship comparisons and public battle cards."""
from __future__ import annotations
import re
from sqlite3 import Error as StorageError
from PySide6.QtCore import QSignalBlocker, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QHBoxLayout, QHeaderView,
    QFrame, QGridLayout, QLineEdit, QProgressBar, QPushButton, QScrollArea, QSpinBox, QStackedWidget, QTabWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget)

from ..career_rating import metrics, project_ships
from ..dog_tags import fetch_dog_tag
from ..query_center import QueryCenter, player_target
from ..shared_profile import SharedProfileError, load_shared_profile, parse_share_link
from .fleet_shell import FleetPageHeader
from .motion import motion_enabled, subscribe_motion
from .multiview_panel import RecordingJob
from .player_dossier_panel import CLASS_NAMES, MODES, REGIONS, count_text, percent, query_status, time_text
from .query_visuals import CareerDistribution, RadarFocusOverlay, RatingRadar, SnapshotCardDialog, axis_title, plain_label
from .career_rating_visuals import RatingMetricCard, install_rating_delegate, style_rating_item
from .statistics_visuals import MetricCard
from .layered_panel import LayeredPanel
from . import academy_palette as tone
from .ui_components import Disclosure
from .account_exhibit import AccountExhibit, CareerInstrumentRow, ConstellationBackdrop, InteractiveDonut
from .achievement_gallery import AchievementGallery, ShipCollectionSummary
from .clan_summary import ClanSummary
from .hidden_career_card import HiddenCareerCard
from .dog_tag_visual import compose_dog_tag_png, dog_tag_image
from .query_favorites import QueryFavorites
from .query_history_picker import QueryHistoryPicker
from .ship_comparison_summary import ShipComparisonSummary


METRICS = (('battles','场次'),('win_rate','胜率'),('avg_damage','场均伤害'),('avg_xp','场均经验'),
    ('avg_frags','场均击沉'),('kd_ratio','击沉 / 阵亡'),('main_battery_hit_rate','主炮命中'),
    ('avg_potential_damage','场均潜在伤害'),('avg_scouting_damage','场均侦察伤害'),
    ('avg_planes_killed','场均击落'),('torpedo_hit_rate','鱼雷命中'),('avg_capture_points','场均占领'),
    ('avg_dropped_capture_points','场均防御'),('survival_rate','存活率'))
PERCENTAGES = {'win_rate','survival_rate','main_battery_hit_rate','torpedo_hit_rate'}
DECIMALS = {'avg_frags','kd_ratio','avg_planes_killed','avg_capture_points','avg_dropped_capture_points'}
SORTS = (('ship_battle_power','战力'),('battles','场次'),('avg_damage','场均伤害'),('win_rate','胜率'),
         ('avg_xp','场均经验'),('avg_frags','场均击沉'))
NATIONS = {'USA':'美国','France':'法国','UK':'英国','Germany':'德国','Japan':'日本','Italy':'意大利',
           'Russia':'苏联','Europe':'欧洲','Pan_Asia':'泛亚','Pan_America':'泛美','Netherlands':'荷兰',
           'Spain':'西班牙','Commonwealth':'英联邦'}
MEMBER_STATUS = {'ok':'公开','unknown':'资料未知','private':'隐藏','no_battles':'无场次',
                 'not_fetched':'未载入','no_matches':'范围内无场次','error':'查询失败'}
GRADE_BANDS = ((100,'S','#805611'),(70,'A','#783C94'),(40,'B','#215C94'),
               (10,'C','#276644'),(0,'D','#A23550'))
_MEMBER_PROGRESS = re.compile(r'^(?:读取|整理)成员船仓 ([0-9]+)/([0-9]+)(?: · .*)?$')


def member_grade(value):
    if value is None:
        return '未知', None
    for threshold,letter,color in GRADE_BANDS:
        if value >= threshold:
            return f'{letter}  {value:.0f}', color
    return '未知', None


def value_text(key, value):
    if value is None:
        return '未知'
    if key in PERCENTAGES:
        return percent(value)
    if key in DECIMALS:
        return f'{value:.2f}'
    return count_text(value)


def combo(entries=()):
    widget = QComboBox()
    widget.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    widget.setMinimumContentsLength(5)
    for key, text in entries:
        widget.addItem(text, key)
    return widget


def button(text, slot):
    widget = QPushButton(text)
    widget.clicked.connect(slot)
    return widget


def table(titles):
    view = QTableWidget(0, len(titles))
    view.setHorizontalHeaderLabels(titles)
    view.setAlternatingRowColors(True); view.setShowGrid(False); view.verticalHeader().hide()
    view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    view.horizontalHeader().setStretchLastSection(True)
    view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    return view


def set_rows(view, rows):
    view.setRowCount(len(rows))
    for i, (key, values) in enumerate(rows):
        for j, value in enumerate(values):
            item = QTableWidgetItem(str(value))
            item.setToolTip(str(value))
            if j == 0:
                item.setData(Qt.ItemDataRole.UserRole, key)
            else:
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            view.setItem(i, j, item)


def repopulate(box, entries, previous=None):
    was = box.blockSignals(True)
    box.clear()
    for key, title in entries:
        box.addItem(str(title), key)
    box.setCurrentIndex(max(0, box.findData(previous)) if box.count() else -1)
    box.blockSignals(was)


def form():
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(4,5,4,5)
    return page, layout


class QueryCenterPanel(QWidget):
    idle = Signal()
    status = Signal(str)

    def __init__(self, host):
        super().__init__(host)
        self.host = host
        self._service = None
        self.job = None
        self.target = self.profile = self.clan = self.detail = None
        self._shared_profile = None
        self._active_dog_tag_png = None
        self.frames = {}
        self.peer = None
        self._picker_context = None
        self._loading = False
        self._progress_step = None
        self.profile_ai = None
        self._build()

    @property
    def service(self):
        if self._service is None:
            self._service = QueryCenter(self.host.dossier_panel.career)
        return self._service

    @property
    def career(self):
        return self.service.career

    @property
    def busy(self):
        return self.job is not None or bool(self.profile_ai and self.profile_ai.busy)

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(22,18,22,14)
        outer.addWidget(FleetPageHeader('战绩查询', 'profile'))
        self.inputs, fields = form()
        outer.addWidget(self.inputs)
        search = QHBoxLayout()
        self.region = combo(REGIONS.items())
        self.kind = combo([('all','玩家及公会'),('player','玩家'),('clan','公会')])
        self.text = QLineEdit(); self.text.setPlaceholderText('昵称、公会名称或 ID')
        self.text.setClearButtonEnabled(True)
        self.search_button = button('搜索', self.search); self.search_button.setObjectName('primary')
        settings = button('查询设置',self.open_settings)
        for w in (self.region,self.kind,self.text,self.search_button,settings):
            search.addWidget(w, 1 if w is self.text else 0)
        self.text.returnPressed.connect(self.search)
        fields.addLayout(search)
        saved = QHBoxLayout()
        self.saved = QueryHistoryPicker(); self.candidates = combo([(None, '搜索候选')])
        self.candidates.setAccessibleName('搜索候选')
        self.saved.targetOpened.connect(self._open_saved)
        self.saved.clearRequested.connect(self._clear_history)
        saved.addWidget(self.saved, 1); saved.addWidget(self.candidates,1)
        saved.addWidget(button('打开所选',self.open_candidate))
        self.refresh_button = button('刷新当前',self.refresh_current); self.refresh_button.setEnabled(False)
        saved.addWidget(self.refresh_button)
        fields.addLayout(saved)
        self.detail_actions = QWidget()
        actions = QHBoxLayout(self.detail_actions)
        actions.setContentsMargins(0, 0, 0, 0)
        self.back_to_favorites = button('← 返回收藏页', self.return_to_favorites)
        self.mark = button('收藏', self.bookmark)
        self.mark.setObjectName('queryToggleFavorite')
        actions.addWidget(self.back_to_favorites)
        actions.addStretch()
        actions.addWidget(self.mark)
        fields.addWidget(self.detail_actions)
        self.detail_actions.hide()
        picker=QHBoxLayout()
        self.use_account_button=button('选用此账号',self.use_selected_account);self.use_account_button.setObjectName('queryUseAccount');self.use_account_button.hide();self.use_account_button.setEnabled(False)
        self.return_dossier_button=button('返回选手档案',self.cancel_account_picker);self.return_dossier_button.setObjectName('queryReturnDossier');self.return_dossier_button.hide()
        picker.addStretch();picker.addWidget(self.return_dossier_button);picker.addWidget(self.use_account_button);fields.addLayout(picker)
        for signal in (self.region.currentIndexChanged,self.kind.currentIndexChanged,self.text.textChanged):
            signal.connect(self._clear_candidates)
        status_row = QHBoxLayout()
        status_track = QVBoxLayout()
        status_track.setSpacing(4)
        self.notice = plain_label()
        self.notice.hide()
        status_track.addWidget(self.notice)
        self.read_progress = QProgressBar()
        self.read_progress.setObjectName('queryReadProgress')
        self.read_progress.setAccessibleName('战绩查询读取进度')
        self.read_progress.setTextVisible(False)
        self.read_progress.setFixedHeight(6)
        self.read_progress.setStyleSheet(
            'QProgressBar#queryReadProgress {border-image:none;background:#D9E7F6;border:0;border-radius:3px;}'
            'QProgressBar#queryReadProgress::chunk {border-image:none;border:0;border-radius:3px;'
            'background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #226BC3,stop:.82 #57A4E5,stop:1 #BA81BE);}')
        self.read_progress.hide()
        status_track.addWidget(self.read_progress)
        subscribe_motion(self, self._update_read_progress)
        self.cancel_button = button('取消查询',self.cancel); self.cancel_button.hide()
        status_row.addLayout(status_track, 1); status_row.addWidget(self.cancel_button)
        outer.addLayout(status_row)
        self.stack = QStackedWidget(); outer.addWidget(self.stack,1)
        self.favorites_page = QueryFavorites()
        self.favorites_page.targetOpened.connect(self._open_saved)
        self.stack.addWidget(self.favorites_page)
        self._build_player()
        self._build_clan()
        self.hidden_account_page, hidden_box = form()
        self.hidden_account = HiddenCareerCard()
        self.hidden_account.shareLinkRequested.connect(self._request_shared_profile)
        hidden_box.addWidget(self.hidden_account)
        self._build_shared_profile_view(hidden_box)
        self.stack.addWidget(self.hidden_account_page)
        self.radar_focus = RadarFocusOverlay(self)
        self.tabs.currentChanged.connect(self.radar_focus.reset)
        self.stack.currentChanged.connect(self.radar_focus.reset)

    def _build_shared_profile_view(self, box):
        self.shared_view, view = form()
        actions = QHBoxLayout()
        self.shared_heading = plain_label()
        self.shared_heading.setObjectName('sectionTitle')
        actions.addWidget(self.shared_heading, 1)
        self.shared_mode = combo()
        self.shared_mode.setAccessibleName('共享战绩的战斗模式')
        self.shared_mode.currentIndexChanged.connect(self._render_shared_profile)
        actions.addWidget(self.shared_mode)
        actions.addWidget(button('关闭共享战绩', self._clear_shared_profile))
        view.addLayout(actions)
        self.shared_note = plain_label()
        self.shared_note.setWordWrap(True)
        view.addWidget(self.shared_note)
        tabs = QTabWidget()
        self.shared_mode_stats = table(['战绩指标', '当前模式'])
        self.shared_mode_stats.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.shared_ship_stats = table(['舰船', '等级', '场次', '胜率', '场均经验', '场均伤害', '场均击沉'])
        self.shared_ship_stats.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        tabs.addTab(self.shared_mode_stats, '模式战绩')
        tabs.addTab(self.shared_ship_stats, '舰船战绩')
        view.addWidget(tabs, 1)
        box.addWidget(self.shared_view, 1)
        box.addStretch()
        self.shared_view.hide()

    def _request_shared_profile(self):
        if (self.busy or self.stack.currentWidget() is not self.hidden_account_page
                or (self.profile or {}).get('status') != 'private'):
            return
        target = dict(self.target)
        dialog = QDialog(self)
        dialog.setObjectName('sharedCareerLinkDialog')
        dialog.setWindowTitle('通过链接查看战绩')
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.resize(500, 160)
        layout = QVBoxLayout(dialog)
        label = plain_label('粘贴该玩家从官网「仅通过链接开放」设置中复制的查看链接。')
        label.setWordWrap(True)
        layout.addWidget(label)
        link_field = QLineEdit(dialog)
        link_field.setObjectName('sharedCareerLink')
        link_field.setAccessibleName('官方战绩查看链接')
        link_field.setEchoMode(QLineEdit.EchoMode.Password)
        link_field.setMaxLength(1024)
        link_field.setPlaceholderText('粘贴官方查看链接')
        layout.addWidget(link_field)
        error_label = plain_label()
        error_label.setWordWrap(True)
        error_label.setStyleSheet('color:#A23550;')
        error_label.hide()
        layout.addWidget(error_label)

        def submit():
            link = link_field.text()
            try:
                parse_share_link(link, target['region'], target['id'])
            except SharedProfileError as error:
                error_label.setText(str(error))
                error_label.show()
                link_field.setFocus()
                return
            dialog.accept()
            self._load_shared_profile(link, target)

        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(button('取消', dialog.reject))
        submit_button = button('读取战绩', submit)
        submit_button.setObjectName('primary')
        submit_button.setDefault(True)
        submit_button.setEnabled(False)
        link_field.textChanged.connect(lambda text: submit_button.setEnabled(bool(text.strip())))
        actions.addWidget(submit_button)
        layout.addLayout(actions)
        dialog.finished.connect(link_field.clear)
        dialog.open()
        link_field.setFocus()

    def _load_shared_profile(self, link, target):
        region, identifier = target['region'], str(target['id'])
        self._clear_shared_profile()

        def load(cancel, _progress):
            return load_shared_profile(link, region, identifier, cancel=cancel)

        def show(value):
            if (self.stack.currentWidget() is not self.hidden_account_page
                    or not self.target or self.service.key(target) != self.service.key(self.target)
                    or (value.get('region'), value.get('account_id')) != (region, identifier)):
                self._say('共享资料与当前选中的账号不一致。')
                return
            self._shared_profile = value
            self.shared_heading.setText('链接共享战绩')
            notices = ['仅在本次查看中显示']
            if value.get('account_created_at'):
                notices.append('创建于 '+time_text(value['account_created_at']))
            if value.get('achievements') is not None:
                notices.append(f"已记录 {len(value['achievements'])} 种游戏成就")
            self.shared_note.setText(' · '.join(notices))
            modes = value['modes']
            repopulate(self.shared_mode, [(key, MODES[key]) for key in MODES if key in modes], 'pvp')
            self._render_shared_profile()
            self.shared_view.show()
            self._say('共享战绩已读取')

        self._job(load, show, '正在向官方读取共享战绩…')

    def _render_shared_profile(self, *_):
        if self._shared_profile is None:
            return
        mode = self.shared_mode.currentData()
        account = metrics(self._shared_profile['modes'].get(mode))
        mode_rows = [(key, (title, value_text(key, account[key]))) for key, title in METRICS
                     if account[key] is not None]
        set_rows(self.shared_mode_stats, mode_rows or [(None, ('官方暂未返回模式战绩', ''))])
        rows = self._shared_profile.get('ships')
        if rows is None:
            set_rows(self.shared_ship_stats, [(None, ('官方暂未返回舰船战绩', '', '', '', '', '', ''))])
            return
        mode_ships = [{'ship_id': row['ship_id'], 'statistics': row['modes'][mode]}
                      for row in rows if mode in row['modes']]
        ships = project_ships(mode_ships, mode, self.career.references, self.career.catalog)
        ship_rows = [(row['ship_id'], (row['name'], row['tier'] or '—',
                      *(value_text(key, row[key]) for key in ('battles', 'win_rate', 'avg_xp', 'avg_damage', 'avg_frags'))))
                     for row in ships if row.get('battles')]
        set_rows(self.shared_ship_stats, ship_rows or [(None, ('当前模式暂无舰船战绩', '', '', '', '', '', ''))])

    def _clear_shared_profile(self):
        self._shared_profile = None
        self.shared_view.hide()
        self.shared_note.clear()
        self.shared_mode_stats.setRowCount(0)
        self.shared_ship_stats.setRowCount(0)

    def _build_player(self):
        page, box = form(); self.stack.addWidget(page)
        head = QHBoxLayout()
        identity = QFrame()
        identity.setObjectName('queryPlayerIdentity')
        identity.setStyleSheet('QFrame#queryPlayerIdentity {border-left:3px solid #177BB1;}')
        identity_row = QHBoxLayout(identity)
        identity_row.setContentsMargins(9, 0, 0, 0)
        identity_row.setSpacing(8)
        self.player_dog_tag = plain_label()
        self.player_dog_tag.setObjectName('queryPlayerDogTag')
        self.player_dog_tag.setAccessibleName('玩家铭牌')
        self.player_dog_tag.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.player_dog_tag.setFixedSize(35, 35)
        self.player_dog_tag.hide()
        identity_row.addWidget(self.player_dog_tag)
        self.player_heading = plain_label(); self.player_heading.setObjectName('sectionTitle')
        self.player_heading.setStyleSheet('color:#193C59;font-size:21px;')
        identity_row.addWidget(self.player_heading, 1)
        head.addWidget(identity, 1)
        self.card_btn = button('战绩名片',self.open_card)
        head.addWidget(self.card_btn); head.addWidget(button('个人档案',self.open_dossier)); box.addLayout(head)
        self.tabs = QTabWidget(); box.addWidget(self.tabs,1)
        overview, overview_box = form()
        overview_box.setContentsMargins(6, 7, 6, 8)
        overview_box.setSpacing(8)
        scroller = self.overview_scroll = QScrollArea(); scroller.setWidgetResizable(True); scroller.setWidget(overview)
        self.tabs.addTab(scroller,'总览')
        kpis = QHBoxLayout()
        self.account_metrics = {}
        for key, title in (('pr','随机战 PR'),('power','综合战力'),('battles','随机战场次'),('wins','随机战胜率')):
            metric = RatingMetricCard(title, power=key=='power') if key in ('pr','power') else MetricCard(title)
            metric.layout().setContentsMargins(15, 7, 24, 8)
            kpis.addWidget(metric, 6 if key=='power' else 5)
            self.account_metrics[key] = metric
        self.constellation = ConstellationBackdrop()
        exhibit_box = QVBoxLayout(self.constellation)
        exhibit_box.setContentsMargins(0, 0, 0, 0)
        exhibit_box.setSpacing(9)
        stage = QWidget()
        stage_box = QVBoxLayout(stage)
        stage_box.setContentsMargins(0, 0, 0, 0)
        stage_box.setSpacing(0)
        toolbar = QFrame()
        toolbar.setObjectName('careerRadarTools')
        toolbar.setStyleSheet('QFrame#careerRadarTools {background:qlineargradient(x1:0,y1:0,x2:1,y2:0,'
                              'stop:0 #18384E,stop:0.65 #23526A,stop:1 #28485F);border-left:3px solid #57CDE1;}')
        filters = QHBoxLayout(toolbar)
        filters.setContentsMargins(10, 5, 7, 5)
        filters.setSpacing(6)
        title = plain_label('多维战力雷达')
        title.setWordWrap(False)
        title.setStyleSheet('font-size:16px;font-weight:600;color:#F1F9FC;background:transparent;')
        filters.addWidget(title, 1)
        self.over_class = combo(); self.over_tier = combo()
        filters.addWidget(self.over_class); filters.addWidget(self.over_tier)
        self.overview_zoom = button('放大雷达', self.expand_overview)
        filters.addWidget(self.overview_zoom)
        stage_box.addWidget(toolbar)
        self.radar = RatingRadar(enlarged=True)
        self.radar.setMinimumHeight(362)
        self.radar.axisActivated.connect(self.inspect_overview_axis)
        stage_box.addWidget(self.radar, 1)
        self.account_exhibit = AccountExhibit()
        self.constellation.phaseChanged.connect(self.radar.set_ambient_phase)
        self.constellation.phaseChanged.connect(self.account_exhibit.set_ambient_phase)
        self.account_exhibit.aceRequested.connect(self.open_ace_ship)
        exhibit_box.addWidget(CareerInstrumentRow(stage, self.account_exhibit), 1)
        exhibit_box.addLayout(kpis)
        composition = QHBoxLayout()
        self.class_donut = InteractiveDonut('class')
        self.tier_donut = InteractiveDonut('tier')
        self.class_donut.activated.connect(lambda value:self._select_overview_filter(self.over_class,value))
        self.tier_donut.activated.connect(lambda value:self._select_overview_filter(self.over_tier,value))
        composition.addWidget(self.class_donut,1)
        composition.addWidget(self.tier_donut,1)
        exhibit_box.addLayout(composition)
        overview_box.addWidget(self.constellation,1)
        self.styles = plain_label()
        overview_box.addWidget(Disclosure('风格与战力修正',self.styles))
        self.over_class.currentIndexChanged.connect(self._overview)
        self.over_tier.currentIndexChanged.connect(self._overview)
        self.over_class.setAccessibleName('总览舰种范围')
        self.over_tier.setAccessibleName('总览等级范围')
        modes_page, mode_box = form()
        self.mode = combo(MODES.items()); self.mode.currentIndexChanged.connect(self._modes)
        mode_box.addWidget(self.mode)
        self.mode_note = plain_label()
        mode_box.addWidget(self.mode_note)
        self.mode_stats = table(['战绩指标','当前模式']); mode_box.addWidget(self.mode_stats,1)
        self.tabs.addTab(modes_page,'模式表现')
        ships_page, ships_box = form()
        ship_filters = QHBoxLayout()
        self.ship_class = combo(); self.ship_tier = combo(); self.nation = combo()
        self.sort = combo(SORTS)
        self.limit = QSpinBox(); self.limit.setRange(5,1000); self.limit.setValue(15); self.limit.setPrefix('前 ')
        for w in (self.ship_class,self.ship_tier,self.nation,self.sort,self.limit):
            ship_filters.addWidget(w,1)
        ships_box.addLayout(ship_filters)
        search_ship = QHBoxLayout()
        self.ship_name = QLineEdit(); self.ship_name.setPlaceholderText('筛选舰名'); self.ship_name.setClearButtonEnabled(True)
        self.min_battles = QSpinBox(); self.min_battles.setRange(1,99999); self.min_battles.setPrefix('至少 '); self.min_battles.setSuffix(' 场')
        search_ship.addWidget(self.ship_name,1); search_ship.addWidget(self.min_battles); ships_box.addLayout(search_ship)
        self.ship_collections = ShipCollectionSummary()
        ships_box.addWidget(self.ship_collections)
        self.distribution = CareerDistribution(); ships_box.addWidget(self.distribution)
        self.ranking_note = plain_label(); ships_box.addWidget(self.ranking_note)
        self.ranking_note.hide()
        self.ship_table = table(['舰船','舰种','等级','场次','胜率','场均伤害','场均经验','场均击沉','生存率','PR','战力'])
        install_rating_delegate(self.ship_table, 9, 10)
        self.ship_table.setMinimumHeight(110)
        for column, width in enumerate((122, 58, 46, 54, 58, 80, 72, 72, 63, 94, 90)):
            self.ship_table.setColumnWidth(column, width)
        ships_box.addWidget(self.ship_table,1)
        peer_row = QHBoxLayout()
        self.peer_edit = QLineEdit(); self.peer_edit.setPlaceholderText('对比玩家昵称或 ID（同区服）')
        self.peer_choices = combo()
        peer_row.addWidget(self.peer_edit,1); peer_row.addWidget(button('找对手',self.find_peer))
        peer_row.addWidget(self.peer_choices,1); peer_row.addWidget(button('对比',self.compare))
        peer_row.addWidget(button('清除',self.clear_peer)); ships_box.addLayout(peer_row)
        self.ship_zoom = button('放大雷达', self.expand_ship)
        peer_row.addWidget(self.ship_zoom)
        self.peer_edit.returnPressed.connect(self.find_peer)
        self.peer_edit.textChanged.connect(lambda: self.peer_choices.clear())
        ship_charts = QHBoxLayout()
        ship_charts.setSpacing(12)
        self.ship_radar = RatingRadar(); ship_charts.addWidget(self.ship_radar,1)
        self.ship_radar.axisActivated.connect(self.inspect_ship_axis)
        # Keep the former image handle for callers; art now lives inside the dossier.
        self.ship_image = plain_label(); self.ship_image.setParent(self); self.ship_image.hide()
        self.ship_info = ShipComparisonSummary(); ship_charts.addWidget(self.ship_info,1)
        ships_box.addLayout(ship_charts)
        ships_scroll = QScrollArea()
        ships_scroll.setWidgetResizable(True)
        ships_scroll.setWidget(ships_page)
        self.tabs.addTab(ships_scroll,'舰船与对比')
        shared = getattr(getattr(self.host, 'workbench', None), 'ai_panel', None)
        if shared is not None:
            from .account_profile_ai import AccountProfileAI
            self.profile_ai = AccountProfileAI(shared, self.tabs)
            self.profile_ai.idle.connect(self.idle)
            self.profile_ai.busy_state.connect(lambda busy: self.inputs.setEnabled(not busy and self.job is None))
            self.tabs.addTab(self.profile_ai, 'AI 画像')
        self.achievements = AchievementGallery(self.tabs)
        self.tabs.addTab(self.achievements, '成就展厅')
        for w in (self.ship_class,self.ship_tier,self.nation,self.sort):
            w.currentIndexChanged.connect(self._ship_rows)
        self.limit.valueChanged.connect(self._ship_rows)
        self.min_battles.valueChanged.connect(self._ship_rows)
        self.ship_name.textChanged.connect(self._ship_rows)
        self.ship_table.itemSelectionChanged.connect(self._ship_selected)

    def _build_clan(self):
        page, box = form(); self.stack.addWidget(page)
        self.clan_heading = plain_label(); self.clan_heading.setObjectName('sectionTitle'); box.addWidget(self.clan_heading)
        selectors = QHBoxLayout()
        self.clan_class = combo([(None,'全部舰种'), *CLASS_NAMES.items()])
        self.clan_tier = combo([(None,'全部等级'), *((v,f'T{v}') for v in range(1,12))])
        selectors.addWidget(self.clan_class); selectors.addWidget(self.clan_tier)
        selectors.addWidget(button('读取成员船仓',self.fetch_fleets)); selectors.addStretch()
        box.addLayout(selectors)
        charts = QHBoxLayout(); charts.setSpacing(10)
        self.clan_radar = RatingRadar()
        self.clan_summary = ClanSummary()
        charts.addWidget(self.clan_radar,3); charts.addWidget(self.clan_summary,2); box.addLayout(charts)
        self.member_table = table(['成员','职务','状态','场次','胜率','场均伤害','筛选评分'])
        box.addWidget(self.member_table,1)
        self.member_table.itemDoubleClicked.connect(lambda *_: self.open_member())
        box.addWidget(button('打开选中成员',self.open_member))
        self.clan_class.currentIndexChanged.connect(self._render_clan)
        self.clan_tier.currentIndexChanged.connect(self._render_clan)

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    def _say(self, message):
        self.notice.setText(message); self.notice.setVisible(bool(message)); self.status.emit(message)

    def refresh(self):
        favorites, cached = self.service.bookmarks(), self.service.directory()
        marked = {self.service.key(x) for x in favorites}
        entries = []
        for row in cached:
            suffix = REGIONS[row['region']] + (' · 公会' if row['kind'] == 'clan' else '')
            if self.service.key(row) in marked:
                suffix += ' · 已收藏'
            entries.append(dict(row, label=f"{row['name']} · {suffix}"))
        self.saved.set_records(entries)
        favorite_tags = self.service.cached_dog_tags((row['region'], row['id']) for row in favorites
                                                     if row['kind'] == 'player')
        self.favorites_page.set_records([dict(row, _dog_tag_png=favorite_tags.get((row['region'], str(row['id']))))
                                         if row['kind'] == 'player' else row for row in favorites])
        self.detail_actions.setVisible(bool(self.target) and self.stack.currentWidget() is not self.favorites_page)
        self.mark.setText('取消收藏' if self.target and self.service.key(self.target) in marked else '收藏')
        self.refresh_button.setEnabled(bool(self.target))

    def _clear_candidates(self, *_):
        repopulate(self.candidates, [(None,'搜索候选')])

    def _clear_history(self, target):
        if self.busy:
            return
        try:
            self.service.clear_history(target)
        except (StorageError, OSError, ValueError):
            self._say('清除本地查询记录失败，请稍后重试。')
            return
        key = self.service.key(target)
        candidate = self.candidates.currentData()
        remaining = []
        matched_search = False
        for index in range(self.candidates.count()):
            row = self.candidates.itemData(index)
            if row is None:
                continue
            if self.service.key(row) == key:
                matched_search = True
            else:
                remaining.append((row, self.candidates.itemText(index)))
        repopulate(self.candidates, [(None, '搜索候选'), *remaining], candidate)
        if (not remaining and self.region.currentData() == target['region']
                and (matched_search or self.text.text().strip().casefold() in (target['name'].casefold(), str(target['id'])))):
            self.text.clear()
        if self.target and self.service.key(self.target) == key:
            self._reset_lookup_view()
        elif target['kind'] == 'player':
            self._drop_cached_comparisons(target)
        self.refresh()
        self._say('已清除该条目的本地缓存与搜索记录。')

    def _drop_cached_comparisons(self, target):
        region, identifier = target['region'], str(target['id'])
        if self.peer and (self.peer.get('region'), str(self.peer.get('account_id'))) == (region, identifier):
            self.clear_peer()
        if self.clan and self.clan['region'] == region:
            self.clan['batch_players'].pop(identifier, None)
            self.frames.pop(int(identifier), None)
            self._render_clan()

    def _reset_lookup_view(self):
        self.target = self.profile = self.clan = self.detail = None
        self._active_dog_tag_png = None
        self.frames = {}
        self.peer = None
        self.peer_edit.clear()
        self.peer_choices.clear()
        self._clear_shared_profile()
        self._clear_player_display()
        self.member_table.setRowCount(0)
        self.use_account_button.setEnabled(False)
        self.stack.setCurrentWidget(self.favorites_page)

    def return_to_favorites(self):
        if self.busy:
            return
        self._reset_lookup_view()
        self._say('')
        self.refresh()
        self.text.setFocus()

    def set_search(self, nickname, region=None):
        """Prefill a roster identity without performing any request."""
        self._end_account_picker()
        self.radar_focus.reset()
        if self.job:
            self.cancel()
        if region == 'com':
            region = 'na'
        if region in REGIONS:
            self.region.setCurrentIndex(self.region.findData(region))
        self.kind.setCurrentIndex(self.kind.findData('player'))
        self.text.setText(str(nickname or ''))
        self._clear_candidates()
        self.text.setFocus()
        self._say('')

    def begin_account_picker(self,context):
        """Choose an exact public account while retaining the caller's identity."""
        self.set_search('',context.get('region'))
        self._picker_context=dict(context)
        self._reset_lookup_view()
        self.use_account_button.show();self.use_account_button.setEnabled(False);self.return_dossier_button.show()
        self.refresh()

    def _end_account_picker(self):
        self._picker_context=None;self.use_account_button.hide();self.return_dossier_button.hide()

    def use_selected_account(self):
        if self.busy or self._picker_context is None or not self.target or self.target.get('kind')!='player':return
        context,target=dict(self._picker_context),dict(self.target)
        self._end_account_picker();self.host.return_account_picker(context,target)

    def cancel_account_picker(self):
        if self._picker_context is None:return
        context=dict(self._picker_context);self.cancel();self._end_account_picker();self.host.return_account_picker(context,None)

    def _job(self, operation, success, message):
        if self.busy:
            return
        self.radar_focus.reset()
        job = RecordingJob(operation, self)
        self.job = job
        self.inputs.setEnabled(False); self.stack.setEnabled(False)
        self.cancel_button.show(); self.cancel_button.setEnabled(True)
        self._job_progress(message)
        self.read_progress.show()
        job.progress.connect(self._job_progress)
        self._accept_result = success
        job.succeeded.connect(self._delivered)
        job.failed.connect(self._failed)
        job.finished.connect(self._finished)
        job.start()

    @Slot(str)
    def _job_progress(self, message):
        if self.job is None or self.job.cancel_event.is_set():
            return
        stage = _MEMBER_PROGRESS.fullmatch(message)
        self._progress_step = None
        if stage:
            index, total = map(int, stage.groups())
            if 1 <= index <= total:
                # The callback starts the current member; only earlier members are processed.
                self._progress_step = (index - 1, total)
        self._update_read_progress(motion_enabled(self))
        self.read_progress.setAccessibleDescription(message)
        self._say(message)

    def _update_read_progress(self, animated):
        if self.job is None or self.job.cancel_event.is_set():
            return
        if self._progress_step is not None:
            completed, total = self._progress_step
            self.read_progress.setRange(0, total)
            self.read_progress.setValue(completed)
        else:
            self.read_progress.setRange(0, 0 if animated else 1)
            self.read_progress.setValue(0)

    @Slot(object)
    def _delivered(self,result):
        if self.job and not self.job.cancel_event.is_set():
            try:
                self._accept_result(result)
            except (ValueError,OSError,KeyError) as error:
                self._say(str(error))

    @Slot(str)
    def _failed(self,error):
        self._say('查询已取消' if self.job and self.job.cancel_event.is_set() else '查询失败 · '+error)

    @Slot()
    def _finished(self):
        job, self.job = self.job, None
        self._accept_result = None
        self._progress_step = None
        self.read_progress.hide()
        self.inputs.setEnabled(True); self.stack.setEnabled(True); self.cancel_button.hide()
        if job:
            job.deleteLater()
        self.idle.emit()

    def cancel(self):
        if self.profile_ai is not None and self.profile_ai.busy:
            self.profile_ai.cancel()
        if self.job:
            self.job.cancel_event.set(); self.cancel_button.setEnabled(False)
            self.read_progress.hide(); self._say('查询已取消')

    def search(self):
        region, text, kind = self.region.currentData(), self.text.text().strip(), self.kind.currentData()
        if not text:
            self._say('请输入昵称、公会名称或 ID'); return
        def shown(result):
            rows = result['candidates']
            repopulate(self.candidates, [(row, row['name']+(' · 公会' if row['kind']=='clan' else ' · 玩家')) for row in rows])
            reasons = list(dict.fromkeys(query_status({'status':state,'reason':reason}) for _,state,reason in result['messages'] if state not in ('ok','not_found')))
            self._say(' · '.join([f'{len(rows)} 个候选', *reasons]))
        self._job(lambda cancel, progress: self.service.search(region,text,kind,cancel), shown, '搜索公开资料…')

    def _open_saved(self, target):
        self.open_target({key: target[key] for key in ('kind', 'region', 'id', 'name')}, query_missing=True)

    def open_candidate(self):
        target = self.candidates.currentData()
        if target:
            self.open_target(target, query_missing=True)
        else:
            self._say('请先搜索并选择结果')

    def _load_player_dog_tag(self, region, account_id, cancel, progress, *, online, force):
        key = (region, str(account_id))
        try:
            cached = self.service.cached_dog_tags((key,)).get(key)
        except (StorageError, OSError):
            cached = None
        if cancel.is_set() or not online or (cached and not force):
            return cached
        progress('读取玩家铭牌…')
        layers = fetch_dog_tag(region, account_id, cancel=cancel)
        png = compose_dog_tag_png(layers) if layers and not cancel.is_set() else None
        if not png or cancel.is_set():
            return cached
        try:
            self.service.save_dog_tag(region, account_id, png)
        except (StorageError, OSError, ValueError):
            pass
        return png

    def _show_player_dog_tag(self):
        image = dog_tag_image(self._active_dog_tag_png)
        if image.isNull():
            self.player_dog_tag.clear()
            self.player_dog_tag.hide()
            return
        pixel_ratio = self.player_dog_tag.devicePixelRatioF()
        size = round(self.player_dog_tag.width() * pixel_ratio)
        scaled = image.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        pixmap = QPixmap.fromImage(scaled)
        pixmap.setDevicePixelRatio(pixel_ratio)
        self.player_dog_tag.setPixmap(pixmap)
        self.player_dog_tag.show()

    def open_target(self, target, query_missing=False, force=False, *, portrait=False):
        if self.busy:
            return
        self.radar_focus.reset()
        region, identifier = target['region'], target['id']
        def load(cancel, progress):
            if target['kind'] == 'clan':
                stored = self.service.cached_clan(region, identifier)
                if force or stored is None and query_missing:
                    stored = self.service.refresh_clan(region, identifier, cancel, progress)
                if stored and stored.get('snapshot_id'):
                    refs = self.service.cached_member_ships(stored) or {}
                    return dict(stored, frames=self._frames_for(stored,refs,cancel,progress)), None
                return stored or {'status':'not_fetched','reason':'no_snapshot'}, None
            profile = self.career.profile(region, identifier)
            if force or query_missing and not profile.get('snapshot_id'):
                profile = self.career.refresh(region, identifier, 'pvp', cancel, progress, include_achievements=True,
                                              include_all_fleets=True, record_failures=False, include_numbers_rankings=force)
            if profile.get('status') not in ('ok', 'partial', 'no_battles', 'unsupported'):
                return profile, None
            png = self._load_player_dog_tag(region, identifier, cancel, progress, force=force,
                                            online=(force or query_missing) and self.career.settings()['enabled'])
            return profile, png
        def display(result):
            value, dog_tag_png = result
            if target['kind']=='player' and (value.get('region'),str(value.get('account_id')))!=(region,str(identifier)):
                self._say('返回账号与所选账号不一致，未载入');return
            if value.get('status') in ('error','not_fetched') and self.target and self.service.key(target)==self.service.key(self.target) and (self.profile or self.clan):
                self._say(query_status(value)+' · 当前显示已有快照')
                return
            self._clear_shared_profile()
            self.target = dict(target)
            self._active_dog_tag_png = dog_tag_png
            self.peer = None; self.peer_choices.clear(); self.peer_edit.clear()
            if target['kind'] == 'clan':
                self.profile = self.detail = None
                if self.profile_ai is not None:
                    self.profile_ai.set_source()
                self.clan = value if 'clan_info' in value else None
                if self.clan:
                    info = value['clan_info']
                    self.target['name'] = self.service.clan_name(info.get('tag'), info.get('name'))
                    self._show_clan()
                else:
                    self.stack.setCurrentIndex(0); self._say(query_status(value))
            else:
                self.clan = None
                self.profile = value
                nickname=value.get('nickname')
                self.target['name'] = nickname if nickname and nickname!=str(identifier) else target['name']
                self._show_player()
                if portrait and self.stack.currentIndex() == 1 and self.profile_ai is not None:
                    self.tabs.setCurrentWidget(self.profile_ai)
            self.refresh()
            self.use_account_button.setEnabled(self._picker_context is not None and target['kind']=='player')
        self._job(load, display, '正在读取战绩…')

    def bookmark(self):
        if self.target and not self.busy:
            saved = self.service.toggle_bookmark(self.target)
            self.refresh()
            self._say('已固定到收藏页。' if saved else '已取消收藏。')

    def refresh_current(self):
        if self.target:
            self.open_target(self.target, force=True)

    def open_settings(self):
        self.host.show_page(7)
        dossier = self.host.dossier_panel; dossier.show_sources()
        for section in dossier.findChildren(Disclosure):
            if section.toggle_button.text() == '查询与缓存设置':
                section.setExpanded(True)
        QTimer.singleShot(0,lambda:dossier.source_scroll.ensureWidgetVisible(dossier.network_enabled))

    def open_dossier(self):
        if self.target and self.target['kind'] == 'player':
            if self._picker_context is not None:
                self.use_selected_account();return
            self.host.show_page(7)
            self.host.dossier_panel.open_public_account(self.target['region'], self.target['id'])

    def _show_player(self):
        profile = self.profile
        if profile.get('status') not in ('ok','partial','no_battles','unsupported'):
            self._clear_player_display()
            if profile.get('status') == 'private':
                self.hidden_account.set_account(self.target['name'], REGIONS.get(profile.get('region')))
                self.stack.setCurrentWidget(self.hidden_account_page)
                self._say('')
                return
            self.stack.setCurrentIndex(0); self._say(self.target['name']+' · '+query_status(profile)); return
        self.stack.setCurrentIndex(1); self._loading = True
        ships = profile.get('ships') or []
        classes = sorted({s['species'] for s in ships if s.get('species') and s.get('battles')})
        tiers = sorted({s['tier'] for s in ships if s.get('tier') and s.get('battles')})
        nations = sorted({s['nation'] for s in ships if s.get('nation') and s.get('battles')})
        for widget in (self.over_class,self.ship_class):
            repopulate(widget,[(None,'全部舰种'),*((c,CLASS_NAMES.get(c,c)) for c in classes)])
        for widget in (self.over_tier,self.ship_tier):
            repopulate(widget,[(None,'全部等级'),*((t,f'T{t}') for t in tiers)])
        repopulate(self.nation,[(None,'全部国家'),*((n,NATIONS.get(n,n)) for n in nations)])
        self.player_heading.setText(f"{profile['nickname']}  ·  {REGIONS[profile['region']]}")
        self._show_player_dog_tag()
        self._loading = False
        self._overview(); self._modes(); self._ship_rows()
        if self.profile_ai is not None:
            snapshot = None
            if profile.get('snapshot_id'):
                try:
                    snapshot = self.career.store.get_run(profile['snapshot_id'])
                except (KeyError, ValueError, OSError):
                    pass
            self.profile_ai.set_source(profile, self.target, snapshot)
        status = profile.get('numbers_rankings_status')
        if profile.get('status') != 'ok':
            self._say(query_status(profile))
        elif status == 'ok':
            self._say(f"单舰榜 · {profile['numbers_rankings_observed_on']}")
        elif status == 'cached':
            self._say(f"已保存的单舰榜 · {profile['numbers_rankings_observed_on']}")
        elif status == 'unavailable':
            self._say('战绩已刷新 · 单舰榜暂不可用')
        else:
            self._say('')

    def _clear_player_display(self):
        self.detail = None
        self.player_heading.clear()
        self.player_dog_tag.clear()
        self.player_dog_tag.hide()
        self.styles.clear()
        self.mode_note.clear()
        self.mode_stats.setRowCount(0)
        for key, metric in self.account_metrics.items():
            if key in ('pr', 'power'):
                metric.set_rating(None, None)
            else:
                metric.set_value('—')
        self.radar.set_scores({})
        self.account_exhibit.set_profile({}, {})
        for donut in (self.class_donut, self.tier_donut):
            donut.set_records([], available=False)
        self.achievements.set_detail({}, None)
        self.ship_collections.set_records(None)
        self.distribution.set_records([])
        self.ranking_note.hide()
        with QSignalBlocker(self.ship_table):
            self.ship_table.setRowCount(0)
        self.ship_radar.set_scores({})
        self.ship_info.clear_summary()
        self.ship_image.clear()
        for button in (self.card_btn, self.overview_zoom, self.ship_zoom):
            button.setEnabled(False)
        if self.profile_ai is not None:
            self.profile_ai.set_source()
        self.tabs.setCurrentIndex(0)

    def _overview(self,*_):
        if self._loading or not self.profile:
            return
        self.radar_focus.reset()
        from ..f1_ratings.personal_rating import ACHIEVEMENT_POWER_BONUSES, ACHIEVEMENT_POWER_CAP, SHIP_POOL_POWER_CAP
        from ..query_ratings import player_ratings
        profile = self.profile
        self.detail = player_ratings(self.career, profile, species=self.over_class.currentData(),
            tier=self.over_tier.currentData(), achievements=profile.get('achievements'))
        self.radar.set_scores(self.detail['scores'], overall=self.detail['overall'])
        self.overview_zoom.setEnabled(len(self.radar.keys) >= 3)
        rating = profile.get('rating') or {}
        stats = profile.get('metrics') or {}
        pr = rating.get('pr')
        self.account_metrics['pr'].set_rating(pr, pr)
        self.account_metrics['power'].set_rating(self.detail['power'], pr)
        self.account_metrics['battles'].set_value(count_text(stats.get('battles')))
        self.account_metrics['wins'].set_value(percent(stats.get('win_rate')))
        rates = ' · '.join(f'{grade} +{bonus * 100:g}%' for grade, bonus in ACHIEVEMENT_POWER_BONUSES.items())
        self.account_metrics['power'].setToolTip(
            f'已达成成就按稀有度累加，最高 +{ACHIEVEMENT_POWER_CAP:.0%}\n{rates}\n'
            f'船池按有随机战记录的不同战舰数取对数，最高 +{SHIP_POOL_POWER_CAP:.0%}\n数字颜色对应随机战 PR')
        self.account_exhibit.set_profile(profile,self.detail)
        fleet_known=profile.get('ships_status') in ('ok','no_battles')
        self.class_donut.set_records(profile.get('ships') or [],self.over_class.currentData(),available=fleet_known)
        self.tier_donut.set_records(profile.get('ships') or [],self.over_tier.currentData(),available=fleet_known)
        styles = [f'{row[0]}　+{count_text(row[3])}' for row in self.detail['styles']]
        mods = self.detail.get('modifiers') or {}
        mod_names = {'challenge':'成就稀有度','horse':'舰种构成','variety':'舰种多样性',
                     'veteran':'参战经验','ship_pool':'船池规模'}
        factors = [f"{mod_names[k]} ×{v:.{4 if k in ('challenge', 'ship_pool') else 2}f}"
                   for k, v in mods.items() if k in mod_names and isinstance(v, (int, float))]
        self.styles.setText('\n'.join(styles + factors) or '资料不足')
        self.achievements.set_detail(self.detail, (profile.get('region'), profile.get('account_id')))
        self.ship_collections.set_records(self.detail.get('ship_collections'))
        self.card_btn.setEnabled(bool(profile.get('metrics',{}).get('battles')))

    def _modes(self,*_):
        if self._loading or not self.profile:
            return
        record = self.career.store.get_run(self.profile['snapshot_id'])['payload']
        entry = record.get('mode_results',{}).get(self.mode.currentData(),{}).get('account') or {}
        status = entry.get('status')
        values = metrics(entry.get('statistics') if status in ('ok','no_battles') else None)
        note = '当前快照未提供该模式' if not entry or status=='unsupported' else query_status(entry,'当前模式资料未知')
        self.mode_note.setText(note)
        self.mode_note.setVisible(status!='ok')
        set_rows(self.mode_stats,[(k,[label,value_text(k,values.get(k))]) for k,label in METRICS])
        self.mode_stats.setToolTip(note)

    def _ship_rows(self,*_):
        if self._loading or not self.profile:
            return
        text = self.ship_name.text().strip().casefold()
        rows = [s for s in self.profile.get('ships',[]) if (s.get('battles') or 0)>=self.min_battles.value()
                and (not text or text in s['name'].casefold()) and all(
            box.currentData() is None or s.get(key)==box.currentData()
            for box,key in ((self.ship_class,'species'),(self.ship_tier,'tier'),(self.nation,'nation')))]
        self.distribution.set_records(rows)
        self.ranking_note.hide()
        sort = self.sort.currentData()
        if sort == 'ship_battle_power':
            positive = [s for s in rows if (s.get(sort) or 0)>0]
            if rows and not positive:
                self.ranking_note.setText('当前范围无正战力舰船')
                self.ranking_note.show()
            rows = positive
        rows.sort(key=lambda s: (s.get(sort) is None,-(s.get(sort) or 0),s['name']))
        rows = rows[:self.limit.value()]
        old = self._ship_id()
        self.ship_table.blockSignals(True)
        set_rows(self.ship_table,[(r['ship_id'],[r['name'],CLASS_NAMES.get(r.get('species'),'未知'),r.get('tier') or '—',*[value_text(k,r.get(k)) for k in
                 ('battles','win_rate','avg_damage','avg_xp','avg_frags','survival_rate','pr','ship_battle_power')]]) for r in rows])
        for index, row in enumerate(rows):
            style_rating_item(self.ship_table.item(index, 9), row.get('pr'))
            if row.get('ship_battle_power') is not None:
                style_rating_item(self.ship_table.item(index, 10), row.get('pr'))
        idx = next((i for i,r in enumerate(rows) if r['ship_id']==old),0)
        if self.ship_table.rowCount():
            self.ship_table.selectRow(idx)
        self.ship_table.blockSignals(False); self._ship_selected()

    def _ship_id(self):
        item = self.ship_table.item(self.ship_table.currentRow(),0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _ship_selected(self):
        if not self.profile:
            return
        self.radar_focus.reset()
        self.ship_zoom.setEnabled(False)
        from ..query_ratings import ship_ratings
        ship = self._ship_id()
        if not ship:
            self.ship_radar.set_scores({}); self.ship_image.clear(); self.ship_info.clear_summary(); return
        left = ship_ratings(self.career,self.profile,ship)
        right = ship_ratings(self.career,self.peer,ship) if self.peer else {}
        axes = ('胜率','场均伤害','场次','场均经验','击杀效率','主炮命中')
        series = [{key:result['scores'][key] for key in axes if key in result.get('scores',{})} for result in (left,right)]
        self.ship_radar.set_scores(*series)
        self.ship_zoom.setEnabled(len(self.ship_radar.keys) >= 3)
        row = next(r for r in self.profile.get('ships',[]) if str(r['ship_id'])==str(ship))
        peer_row = next((r for r in (self.peer or {}).get('ships', []) if str(r['ship_id']) == str(ship)), {})
        accounts = [{'name':self.profile['nickname'], 'record':row, 'ratings':left}]
        if self.peer:
            accounts.append({'name':self.peer['nickname'], 'record':peer_row, 'ratings':right})
        self.ship_info.set_ship(ship,row['name'],accounts)

    def find_peer(self):
        if not self.profile or not self._ship_id():
            self._say('先选择要比较的舰船'); return
        text = self.peer_edit.text().strip()
        if not text:
            self._say('请输入对比玩家昵称或 ID'); return
        local = [t for t in self.service.directory() if t['kind']=='player' and t['region']==self.profile['region']
                 and (text.casefold()==t['name'].casefold() or text==t['id'])]
        def done(result):
            choices = result['candidates']
            repopulate(self.peer_choices,[(t,t['name']) for t in choices])
            reason = (result.get('messages') or [(None,None,None)])[0]
            self._say(f'{len(choices)} 个对比候选' if choices else query_status({'status':reason[1],'reason':reason[2]},'未找到玩家'))
        if local:
            done({'candidates':local}); return
        region = self.profile['region']
        self._job(lambda cancel,progress:self.service.search(region,text,'player',cancel),done,'搜索对比玩家…')

    def expand_overview(self):
        if self.profile:
            caption = ' · '.join((REGIONS[self.profile['region']], self.over_class.currentText(),
                                  self.over_tier.currentText()))
            self.radar_focus.present(self.radar, self.profile['nickname'], caption, self.overview_zoom)

    def _select_overview_filter(self, box, value):
        index = box.findData(value)
        if index >= 0:
            box.setCurrentIndex(index)

    def open_ace_ship(self):
        ace = (self.profile or {}).get('ace_ship') or {}
        if not ace.get('ship_id'):
            return
        self.tabs.setCurrentIndex(2)
        for box in (self.ship_class,self.ship_tier,self.nation):
            box.setCurrentIndex(max(0,box.findData(None)))
        self.min_battles.setValue(1)
        self.ship_name.setText(ace.get('name') or '')
        self._ship_rows()
        for row in range(self.ship_table.rowCount()):
            if str(self.ship_table.item(row,0).data(Qt.ItemDataRole.UserRole)) == str(ace['ship_id']):
                self.ship_table.selectRow(row)
                break

    @staticmethod
    def _axis_caption(chart, key):
        entry = chart.scores.get(key)
        raw = entry[0] if entry else None
        if raw is None:
            observed = '未知'
        elif key == '🐴含量':
            observed = f'纯水面舰占比 {raw:.1%}'
        elif key in ('胜率','主炮命中','隐藏🐴'):
            observed = f'{raw:.1%}'
        elif key == '击杀效率':
            observed = f'{raw:.2f}'
        else:
            observed = f'{raw:,.1f}'.rstrip('0').rstrip('.')
        return f'{axis_title(key)}  {observed}　·　维度分 {chart._readout(key, include_grade=True)}'

    def inspect_overview_axis(self, key):
        self.expand_overview()
        self.radar_focus.caption.setText(self.radar_focus.caption.text()+'\n'+self._axis_caption(self.radar,key))
        self.radar_focus.chart._activate_axis(key)

    def inspect_ship_axis(self, key):
        self.expand_ship()
        self.radar_focus.caption.setText(self.radar_focus.caption.text()+'\n'+self._axis_caption(self.ship_radar,key))
        self.radar_focus.chart._activate_axis(key)

    def expand_ship(self):
        if not self.profile or not self._ship_id():
            return
        ship = next(r for r in self.profile['ships'] if str(r['ship_id']) == str(self._ship_id()))
        legend = f"{self.profile['nickname']}　·　{count_text(ship.get('battles'))} 场"
        if self.peer:
            if self.ship_radar.comparison:
                legend = f"青实线　{legend}　　 粉虚线　{self.peer['nickname']}"
            else:
                legend += f"　　 {self.peer['nickname']}　·　该舰资料不足"
        self.radar_focus.present(self.ship_radar, ship['name'], legend, self.ship_zoom)

    def compare(self):
        target = self.peer_choices.currentData()
        if not target:
            self._say('先搜索并选择对比玩家'); return
        def load(cancel, progress):
            profile = self.career.profile(target['region'],target['id'])
            if not profile.get('snapshot_id') or profile.get('ships_status')=='not_fetched' and profile.get('account_status')=='ok':
                profile = self.career.refresh(target['region'],target['id'], 'pvp',cancel,progress,record_failures=False)
            return profile
        def loaded(profile):
            self.peer = profile if profile.get('ships_status')=='ok' else None
            self._ship_selected()
            self._say('' if self.peer else query_status(profile))
        self._job(load,loaded,'读取对手船仓…')

    def clear_peer(self):
        self.peer = None; self.peer_edit.clear(); self.peer_choices.clear(); self._ship_selected()

    def open_card(self):
        from ..query_ratings import player_ratings
        if self.profile and self.card_btn.isEnabled():
            data = player_ratings(self.career,self.profile,achievements=self.profile.get('achievements'))
            SnapshotCardDialog(self.profile, data, self, dog_tag_png=self._active_dog_tag_png).exec()

    def _show_clan(self):
        self.stack.setCurrentIndex(2)
        self.frames = self.clan.pop('frames',{})
        self._render_clan()
        self._say('')

    def _frames_for(self, clan, results, cancel, progress):
        from ..query_ratings import player_frame
        frames = {}
        for index,(aid,ref) in enumerate(results.items(),1):
            if cancel.is_set():
                raise InterruptedError('已取消读取船仓')
            if ref.get('snapshot_id') and ref.get('status') in ('ok','no_battles'):
                progress(f'整理成员船仓 {index}/{len(results)}')
                profile = self.career.profile(clan['region'],aid,'pvp',snapshot_id=ref['snapshot_id'])
                frames[int(aid)] = player_frame(self.career,profile)
        return frames

    def _render_clan(self,*_):
        if not self.clan:
            return
        from ..query_ratings import clan_ratings
        info = self.clan['clan_info']
        self.clan_heading.setText(self.service.clan_name(info.get('tag'),info.get('name'))+'　'+REGIONS[self.clan['region']])
        species, tier = self.clan_class.currentData(),self.clan_tier.currentData()
        stats = clan_ratings(info,self.clan['batch_players'],self.career.references['benchmarks'],
                             member_frames=self.frames,species=species,tier=tier)
        self.clan_radar.set_scores(stats['scores'], overall=stats['overall'])
        members = stats['members']
        self.clan_summary.set_summary(stats, filtered=species is not None or tier is not None,
                                      fleets_loaded=bool(self.frames))
        self.clan_heading.setToolTip(str(info.get('motto') or ''))
        rows=[]
        rank_key = 'score' if species or tier else 'avg_damage'
        roles = {'commander':'指挥官','executive_officer':'副指挥官','recruitment_officer':'招募官',
                 'commissioned_officer':'军官','officer':'军官','private':'成员','recruit':'新成员'}
        colors = []
        for m in sorted(members, key=lambda m:(m.get(rank_key) is None,-(m.get(rank_key) or 0),str(m.get('nickname') or ''))):
            grade,color = member_grade(m.get('score'))
            colors.append(color)
            rows.append((m['account_id'],[m.get('nickname') or m['account_id'],roles.get(m.get('role'),m.get('role') or '未知'),
                MEMBER_STATUS.get(m['status'],'未知'),count_text(m.get('battles')),percent(m.get('win_rate')),
                count_text(m.get('avg_damage')),grade]))
        set_rows(self.member_table, rows)
        self.member_table.setColumnHidden(6, not (species or tier))
        for row,color in enumerate(colors):
            if color:
                self.member_table.item(row,6).setForeground(QColor(color))

    def fetch_fleets(self):
        if not self.clan:
            return
        clan = self.clan
        def load(cancel,progress):
            refs = self.service.load_member_ships(clan,cancel,progress)
            return self._frames_for(clan,refs,cancel,progress),len(refs),sum(v.get('refresh_failed',False) for v in refs.values())
        def received(result):
            self.frames,total,failed = result
            self._render_clan()
            self._say(f'可用 {len(self.frames)} / {total} 个成员船仓'+(f' · 本次 {failed} 个未刷新' if failed else ''))
        self._job(load,received,'读取所选公会成员船仓…')

    def open_member(self):
        item = self.member_table.item(self.member_table.currentRow(),0)
        if item and self.clan:
            self.open_target(player_target(self.clan['region'],item.data(Qt.ItemDataRole.UserRole),item.text()),query_missing=True)
