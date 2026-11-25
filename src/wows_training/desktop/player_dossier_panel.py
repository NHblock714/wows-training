"""Personal operations dossier with independently scoped public and replay data."""
from collections import Counter
from datetime import datetime,timezone
import math
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import Qt,QRectF,QSize,QTimer,Signal
from PySide6.QtGui import QColor,QFont,QPainter,QPen
from PySide6.QtWidgets import (QAbstractItemView,QCheckBox,QComboBox,QDialog,QDialogButtonBox,QFileDialog,QFormLayout,QFrame,QHBoxLayout,
    QLabel,QLineEdit,QListWidget,QListWidgetItem,QPlainTextEdit,QPushButton,QScrollArea,QSplitter,QTableWidget,
    QTableWidgetItem,QTabWidget,QVBoxLayout,QWidget,QSizePolicy)

from .multiview_panel import RecordingJob
from .naval_widgets import ShipArtwork
from .career_rating_visuals import install_rating_delegate, rating_accent, rating_ink, set_rating_halo, style_rating_item
from .chart_motion import ChartReveal
from .layered_panel import LayeredPanel
from . import academy_palette as tone
from .naval_assets import PALETTE
from .ui_components import Disclosure,align_cell,style_table
from ..player_dossier_service import PlayerDossierService
from ..career_service import CareerService
from ..statistics_analysis import CATEGORIES,aggregate_damage

REGIONS={'asia':'亚服','eu':'欧服','na':'美服'}
MODES={'pvp':'随机战','pvp_solo':'随机单人','pvp_div2':'随机双人','pvp_div3':'随机三人',
       'rank_solo':'排位单人','pve':'联合行动','club':'公会战','oper_solo':'战役单人','oper_div':'战役组队'}
STATUSES={'ok':'已取得','partial':'部分资料可用','private':'战绩隐藏','no_battles':'当前模式无场次',
          'not_fetched':'未查询','not_found':'未找到账号','error':'本次查询失败','unsupported':'模式或区服不支持'}
QUERY_REASONS={'network_disabled':'联网查询未开启','application_id_missing':'查询凭据未配置','cancelled':'查询已取消',
    'region_or_mode_not_supported':'区服或模式不支持','mode_not_supported':'当前模式不支持',
    'request_or_response_failed':'请求失败或返回资料无效','reference_request_failed':'基准资料更新失败',
    'encyclopedia_request_failed':'舰船资料更新失败','region_not_supported':'当前区服不支持查询',
    'invalid_search':'查询条件无效','invalid_account_id':'公开账号 ID 无效','invalid_clan_id':'公会 ID 无效',
    'account_not_found':'未找到账号','clan_not_found':'未找到公会','hidden_profile':'战绩隐藏','hidden_profile_meta':'战绩隐藏',
    'invalid_ship_filter':'舰船筛选无效','result_limit_exceeded_use_ship_filter':'舰船记录超过查询上限，请按舰船筛选',
    'mode_data_not_returned':'接口未提供当前模式资料','mode_ship_stats_not_requested':'尚未查询当前模式船仓',
    'mode_ship_stats_unsupported':'当前模式不支持船仓查询','ship_cache_missing':'尚无船仓缓存',
    'no_snapshot':'尚无公开战绩快照','mode_not_in_snapshot':'当前快照未包含所选模式',
    'invalid_ship_data':'船仓资料无效','ship_query_failed':'船仓查询失败','account_summary_missing':'接口未提供账号汇总',
    'public_account_unlinked':'公开账号待关联','snapshot_not_found':'未找到所选快照'}
IDENTITIES={'confirmed':'个人分配已确认','confirmed_account':'昵称 / 区服已确认','tournament_account':'赛事账号 · 个人待关联','conflict':'身份存在冲突'}
CLASS_NAMES={'Battleship':'战列','Cruiser':'巡洋','Destroyer':'驱逐','AirCarrier':'航母','Submarine':'潜艇','Auxiliary':'辅助'}
CLASS_COLORS={'Battleship':'#C9A661','Cruiser':'#86A9B5','Destroyer':'#82A897','AirCarrier':'#ACA0B4','Submarine':'#A4A990'}


def number(value):return type(value) in (int,float) and math.isfinite(value) and value>=0


def query_status(result,fallback='查询未完成'):
    return QUERY_REASONS.get(result.get('reason')) or STATUSES.get(result.get('status'),fallback)


def count_text(value):return f'{value:,.0f}' if number(value) else '—'


def percent(value):return f'{value:.1%}' if number(value) and value<=1 else '—'


def _style_rating_value(label, pr):
    accent = rating_accent(pr)
    label.setStyleSheet(f'color:{rating_ink(accent).name()};font-size:15px;font-weight:600;')
    set_rating_halo(label, accent)


def plain(text='',name=None):
    label=QLabel(text);label.setTextFormat(Qt.TextFormat.PlainText);label.setWordWrap(True)
    if name:label.setObjectName(name)
    return label


class _IdentityName(QLabel):
    """Keep the complete identity while allowing the hero column to shrink."""
    def __init__(self,text):
        super().__init__(text)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(0)
        self.setMinimumHeight(46)

    def setText(self,value):
        super().setText(value)
        self.setToolTip(value)

    def minimumSizeHint(self):
        return QSize(0,46)

    def paintEvent(self,event):
        painter=QPainter(self)
        painter.setFont(self.font());painter.setPen(QColor(tone.INK))
        value=painter.fontMetrics().elidedText(self.text(),Qt.TextElideMode.ElideRight,max(0,self.width()-2))
        painter.drawText(self.rect().adjusted(0,0,-2,0),Qt.AlignmentFlag.AlignLeft|Qt.AlignmentFlag.AlignVCenter,value)
        painter.end()


def time_text(value):
    if type(value) in (int,float):
        try:return datetime.fromtimestamp(value,timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        except (ValueError,OverflowError,OSError):return '时间未知'
    if isinstance(value,str) and value:
        try:
            stamp=datetime.fromisoformat(value.replace('Z','+00:00'))
            return stamp.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC') if stamp.tzinfo else stamp.strftime('%Y-%m-%d %H:%M')
        except ValueError:return value[:50]
    return '时间未知'


def snapshot_caption(profile,historical=False):
    kind=profile.get('time_kind') or ''
    if profile.get('timestamp') is None:return '快照时间未知' if profile.get('snapshot_id') else '尚无快照'
    prefix='缓存文件时间' if 'file' in kind or 'mtime' in kind else '请求时间' if kind=='request_attempt' else '抓取时间'
    if historical:prefix='所选历史快照 · '+prefix
    elif profile.get('source_type')=='legacy_cache':prefix='历史缓存 · '+prefix
    return prefix+' '+time_text(profile.get('timestamp'))


def grid(headers,name):
    table=QTableWidget(0,len(headers));table.setObjectName(name);table.setHorizontalHeaderLabels(headers)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers);table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection);table.verticalHeader().hide();table.horizontalHeader().setStretchLastSection(True)
    style_table(table);return table


def fill(table,values,data=None):
    table.setRowCount(len(values))
    for row,items in enumerate(values):
        for col,value in enumerate(items):
            item=QTableWidgetItem('—' if value is None else str(value));align_cell(item,value)
            item.setToolTip(item.text())
            if data is not None:item.setData(Qt.ItemDataRole.UserRole,data[row])
            table.setItem(row,col,item)


