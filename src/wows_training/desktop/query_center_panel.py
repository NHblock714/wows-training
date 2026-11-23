"""Account and clan lookup, ship comparisons and public battle cards."""
from __future__ import annotations
from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView,
    QFrame, QGridLayout, QLineEdit, QPushButton, QScrollArea, QSpinBox, QStackedWidget, QTabWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget)

from ..career_rating import metrics
from ..query_center import QueryCenter, player_target
from .fleet_shell import FleetPageHeader
from .multiview_panel import RecordingJob
from .player_dossier_panel import CLASS_NAMES, MODES, REGIONS, count_text, percent, query_status, time_text
from .query_visuals import CareerDistribution, RadarFocusOverlay, RatingRadar, SnapshotCardDialog, axis_title, plain_label
from .statistics_visuals import MetricCard
from .layered_panel import LayeredPanel
from . import academy_palette as tone
from .ui_components import Disclosure
from .account_exhibit import AccountExhibit, ConstellationBackdrop, InteractiveDonut
from .clan_summary import ClanSummary
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
        self.frames = {}
        self.peer = None
        self._picker_context = None
        self._loading = False
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
        self.saved = combo(); self.candidates = combo([(None, '搜索候选')])
        self.saved.setAccessibleName('常用与缓存'); self.candidates.setAccessibleName('搜索候选')
        self.saved.activated.connect(self._open_saved)
        saved.addWidget(self.saved, 1); saved.addWidget(self.candidates,1)
        saved.addWidget(button('打开所选',self.open_candidate))
        self.mark = button('收藏',self.bookmark); self.mark.setEnabled(False)
        saved.addWidget(self.mark)
        self.refresh_button = button('刷新当前',self.refresh_current); self.refresh_button.setEnabled(False)
        saved.addWidget(self.refresh_button)
        fields.addLayout(saved)
        picker=QHBoxLayout()
        self.use_account_button=button('选用此账号',self.use_selected_account);self.use_account_button.setObjectName('queryUseAccount');self.use_account_button.hide();self.use_account_button.setEnabled(False)
        self.return_dossier_button=button('返回选手档案',self.cancel_account_picker);self.return_dossier_button.setObjectName('queryReturnDossier');self.return_dossier_button.hide()
        picker.addStretch();picker.addWidget(self.return_dossier_button);picker.addWidget(self.use_account_button);fields.addLayout(picker)
        for signal in (self.region.currentIndexChanged,self.kind.currentIndexChanged,self.text.textChanged):
            signal.connect(self._clear_candidates)
        status_row = QHBoxLayout()
        self.notice = plain_label()
        self.notice.hide()
        self.cancel_button = button('取消查询',self.cancel); self.cancel_button.hide()
        status_row.addWidget(self.notice,1); status_row.addWidget(self.cancel_button)
        outer.addLayout(status_row)
        self.stack = QStackedWidget(); outer.addWidget(self.stack,1)
        empty = ConstellationBackdrop()
        self.stack.addWidget(empty)
        self._build_player()
        self._build_clan()
        self.radar_focus = RadarFocusOverlay(self)
        self.tabs.currentChanged.connect(self.radar_focus.reset)
        self.stack.currentChanged.connect(self.radar_focus.reset)

    def _build_player(self):
        page, box = form(); self.stack.addWidget(page)
        head = QHBoxLayout()
        self.player_heading = plain_label(); self.player_heading.setObjectName('sectionTitle')
        head.addWidget(self.player_heading,1)
        self.card_btn = button('战绩名片',self.open_card)
        head.addWidget(self.card_btn); head.addWidget(button('个人档案',self.open_dossier)); box.addLayout(head)
        self.tabs = QTabWidget(); box.addWidget(self.tabs,1)
        overview, overview_box = form()
        scroller = self.overview_scroll = QScrollArea(); scroller.setWidgetResizable(True); scroller.setWidget(overview)
        self.tabs.addTab(scroller,'总览')
        kpis = QHBoxLayout()
        self.account_metrics = {}
        for key, title in (('pr','随机战 PR'),('power','综合战力'),('battles','随机战场次'),('wins','随机战胜率')):
            metric = MetricCard(title)
            kpis.addWidget(metric, 1)
            self.account_metrics[key] = metric
        overview_box.addLayout(kpis)
        filters = QHBoxLayout()
        self.over_class = combo(); self.over_tier = combo()
        filters.addWidget(self.over_class); filters.addWidget(self.over_tier); filters.addStretch()
        self.summary = plain_label(); self.summary.setObjectName('sectionTitle')
        filters.addWidget(self.summary)
        self.overview_zoom = button('放大雷达', self.expand_overview)
        filters.addWidget(self.overview_zoom)
        overview_box.addLayout(filters)
        self.constellation = ConstellationBackdrop()
        exhibit_box = QVBoxLayout(self.constellation)
        exhibit_box.setContentsMargins(4,6,4,8)
        exhibit_box.setSpacing(12)
        graphs = QHBoxLayout()
        self.radar = RatingRadar()
        self.radar.setMinimumHeight(344)
        self.radar.axisActivated.connect(self.inspect_overview_axis)
        graphs.addWidget(self.radar,3)
        self.account_exhibit = AccountExhibit()
        self.constellation.phaseChanged.connect(self.radar.set_ambient_phase)
        self.constellation.phaseChanged.connect(self.account_exhibit.set_ambient_phase)
        self.account_exhibit.aceRequested.connect(self.open_ace_ship)
        graphs.addWidget(self.account_exhibit,2)
        exhibit_box.addLayout(graphs)
        composition = QHBoxLayout()
        self.class_donut = InteractiveDonut('class')
        self.tier_donut = InteractiveDonut('tier')
        self.class_donut.activated.connect(lambda value:self._select_overview_filter(self.over_class,value))
        self.tier_donut.activated.connect(lambda value:self._select_overview_filter(self.over_tier,value))
        composition.addWidget(self.class_donut,1)
        composition.addWidget(self.tier_donut,1)
        exhibit_box.addLayout(composition)
        overview_box.addWidget(self.constellation,1)
        self.styles = plain_label(); self.challenges = plain_label()
        overview_box.addWidget(Disclosure('风格与战力修正',self.styles))
        overview_box.addWidget(Disclosure('挑战记录',self.challenges))
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
        self.distribution = CareerDistribution(); ships_box.addWidget(self.distribution)
        self.ranking_note = plain_label(); ships_box.addWidget(self.ranking_note)
        self.ranking_note.hide()
        self.ship_table = table(['舰船','舰种','等级','场次','胜率','场均伤害','场均经验','场均击沉','生存率','PR','战力'])
        self.ship_table.setMinimumHeight(110); self.ship_table.setColumnWidth(0,130)
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
        entries = [(None,'常用与本地档案')]
        for prefix, items in [('★ ',favorites),('',[x for x in cached if self.service.key(x) not in marked])]:
            for row in items:
                label = REGIONS[row['region']] + (' · 公会' if row['kind']=='clan' else '')
                entries.append((row, f"{prefix}{row['name']} · {label}"))
        repopulate(self.saved, entries)
        self.mark.setEnabled(bool(self.target))
        self.mark.setText('取消收藏' if self.target and self.service.key(self.target) in marked else '收藏')
        self.refresh_button.setEnabled(bool(self.target))

    def _clear_candidates(self, *_):
        repopulate(self.candidates, [(None,'搜索候选')])

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
        self.target=self.profile=self.clan=self.detail=None
        if self.profile_ai is not None:
            self.profile_ai.set_source()
        self.stack.setCurrentIndex(0)
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
        self.cancel_button.show(); self.cancel_button.setEnabled(True); self._say(message)
        job.progress.connect(self._say)
        self._accept_result = success
        job.succeeded.connect(self._delivered)
        job.failed.connect(self._failed)
        job.finished.connect(self._finished)
        job.start()

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
        self.inputs.setEnabled(True); self.stack.setEnabled(True); self.cancel_button.hide()
        if job:
            job.deleteLater()
        self.idle.emit()

    def cancel(self):
        if self.profile_ai is not None and self.profile_ai.busy:
            self.profile_ai.cancel()
        if self.job:
            self.job.cancel_event.set(); self.cancel_button.setEnabled(False); self._say('查询已取消')

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

    def _open_saved(self):
        target = self.saved.currentData()
        if target:
            self.open_target(target)

    def open_candidate(self):
        target = self.candidates.currentData()
        if target:
            self.open_target(target, query_missing=True)
        else:
            self._say('请先搜索并选择结果')

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
                    return dict(stored, frames=self._frames_for(stored,refs,cancel,progress))
                return stored or {'status':'not_fetched','reason':'no_snapshot'}
            profile = self.career.profile(region, identifier)
            if force or query_missing and not profile.get('snapshot_id'):
                profile = self.career.refresh(region, identifier, 'pvp', cancel, progress, include_achievements=True,
                                              include_all_fleets=True, record_failures=False)
            return profile
        def display(value):
            if target['kind']=='player' and (value.get('region'),str(value.get('account_id')))!=(region,str(identifier)):
                self._say('返回账号与所选账号不一致，未载入');return
            if value.get('status') in ('error','not_fetched') and self.target and self.service.key(target)==self.service.key(self.target) and (self.profile or self.clan):
                self._say(query_status(value)+' · 当前显示已有快照')
                return
            self.target = dict(target)
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
                if portrait and self.profile_ai is not None:
                    self.tabs.setCurrentWidget(self.profile_ai)
            self.refresh()
            self.use_account_button.setEnabled(self._picker_context is not None and target['kind']=='player')
        self._job(load, display, '正在读取战绩…')

    def bookmark(self):
        if self.target:
            self.service.toggle_bookmark(self.target); self.refresh()

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
            if self.profile_ai is not None:
                self.profile_ai.set_source()
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
        self._say('' if profile.get('status')=='ok' else query_status(profile))

    def _overview(self,*_):
        if self._loading or not self.profile:
            return
        self.radar_focus.reset()
        from ..query_ratings import player_ratings
        profile = self.profile
        self.detail = player_ratings(self.career, profile, species=self.over_class.currentData(),
            tier=self.over_tier.currentData(), achievements=profile.get('achievements'))
        scores = self.detail['scores']; self.radar.set_scores(scores)
        self.overview_zoom.setEnabled(len(self.radar.keys) >= 3)
        rating = profile.get('rating') or {}
        stats = profile.get('metrics') or {}
        for key, text in (('pr',count_text(rating.get('pr'))),('power',count_text(self.detail['power'])),
                          ('battles',count_text(stats.get('battles'))),('wins',percent(stats.get('win_rate')))):
            self.account_metrics[key].set_value(text)
        self.account_metrics['power'].setToolTip('含已确认挑战修正')
        self.summary.setText(f"维度均分　{count_text(self.detail.get('overall'))}")
        self.account_exhibit.set_profile(profile,self.detail)
        fleet_known=profile.get('ships_status') in ('ok','no_battles')
        self.class_donut.set_records(profile.get('ships') or [],self.over_class.currentData(),available=fleet_known)
        self.tier_donut.set_records(profile.get('ships') or [],self.over_tier.currentData(),available=fleet_known)
        styles = [f'{row[0]}　+{count_text(row[3])}' for row in self.detail['styles']]
        mods = self.detail.get('modifiers') or {}
        mod_names = {'challenge':'挑战','horse':'舰种构成','variety':'舰种多样性','veteran':'参战经验'}
        factors = [f"{mod_names[k]} ×{v:.2f}" for k,v in mods.items() if k in mod_names and isinstance(v,(int,float))]
        self.styles.setText('\n'.join(styles + factors) or '资料不足')
        achievements = ['　'.join(str(x) for x in row[:2]) for row in self.detail['challenges']]
        if not self.detail.get('challenge_complete_data'):
            achievements.append('部分快照字段或战斗成就未知')
        self.challenges.setText('\n'.join(achievements) or '尚无达成记录')
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
        old = self._ship_id()
        self.ship_table.blockSignals(True)
        set_rows(self.ship_table,[(r['ship_id'],[r['name'],CLASS_NAMES.get(r.get('species'),'未知'),r.get('tier') or '—',*[value_text(k,r.get(k)) for k in
                 ('battles','win_rate','avg_damage','avg_xp','avg_frags','survival_rate','pr','ship_battle_power')]]) for r in rows[:self.limit.value()]])
        idx = next((i for i,r in enumerate(rows[:self.limit.value()]) if r['ship_id']==old),0)
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
                                  self.over_tier.currentText(), self.summary.text()))
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
        elif key in ('胜率','主炮命中','🐴含量','隐藏🐴'):
            observed = f'{raw:.1%}'
        elif key == '击杀效率':
            observed = f'{raw:.2f}'
        else:
            observed = f'{raw:,.1f}'.rstrip('0').rstrip('.')
        return f'{axis_title(key)}  {observed}　·　维度分 {chart._readout(key)}'

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
        legend = f"青实线　{self.profile['nickname']}　·　{count_text(ship.get('battles'))} 场"
        if self.peer:
            legend += f"　　 粉虚线　{self.peer['nickname']}"
            if not self.ship_radar.comparison:
                legend += '　·　该舰资料不足'
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
            SnapshotCardDialog(self.profile,data,self).exec()

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
        self.clan_radar.set_scores(stats['scores'])
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
