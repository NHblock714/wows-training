"""Nontechnical team/player selection statistics and verified damage summaries."""
from datetime import datetime
from pathlib import Path
from PySide6.QtCore import Qt,Signal
from PySide6.QtWidgets import (QAbstractItemView,QCheckBox,QComboBox,QFileDialog,QHBoxLayout,QHeaderView,QLabel,QPlainTextEdit,QPushButton,
                              QSizePolicy,QSplitter,QTableWidget,QTableWidgetItem,QTabWidget,QToolButton,QVBoxLayout,QWidget)
from .multiview_panel import RecordingJob
from .ui_components import StatusLabel
from .statistics_visuals import MetricCard,RankedBars,LineupRoster,chart_scroll,lineup_groups,section,CLASS_COLORS,RESULT_COLORS
from wows_training.statistics_analysis import CATEGORIES,aggregate_damage,aggregate_outcomes,WIN_RATE_RULE
from wows_training.statistics_reporting import shown,percentage,OUTCOMES

CLASSES={'Battleship':'战列舰','Cruiser':'巡洋舰','Destroyer':'驱逐舰','AirCarrier':'航空母舰','Submarine':'潜艇'}


def grid(headers,name,numeric=(),details=False):
    table=QTableWidget(0,len(headers));table.setObjectName(name);table.setHorizontalHeaderLabels(headers)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers);table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.verticalHeader().hide();table.setAlternatingRowColors(True);table.setWordWrap(False)
    table.verticalHeader().setDefaultSectionSize(36);table.setShowGrid(False);table.setProperty('numericColumns',list(numeric))
    table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive if details else QHeaderView.ResizeMode.Stretch)
    table.horizontalHeader().setMinimumSectionSize(54)
    for col in numeric:table.horizontalHeaderItem(col).setTextAlignment(Qt.AlignmentFlag.AlignRight|Qt.AlignmentFlag.AlignVCenter)
    return table


def rows(table,values,identities=None):
    table.blockSignals(True)
    table.setRowCount(len(values))
    for i,row in enumerate(values):
        for col,value in enumerate(row):
            cell=QTableWidgetItem(shown(value));cell.setToolTip(shown(value))
            if col in (table.property('numericColumns') or []):cell.setTextAlignment(Qt.AlignmentFlag.AlignRight|Qt.AlignmentFlag.AlignVCenter)
            if identities is not None:cell.setData(Qt.ItemDataRole.UserRole,identities[i])
            table.setItem(i,col,cell)
    if table.horizontalHeader().sectionResizeMode(0)==QHeaderView.ResizeMode.Interactive:
        table.resizeColumnsToContents()
        for col in range(table.columnCount()):table.setColumnWidth(col,min(280,max(75,table.columnWidth(col))))
    table.blockSignals(False)


def fold(title,content):
    from .ui_components import Disclosure
    wrapper=Disclosure(title,content)
    return wrapper,wrapper.toggle_button