class DataLedger(LayeredPanel):
    def __init__(self,title,rows,color,parent=None):
        super().__init__(parent,accent=color);layout=QVBoxLayout(self);layout.setContentsMargins(16,13,23,17);layout.setSpacing(6)
        heading=plain(title);heading.setStyleSheet('font-size:19px;font-weight:600;color:'+color)
        self.heading_layout=QHBoxLayout();self.heading_layout.setContentsMargins(0,0,0,0);self.heading_layout.addWidget(heading);self.heading_layout.addStretch();layout.addLayout(self.heading_layout)
        self.caption=plain('未载入');self.caption.setStyleSheet('color:'+color+';font-size:11px');layout.addWidget(self.caption)
        form=QFormLayout();form.setContentsMargins(0,4,0,0);form.setHorizontalSpacing(24);form.setVerticalSpacing(8)
        self.values={}
        for key,label in rows:
            name=plain(label);name.setStyleSheet(f'color:{tone.MUTED};');value=plain('—');value.setAlignment(Qt.AlignmentFlag.AlignRight|Qt.AlignmentFlag.AlignVCenter)
            value.setStyleSheet(f'color:{tone.INK};font-size:15px;font-weight:600;')
            form.addRow(name,value);self.values[key]=value
        layout.addLayout(form);layout.addStretch()


    def update_values(self,values,caption):
        self.caption.setText(caption)
        for key,label in self.values.items():label.setText(str(values.get(key,'—')))


class FleetStructure(QWidget):
    def __init__(self,parent=None):
        super().__init__(parent);self.public=[];self.replay=[];self.public_available=False;self.rep_available=False;self.setMinimumHeight(205)
        self._reveal=ChartReveal(self)

    def set_data(self,public,replay,public_available=True,replay_available=True):
        self.public=public or [];self.replay=replay or [];self.public_available=public_available;self.rep_available=replay_available
        self._reveal.set_content((self.public,self.replay,self.public_available,self.rep_available) if self.public or self.replay else None);self.update()

    def paintEvent(self,event):
        painter=QPainter(self);painter.setRenderHint(QPainter.RenderHint.Antialiasing);painter.setFont(QFont('Microsoft YaHei UI',9))
        career={r.get('species'):r.get('battles',0) for r in self.public};rep={r.get('species'):r.get('battles',0) for r in self.replay}
        total_public=sum(v for v in career.values() if number(v));total_rep=sum(v for v in rep.values() if number(v))
        painter.setPen(QColor(tone.MUTED));painter.drawText(QRectF(0,0,self.width(),20),f'公开船记录 {count_text(total_public) if self.public_available else "—"} 场  /  REP {count_text(total_rep) if self.rep_available else "—"} 场')
        kinds=[k for k in CLASS_NAMES if k in career or k in rep]+[k for k in set(career)|set(rep) if k not in CLASS_NAMES]
        if not kinds:painter.drawText(self.rect(),Qt.AlignmentFlag.AlignCenter,'暂无舰种数据');painter.end();return
        width=max(30,self.width()-135)
        for i,kind in enumerate(kinds[:6]):
            y=30+i*27;painter.setPen(QColor(tone.INK));painter.drawText(QRectF(0,y-4,46,25),CLASS_NAMES.get(kind,'未知'))
            p=career.get(kind,0);r=rep.get(kind,0)
            for top,value,total,color in ((y,p,total_public,tone.BLUE),(y+8,r,total_rep,'#A73274')):
                painter.fillRect(QRectF(47,top,width,4),QColor(tone.ICE))
                if number(value) and total>0:painter.fillRect(QRectF(47,top,width*value/total*self._reveal.progress,4),QColor(color))
            painter.setPen(QColor(tone.MUTED));painter.drawText(QRectF(self.width()-80,y-4,80,25),Qt.AlignmentFlag.AlignRight,
                f'{count_text(p) if self.public_available else "—"} / {count_text(r) if self.rep_available else "—"}')
        painter.end()


class DamageComposition(QWidget):
    def __init__(self,parent=None):
        super().__init__(parent);self.data={};self.setMinimumHeight(205)
        self._reveal=ChartReveal(self)

    def set_data(self,data):
        self.data=data or {};self._reveal.set_content(self.data);self.update()

    def paintEvent(self,event):
        painter=QPainter(self);painter.setFont(QFont('Microsoft YaHei UI',9));painter.setPen(QColor(tone.MUTED))
        if not self.data:painter.drawText(self.rect(),Qt.AlignmentFlag.AlignCenter,'当前未选择 REP 承伤记录');painter.end();return
        n=self.data.get('appearances',0);known=self.data.get('confirmed_appearances',0);total=self.data.get('total')
        painter.drawText(QRectF(0,0,self.width(),20),f'完整结算 {known}/{n} 场 · 承伤 {count_text(total)}')
        bar=QRectF(0,29,self.width(),12);painter.fillRect(bar,QColor(tone.ICE))
        colors=['#DDD9C8','#D6B66C','#C5796D','#8F9B9A','#7EA5B1','#BB8A60','#648C9C','#A6A09A','#8E87A3','#6D7A7E']
        values=self.data.get('categories') or {}
        if number(total) and total>0:
            painter.save();painter.setClipRect(QRectF(0,29,self.width()*self._reveal.progress,12))
            x=0.
            for (key,_),color in zip(CATEGORIES,colors):
                value=values.get(key)
                if number(value):
                    width=self.width()*value/total;painter.fillRect(QRectF(x,29,width,12),QColor(color));x+=width
            painter.restore()
        elif total is None:
            painter.setPen(QColor('#A73274'));painter.drawText(QRectF(0,45,self.width(),22),'结算不完整，比例保持未知')
        for i,((key,name),color) in enumerate(zip(CATEGORIES,colors)):
            col=i%2;row=i//2;x=col*self.width()/2;y=75+row*23
            painter.fillRect(QRectF(x,y+5,4,10),QColor(color));painter.setPen(QColor(tone.MUTED))
            painter.drawText(QRectF(x+9,y,self.width()/2-14,20),f'{name}  {count_text(values.get(key))}')
        painter.end()


class PlayerDossierPanel(QWidget):
    idle=Signal();status=Signal(str)

    def __init__(self,host):
        super().__init__(host);self.host=host;self.job=None;self._service=None;self._career_service=None;self.index=None
        self.rep_profile=None;self.public_profile=None;self._rep_key=None;self._public_target=None;self._wanted_key=None
        self._generation=0;self._dirty=True;self._closing=False;self._public_summaries=[];self._candidates=[];self._ship_rows=[]
        self._snapshot_id=None;self._custom_ids=None;self._load_key=None;self._account_picker_context=None;self._link_dialog=None;self._build_ui()

    @property
    def busy(self):return bool(self.job and self.job.isRunning())

    @property
    def service(self):
        if self._service is None:self._service=PlayerDossierService(self.host.library.root,self.host.bundle)
        return self._service

    @property
    def career(self):
        if self._career_service is None:self._career_service=CareerService(self.host.library.root,self.host.bundle)
        return self._career_service

    def _button(self,text,callback,name):
        value=QPushButton(text);value.setObjectName(name);value.clicked.connect(callback);return value

    def _build_ui(self):
        outer=QVBoxLayout(self);outer.setContentsMargins(22,18,22,16);outer.setSpacing(10)
        heading=plain('个人作战档案','title');outer.addWidget(heading)
        query=QHBoxLayout();self.query_text=QLineEdit();self.query_text.setObjectName('dossierQuery');self.query_text.setPlaceholderText('个人昵称或公会名称')
        self.query_text.returnPressed.connect(self.search);query.addWidget(self.query_text,2)
        self.query_kind=QComboBox();self.query_kind.addItems(['玩家','公会']);query.addWidget(self.query_kind)
        self.region=QComboBox();self.region.setObjectName('dossierRegion')
        for key,label in REGIONS.items():self.region.addItem(label,key)
        query.addWidget(self.region)
        self.mode=QComboBox();self.mode.setObjectName('dossierMode')
        for key,label in MODES.items():self.mode.addItem(label,key)
        self.mode.currentIndexChanged.connect(self._change_mode);query.addWidget(self.mode)
        self.search_button=self._button('查询',self.search,'dossierSearch');query.addWidget(self.search_button)
        self.update_button=self._button('刷新所选',self.refresh_public,'dossierRefreshPublic');query.addWidget(self.update_button)
        query.addWidget(self._button('数据来源',self.show_sources,'dossierSources'))
        self.cancel_button=self._button('取消',self.cancel,'dossierCancel');self.cancel_button.setEnabled(False);query.addWidget(self.cancel_button);outer.addLayout(query)
        self.notice=plain('');self.notice.setObjectName('dossierNotice');outer.addWidget(self.notice)
        split=QSplitter(Qt.Orientation.Horizontal);split.setObjectName('dossierSplitter');outer.addWidget(split,1)
        directory=QWidget();left=QVBoxLayout(directory);left.setContentsMargins(0,0,10,0);left.setSpacing(7)
        self.scope=QComboBox();self.scope.setObjectName('dossierScope');self.scope.addItem('全部已导入比赛','all');self.scope.addItem('已保存双边比赛','paired');self.scope.addItem('比赛库当前选择','selected')
        self.scope.currentIndexChanged.connect(lambda *_:self.refresh(force=True));left.addWidget(self.scope)
        self.filter=QLineEdit();self.filter.setPlaceholderText('筛选赛号 / 已保存账号');self.filter.setClearButtonEnabled(True);self.filter.textChanged.connect(self._render_directory);left.addWidget(self.filter)
        self.directory=QListWidget();self.directory.setObjectName('dossierDirectory');self.directory.currentItemChanged.connect(self._directory_selected);left.addWidget(self.directory,1)
        self.rep_refresh=self._button('重载本地档案',lambda:self.refresh(force=True),'dossierReload');left.addWidget(self.rep_refresh)
        split.addWidget(directory)
        self.scroll=QScrollArea();self.scroll.setWidgetResizable(True);self.scroll.setFrameShape(QFrame.Shape.NoFrame);split.addWidget(self.scroll)
        content=QWidget();body=QVBoxLayout(content);body.setContentsMargins(7,0,7,4);body.setSpacing(12);self.scroll.setWidget(content)
        hero=QHBoxLayout();identity=LayeredPanel(accent=tone.PINK);identity_box=QVBoxLayout(identity);identity_box.setContentsMargins(17,15,24,18)
        self.name=_IdentityName('选择选手');self.name.setObjectName('dossierName');self.name.setStyleSheet('font-size:30px;font-weight:700;color:#12263D;');identity_box.addWidget(self.name)
        self.identity_text=plain('');self.identity_text.setStyleSheet('color:#486078;');identity_box.addWidget(self.identity_text)
        self.account_text=plain('');self.account_text.setObjectName('dossierAccount');self.account_text.setStyleSheet('color:#1263D3;');identity_box.addWidget(self.account_text)
        self.ship=QComboBox();self.ship.setObjectName('dossierShip');self.ship.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon);self.ship.setMinimumContentsLength(9);self.ship.addItem('全舰队总览',None);self.ship.currentIndexChanged.connect(self._render_selected_ship);identity_box.addWidget(self.ship);identity_box.addStretch()
        hero.addWidget(identity,1);self.artwork=ShipArtwork();self.artwork.setMinimumHeight(260);self.artwork.setMaximumHeight(280);hero.addWidget(self.artwork,1);body.addLayout(hero)
        ledgers=QHBoxLayout();ledgers.setSpacing(17)
        self.public_ledger=DataLedger('公开生涯',[('battles','累计场次'),('win_rate','胜率'),('damage','场均造成伤害'),('frags','场均击沉'),('survival','存活率'),('pr','社区 PR'),('power','基础战力')],tone.BLUE)
        self.public_account_button=self._button('选择公开账号',self.request_account_pick,'dossierChoosePublicAccount')
        self.public_ledger.heading_layout.addWidget(self.public_account_button)
        self.public_ai_button=self._button('AI 画像',self.open_public_ai,'dossierPublicAI')
        self.public_ledger.heading_layout.addWidget(self.public_ai_button)
        self.public_ledger.heading_layout.addSpacing(44)
        self.rep_ledger=DataLedger('赛训履历',[('battles','个人参赛'),('result','胜 / 负 / 平'),('win_rate','胜率 / 已确认'),('unknown','未知赛果'),('damage','完整局承伤均值'),('complete','承伤完整记录'),('scope','样本范围')],'#A73274')
        ledgers.addWidget(self.public_ledger,1);ledgers.addWidget(self.rep_ledger,1);body.addLayout(ledgers)
        self.tabs=QTabWidget();self.tabs.setObjectName('dossierTabs');self.tabs.setMinimumHeight(320);body.addWidget(self.tabs)
        overview=QWidget();overview_layout=QHBoxLayout(overview);overview_layout.setContentsMargins(4,10,4,4)
        self.structure=FleetStructure();self.damage_chart=DamageComposition()
        for title,widget in (('舰种结构',self.structure),('比赛实际承伤',self.damage_chart)):
            frame=LayeredPanel();box=QVBoxLayout(frame);box.setContentsMargins(16,13,22,17);caption=plain(title);caption.setMaximumHeight(27);caption.setStyleSheet('font-size:16px;font-weight:600;color:#12263D;');box.addWidget(caption);box.addWidget(widget,1);overview_layout.addWidget(frame,1)
        self.tabs.addTab(overview,'概览')
        ships=QWidget();ships_layout=QVBoxLayout(ships);self.ship_note=plain('同舰对照 · 两侧样本独立');ships_layout.addWidget(self.ship_note)
        self.ship_table=grid(['舰船','REP 出场','REP 胜率','REP 承伤均值','公开场次','公开胜率','公开场均输出','PR'],'dossierShips')
        install_rating_delegate(self.ship_table, 7)
        for column,width in enumerate((190,80,90,105,90,90,115,96)):self.ship_table.setColumnWidth(column,width)
        self.ship_table.itemSelectionChanged.connect(self._table_ship_selected);ships_layout.addWidget(self.ship_table);self.tabs.addTab(ships,'用舰档案')
        matches=QWidget();match_layout=QVBoxLayout(matches);self.match_caption=plain('暂无比赛记录');match_layout.addWidget(self.match_caption)
        self.match_table=grid(['日期 / 地图','赛事赛号','舰船','赛果','实际承伤','结算字段'],'dossierMatches');self.match_table.itemDoubleClicked.connect(lambda _:self.open_match())
        for column,width in enumerate((205,220,155,55,105,85)):self.match_table.setColumnWidth(column,width)
        match_layout.addWidget(self.match_table);match_layout.addWidget(self._button('打开所选比赛',self.open_match,'dossierOpenMatch'));self.tabs.addTab(matches,'比赛记录')
        sources=QWidget();self.source_page=sources;source_layout=QVBoxLayout(sources)
        self.public_selector=QComboBox();self.public_selector.setObjectName('dossierPublicAccount');self.public_selector.currentIndexChanged.connect(self._choose_bound_account)
        source_layout.addWidget(plain('已确认的个人账号'));source_layout.addWidget(self.public_selector)
        self.history=QComboBox();self.history.setObjectName('dossierSnapshot');self.history.currentIndexChanged.connect(self._change_history);source_layout.addWidget(self.history)
        self.source_details=QPlainTextEdit();self.source_details.setReadOnly(True);self.source_details.setMaximumHeight(120)
        source_layout.addWidget(Disclosure('快照与身份来源',self.source_details))
        self.candidates=grid(['候选昵称 / 公会','区服','公开 ID','类型'],'dossierCandidates');self.candidates.setMaximumHeight(170);self.candidates.itemSelectionChanged.connect(self._select_candidate);source_layout.addWidget(self.candidates)
        self.clan_button=self._button('查看所选公会成员',self.open_clan,'dossierClanMembers');source_layout.addWidget(self.clan_button)
        bind=QHBoxLayout();self.bind_actor=QComboBox();self.bind_actor.setObjectName('dossierBindActor');self.bind_actor.addItem('选择待核对的赛事账号',None);bind.addWidget(self.bind_actor,1)
        self.bind_button=self._button('核对并关联',self.open_identity_editor,'dossierBind');bind.addWidget(self.bind_button);source_layout.addLayout(bind)
        settings=QWidget();form=QFormLayout(settings)
        self.network_enabled=QCheckBox('启用手动公开查询');self.network_enabled.setObjectName('dossierNetworkEnabled');form.addRow(self.network_enabled)
        self.application_id=QLineEdit();self.application_id.setEchoMode(QLineEdit.EchoMode.Password);self.application_id.setPlaceholderText('Application ID · 留空保留已有值');self.application_id.setObjectName('dossierApplicationId');form.addRow('查询凭据',self.application_id)
        self.credential_status=plain('未读取设置');form.addRow(self.credential_status)
        settings_actions=QHBoxLayout();settings_actions.addWidget(self._button('保存设置',self.save_settings,'dossierSaveSettings'))
        form.addRow(settings_actions)
        refs=QHBoxLayout();refs.addWidget(self._button('导入 PR 基准',self.import_reference,'dossierImportReference'));refs.addWidget(self._button('更新 PR 基准',self.refresh_reference,'dossierRefreshReference'));form.addRow(refs)
        self.encyclopedia_button=self._button('更新舰船资料',self.refresh_encyclopedia,'dossierRefreshEncyclopedia');form.addRow(self.encyclopedia_button)
        self.reference_caption=plain('');self.reference_caption.setObjectName('dossierReferenceStatus');form.addRow(self.reference_caption)
        source_layout.addWidget(Disclosure('查询与缓存设置',settings));source_layout.addStretch()
        self.source_scroll=QScrollArea();self.source_scroll.setWidgetResizable(True);self.source_scroll.setFrameShape(QFrame.Shape.NoFrame);self.source_scroll.setWidget(sources)
        self.tabs.addTab(self.source_scroll,'关联与来源')
        split.setSizes([210,1050]);split.setStretchFactor(0,0);split.setStretchFactor(1,1)

    def showEvent(self,event):
        super().showEvent(event)
        if self._dirty:QTimer.singleShot(0,self.refresh)

    def _say(self,text):
        self.notice.setText(text);self.notice.setVisible(bool(text));self.status.emit(text)

    def _ids(self):
        kind=self.scope.currentData()
        if kind=='paired':return sorted({r['battle_id'] for r in self.host.recording_store.list_recordings()})
        if kind=='selected':return list(self.host.selected_battles())
        if kind=='custom':return list(self._custom_ids or [])
        return None

    def refresh(self,*_,force=False):
        if not self.isVisible():self._dirty=True;return
        if self.busy:self._dirty=True;return
        ids=self._ids();key=self.service.cache_key(ids)
        self._public_summaries=self.career.list_profiles();self._load_settings()
        if self.index is not None and self.index['cache_key']==key and not force:
            self._dirty=False;self._render_directory()
            if self._wanted_key:
                wanted=self.index.get('aliases',{}).get(self._wanted_key,self._wanted_key)
                if wanted in self.index['profiles']:self._choose_rep(wanted)
                self._wanted_key=None
            return
        if self.index is None or self.index['cache_key']!=key:
            self._wanted_key=self._wanted_key or self._rep_key
            if self._wanted_key or self._public_target is None:self._clear_scope_view('正在核对所选比赛范围')
        self._dirty=False;self._load_key=key
        self._start(lambda cancel,progress:self.service.build(ids,cancel,progress),self._accept_index,'REP 档案载入中',indexing=True)

    def _clear_scope_view(self,message):
        old_name=(self.rep_profile or {}).get('name')
        self.rep_profile=None;self.public_profile=None;self._rep_key=None;self._public_target=None;self._snapshot_id=None
        self.public_selector.blockSignals(True);self.public_selector.clear();self.public_selector.addItem('等待核对当前范围',None);self.public_selector.blockSignals(False)
        self._refresh_history();self._render_profile();self._render_directory()
        if old_name:self.name.setText(old_name)
        self.identity_text.setText(message)

    def open_player(self,key,battle_ids=None):
        self._wanted_key=key;self._generation+=1;self.public_profile=None;self._public_target=None;self._snapshot_id=None
        self.scope.blockSignals(True)
        if battle_ids is not None:
            self._custom_ids=list(dict.fromkeys(battle_ids));index=self.scope.findData('custom')
            if index<0:self.scope.addItem(f'来自队伍分析（{len(self._custom_ids)} 场）','custom');index=self.scope.findData('custom')
            self.scope.setCurrentIndex(index)
        else:self.scope.setCurrentIndex(self.scope.findData('all'))
        self.scope.blockSignals(False)
        if self.index and self.index['cache_key']==self.service.cache_key(self._ids()):
            resolved=self.index.get('aliases',{}).get(key,key)
            if resolved in self.index['profiles']:self._choose_rep(resolved)
        else:self._clear_scope_view('正在载入所选比赛范围')
        self._wanted_key=key;self._dirty=True
        if not self.busy:self.refresh()

    def open_public_account(self,region,account_id,mode=None):
        """Browse an exact cached public identity without querying or binding it."""
        region='na' if region=='com' else region
        if region not in REGIONS or not str(account_id).isdigit():self._say('公开账号或区服无效');return
        if mode is not None:
            self.mode.blockSignals(True);self.mode.setCurrentIndex(max(0,self.mode.findData(mode)));self.mode.blockSignals(False)
        self._wanted_key=None;self._choose_public(region,str(int(account_id)))

    def request_account_pick(self):
        if self.busy:self._say('当前档案载入中，请稍后选择');return
        if not hasattr(self.host,'open_account_picker'):self._say('账号选择入口不可用');return
        self._account_picker_context={'token':str(uuid4()),'generation':self._generation,'player_key':self._rep_key,
            'scope_ids':tuple(sorted(self._ids() if self._ids() is not None else self.service.battle_ids())),'cache_key':self.service.cache_key(self._ids()),
            'mode':self.mode.currentData(),'snapshot_id':self._snapshot_id,
            'region':self.region.currentData()}
        self.host.open_account_picker(dict(self._account_picker_context))

    def _valid_account_pick(self,context):
        return (isinstance(context,dict) and context==self._account_picker_context and context.get('generation')==self._generation
            and context.get('player_key')==self._rep_key and context.get('scope_ids')==tuple(sorted(self._ids() if self._ids() is not None else self.service.battle_ids()))
            and context.get('mode')==self.mode.currentData() and context.get('snapshot_id')==self._snapshot_id
            and context.get('cache_key')==self.service.cache_key(self._ids()))

    def accept_account_pick(self,context,target):
        """Return to the original REP identity; saving always needs confirmation."""
        if not self._valid_account_pick(context):self._say('原选手或范围已变化，请重新选择公开账号');return False
        if target is None:
            self._account_picker_context=None;return True
        if (target.get('kind')!='player' or target.get('region') not in REGIONS
                or not str(target.get('id','')).isdigit() or int(target['id'])<=0):
            self._say('请选择确切的个人公开账号');return False
        target=dict(target,id=str(int(target['id'])))
        if self.rep_profile is None:
            self._account_picker_context=None
            self.open_public_account(target['region'],target['id'],context['mode']);return True
        dialog=QDialog(self);dialog.setObjectName('dossierPublicLinkDialog');dialog.setWindowTitle('确认公开账号');dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setMinimumWidth(460);box=QVBoxLayout(dialog);box.setContentsMargins(24,22,24,22);box.setSpacing(14)
        form=QFormLayout();form.setVerticalSpacing(12);form.setHorizontalSpacing(18)
        form.addRow('公开账号',plain(str(target.get('name') or target['id'])+' · '+REGIONS[target['region']]+' / '+target['id']))
        actor=QComboBox();actor.setObjectName('dossierLinkActor')
        for row in self.rep_profile['tournament_accounts']:
            actor.addItem(row['tournament_name']+' · '+row['event_key'],(row['event_key'],row['tournament_name'],str(row['tournament_account_id'])))
        form.addRow('赛事账号',actor)
        scope=QComboBox();scope.setObjectName('dossierLinkScope');scope.addItem('当前范围内该赛号的比赛','selected');scope.addItem('整个赛事','event');form.addRow('适用范围',scope)
        notes=QLineEdit();notes.setObjectName('dossierLinkNotes');notes.setPlaceholderText('可选');form.addRow('核对备注',notes);box.addLayout(form)
        confirmed=QCheckBox('已核对：该赛号属于此公开账号选手');confirmed.setObjectName('dossierLinkConfirmed');box.addWidget(confirmed)
        notice=plain();notice.setObjectName('dossierLinkNotice');notice.hide();box.addWidget(notice)
        actions=QDialogButtonBox(QDialogButtonBox.StandardButton.Save|QDialogButtonBox.StandardButton.Cancel)
        save=actions.button(QDialogButtonBox.StandardButton.Save);save.setText('确认关联');save.setObjectName('dossierConfirmLink');save.setEnabled(False)
        actions.button(QDialogButtonBox.StandardButton.Cancel).setText('取消');confirmed.toggled.connect(save.setEnabled)
        actions.rejected.connect(dialog.reject);box.addWidget(actions)
        def commit():
            if not self._valid_account_pick(context):notice.setText('原选手或范围已变化，请重新选择');notice.show();save.setEnabled(False);return
            try:
                wanted=self.service.confirm_public_link(self.index,self._rep_key,target,actor.currentData(),scope.currentData(),confirmed=confirmed.isChecked(),notes=notes.text().strip())
            except (ValueError,OSError) as error:notice.setText(str(error));notice.show();return
            self._account_picker_context=None;self._wanted_key=wanted;dialog.accept();self._dirty=True;self.refresh(force=True)
        actions.accepted.connect(commit)
        def finished(*_):
            self._link_dialog=None
            if self._account_picker_context==context:self._account_picker_context=None
            dialog.deleteLater()
        dialog.finished.connect(finished)
        self._link_dialog=dialog;dialog.open();return True

    def _start(self,operation,success,notice,indexing=False):
        if self.busy:self._say('当前操作进行中，可取消后重试');return False
        self._say(notice);self.job=RecordingJob(operation,self)
        self.job.progress.connect(self._say);self.job.succeeded.connect(success)
        self.job.failed.connect(lambda message:self._say('操作未完成：'+message));self.job.finished.connect(self._finished)
        for button in (self.search_button,self.update_button,self.rep_refresh,self.bind_button,self.clan_button):button.setEnabled(False)
        if indexing:self.directory.setEnabled(False)
        self.cancel_button.setEnabled(True);self.job.start();return True

    def _finished(self):
        for button in (self.search_button,self.update_button,self.rep_refresh,self.bind_button,self.clan_button):button.setEnabled(True)
        self.directory.setEnabled(True)
        self.cancel_button.setEnabled(False);self.idle.emit()
        if self._closing:self.close()
        elif self._dirty and self.isVisible():QTimer.singleShot(0,self.refresh)

    def cancel(self):
        if self.busy:self.job.cancel_event.set();self._say('正在取消当前操作')

    def _accept_index(self,index):
        if index['cache_key']!=self.service.cache_key(self._ids()):self._dirty=True;return
        self.index=index;self._public_summaries=self.career.list_profiles();self._render_directory()
        self.bind_actor.blockSignals(True);previous=self.bind_actor.currentData();self.bind_actor.clear();self.bind_actor.addItem('选择待核对的赛事账号',None)
        for p in index['players']:self.bind_actor.addItem(p['name']+' · '+str(p['appearances'])+' 场',p['player_key'])
        self.bind_actor.setCurrentIndex(max(0,self.bind_actor.findData(previous)));self.bind_actor.blockSignals(False)
        key=self._wanted_key or self._rep_key
        key=index.get('aliases',{}).get(key,key)
        if key in index['profiles']:self._choose_rep(key)
        elif self._public_target is not None:self._load_public_local()
        elif key:
            self.rep_profile=None;self.public_profile=None;self._rep_key=None;self._public_target=None;self._render_profile()
            self.identity_text.setText('该身份不在当前范围')
        elif index['players']:self._choose_rep(index['players'][0]['player_key'])
        else:self.rep_profile=None;self._rep_key=None;self._render_profile()
        self._wanted_key=None;counts=index['counts'];self._say(f'已载入 {counts["selected_battles"]} 场 · {counts["players"]} 个选手 / 赛号')

    def _render_directory(self,*_):
        if not hasattr(self,'directory'):return
        needle=self.filter.text().strip().casefold();self.directory.blockSignals(True);self.directory.clear()
        def add(title,rows):
            if not rows:return
            group=QListWidgetItem(title);group.setFlags(Qt.ItemFlag.NoItemFlags);group.setForeground(QColor('#A73274'));self.directory.addItem(group)
            for text,key,tip in rows:
                item=QListWidgetItem(text);item.setData(Qt.ItemDataRole.UserRole,key);item.setToolTip(tip);item.setSizeHint(QSize(190,49));self.directory.addItem(item)
                if key==('rep',self._rep_key) or (key[0]=='public' and self._rep_key is None and self._public_target and key[1:3]==self._public_target[:2]):self.directory.setCurrentItem(item)
        rep=[]
        for p in (self.index or {}).get('players',[]):
            names=' '.join([p['name'],*[t['tournament_name'] for t in p['tournament_accounts']]])
            if needle and needle not in names.casefold():continue
            rep.append((p['name']+f'\n{p["appearances"]} 场 · {p["scope_label"]}',('rep',p['player_key']),names))
        add('REP 参赛档案',rep)
        public=[]
        for p in self._public_summaries:
            name=str(p.get('nickname') or p['account_id'])
            if needle and needle not in (name+' '+str(p['account_id'])).casefold():continue
            public.append((name+'\n'+REGIONS.get(p['region'],p['region'])+' · '+STATUSES.get(p['status'],p['status']),('public',p['region'],str(p['account_id'])),snapshot_caption(p)))
        add('公开账号快照',public);self.directory.blockSignals(False)

    def _directory_selected(self,current,previous):
        key=current.data(Qt.ItemDataRole.UserRole) if current else None
        if not key:return
        if key[0]=='rep':self._choose_rep(key[1])
        else:self._choose_public(key[1],key[2])

    def _choose_rep(self,key):
        if not self.index or key not in self.index['profiles']:self._wanted_key=key;return
        if self.index['cache_key']!=self.service.cache_key(self._ids()):
            self._wanted_key=key;self._dirty=True;self._clear_scope_view('当前比赛范围尚未载入');return
        changed=key!=self._rep_key
        self._generation+=1;self._rep_key=key;self.rep_profile=self.index['profiles'][key]
        self.public_profile=None;self._public_target=None;self._snapshot_id=None;self._wanted_key=None
        self.bind_actor.setCurrentIndex(max(0,self.bind_actor.findData(key)))
        accounts=[a for a in self.rep_profile.get('public_accounts',[]) if a.get('confirmed') and a.get('account_id') and a.get('region') in REGIONS]
        known=self.rep_profile.get('public_accounts') or []
        if changed:
            self.query_text.setText(known[0]['nickname'] if known else '')
            if known and known[0].get('region') in REGIONS:self.region.setCurrentIndex(self.region.findData(known[0]['region']))
        self.public_selector.blockSignals(True);self.public_selector.clear()
        if not accounts:self.public_selector.addItem('尚无已确认的公开账号 ID',None)
        for a in accounts:self.public_selector.addItem(a['nickname']+' · '+REGIONS[a['region']]+' · '+a['account_id'],(a['region'],a['account_id']))
        self.public_selector.blockSignals(False)
        if accounts:self._public_target=(accounts[0]['region'],accounts[0]['account_id'],self.mode.currentData());self._load_public_local()
        else:self._refresh_history();self._render_profile()
        self._render_directory()

    def _choose_public(self,region,account_id):
        self._generation+=1;self._wanted_key=None;self._rep_key=None;self.rep_profile=None;self._snapshot_id=None;self.public_profile=None
        self._public_target=(region,str(account_id),self.mode.currentData())
        self.region.setCurrentIndex(max(0,self.region.findData(region)))
        self.public_selector.blockSignals(True);self.public_selector.clear();self.public_selector.addItem('独立公开账号浏览',None);self.public_selector.blockSignals(False)
        self._load_public_local()
        self._public_summaries=self.career.list_profiles();self._render_directory()

    def _choose_bound_account(self,*_):
        target=self.public_selector.currentData()
        if target is None:return
        self._generation+=1;self._snapshot_id=None;self.public_profile=None;self._public_target=(*target,self.mode.currentData());self._load_public_local()

    def _change_mode(self,*_):
        if not hasattr(self,'history'):return
        self._generation+=1
        if self._public_target is None:return
        self._snapshot_id=None;self.public_profile=None
        self._public_target=(*self._public_target[:2],self.mode.currentData());self._load_public_local()

    def _load_public_local(self):
        if self._public_target:
            region,account,mode=self._public_target
            self.public_profile=self.career.profile(region,account,mode,snapshot_id=self._snapshot_id)
        else:self.public_profile=None
        self._refresh_history();self._render_profile()

    def _refresh_history(self):
        self.history.blockSignals(True);self.history.clear();self.history.addItem('最新状态',None)
        if self._public_target:
            for item in self.career.history(*self._public_target[:2]):self.history.addItem(snapshot_caption(item)+' · '+STATUSES.get(item['status'],item['status']),item['snapshot_id'])
        self.history.setCurrentIndex(max(0,self.history.findData(self._snapshot_id)));self.history.blockSignals(False)

    def _change_history(self,*_):
        if self._public_target is None:return
        self._generation+=1;self._snapshot_id=self.history.currentData();self._load_public_local()

    def _render_profile(self):
        rep=self.rep_profile;pub=self.public_profile;target=self._public_target
        self.public_ai_button.setEnabled(bool(target and hasattr(self.host, 'query_center_panel')))
        self.name.setText(str((rep or {}).get('name') or (pub or {}).get('nickname') or (target[1] if target else '选择选手'))[:120])
        if rep:
            names=' / '.join(t['tournament_name'] for t in rep['tournament_accounts'])
            self.identity_text.setText(IDENTITIES.get(rep['identity_status'],'赛事账号')+'\n'+names);self.identity_text.setToolTip(names)
            linked=rep.get('public_accounts') or []
            self.account_text.setText('公开账号已确认关联' if target else '昵称 / 区服已确认，公开 ID 待补' if linked else '公开账号待关联')
        elif target or pub:
            self.identity_text.setText('公开账号');self.account_text.clear()
        else:
            self.identity_text.clear();self.account_text.clear()
        if target:self.account_text.setText(' · '.join(filter(None,(self.account_text.text(),REGIONS.get(target[0],target[0])+' / '+target[1]))))
        current=self.ship.currentData();choices={}
        for row in (pub or {}).get('ships',[]):choices[str(row['ship_id'])]=row.get('name') or str(row['ship_id'])
        for row in (rep or {}).get('ships',[]):choices[row['ship_id']]=row['ship_name']
        self.ship.blockSignals(True);self.ship.clear();self.ship.addItem('全舰队总览',None)
        for identifier,name in sorted(choices.items(),key=lambda item:item[1]):self.ship.addItem(name,identifier)
        self.ship.setCurrentIndex(max(0,self.ship.findData(current)));self.ship.blockSignals(False)
        self._render_ship_table();self._render_selected_ship()
        self._matches=list((rep or {}).get('matches',[]));self.match_caption.setText(f'{len(self._matches)} 场' if rep else '当前未选择赛事档案')
        fill(self.match_table,[[r['date']+' · '+r['map_name'],r['tournament_name'],r['ship_name'],{'win':'胜','loss':'负','draw':'平'}.get(r['outcome'],'未知'),
            count_text(r['total']),f'{r["known_field_count"]}/{r["field_count"]}'] for r in self._matches],self._matches)
        lines=[]
        if rep:
            lines.append('身份：'+rep['scope_label'])
            if rep.get('excluded_matches'):lines.append(f'{len(rep["excluded_matches"])} 场存在重复身份，未纳入个人聚合')
            for a in rep['tournament_accounts']:lines.append(f'{a["event_key"]} / {a["tournament_name"]} · {len(a["battle_ids"])} 场')
        if pub:
            lines.extend([snapshot_caption(pub,self._snapshot_id is not None),'状态：'+query_status(pub),
                '公共账号：'+REGIONS.get(pub.get('region'),str(pub.get('region')))+' / '+str(pub.get('account_id')),
                '资料来源：'+('历史缓存' if pub.get('source_type')=='legacy_cache' else '公开战绩查询')])
        self.source_details.setPlainText('\n'.join(lines) or '尚无资料来源')

    def open_public_ai(self):
        panel = getattr(self.host, 'query_center_panel', None)
        if panel is None or panel.busy or not self._public_target:
            return
        from ..query_center import player_target
        from .navigation import QUERY_PAGE
        region, account, _mode = self._public_target
        name = (self.public_profile or {}).get('nickname') or account
        target = player_target(region, account, name)
        self.host.show_page(QUERY_PAGE)
        panel.open_target(target, portrait=True)

    def _render_selected_ship(self,*_):
        if not hasattr(self,'public_ledger'):return
        rep=self.rep_profile or {};pub=self.public_profile or {};ship_id=self.ship.currentData()
        rep_ship=next((r for r in rep.get('ships',[]) if r['ship_id']==ship_id),None)
        pub_ship=next((r for r in pub.get('ships',[]) if str(r['ship_id'])==ship_id),None)
        shown_rep=rep_ship if ship_id else rep;shown_pub=pub_ship if ship_id else pub.get('metrics',{})
        shown_rep=shown_rep or {};shown_pub=shown_pub or {};damage=shown_rep.get('damage') or aggregate_damage([])
        figure=ship_id or rep.get('top_ship_id') or ((pub.get('ace_ship') or {}).get('ship_id') if isinstance(pub.get('ace_ship'),dict) else None)
        if not figure and pub.get('ships'):figure=max(pub['ships'],key=lambda row:row.get('battles') or 0)['ship_id']
        art_rep=next((r for r in rep.get('ships',[]) if r['ship_id']==str(figure)),{})
        art_pub=next((r for r in pub.get('ships',[]) if str(r['ship_id'])==str(figure)),{})
        title=art_rep.get('ship_name') or art_pub.get('name') or ('舰船 '+str(figure) if figure else '尚无舰船资料')
        self.artwork.set_ship(figure,title=title,subtitle='',
                              species=art_rep.get('ship_class') or art_pub.get('species'),nation=art_rep.get('nation') or art_pub.get('nation'))
        status=pub.get('status');usable=status in ('ok','partial','no_battles')
        if not usable:shown_pub={}
        if status=='no_battles':shown_pub=dict(shown_pub,battles=0)
        rating=pub.get('rating') or {};pr=pub_ship.get('pr') if ship_id and pub_ship and usable else rating.get('pr') if not ship_id and usable else None
        power=pub_ship.get('ship_battle_power') if ship_id and pub_ship and usable else rating.get('battle_power') if not ship_id and usable else None
        public_caption=(REGIONS.get(pub.get('region'),'公开账号')+' · '+MODES.get(pub.get('mode'),'未知模式')+' · '+query_status(pub)) if pub else '公开账号待关联' if self.rep_profile else '未选择公开账号'
        if pub and self._snapshot_id is not None:public_caption+='\n所选历史快照 · '+time_text(pub.get('timestamp'))
        if ship_id:public_caption+=' · 当前舰船'
        self.public_ledger.update_values({'battles':count_text(shown_pub.get('battles')),'win_rate':percent(shown_pub.get('win_rate')),
            'damage':count_text(shown_pub.get('avg_damage')),'frags':f'{shown_pub["avg_frags"]:.2f}' if number(shown_pub.get('avg_frags')) else '—',
            'survival':percent(shown_pub.get('survival_rate')),'pr':count_text(pr),'power':count_text(power)},public_caption)
        _style_rating_value(self.public_ledger.values['pr'], pr)
        _style_rating_value(self.public_ledger.values['power'], pr if number(power) else None)
        self.public_ledger.values['power'].setToolTip('公开战绩评分')
        if rating:
            baseline=(rating.get('baseline') or {}).get('expected_reference') or {}
            self.public_ledger.values['pr'].setToolTip(f'期望值覆盖 {count_text(rating.get("covered_battles"))}/{count_text(rating.get("total_battles"))} 场\n基准 {time_text(baseline.get("timestamp"))} · {baseline.get("time_kind", "时间未知")}')
        else:self.public_ledger.values['pr'].setToolTip('无可用评分或期望值覆盖不足')
        appearances=shown_rep.get('appearances',0 if self.rep_profile else None);known=shown_rep.get('known_results',0)
        rep_caption=(rep.get('scope_label','赛事账号')+' · '+str(rep.get('appearances',0))+' 次个人出场') if self.rep_profile else '当前未选择赛事档案'
        if ship_id and self.rep_profile:rep_caption+=f'\n该舰 {appearances}/{rep.get("appearances",0)} 场'
        self.rep_ledger.update_values({'battles':count_text(appearances),'result':f'{shown_rep.get("wins",0)} / {shown_rep.get("losses",0)} / {shown_rep.get("draws",0)}' if self.rep_profile else '—',
            'win_rate':percent(shown_rep.get('win_rate'))+f' / {known} 场' if self.rep_profile else '—',
            'unknown':str(shown_rep.get('unknown_results',0))+' 场' if self.rep_profile else '—',
            'damage':count_text(damage.get('confirmed_match_mean')),'complete':f'{damage.get("confirmed_appearances",0)}/{damage.get("appearances",0)}' if self.rep_profile else '—',
            'scope':f'{(self.index or {}).get("counts",{}).get("selected_battles",0)} 场已选比赛' if self.rep_profile else '—'},rep_caption)
        self.rep_ledger.values['win_rate'].setToolTip('胜 / (胜+负+平)；未知赛果不计分母。未知 '+str(shown_rep.get('unknown_results',0))+' 场。')
        self.structure.set_data(pub.get('class_distribution',[]) if usable else [],rep.get('class_distribution',[]),usable,self.rep_profile is not None)
        self.damage_chart.set_data(damage if self.rep_profile else None)

    def _render_ship_table(self):
        rep={r['ship_id']:r for r in (self.rep_profile or {}).get('ships',[])}
        pub={str(r['ship_id']):r for r in (self.public_profile or {}).get('ships',[]) if self.public_profile.get('status') in ('ok','partial','no_battles')}
        self._ship_rows=sorted(rep.keys()|pub.keys(),key=lambda key:(-rep.get(key,{}).get('appearances',0),-pub.get(key,{}).get('battles',0),key))
        values=[]
        for key in self._ship_rows:
            a,b=rep.get(key,{}),pub.get(key,{})
            values.append([a.get('ship_name') or b.get('name') or key,f'{a.get("appearances",0)}/{(self.rep_profile or {}).get("appearances",0)}' if self.rep_profile else None,
                percent(a.get('win_rate')),count_text((a.get('damage') or {}).get('confirmed_match_mean')),
                count_text(b.get('battles')),percent(b.get('win_rate')),count_text(b.get('avg_damage')),count_text(b.get('pr'))])
        self.ship_table.blockSignals(True)
        fill(self.ship_table,values,self._ship_rows)
        for row, key in enumerate(self._ship_rows):
            style_rating_item(self.ship_table.item(row, 7), pub.get(key, {}).get('pr'))
        self.ship_table.blockSignals(False)
        for col in (1,2,3):self.ship_table.setColumnHidden(col,self.rep_profile is None)
        for col in (4,5,6,7):self.ship_table.setColumnHidden(col,self.public_profile is None or self.public_profile.get('status') not in ('ok','partial','no_battles'))
        self.ship_note.setText(f'同舰对照 · {len(self._ship_rows)} 种舰船 · 两侧场次与模式各自独立')

    def _table_ship_selected(self):
        row=self.ship_table.currentRow()
        if 0<=row<len(self._ship_rows):self.ship.setCurrentIndex(max(0,self.ship.findData(self._ship_rows[row])))

    def open_match(self):
        row=self.match_table.currentRow()
        if 0<=row<len(getattr(self,'_matches',[])):self.host.open_battle(self._matches[row]['battle_id'])
        else:self._say('请选择一场比赛')

    def show_sources(self):
        self.tabs.setCurrentIndex(3);self.scroll.ensureWidgetVisible(self.tabs)

    def search(self):
        name=self.query_text.text().strip();region=self.region.currentData();kind=self.query_kind.currentText()
        if not name:self._say('请输入个人昵称或公会名称');return
        generation=self._generation
        operation=lambda cancel,progress:self.career.search(region,name,cancel=cancel) if kind=='玩家' else self.career.search_clans(region,name,cancel=cancel)
        self._start(operation,lambda result:self._accept_candidates(result,region,kind,generation,(region,name,kind)),'查询公开候选…')

    def _accept_candidates(self,result,region,kind,generation,query=None):
        if generation!=self._generation or (query is not None and query!=(self.region.currentData(),self.query_text.text().strip(),self.query_kind.currentText())):
            self._say('候选查询已完成；当前选择已变化，未替换页面');return
        if result.get('status')!='ok':self._say(query_status(result));return
        self._candidates=[dict(row,region=row.get('region',region),candidate_kind='clan' if kind=='公会' else 'player') for row in (result.get('data') or {}).get('candidates',[])]
        self.candidates.blockSignals(True)
        fill(self.candidates,[[r.get('nickname') or r.get('name') or r.get('tag'),REGIONS.get(r['region'],r['region']),r.get('account_id') or r.get('clan_id'),'公会' if r['candidate_kind']=='clan' else '个人账号'] for r in self._candidates])
        self.candidates.clearSelection();self.candidates.setCurrentCell(-1,-1);self.candidates.blockSignals(False)
        self._say(f'{len(self._candidates)} 个候选')
        if generation==self._generation:self.show_sources();self.source_scroll.ensureWidgetVisible(self.candidates)

    def _select_candidate(self):
        row=self.candidates.currentRow()
        if not 0<=row<len(self._candidates):return
        item=self._candidates[row]
        if item['candidate_kind']=='player':self._choose_public(item['region'],item['account_id'])
        else:self._generation+=1

    def open_clan(self):
        row=self.candidates.currentRow()
        if not 0<=row<len(self._candidates) or self._candidates[row]['candidate_kind']!='clan':self._say('请选择公会候选');return
        item=dict(self._candidates[row]);generation=self._generation
        self._start(lambda cancel,progress:self.career.clan(item['region'],item['clan_id'],cancel=cancel),
            lambda result:self._accept_members(result,item['region'],generation),'载入公会成员…')

    def _accept_members(self,result,region,generation):
        if result.get('status')!='ok':self._say(query_status(result,'公会查询未完成'));return
        converted={'status':'ok','data':{'candidates':(result.get('data') or {}).get('members',[])}}
        self._accept_candidates(converted,region,'玩家',generation)

    def refresh_public(self):
        if self._public_target is None:self._say('先选择已保存账号或查询候选，再刷新公开资料');self.show_sources();return
        target=self._public_target;generation=self._generation
        self._start(lambda cancel,progress:self.career.refresh(*target,cancel=cancel,progress=progress),
                    lambda profile:self._accept_public(profile,target,generation),'刷新公开资料…')

    def _accept_public(self,profile,target,generation):
        self._public_summaries=self.career.list_profiles();self._render_directory()
        if generation!=self._generation or target!=self._public_target:
            self._say('所查询账号的快照已保存；当前选择未改变');return
        if (profile.get('region'),str(profile.get('account_id')),profile.get('mode'))!=target:
            self.public_profile=None;self._render_profile();self._say('返回账号与请求不一致，未显示该结果');return
        self.public_profile=profile;self._snapshot_id=None;self._refresh_history();self._render_profile()
        self._say(query_status(profile,'公开查询完成'))

    def _load_settings(self):
        settings=self.career.settings();self.network_enabled.setChecked(settings.get('enabled') is True)
        self.credential_status.setText('查询凭据已配置' if settings.get('credential_present') else '查询凭据未配置')
        self._load_reference_status()

    def _load_reference_status(self):
        reference=self.career.reference_status();source=reference.get('encyclopedia_reference') or {}
        parts=['舰船百科 '+count_text(reference.get('encyclopedia_records'))]
        if source.get('game_version'):parts.append(str(source['game_version']))
        if source.get('timestamp'):parts.append(time_text(source['timestamp']).split(' ')[0])
        self.reference_caption.setText(' · '.join(parts))

    def save_settings(self):
        if self.busy:self._say('请先完成当前操作');return
        try:
            settings=self.career.save_settings(self.network_enabled.isChecked(),self.application_id.text().strip() or None)
            self.application_id.clear();self._load_settings();self._say('查询设置已保存')
        except Exception:self._say('查询设置未保存；请检查本机加密存储')

    def import_reference(self):
        if self.busy:return
        path,_=QFileDialog.getOpenFileName(self,'选择 PR 期望值 JSON','','JSON (*.json)')
        if path:self._start(lambda cancel,progress:self.career.import_reference(path,cancel=cancel),lambda result:self._reference_done(result),'导入 PR 基准…')

    def refresh_reference(self):
        self._start(lambda cancel,progress:self.career.refresh_reference(cancel=cancel,progress=progress),self._reference_done,'更新公开 PR 基准…')

    def _reference_done(self,result):
        self._say('PR 基准已更新' if result.get('status')=='ok' else query_status(result,'PR 基准未更新'))
        self._load_reference_status()
        if self._public_target:self._load_public_local()

    def refresh_encyclopedia(self):
        region=self.region.currentData()
        self._start(lambda cancel,progress:self.career.refresh_encyclopedia(region,cancel=cancel,progress=progress),
                    self._encyclopedia_done,'更新舰船资料…')

    def _encyclopedia_done(self,result):
        self._load_reference_status()
        self._say('舰船资料已更新 · '+count_text(result.get('records'))+' 艘' if result.get('status')=='ok' else query_status(result,'舰船资料未更新'))
        if self._public_target:self._load_public_local()

    def open_identity_editor(self):
        key=self.bind_actor.currentData() or self._rep_key
        profile=(self.index or {}).get('profiles',{}).get(key)
        if profile is None or not profile.get('matches'):self._say('先选择要关联的赛事账号');self.show_sources();return
        if self._public_target is None:self._say('先选择确切的公开账号候选或缓存账号');self.show_sources();return
        public=self.public_profile or {}
        nickname=next((r.get('nickname') for r in self._candidates if r.get('candidate_kind')=='player' and (r.get('region'),str(r.get('account_id')))==self._public_target[:2]),None)
        if not nickname and public.get('snapshot_id') and public.get('status') in ('ok','partial','no_battles','private') and public.get('nickname')!=self._public_target[1]:nickname=public.get('nickname')
        if not nickname:self._say('尚无可确认的公开昵称，请先查询该账号');return
        match=profile['matches'][0];self.host.open_battle(match['battle_id']);self.host.show_page(4)
        editor=self.host.workbench.identity_panel;editor.refresh()
        for row in range(editor.roster_table.rowCount()):
            if editor.roster_table.item(row,0).data(Qt.ItemDataRole.UserRole)==match['tournament_name']:
                editor.roster_table.selectRow(row);break
        editor.player_name.setText(profile['name']);editor.personal_name.setText(nickname)
        editor.region.setCurrentIndex(max(0,editor.region.findData(self._public_target[0])));editor.account_id.setText(self._public_target[1])
        editor.confirmed.setChecked(False)
        editor._notice('候选已填入，身份与分配范围待确认')
        self.host.workbench.show_tool('identity')

    def closeEvent(self,event):
        if self.busy:self._closing=True;self.cancel();event.ignore();return
        super().closeEvent(event)