class StatisticsPanel(QWidget):
    idle=Signal()

    def __init__(self,host):
        super().__init__(host);self.host=host;self.job=None;self.result=None;self._match_rows=[];self._lineup_rows=[];self._lineup_key=None
        layout=QVBoxLayout(self);layout.setContentsMargins(26,23,26,18);layout.setSpacing(10)
        title=QLabel('选手与阵容');title.setObjectName('title');layout.addWidget(title)
        controls=QHBoxLayout();self.scope=QComboBox();self.scope.setObjectName('statisticsScope');controls.addWidget(self.scope,3)
        self.team=QComboBox();self.team.setEditable(True);self.team.addItems(['ARIS','BYL','FND']);self.team.setObjectName('statisticsTeam')
        controls.addWidget(QLabel('队伍'));controls.addWidget(self.team,1)
        self.team.setToolTip('按赛事账号中的队伍前缀匹配；缺失或跨边冲突的比赛不纳入。')
        self.only_formal=QCheckBox('仅正式赛');self.only_formal.setToolTip('按人工比赛分类筛选');controls.addWidget(self.only_formal)
        self.refresh_button=QPushButton('刷新');self.refresh_button.clicked.connect(self.refresh_scopes);controls.addWidget(self.refresh_button);layout.addLayout(controls)
        actions=QHBoxLayout();self.run_button=QPushButton('生成分析');self.run_button.setObjectName('runStatistics');self.run_button.clicked.connect(self.run_statistics)
        self.cancel_button=QPushButton('取消');self.cancel_button.setEnabled(False);self.cancel_button.clicked.connect(self.cancel)
        self.export_button=QPushButton('导出');self.export_button.setToolTip('导出整个比赛集合的 HTML、JSON 和 CSV。');self.export_button.setObjectName('exportStatistics');self.export_button.setEnabled(False);self.export_button.clicked.connect(self.export_result)
        for button in (self.run_button,self.cancel_button,self.export_button):actions.addWidget(button)
        self.player=QComboBox();self.player.setObjectName('statisticsPlayer');self.player.addItem('全部选手 / 赛号',None);self.player.currentIndexChanged.connect(self.render)
        self.ship=QComboBox();self.ship.setObjectName('statisticsShip');self.ship.addItem('全部舰船',None);self.ship.currentIndexChanged.connect(self.render)
        actions.addWidget(self.player,3);actions.addWidget(self.ship,2)
        self.dossier_button=QPushButton('个人档案')
        self.dossier_button.clicked.connect(lambda:self.host.open_player_profile(self.player.currentData(),
            [row['battle_id'] for row in self.result['battles']] if self.result else None))
        actions.addWidget(self.dossier_button)
        self.clear_filter=QPushButton('清空筛选');self.clear_filter.clicked.connect(self.reset_filters);actions.addWidget(self.clear_filter);layout.addLayout(actions)
        self.notice=StatusLabel();self.notice.setObjectName('subtitle');self.notice.setWordWrap(True);layout.addWidget(self.notice)
        self.summary=QLabel();self.summary.setObjectName('statisticsSummary');self.summary.hide()
        metrics=QHBoxLayout();self.metric_cards={}
        for key,label in [('matches','样本比赛'),('appearances','出场席位'),('players','选手 / 赛事账号'),('win_rate','队伍胜率')]:
            card=MetricCard(label);self.metric_cards[key]=card;metrics.addWidget(card)
        layout.addLayout(metrics)
        self.tabs=QTabWidget();self.tabs.setObjectName('statisticsTabs');layout.addWidget(self.tabs,1)
        team_page=QWidget();team_layout=QVBoxLayout(team_page);team_layout.setContentsMargins(0,0,0,0)
        team_split=QSplitter();team_layout.addWidget(team_split,1)
        ships_card,ships_layout=section('舰船偏好','出现局数 / 集合场次')
        self.ship_chart=RankedBars();self.ship_chart.setObjectName('teamShipChart');self.ship_chart.activated.connect(self.select_ship);ships_layout.addWidget(chart_scroll(self.ship_chart),1)
        self.team_table=grid(['舰船','舰种','出场席位','出现局数','出现比例'],'teamShipStatistics',(2,3,4),True);self.team_table.setMaximumHeight(190)
        team_detail,_=fold('舰船明细',self.team_table);ships_layout.addWidget(team_detail);team_split.addWidget(ships_card)
        lineup_card,lineup_layout=section('阵容构成')
        self.lineup_chart=RankedBars(compact=True);self.lineup_chart.setObjectName('lineupChart');self.lineup_chart.activated.connect(self.select_lineup)
        lineup_scroll=chart_scroll(self.lineup_chart);lineup_scroll.setFixedHeight(140);lineup_layout.addWidget(lineup_scroll)
        self.lineup_label=QLabel('逐场阵容');self.lineup_label.setObjectName('subtitle');lineup_layout.addWidget(self.lineup_label)
        self.lineup_table=grid(['日期 / 地图','舰船组合','赛果'],'lineupStatistics');self.lineup_table.itemDoubleClicked.connect(self.open_lineup_match);self.lineup_table.itemSelectionChanged.connect(self.show_lineup_roster);lineup_layout.addWidget(self.lineup_table,2)
        self.lineup_table.setMinimumHeight(80);self.lineup_table.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Ignored)
        self.lineup_roster=LineupRoster();self.lineup_roster.setObjectName('selectedLineupRoster');lineup_layout.addWidget(self.lineup_roster)
        lineup_card.setMinimumHeight(520)
        team_split.addWidget(chart_scroll(lineup_card));team_split.setSizes([440,620]);self.tabs.addTab(team_page,'阵容分析')
        personal=QWidget();personal_layout=QVBoxLayout(personal);personal_layout.setContentsMargins(0,0,0,0)
        personal_split=QSplitter();personal_layout.addWidget(personal_split,1)
        roster_card,roster_layout=section('选手出场')
        self.player_chart=RankedBars();self.player_chart.setObjectName('playerAppearancesChart');self.player_chart.activated.connect(self.select_player)
        self.player_chart_scroll=chart_scroll(self.player_chart);roster_layout.addWidget(self.player_chart_scroll,1)
        self.player_table=grid(['选手 / 赛号','身份','参赛','舰船数','常用舰船'],'playerStatistics',(2,3),True);self.player_table.setMaximumHeight(190)
        player_detail,_=fold('选手明细',self.player_table);roster_layout.addWidget(player_detail);personal_split.addWidget(roster_card)
        proficiency_card,proficiency_layout=section('用船与胜率')
        self.player_heading=QLabel('全部选手 / 赛事账号');self.player_heading.setWordWrap(True);proficiency_layout.addWidget(self.player_heading)
        self.player_caption=QLabel('');self.player_caption.setObjectName('subtitle');self.player_caption.setWordWrap(True);proficiency_layout.addWidget(self.player_caption)
        self.proficiency_chart=RankedBars();self.proficiency_chart.setObjectName('playerShipChart');self.proficiency_chart.activated.connect(self.select_player_ship);proficiency_layout.addWidget(chart_scroll(self.proficiency_chart),1)
        legend=QLabel('条长：出场次数　青：胜　红：负　金：平　灰：未知');legend.setObjectName('subtitle');proficiency_layout.addWidget(legend)
        self.player_ship_table=grid(['选手 / 赛号','舰船','出场 / 本人参赛','选船比例','胜','负','平','未知','胜率 / 已确认局'],'playerShipStatistics',(2,3,4,5,6,7,8),True);self.player_ship_table.setMaximumHeight(210)
        self.player_ship_table.itemDoubleClicked.connect(self.inspect_player_ship)
        player_ship_detail,self.ship_detail_toggle=fold('用船明细',self.player_ship_table);proficiency_layout.addWidget(player_ship_detail)
        personal_split.addWidget(proficiency_card);personal_split.setSizes([370,690]);self.tabs.addTab(personal,'选手分析')
        damage=QSplitter()
        types_card,types_layout=section('承伤类型','赛后结算')
        self.damage_chart=RankedBars();self.damage_chart.setObjectName('damageTypeChart');types_layout.addWidget(chart_scroll(self.damage_chart),1)
        self.damage_type_table=grid(['类型','分类合计','已确认 / 参赛','状态'],'damageTypeStatistics',(1,2),True);self.damage_type_table.setMaximumHeight(170)
        types_detail,_=fold('类型明细',self.damage_type_table);types_layout.addWidget(types_detail);damage.addWidget(types_card)
        damage_card,damage_layout=section('选手承伤')
        self.damage_caption=QLabel('');self.damage_caption.setWordWrap(True);self.damage_caption.setObjectName('subtitle');damage_layout.addWidget(self.damage_caption)
        self.damage_player_table=grid(['选手 / 赛号','参赛','完整记录','承伤合计','已知部分','完整局均值'],'damagePlayerStatistics',(1,2,3,4,5),True);damage_layout.addWidget(self.damage_player_table,1)
        damage.addWidget(damage_card);damage.setSizes([350,710]);self.tabs.addTab(damage,'承伤分析')
        self.match_table=grid(['日期','地图','原始赛事赛号','舰船','承伤合计','确认字段','状态','赛果'],'damageMatchStatistics',(4,5),True);self.tabs.addTab(self.match_table,'比赛明细')
        self.details=QPlainTextEdit();self.details.setReadOnly(True);self.details.setMaximumHeight(160);self.details.setObjectName('statisticsDetails')
        self.details.setPlaceholderText('未选记录')
        self.match_table.itemSelectionChanged.connect(self.describe_match);self.match_table.itemDoubleClicked.connect(lambda _:self.open_match())
        bottom=QHBoxLayout();self.open_button=QPushButton('打开所选比赛');self.open_button.clicked.connect(self.open_match)
        self.source_button=QPushButton('打开原始结算来源');self.source_button.clicked.connect(self.open_result_source)
        self.evidence_panel=QWidget();evidence_layout=QVBoxLayout(self.evidence_panel);evidence_layout.setContentsMargins(0,0,0,0)
        evidence_fold,self.evidence_toggle=fold('记录来源',self.details);bottom.addWidget(self.open_button);bottom.addWidget(self.source_button);bottom.addStretch()
        evidence_layout.addLayout(bottom);evidence_layout.addWidget(evidence_fold);layout.addWidget(self.evidence_panel);self.evidence_panel.hide()
        self.tabs.currentChanged.connect(lambda index:self.evidence_panel.setVisible(index==3))
        self.refresh_scopes()

    @property
    def busy(self):return bool(self.job and self.job.isRunning())

    def refresh_scopes(self):
        selected=self.scope.currentData();self.scope.blockSignals(True);self.scope.clear()
        count=len({r['battle_id'] for r in self.host.recording_store.list_recordings()})
        self.scope.addItem(f'已保存双边比赛（{count} 场）',{'kind':'paired'})
        self.scope.addItem('使用比赛库当前选中项',{'kind':'selected'})
        self.scope.addItem('全部已导入比赛',{'kind':'all'})
        for doc in self.host.workbench.store.list_documents('collection'):self.scope.addItem('集合：'+doc['name'],{'kind':'collection','id':doc['id']})
        index=self.scope.findData(selected);self.scope.setCurrentIndex(max(0,index));self.scope.blockSignals(False)

    def selected_battle_ids(self):
        scope=self.scope.currentData() or {};kind=scope.get('kind')
        if kind=='paired':ids=sorted({r['battle_id'] for r in self.host.recording_store.list_recordings()})
        elif kind=='selected':ids=self.host.selected_battles()
        elif kind=='all':ids=[r['id'] for r in self.host.library.list_battles()]
        else:ids=self.host.workbench.store.get_document(scope['id'])['payload']['battle_ids']
        return [key for key in ids if not self.only_formal.isChecked() or self.host.library.get_battle(key)['label']=='正式比赛']

    def run_statistics(self):
        if self.busy:return
        if self.host.multiview_panel.busy or (self.host.import_worker and self.host.import_worker.isRunning()):
            self.notice.setText('请先完成当前导入任务。');return
        ids=self.selected_battle_ids();team=self.team.currentText().strip()
        if not ids or not team:self.notice.setText('当前范围没有比赛或队伍');return
        def operation(cancel,progress):
            from wows_training.statistics_service import StatisticsService
            return StatisticsService(self.host.library.root,self.host.bundle.root).analyze(ids,team,cancel,progress)
        self.job=RecordingJob(operation,self);self.job.progress.connect(self.notice.setText);self.job.succeeded.connect(self.show_result)
        self.job.failed.connect(lambda error:self.notice.setText('统计未完成：'+error));self.job.finished.connect(self.finished)
        self.run_button.setEnabled(False);self.cancel_button.setEnabled(True);self.export_button.setEnabled(False);self.job.start()

    def cancel(self):
        if self.busy:self.job.cancel_event.set();self.notice.setText('正在停止统计…')

    def finished(self):
        self.run_button.setEnabled(True);self.cancel_button.setEnabled(False);self.export_button.setEnabled(self.result is not None);self.idle.emit()

    def show_result(self,result):
        self.result=result;selected=self.player.currentData();selected_ship=self.ship.currentData();self.player.blockSignals(True);self.player.clear();self.player.addItem('全部选手 / 赛号',None)
        for p in result['players']:self.player.addItem(p['player_name'],p['player_key'])
        self.player.setCurrentIndex(max(0,self.player.findData(selected)));self.player.blockSignals(False)
        self.ship.blockSignals(True);self.ship.clear();self.ship.addItem('全部舰船',None)
        for ship in sorted(result['team_ships'],key=lambda r:r['ship_name']):self.ship.addItem(ship['ship_name'],ship['ship_id'])
        self.ship.setCurrentIndex(max(0,self.ship.findData(selected_ship)));self.ship.blockSignals(False)
        count=result['counts'];damage=result['received_damage']
        self.summary.setText(f'{result["team"]} · 纳入 {count["included_battles"]}/{count["requested_battles"]} 场 · {count["appearances"]} 个席位 · {count["players"]} 个选手/赛事账号 · 承伤完整记录 {damage["confirmed_appearances"]}/{damage["appearances"]} · 分类承伤合计 {shown(damage["total"])}')
        outcome=result.get('outcomes') or aggregate_outcomes(result['battles'])
        self.metric_cards['matches'].set_value(f'{count["included_battles"]} / {count["requested_battles"]}',f'{result["team"]} · 排除 {count["excluded_battles"]} 场')
        self.metric_cards['appearances'].set_value(count['appearances'],f'{count["unique_ships"]} 种舰船 · 双边去重')
        self.metric_cards['players'].set_value(count['players'],f'承伤完整 {damage["confirmed_appearances"]}/{damage["appearances"]} 条')
        self.metric_cards['win_rate'].set_value(percentage(outcome['win_rate']),f'{outcome["wins"]} 胜 {outcome["losses"]} 负 {outcome["draws"]} 平 · 未知 {outcome["unknown_results"]} 场')
        self.metric_cards['win_rate'].setToolTip(WIN_RATE_RULE+f'\n当前已确认赛果：{outcome["known_results"]} 场。')
        quality=[]
        if result.get('excluded'):quality.append(f"{len(result['excluded'])} 场未纳入："+'；'.join(dict.fromkeys(str(item['reason']) for item in result['excluded'])))
        warnings_count=len(result.get('source_warnings',[]))
        if warnings_count:quality.append(f'{warnings_count} 份录像数据不完整')
        if result.get('source_errors'):quality.append(f"{len(result['source_errors'])} 份录像读取失败")
        self.notice.setText(' · '.join(quality))
        self._lineup_groups=lineup_groups(result['damage_matches'])
        if self._lineup_key not in {g['key'] for g in self._lineup_groups}:self._lineup_key=None
        self.export_button.setEnabled(True);self.render()

    def render(self,*_):
        if self.result is None:return
        data=self.result;key=self.player.currentData();ship_key=self.ship.currentData()
        players=[r for r in data['players'] if key is None or r['player_key']==key]
        ships=[r for r in data['player_ships'] if (key is None or r['player_key']==key) and (ship_key is None or r['ship_id']==ship_key)]
        self._player_ship_rows=ships
        self._match_rows=[r for r in data['damage_matches'] if (key is None or r['player_key']==key) and (ship_key is None or r['ship_id']==ship_key)]
        rows(self.team_table,[[r['ship_name'],CLASSES.get(r['ship_class'],r['ship_class']),r['appearances'],f'{r["battle_count"]}/{r["battle_denominator"]}',f'{r["battle_share"]:.1%}'] for r in data['team_ships']])
        identity={'confirmed':'已确认个人','confirmed_account':'赛事账号分开统计','tournament_account':'赛事账号','conflict':'身份冲突'}
        rows(self.player_table,[[r['player_name'],identity.get(r['identity_status']),r['battle_count'],r['distinct_ships'],'；'.join(f'{name} {count} 场' for name,count in r['top_ships'])] for r in players])
        rows(self.player_ship_table,[[r['player_name'],r['ship_name'],f'{r["appearances"]}/{r["player_appearances"]}',f'{r["share"]:.1%}',r['wins'],r['losses'],r['draws'],r['unknown_results'],f'{percentage(r["win_rate"])} / {r["known_results"]} 场'] for r in ships])
        damage_players=[]
        for player in players:
            matching=[r for r in self._match_rows if r['player_key']==player['player_key']]
            if matching:damage_players.append({**player,**aggregate_damage(matching),'battle_count':len(matching)})
        rows(self.damage_player_table,[[self.short_name(r['player_name']),r['battle_count'],r['confirmed_appearances'],r['total'],r['known_subtotal'],r['confirmed_match_mean']] for r in damage_players])
        for i,player in enumerate(damage_players):self.damage_player_table.item(i,0).setToolTip(player['player_name'])
        self.damage_player_table.setColumnHidden(4,bool(damage_players) and all(r['total'] is not None for r in damage_players))
        damage=aggregate_damage(self._match_rows)
        rows(self.damage_type_table,[[name,damage['categories'][field],f'{damage["category_known_counts"][field]}/{damage["appearances"]}','完整' if damage['categories'][field] is not None else '未知 / 不完整'] for field,name in CATEGORIES])
        rows(self.match_table,[[r['date'],r['map_name'],r['tournament_name'],r['ship_name'],r['total'],f'{r["known_field_count"]}/{r["field_count"]}','完整' if r['total'] is not None else '冲突' if r['conflicts'] else '未知 / 不完整',OUTCOMES[r['outcome']]] for r in self._match_rows])
        self.details.clear();self.open_button.setEnabled(False);self.source_button.setEnabled(False)
        self.ship_chart.set_items([{'key':r['ship_id'],'title':r['ship_name'],'value':f'{r["battle_count"]} / {r["battle_denominator"]} 场',
            'subtitle':f'{CLASSES.get(r["ship_class"],"未知舰种")} · 出场 {r["appearances"]} 席 · 出现率 {r["battle_share"]:.0%}',
            'ratio':r['battle_share'],'color':CLASS_COLORS.get(r['ship_class'],'#8ba3bd')} for r in data['team_ships']],ship_key)
        all_players=sorted(data['players'],key=lambda r:(-r['battle_count'],r['player_name']))
        maximum=max([r['battle_count'] for r in all_players],default=1)
        self.player_chart.set_items([{'key':r['player_key'],'title':self.short_name(r['player_name']),
            'value':f'{r["battle_count"]} 场','subtitle':f'{r["distinct_ships"]} 种舰船 · 胜率 {percentage(r["win_rate"])} · 未知 {r["unknown_results"]} 场',
            'ratio':r['battle_count']/maximum,'tooltip':r['player_name']+'\n'+identity.get(r['identity_status'],'未知身份')} for r in all_players],key)
        if key is not None:
            index=next((i for i,r in enumerate(all_players) if r['player_key']==key),0)
            self.player_chart_scroll.ensureVisible(0,index*self.player_chart.row_height+32,0,45)
        if key is None:
            self.player_heading.setText('全部选手 / 赛事账号')
            self.player_caption.setText(f'{len(data["players"])} 个账号 · 选船比例按个人参赛数计算')
        else:
            person=next(r for r in players if r['player_key']==key)
            self.player_heading.setText(self.short_name(person['player_name']))
            self.player_heading.setToolTip(person['player_name'])
            self.player_caption.setText(f'{identity.get(person["identity_status"],"未知身份")} · {person["battle_count"]} 场 · {person["distinct_ships"]} 种舰船 · 胜率 {percentage(person["win_rate"])}（已知 {person["known_results"]} 场，未知 {person["unknown_results"]} 场）')
        ship_max=max([r['appearances'] for r in ships],default=1)
        self.proficiency_chart.set_items([{'key':(r['player_key'],r['ship_id']),
            'title':r['ship_name'] if key is not None else self.short_name(r['player_name'])+' · '+r['ship_name'],
            'value':percentage(r['win_rate']),
            'subtitle':f'{r["appearances"]}/{r["player_appearances"]} 场 · 选船 {r["share"]:.0%} · {r["wins"]}胜 {r["losses"]}负 {r["draws"]}平 · 已知 {r["known_results"]}，未知 {r["unknown_results"]}',
            'ratio':r['appearances']/ship_max,'segments':[(r[field]/r['appearances'],color) for field,color in RESULT_COLORS.items()],
            'tooltip':r['player_name']+' · '+r['ship_name']+'\n'+WIN_RATE_RULE} for r in sorted(ships,key=lambda r:(-r['appearances'],r['player_name'],r['ship_name']))])
        self.damage_caption.setText(f'{len(self._match_rows)} 条参赛记录 · 完整 {damage["confirmed_appearances"]} · 未知/不完整 {damage["unknown_appearances"]}\n承伤合计 {shown(damage["total"])} · 完整局均值 {shown(damage["confirmed_match_mean"])}')
        maximum=max([v for v in damage['categories'].values() if v is not None]+[1])
        self.damage_chart.set_items([{'title':name,'value':shown(damage['categories'][field]),
            'subtitle':f'已确认 {damage["category_known_counts"][field]} / {damage["appearances"]} 条',
            'ratio':damage['categories'][field]/maximum if damage['categories'][field] is not None else None} for field,name in sorted(CATEGORIES,key=lambda x:-(damage['categories'][x[0]] or 0))])
        self.render_lineups()

    @staticmethod
    def short_name(name):
        import re
        return re.sub(r'_KOTS\w*$','',name,flags=re.IGNORECASE)

    def reset_filters(self):
        self.player.blockSignals(True);self.ship.blockSignals(True);self.player.setCurrentIndex(0);self.ship.setCurrentIndex(0)
        self.player.blockSignals(False);self.ship.blockSignals(False);self._lineup_key=None;self.render()

    def select_player(self,key):
        self.player.setCurrentIndex(0 if self.player.currentData()==key else max(0,self.player.findData(key)))

    def select_ship(self,key):
        self.ship.setCurrentIndex(0 if self.ship.currentData()==key else max(0,self.ship.findData(key)))
        if self.ship.currentData() is not None:self.tabs.setCurrentIndex(1)

    def select_player_ship(self,selection):
        player,ship=selection;self.player.blockSignals(True);self.ship.blockSignals(True)
        self.player.setCurrentIndex(max(0,self.player.findData(player)));self.ship.setCurrentIndex(max(0,self.ship.findData(ship)))
        self.player.blockSignals(False);self.ship.blockSignals(False);self.render();self.tabs.setCurrentIndex(3)
        if self.match_table.rowCount():self.match_table.selectRow(0)

    def inspect_player_ship(self,item):
        if item.row()<len(self._player_ship_rows):
            row=self._player_ship_rows[item.row()];self.select_player_ship((row['player_key'],row['ship_id']))

    def select_lineup(self,key):
        self._lineup_key=None if self._lineup_key==key else key;self.render_lineups()

    def render_lineups(self):
        groups=self._lineup_groups;count=self.result['counts']['included_battles']
        self.lineup_chart.set_items([{'key':g['key'],'title':g['label'],'value':f'{g["count"]} / {count} 场',
            'subtitle':f'{len({b["map_name"] for b in g["battles"]})} 张地图',
            'ratio':g['count']/count if count else None,
            'segments':[(amount/sum(g['class_counts'].values()),CLASS_COLORS.get(kind,'#849bb3')) for kind,amount in g['class_counts'].items()]} for g in groups],self._lineup_key)
        selected=[g for g in groups if self._lineup_key is None or g['key']==self._lineup_key]
        def dated(row):
            try:return (0,datetime.strptime(row['date'],'%d.%m.%Y %H:%M:%S').isoformat(),row['battle_id'])
            except (ValueError,TypeError):return (1,str(row['date']),row['battle_id'])
        self._lineup_rows=sorted([b for group in selected for b in group['battles']],key=dated)
        self.lineup_label.setText(f'逐场阵容 · {len(self._lineup_rows)} / {count} 场')
        rows(self.lineup_table,[[r['date'][:10]+' · '+r['map_name'],r['ships'],OUTCOMES[r['outcome']]] for r in self._lineup_rows])
        self.lineup_table.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeMode.Interactive);self.lineup_table.setColumnWidth(0,165)
        self.lineup_table.horizontalHeader().setSectionResizeMode(1,QHeaderView.ResizeMode.Stretch)
        self.lineup_table.horizontalHeader().setSectionResizeMode(2,QHeaderView.ResizeMode.Interactive);self.lineup_table.setColumnWidth(2,54)
        for i,row in enumerate(self._lineup_rows):
            self.lineup_table.item(i,0).setToolTip(row['date']+' · '+row['map_name']+'\n'+row['battle_id'])
            self.lineup_table.item(i,1).setToolTip(row['ships']+'\n'+'\n'.join(p['tournament_name']+' · '+p['ship_name'] for p in row['players']))
            self.lineup_table.item(i,2).setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        if self._lineup_rows:self.lineup_table.selectRow(0);self.show_lineup_roster()
        else:self.lineup_roster.set_players([])

    def show_lineup_roster(self):
        selected=self.lineup_table.selectionModel().selectedRows()
        if selected and selected[0].row()<len(self._lineup_rows):self.lineup_roster.set_players(self._lineup_rows[selected[0].row()]['players'])

    def open_lineup_match(self,item):
        if item.row()<len(self._lineup_rows):self.host.open_battle(self._lineup_rows[item.row()]['battle_id'])

    def selected_match(self):
        selected=self.match_table.selectionModel().selectedRows()
        return self._match_rows[selected[0].row()] if selected and selected[0].row()<len(self._match_rows) else None

    def describe_match(self):
        r=self.selected_match()
        if not r:return
        self.open_button.setEnabled(True);self.source_button.setEnabled(bool(r['result_sources']))
        lines=[f'{r["date"]} · {r["map_name"]} · {r["tournament_name"]} · {r["ship_name"]}',
               f'本方赛果：{OUTCOMES[r["outcome"]]}',
               f'承伤合计：{shown(r["total"])} · 完整度 {r["known_field_count"]}/{r["field_count"]}',
               '；'.join(f'{name}：{shown(r["categories"][key])}' for key,name in CATEGORIES)]
        if r['outcome_evidence']:lines.append(f'赛果来源：{len(r["outcome_evidence"])} 份录像结算')
        if r['outcome_rejections']:lines.append('部分录像未能确认赛果。')
        if r['conflicts']:lines.append('部分承伤记录有差异，已标为未知。')
        if not r['result_sources']:lines.append('暂无完整承伤结算，缺失量不是零。')
        self.details.setPlainText('\n'.join(lines))

    def open_match(self):
        r=self.selected_match()
        if r:self.host.open_battle(r['battle_id'])
        else:self.notice.setText('请先选择一条比赛记录。');self.tabs.setCurrentIndex(3)

    def open_result_source(self):
        r=self.selected_match()
        if not r or not r['result_sources']:self.notice.setText('当前行没有可打开的原始结算来源。');return
        source=r['result_sources'][0];self.host.open_battle(r['battle_id'])
        self.host.source_combo.setCurrentIndex(self.host.source_combo.findData(source['source_sha']))
        self.host.replay_panel.set_clock(source['packet_clock'])

    def export_result(self):
        if self.result is None:return
        directory=QFileDialog.getExistingDirectory(self,'选择统计导出目录',str(self.host.library.root/'exports'))
        if directory:
            try:
                from wows_training.statistics_reporting import export_statistics
                paths=export_statistics(self.result,Path(directory));self.notice.setText('统计报告已导出');self.notice.setToolTip(str(directory));self.last_export=paths
            except Exception as exc:self.notice.setText('导出失败：'+str(exc))
