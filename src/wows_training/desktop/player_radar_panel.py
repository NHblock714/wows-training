"""Editable player radar cards, an ordered local playlist and image export."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re
import sqlite3

from PySide6.QtCore import QEvent,Qt,QSize,QTimer,Signal
from PySide6.QtGui import QColor,QIcon,QKeySequence,QPixmap,QShortcut
from PySide6.QtWidgets import (QAbstractItemView,QComboBox,QCompleter,QDialog,QDoubleSpinBox,
    QFileDialog,QFrame,QGridLayout,QHBoxLayout,QLabel,QLineEdit,QListWidget,QListWidgetItem,
    QMessageBox,QPushButton,QScrollArea,QSpinBox,QSplitter,QVBoxLayout,QWidget)

from ..career_service import CareerService
from ..player_radar import DIMENSIONS,SPECIALTIES,RadarStore,account_from_profile,evaluate_card
from ..radar_exchange import export_saved_list,import_saved_list
from .fleet_shell import FleetPageHeader
from .multiview_panel import RecordingJob
from .naval_assets import default_naval_assets
from .player_dossier_panel import query_status,time_text
from .player_radar_canvas import RadarCanvas,specialty_icon_image,grade_color
from .ui_components import Disclosure
from . import academy_palette as palette

REGIONS={'asia':'亚服','eu':'欧服','na':'美服'}
CLASS_NAMES={'Destroyer':'驱逐','Cruiser':'巡洋','Battleship':'战列'}
NEW='__new__'
CARD_FIELDS=('team','player_name','region','account_id','role','ship_id','specialties','scores','account_evidence','pr_max',
             'account_score_mode','manual_account_score')


def payload(card):
    value=evaluate_card(card)
    return {key:deepcopy(value[key]) for key in CARD_FIELDS}


def label(text='',name=None):
    value=QLabel(text);value.setTextFormat(Qt.TextFormat.PlainText)
    if name:value.setObjectName(name)
    return value


def _grade_badge_style(score, *, overall=False):
    accent = QColor(grade_color(score) if score is not None else '#7D95AF')

    def tint(base, proportion):
        rgb = (round(start*(1-proportion)+end*proportion) for start, end in
               zip(QColor(base).getRgb()[:3], accent.getRgb()[:3]))
        return QColor(*rgb).name()

    ink = tint(palette.INK, .30)
    glass = tint('#F2F7FC', .13)
    edge = tint(palette.LINE, .32)
    rail = tint('#768DA7', .60)
    padding = '2px 10px' if overall else '1px 2px'
    return (f'QLabel {{ background:qlineargradient(x1:0,y1:0,x2:0,y2:1,'
            f'stop:0 #FFFFFF,stop:1 {glass}); color:{ink}; border:1px solid {edge}; '
            f'border-bottom:2px solid {rail}; border-radius:4px; padding:{padding}; font-weight:650; }}')


class RadarPresentation(QDialog):
    def __init__(self,panel):
        super().__init__(panel.host);self.panel=panel;self.setWindowTitle('选手八维图 · 播放')
        self.setStyleSheet('QDialog {background:#EDF4FA;} QLabel {color:#254C73;background:transparent;}')
        layout=QVBoxLayout(self);layout.setContentsMargins(0,0,0,0);layout.setSpacing(0)
        self.canvas=RadarCanvas(self);layout.addWidget(self.canvas,1)
        self.canvas.set_background_enabled(panel.canvas.background_enabled)
        self.controls=QFrame(self);self.controls.setObjectName('radarPresentationControls')
        self.controls.setStyleSheet('QFrame#radarPresentationControls {background:rgba(244,250,255,232);border:1px solid #8EB3D2;border-bottom:2px solid #E889BF;border-radius:3px;}')
        controls=QHBoxLayout(self.controls);controls.setContentsMargins(12,8,12,8)
        controls.addWidget(panel._button('上一位',lambda:panel.next_card(-1),'radarPresentationPrevious'))
        self.play=panel._button('暂停',panel.toggle_play,'radarPresentationPlay');controls.addWidget(self.play)
        controls.addWidget(panel._button('下一位',lambda:panel.next_card(1),'radarPresentationNext'))
        self.position=label();controls.addWidget(self.position);controls.addStretch()
        controls.addWidget(panel._button('退出全屏',self.close,'radarPresentationClose'))
        self._controls_timer=QTimer(self);self._controls_timer.setSingleShot(True);self._controls_timer.timeout.connect(self._hide_controls)
        for widget in [self,self.canvas,self.controls,*self.controls.findChildren(QWidget)]:
            widget.setMouseTracking(True);widget.installEventFilter(self)
        for sequence,action in ((Qt.Key.Key_Escape,self.close),(Qt.Key.Key_Space,panel.toggle_play),
                                (Qt.Key.Key_Left,lambda:panel.next_card(-1)),(Qt.Key.Key_Right,lambda:panel.next_card(1))):
            shortcut=QShortcut(QKeySequence(sequence),self);shortcut.activated.connect(action)

    def _position_controls(self):
        width=min(900,max(260,self.width()-48));height=max(52,self.controls.sizeHint().height())
        self.controls.setGeometry((self.width()-width)//2,max(0,self.height()-height-16),width,height);self.controls.raise_()

    def _show_controls(self):
        self.unsetCursor();self.controls.show();self._position_controls();self._controls_timer.start(2000)

    def _hide_controls(self):
        if any(button.isDown() for button in self.controls.findChildren(QPushButton)):
            self._controls_timer.start(500);return
        self.controls.hide();self.setCursor(Qt.CursorShape.BlankCursor)

    def eventFilter(self,watched,event):
        if event.type() in (QEvent.Type.MouseMove,QEvent.Type.MouseButtonPress,QEvent.Type.TouchBegin):self._show_controls()
        return super().eventFilter(watched,event)

    def showEvent(self,event):
        super().showEvent(event);self._show_controls()

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if hasattr(self,'controls'):self._position_controls()

    def hideEvent(self,event):
        self._controls_timer.stop();self.unsetCursor();super().hideEvent(event)

    def closeEvent(self,event):
        self._controls_timer.stop();self.unsetCursor();self.panel._presentation_closed(self);super().closeEvent(event)


class PlayerRadarPanel(QWidget):
    idle=Signal();status=Signal(str)

    def __init__(self,host):
        super().__init__(host);self.host=host;self.store=RadarStore(host.library.root)
        self._career_service=None;self.job=None;self._generation=0;self._loading=False;self._refreshing=False
        self._current_key=NEW;self._edit_revision=None;self._base={};self._drafts={};self._draft_revisions={}
        self._account_evidence=None;self._account_id=None;self._candidates=[];self._candidate_mode=None
        self._candidate_query=None;self._last_deleted=None;self._deleted_drafts={};self._cards=[]
        self._playing=False;self.presentation=None;self._timer=QTimer(self);self._timer.timeout.connect(lambda:self.next_card(1))
        self._settings=self.store.settings();self._build_ui();self.refresh();self.new_card()

    @property
    def busy(self):return bool(self.job and self.job.isRunning())

    @property
    def career(self):
        if self._career_service is None:self._career_service=CareerService(self.host.library.root,self.host.bundle)
        return self._career_service

    @property
    def selected_id(self):return None if self._current_key==NEW else self._current_key

    @property
    def playing(self):return self._playing

    @property
    def dirty(self):return bool(self._drafts)

    def _button(self,text,action,name):
        widget=QPushButton(text);widget.setObjectName(name);widget.clicked.connect(action);return widget

    def _build_ui(self):
        self.setStyleSheet(
            'QDoubleSpinBox,QSpinBox {padding:4px;}'
            'QPushButton[radarSpecialty="true"] {padding:4px 8px;}'
            'QPushButton[radarSpecialty="true"]:checked {'
            'border-image:none;background:#C5EFFF;color:#132840;border:1px solid #1178BD;font-weight:650;}'
            'QPushButton[radarSpecialty="true"]:focus {border-color:#AF286F;}'
        )
        outer=QVBoxLayout(self);outer.setContentsMargins(18,16,18,12);outer.setSpacing(7)
        outer.addWidget(FleetPageHeader('选手八维图', 'profile'))
        card_actions=QHBoxLayout()
        self.draft_status=label('');self.draft_status.setStyleSheet('color:#9C2864')
        card_actions.addWidget(self.draft_status)
        card_actions.addStretch()
        self.new_button=self._button('新建',self.new_card,'radarNew');card_actions.addWidget(self.new_button)
        self.duplicate_button=self._button('复制为新选手',self.duplicate_card,'radarDuplicate');card_actions.addWidget(self.duplicate_button)
        outer.addLayout(card_actions)
        form_widget=QWidget();form=QVBoxLayout(form_widget);form.setContentsMargins(0,0,0,2);form.setSpacing(6)
        identity=QGridLayout();identity.setHorizontalSpacing(7);identity.setVerticalSpacing(5)
        self.team_edit=QLineEdit();self.team_edit.setObjectName('radarTeam');self.team_edit.setPlaceholderText('所属队伍');self.team_edit.setMaxLength(64)
        self.name_edit=QLineEdit();self.name_edit.setObjectName('radarPlayerName');self.name_edit.setPlaceholderText('个人昵称 / 公开账号 ID');self.name_edit.setMaxLength(128)
        self.region_combo=QComboBox();self.region_combo.setObjectName('radarRegion')
        for key,value in REGIONS.items():self.region_combo.addItem(value,key)
        self.query_button=self._button('查询 PR',self.query_account,'radarQuery')
        self.cache_button=self._button('使用已存快照',self.use_cached,'radarUseCache')
        self.settings_button=self._button('查询设置',self.open_query_settings,'radarQuerySettings')
        self.cancel_button=self._button('取消',self.cancel,'radarCancel');self.cancel_button.setEnabled(False)
        for column,widget in enumerate((self.team_edit,self.name_edit,self.region_combo,self.query_button,self.cache_button,self.settings_button,self.cancel_button)):
            identity.addWidget(widget,0,column)
        identity.setColumnStretch(0,2);identity.setColumnStretch(1,3)
        self.role_edit=QLineEdit();self.role_edit.setObjectName('radarRole');self.role_edit.setPlaceholderText('选手定位');self.role_edit.setMaxLength(240)
        self.ship_combo=QComboBox();self.ship_combo.setObjectName('radarShip');self.ship_combo.setEditable(True)
        self.ship_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert);self.ship_combo.setMinimumWidth(175)
        self.ship_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.ship_combo.addItem('代表舰船',None)
        assets=default_naval_assets()
        ships=getattr(self.host,'catalog',None) or self.host.bundle.ship_catalog()
        for sid,ship in sorted(ships.items(),key=lambda row:(str(row[1].get('display_name') or row[1].get('internal_name') or row[0]),row[0])):
            native=assets.ship_info(sid)
            if not native.get('render') and not native.get('silhouette'):continue
            title=ship.get('display_name') or native.get('name') or ship.get('internal_name') or sid
            self.ship_combo.addItem(f'{title} · {ship.get("index",sid)}',str(sid))
        self.ship_combo.completer().setFilterMode(Qt.MatchFlag.MatchContains)
        self.ship_combo.completer().setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.ship_combo.completer().setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        specialty=QWidget();specialty_row=QHBoxLayout(specialty);specialty_row.setContentsMargins(0,0,0,0);specialty_row.setSpacing(4)
        self.specialty_checks={}
        for kind in SPECIALTIES:
            choice=QPushButton(CLASS_NAMES[kind]);choice.setCheckable(True)
            icon=specialty_icon_image(kind,height=22,outline=True)
            if icon is not None:
                pixmap=QPixmap.fromImage(icon)
                modes=QIcon(pixmap)
                modes.addPixmap(pixmap,QIcon.Mode.Normal,QIcon.State.On)
                choice.setIcon(modes)
            choice.setIconSize(QSize(44,26));choice.setProperty('radarSpecialty',True);choice.setObjectName('radarSpecialty'+kind)
            choice.toggled.connect(self._changed);self.specialty_checks[kind]=choice;specialty_row.addWidget(choice)
        identity.addWidget(self.ship_combo,1,0);identity.addWidget(self.role_edit,1,1,1,2);identity.addWidget(specialty,1,3,1,4)
        form.addLayout(identity)
        account_row=QHBoxLayout();self.account_notice=label('PR 未查询');self.account_notice.setWordWrap(True)
        self.account_notice.setStyleSheet('color:'+palette.MUTED+';font-size:11px');account_row.addWidget(self.account_notice,1)
        self.candidate_combo=QComboBox();self.candidate_combo.setObjectName('radarCandidate');self.candidate_combo.setMinimumWidth(180);self.candidate_combo.hide()
        self.candidate_button=self._button('选用该账号',self.apply_candidate,'radarUseCandidate');self.candidate_button.hide()
        account_row.addWidget(self.candidate_combo);account_row.addWidget(self.candidate_button)
        self.account_mode_combo=QComboBox();self.account_mode_combo.setObjectName('radarAccountMode')
        self.account_mode_combo.addItem('PR 自动','pr');self.account_mode_combo.addItem('手动评分','manual')
        self.account_mode_combo.setFixedWidth(100);self.account_mode_combo.setToolTip('账号评分方式')
        account_row.addWidget(self.account_mode_combo)
        account_row.addWidget(label('PR 满分基准'))
        self.pr_max_input=QDoubleSpinBox();self.pr_max_input.setObjectName('radarPRMaximum');self.pr_max_input.setRange(100,100000)
        self.pr_max_input.setDecimals(0);self.pr_max_input.setSingleStep(100);self.pr_max_input.setValue(self._settings['default_pr_max']);self.pr_max_input.setFixedWidth(94)
        self.pr_max_input.setToolTip('120 分对应的 PR');account_row.addWidget(self.pr_max_input)
        form.addLayout(account_row)
        axes=QGridLayout();axes.setSpacing(5);self.score_inputs={};self.axis_grades={}
        for index,(key,title) in enumerate(DIMENSIONS):
            cell=QFrame();cell.setObjectName('radarAxisCell')
            cell.setStyleSheet('QFrame#radarAxisCell {background:'+palette.PAPER+';border:1px solid '+palette.LINE+';}')
            cell_layout=QHBoxLayout(cell);cell_layout.setContentsMargins(8,5,8,5);cell_layout.setSpacing(5)
            name=label(title);name.setStyleSheet('font-size:12px;color:'+palette.INK);cell_layout.addWidget(name)
            cell_layout.addStretch()
            if key=='account':
                self.account_score=label('—');self.account_score.setObjectName('radarAccountScore');self.account_score.setAlignment(Qt.AlignmentFlag.AlignRight|Qt.AlignmentFlag.AlignVCenter)
                self.account_score.setMinimumWidth(56);cell_layout.addWidget(self.account_score)
                self.manual_account_input=QDoubleSpinBox();self.manual_account_input.setObjectName('radarManualAccountScore')
                self.manual_account_input.setRange(-1,120);self.manual_account_input.setSpecialValueText('—')
                self.manual_account_input.setDecimals(1);self.manual_account_input.setValue(-1);self.manual_account_input.setSingleStep(1)
                self.manual_account_input.setFixedWidth(76);self.manual_account_input.setToolTip('账号评分（0–120）')
                self.manual_account_input.valueChanged.connect(self._manual_score_changed);self.manual_account_input.hide();cell_layout.addWidget(self.manual_account_input)
            else:
                value=QDoubleSpinBox();value.setObjectName('radarScore'+key.title());value.setRange(-1,120);value.setSpecialValueText('—');value.setDecimals(1);value.setValue(-1);value.setSingleStep(1)
                value.setToolTip('评分（0–120）');value.setFixedWidth(76)
                value.valueChanged.connect(lambda number,k=key:self._score_changed(k,number));cell_layout.addWidget(value);self.score_inputs[key]=value
            grade=label('—','radarAxisGrade');grade.setFixedWidth(28);grade.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.axis_grades[key]=grade;cell_layout.addWidget(grade);axes.addWidget(cell,index//4,index%4)
        for column in range(4):axes.setColumnStretch(column,1)
        form.addLayout(axes)
        actions=QHBoxLayout();self.save_button=self._button('添加到列表',self.save_current,'radarSave');self.save_button.setObjectName('radarSave')
        actions.addWidget(self.save_button);self.save_copy_button=self._button('另存为新卡',lambda:self.save_current(as_new=True),'radarSaveCopy');actions.addWidget(self.save_copy_button)
        self.discard_button=self._button('还原未保存修改',self.discard_current,'radarDiscard');actions.addWidget(self.discard_button);actions.addStretch()
        self.overall_label=label('未完成 · 0 / 8','radarOverallGrade');actions.addWidget(self.overall_label);form.addLayout(actions)
        self.editor=Disclosure('录入与评分',form_widget,expanded=True);outer.addWidget(self.editor)
        self.splitter=QSplitter(Qt.Orientation.Horizontal);outer.addWidget(self.splitter,1)
        roster=QWidget();roster.setMinimumWidth(182);roster.setMaximumWidth(285);roster_layout=QVBoxLayout(roster);roster_layout.setContentsMargins(0,0,0,0);roster_layout.setSpacing(5)
        row=QHBoxLayout();self.list_count=label('播放名单 · 0');row.addWidget(self.list_count);row.addStretch()
        self.up_button=self._button('↑',lambda:self.move_selected(-1),'radarMoveUp');self.down_button=self._button('↓',lambda:self.move_selected(1),'radarMoveDown')
        self.up_button.setFixedWidth(34);self.down_button.setFixedWidth(34);self.up_button.setToolTip('向上移动');self.down_button.setToolTip('向下移动');row.addWidget(self.up_button);row.addWidget(self.down_button);roster_layout.addLayout(row)
        transfer=QHBoxLayout();transfer.setSpacing(5)
        self.import_list_button=self._button('导入列表',self.import_list,'radarImportList')
        self.export_list_button=self._button('导出列表',self.export_list,'radarExportList')
        for widget in (self.import_list_button,self.export_list_button):
            widget.setStyleSheet('padding:6px 8px;');transfer.addWidget(widget)
        self.import_list_button.setToolTip('导入 JSON 名单')
        self.export_list_button.setToolTip('导出 JSON 名单')
        roster_layout.addLayout(transfer)
        self.card_list=QListWidget();self.card_list.setObjectName('radarCardList');self.card_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.card_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove);self.card_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.card_list.currentItemChanged.connect(self._selection_changed);self.card_list.model().rowsMoved.connect(self._drag_reordered);roster_layout.addWidget(self.card_list,1)
        footer=QHBoxLayout();self.delete_button=self._button('移除',self.delete_selected,'radarDelete');footer.addWidget(self.delete_button)
        self.restore_button=self._button('撤销移除',self.restore_deleted,'radarRestore');self.restore_button.setEnabled(False);footer.addWidget(self.restore_button);roster_layout.addLayout(footer)
        self.splitter.addWidget(roster)
        stage=QWidget();stage_layout=QVBoxLayout(stage);stage_layout.setContentsMargins(0,0,0,0);stage_layout.setSpacing(5)
        stage_header=QHBoxLayout();self.stage_caption=label('编辑预览');self.stage_caption.setStyleSheet('color:'+palette.MUTED);stage_header.addWidget(self.stage_caption);stage_header.addStretch()
        self.compare_combo=QComboBox()
        self.compare_combo.setObjectName('radarComparison')
        self.compare_combo.setAccessibleName('选择八维图对照档案')
        self.compare_combo.setMinimumContentsLength(8)
        self.compare_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.compare_combo.setToolTip('对照档案')
        self.compare_combo.currentIndexChanged.connect(self._apply_comparison)
        stage_header.addWidget(self.compare_combo)
        self.export_button=self._button('导出此选手 PNG',self.export_selected,'radarExport');stage_header.addWidget(self.export_button)
        self.fullscreen_button=self._button('全屏轮播',self.show_presentation,'radarFullscreen');stage_header.addWidget(self.fullscreen_button);stage_layout.addLayout(stage_header)
        self.canvas=RadarCanvas(self);self.canvas.setMinimumHeight(132);stage_layout.addWidget(self.canvas,1)
        playback=QHBoxLayout();self.previous_button=self._button('上一位',lambda:self.next_card(-1),'radarPrevious');playback.addWidget(self.previous_button)
        self.play_button=self._button('开始轮播',self.toggle_play,'radarPlay');playback.addWidget(self.play_button)
        self.next_button=self._button('下一位',lambda:self.next_card(1),'radarNext');playback.addWidget(self.next_button)
        self.play_position=label('0 / 0');playback.addWidget(self.play_position);playback.addStretch();playback.addWidget(label('每位'))
        self.interval_input=QSpinBox();self.interval_input.setObjectName('radarInterval');self.interval_input.setRange(2,120);self.interval_input.setSuffix(' 秒');self.interval_input.setValue(self._settings['interval_seconds']);self.interval_input.setFixedWidth(82)
        playback.addWidget(self.interval_input);stage_layout.addLayout(playback);self.splitter.addWidget(stage);self.splitter.setStretchFactor(1,1);self.splitter.setSizes([220,900])
        self.notice=label('');self.notice.setWordWrap(True);self.notice.setStyleSheet('color:'+palette.MUTED+';font-size:11px');outer.addWidget(self.notice)
        self.name_edit.textChanged.connect(self._identity_changed);self.region_combo.currentIndexChanged.connect(self._identity_changed)
        for widget in (self.team_edit,self.role_edit):widget.textChanged.connect(self._changed)
        self.ship_combo.currentIndexChanged.connect(self._changed);self.ship_combo.editTextChanged.connect(self._changed)
        self.pr_max_input.valueChanged.connect(self._changed);self.interval_input.valueChanged.connect(self._interval_changed)
        self.account_mode_combo.currentIndexChanged.connect(self._changed)

    def _say(self,text):self.notice.setText(str(text));self.status.emit(str(text))

    def _empty(self):return evaluate_card({'pr_max':self.store.settings()['default_pr_max']})

    def _ship_id(self):
        index=self.ship_combo.findText(self.ship_combo.currentText(),Qt.MatchFlag.MatchExactly)
        return self.ship_combo.itemData(index) if index>=0 else None

    def _form(self):
        return {'team':self.team_edit.text(),'player_name':self.name_edit.text(),'region':self.region_combo.currentData(),
            'account_id':self._account_id,'role':self.role_edit.text(),'ship_id':self._ship_id(),
            'specialties':[key for key,widget in self.specialty_checks.items() if widget.isChecked()],
            'scores':{key:(None if widget.value()<0 else widget.value()) for key,widget in self.score_inputs.items()},
            'account_evidence':deepcopy(self._account_evidence),'pr_max':self.pr_max_input.value(),
            'account_score_mode':self.account_mode_combo.currentData(),
            'manual_account_score':None if self.manual_account_input.value()<0 else self.manual_account_input.value()}

    def _score_changed(self,key,number):
        if number<0 and number!=-1:self.score_inputs[key].setValue(-1);return
        self._changed()

    def _manual_score_changed(self,number):
        if number<0 and number!=-1:self.manual_account_input.setValue(-1);return
        self._changed()

    def _changed(self,*_):
        if self._loading or self._playing:return
        value=payload(self._form())
        if value!=self._base:
            self._drafts[self._current_key]=value;self._draft_revisions[self._current_key]=self._edit_revision
        else:self._drafts.pop(self._current_key,None);self._draft_revisions.pop(self._current_key,None)
        self._paint_preview(value);self._mark_list()

    def _identity_changed(self,*_):
        if self._loading:return
        self._generation+=1;self._account_id=None;self._account_evidence=None;self._clear_candidates()
        self._loading=True;self.account_mode_combo.setCurrentIndex(0);self.manual_account_input.setValue(-1);self._loading=False
        self._changed()

    def _clear_candidates(self):
        self._candidates=[];self._candidate_mode=None;self._candidate_query=None
        self.candidate_combo.clear();self.candidate_combo.hide();self.candidate_button.hide()

    def _paint_preview(self,value=None,*,animate=False):
        card=evaluate_card(self._form() if value is None else value);self.canvas.set_card(card,animate=animate)
        score=card['scores']['account'];self.account_score.setText('—' if score is None else f'{score:.1f}')
        manual=card.get('account_score_mode','pr')=='manual'
        self.account_score.setVisible(not manual);self.manual_account_input.setVisible(manual);self.manual_account_input.setEnabled(manual)
        self.pr_max_input.setEnabled(not manual)
        for key,grade in self.axis_grades.items():
            score=card['scores'][key]
            grade.setText(card['grades'][key])
            badge_style = _grade_badge_style(score)
            if grade.styleSheet() != badge_style:
                grade.setStyleSheet(badge_style)
        complete=card['overall_score']
        self.overall_label.setText(f'综合 {complete:.1f} · {card["overall_grade"]}' if complete is not None else f'未完成 · {card["known_count"]} / 8')
        overall_style = _grade_badge_style(complete, overall=True)
        if self.overall_label.styleSheet() != overall_style:
            self.overall_label.setStyleSheet(overall_style)
        evidence=card.get('account_evidence')
        if manual:
            self.account_notice.setText('');self.account_notice.setToolTip('')
        elif evidence:
            source='历史快照' if evidence.get('source_type')=='legacy_cache' or 'mtime' in (evidence.get('time_kind') or '') else '公开快照'
            when=time_text(evidence.get('timestamp'))
            pr=evidence.get('pr');text=f'{evidence.get("nickname") or evidence.get("account_id")} · {REGIONS.get(evidence["region"])} · '
            unavailable='PR 未知' if evidence.get('status') in ('ok','partial') else query_status(evidence,'PR 未知')
            text+=(f'PR {pr:,.0f}' if pr is not None else unavailable)+f' · {source} {when}'
            self.account_notice.setText(text);self.account_notice.setToolTip(f'公开账号 ID：{evidence.get("account_id")}\n随机战综合 PR')
        else:self.account_notice.setText('PR 未查询');self.account_notice.setToolTip('随机战综合 PR')
        self.stage_caption.setText('未保存预览' if self._current_key in self._drafts or self._current_key==NEW else '已保存')
        self.draft_status.setText(f'{len(self._drafts)} 份未保存' if self._drafts else '')
        self.discard_button.setEnabled(self._current_key in self._drafts)

    def _mark_list(self):
        for index in range(self.card_list.count()):
            item=self.card_list.item(index);card=next((row for row in self._cards if row['id']==item.data(Qt.ItemDataRole.UserRole)),None)
            if card:
                mark=' *' if card['id'] in self._drafts else ''
                grade=card['overall_grade'] if card['overall_score'] is not None else '未完成'
                item.setText(f'{index+1:02d}  {card["player_name"]}{mark}\n{card["team"] or "独立选手"} · {grade}')

    def refresh(self):
        self._cards=self.store.list_cards();selected=self.selected_id;self._refreshing=True
        self.card_list.clear()
        for card in self._cards:
            item=QListWidgetItem();item.setData(Qt.ItemDataRole.UserRole,card['id']);item.setSizeHint(QSize(190,53));self.card_list.addItem(item)
            if card['id']==selected:self.card_list.setCurrentItem(item)
        self._refreshing=False;self._mark_list();self.list_count.setText(f'播放名单 · {len(self._cards)}');self._update_position()
        current=self.compare_combo.currentData()
        self.compare_combo.blockSignals(True)
        self.compare_combo.clear()
        self.compare_combo.addItem('单人展示',None)
        for card in self._cards:
            name=' · '.join(part for part in (card.get('team'), card.get('player_name')) if part)
            self.compare_combo.addItem('对照 · '+name,card['id'])
        self.compare_combo.setCurrentIndex(max(0,self.compare_combo.findData(current)))
        self.compare_combo.blockSignals(False)
        self._apply_comparison()
        enabled=bool(self._cards)
        self.export_list_button.setEnabled(enabled)
        for widget in (self.play_button,self.previous_button,self.next_button,self.fullscreen_button):widget.setEnabled(enabled)
        self.restore_button.setEnabled(self._last_deleted is not None)

    def _update_position(self):
        index=next((i for i,card in enumerate(self._cards) if card['id']==self.selected_id),-1)
        text=f'{index+1 if index>=0 else 0} / {len(self._cards)}';self.play_position.setText(text)
        has=index>=0
        self.export_button.setEnabled(has);self.delete_button.setEnabled(has);self.up_button.setEnabled(index>0);self.down_button.setEnabled(has and index<len(self._cards)-1)
        if self.presentation:self.presentation.position.setText(text)

    def _comparison_card(self):
        identifier=self.compare_combo.currentData()
        if not identifier or identifier==self.selected_id:return None
        return next((row for row in self._cards if row['id']==identifier),None)

    def _apply_comparison(self,*_):
        card=self._comparison_card()
        self.canvas.set_comparison(card)
        self.export_button.setText('导出对照 PNG' if card else '导出此选手 PNG')
        if self.presentation:
            self.presentation.canvas.set_comparison(card)

    def _selection_changed(self,current,previous):
        if self._refreshing or current is None:return
        self._load_card(current.data(Qt.ItemDataRole.UserRole),force_saved=self._playing)

    def select_card(self,card_id):
        for index in range(self.card_list.count()):
            item=self.card_list.item(index)
            if item.data(Qt.ItemDataRole.UserRole)==card_id:
                self.card_list.setCurrentItem(item);self.card_list.scrollToItem(item,QAbstractItemView.ScrollHint.EnsureVisible)
                if self._current_key!=card_id:self._load_card(card_id,force_saved=self._playing)
                return True
        return False

    def _load_card(self,key,force_saved=False):
        entering=self._current_key!=key
        base=self._empty() if key==NEW else self.store.get(key)
        value=base if force_saved else self._drafts.get(key,base)
        self._generation+=1;self._current_key=key;self._edit_revision=self._draft_revisions.get(key,base.get('revision'))
        self._base=payload(base);self._loading=True;self._clear_candidates()
        self._account_id=value.get('account_id');self._account_evidence=deepcopy(value.get('account_evidence'))
        self.account_mode_combo.setCurrentIndex(max(0,self.account_mode_combo.findData(value.get('account_score_mode','pr'))))
        manual_score=value.get('manual_account_score');self.manual_account_input.setValue(-1 if manual_score is None else manual_score)
        self.team_edit.setText(value.get('team',''));self.name_edit.setText(value.get('player_name',''))
        self.region_combo.setCurrentIndex(max(0,self.region_combo.findData(value.get('region','asia'))));self.role_edit.setText(value.get('role',''))
        ship_id=value.get('ship_id');ship_index=self.ship_combo.findData(ship_id)
        if ship_id is not None and ship_index<0:
            self.ship_combo.addItem(f'舰船 {ship_id}（暂无本机素材）',ship_id);ship_index=self.ship_combo.count()-1
        self.ship_combo.setCurrentIndex(max(0,ship_index))
        for kind,widget in self.specialty_checks.items():widget.setChecked(kind in value.get('specialties',[]))
        for name,widget in self.score_inputs.items():
            number=value.get('scores',{}).get(name);widget.setValue(-1 if number is None else number)
        self.pr_max_input.setValue(value.get('pr_max',3000));self._loading=False
        self.save_button.setText('添加到列表' if key==NEW else '保存修改');self.save_copy_button.setEnabled(key!=NEW)
        self._paint_preview(value,animate=entering);self._update_position();self._apply_comparison()
        if self._playing:self._show_playback(base)

    def new_card(self):
        if self._playing:self.pause()
        self.card_list.blockSignals(True);self.card_list.setCurrentRow(-1);self.card_list.blockSignals(False)
        self._load_card(NEW);self.editor.setExpanded(True)
        if NEW in self._drafts:self._say('已返回未保存的新选手草稿')
        else:self._say('')

    def duplicate_card(self):
        if self._playing:self.pause()
        if NEW in self._drafts and self._current_key!=NEW:
            self.new_card();self._say('已有新选手草稿；请先保存或还原后再复制');return
        original=payload(self._form());self.new_card()
        self._drafts[NEW]=original;self._draft_revisions[NEW]=None;self._load_card(NEW);self._say('副本尚未保存，可修改个人 ID 后添加')

    def save_current(self,*_,as_new=False):
        if self.busy:self._say('请等待查询完成或取消后保存');return None
        if self._playing:self.pause()
        original_key=self._current_key
        try:
            data=self._form();record=self.store.save(data,card_id=None if as_new or original_key==NEW else original_key,
                expected_revision=None if as_new or original_key==NEW else self._edit_revision)
        except (ValueError,KeyError,RuntimeError,OSError,sqlite3.Error) as exc:self._say('档案未保存：'+str(exc));return None
        self._drafts.pop(original_key,None);self._draft_revisions.pop(original_key,None)
        self._current_key=record['id'];self.refresh();self._load_card(record['id']);self.select_card(record['id'])
        try:self.store.save_settings(default_pr_max=data['pr_max'])
        except (ValueError,RuntimeError,OSError,sqlite3.Error):
            self._say('档案已保存，默认设置未更新');return record
        self._say('已添加到播放名单' if original_key==NEW or as_new else '修改已保存');return record

    def discard_current(self):
        self._drafts.pop(self._current_key,None);self._draft_revisions.pop(self._current_key,None)
        self._load_card(self._current_key);self._mark_list();self._say('已还原为保存状态' if self.selected_id else '新选手草稿已清空')

    def move_selected(self,delta):
        if self.selected_id is None:return
        ids=[card['id'] for card in self._cards];index=ids.index(self.selected_id);target=index+delta
        if not 0<=target<len(ids):return
        ids[index],ids[target]=ids[target],ids[index];self.store.reorder(ids);self.refresh()
        if self.selected_id:self.select_card(self.selected_id)

    def _drag_reordered(self,*_):
        if self._refreshing:return
        ids=[self.card_list.item(index).data(Qt.ItemDataRole.UserRole) for index in range(self.card_list.count())]
        try:self.store.reorder(ids);self._cards=self.store.list_cards();self._mark_list();self._update_position()
        except (ValueError,KeyError) as exc:self._say(str(exc));self.refresh()

    def delete_selected(self):
        identifier=self.selected_id
        if identifier is None:return
        self.pause();index=next((i for i,row in enumerate(self._cards) if row['id']==identifier),0)
        self.store.delete(identifier);self._last_deleted=identifier
        if identifier in self._drafts:self._deleted_drafts[identifier]=(self._drafts.pop(identifier),self._draft_revisions.pop(identifier,None))
        self._current_key=NEW;self.refresh()
        if self._cards:self.select_card(self._cards[min(index,len(self._cards)-1)]['id'])
        else:self.new_card()
        self._say('已从名单移除，可撤销')

    def restore_deleted(self):
        if not self._last_deleted:return
        card=self.store.restore(self._last_deleted);identifier=card['id'];self._last_deleted=None
        draft=self._deleted_drafts.pop(identifier,None)
        if draft:self._drafts[identifier]=draft[0];self._draft_revisions[identifier]=card['revision']
        self.refresh();self.select_card(identifier);self._say('已恢复到播放名单')

    def import_list(self,path=None):
        if isinstance(path,bool):path=None
        if self.busy:self._say('请先完成或取消账号查询');return None
        self.pause()
        if path is None:
            path,_=QFileDialog.getOpenFileName(self,'导入选手八维图列表',str(self.host.library.root),'八维图列表 (*.json)')
        if not path:return None
        try:result=import_saved_list(self.store,path)
        except (OSError,ValueError,RuntimeError,sqlite3.Error) as exc:
            self._say('列表导入失败：'+str(exc));return None
        self.refresh()
        if result['ids']:self.select_card(result['ids'][0])
        self._say(f'已导入 {result["added"]} 位 · 跳过重复 {result["duplicates"]} 位 · 名单共 {result["total"]} 位')
        return result

    def export_list(self,path=None):
        if isinstance(path,bool):path=None
        if not self.store.list_cards():self._say('名单为空');return None
        self.pause()
        if path is None:
            path,_=QFileDialog.getSaveFileName(self,'导出选手八维图列表',str(Path(self.host.library.root)/'选手八维图列表.json'),'八维图列表 (*.json)')
        if not path:return None
        destination=Path(path)
        if destination.suffix.lower()!='.json':destination=destination.with_suffix('.json')
        try:result=export_saved_list(self.store,destination)
        except (OSError,ValueError,RuntimeError,sqlite3.Error) as exc:
            self._say('列表导出失败：'+str(exc));return None
        suffix='；未保存修改仍保留在本机' if self.dirty else ''
        self._say(f'已导出 {result["count"]} 位已保存选手'+suffix)
        return result

    def _query_identity(self):return (self.region_combo.currentData(),self.name_edit.text().strip())

    def _start(self,operation,success,text):
        if self.busy:self._say('查询进行中');return False
        self._say(text);self.job=RecordingJob(operation,self)
        generation=self._generation
        self.job.progress.connect(lambda value:self._say(value) if generation==self._generation else None)
        self.job.succeeded.connect(success);self.job.failed.connect(lambda _:self._say('查询已取消' if self.job and self.job.cancel_event.is_set() else '查询未完成，请检查查询设置'))
        self.job.finished.connect(self._job_finished)
        for button in (self.query_button,self.cache_button,self.candidate_button,self.save_button,self.save_copy_button,self.import_list_button):button.setEnabled(False)
        self.cancel_button.setEnabled(True);self.job.start();return True

    def _job_finished(self):
        for button in (self.query_button,self.cache_button,self.candidate_button,self.save_button,self.import_list_button):button.setEnabled(True)
        self.save_copy_button.setEnabled(self.selected_id is not None);self.cancel_button.setEnabled(False);self.idle.emit()

    def cancel(self):
        self._generation+=1;self._pending_query=None
        if self.busy:self.job.cancel_event.set();self._say('正在取消查询')

    def query_account(self):
        region,name=self._query_identity()
        if not name:self._say('缺少昵称或账号 ID');return
        self._generation+=1;generation=self._generation;target=(region,name);self._clear_candidates()
        if name.isascii() and name.isdigit():
            if int(name)<=0:self._say('公开账号 ID 无效');return
            self._query_profile(region,str(int(name)),generation,target);return
        def accepted(result):
            if generation!=self._generation or target!=self._query_identity():self._say('查询完成；当前选手已变化，未应用结果');return
            if result.get('status')!='ok':self._say(query_status(result));return
            rows=(result.get('data') or {}).get('candidates',[])
            exact=[row for row in rows if str(row.get('nickname','')).casefold()==name.casefold()]
            if len(exact)==1:
                row=exact[0]
                # Start the second worker after the search thread's finished signal.
                self._pending_query=(region,str(row['account_id']),generation,target)
            else:self._show_candidates(rows,'query',target)
        self._pending_query=None
        self._start(lambda cancel,progress:self.career.search(region,name,cancel=cancel),accepted,'检索个人账号…')
        if self.job:
            job=self.job
            def next_request():
                pending=self._pending_query;self._pending_query=None
                if pending and pending[2]==self._generation and pending[3]==self._query_identity():
                    QTimer.singleShot(0,lambda:self._query_profile(*pending))
            job.finished.connect(next_request)

    def _query_profile(self,region,account_id,generation,target):
        if generation!=self._generation or target!=self._query_identity():return
        self._start(lambda cancel,progress:self.career.refresh(region,account_id,'pvp',cancel=cancel,progress=progress),
            lambda profile:self._accept_profile(profile,region,account_id,generation,target),'读取随机战综合 PR…')

    def _accept_profile(self,profile,region,account_id,generation,target):
        if generation!=self._generation or target!=self._query_identity():self._say('查询完成；当前选手已变化，未应用结果');return
        if profile.get('region')!=region or str(profile.get('account_id'))!=str(account_id) or profile.get('mode')!='pvp':
            self._say('返回账号与当前选择不一致，未应用');return
        evidence=account_from_profile(profile)
        self._account_id=str(account_id);self._account_evidence=evidence
        if evidence.get('nickname') and evidence['nickname']!=str(account_id) and self.name_edit.text()!=evidence['nickname']:
            self._loading=True;self.name_edit.setText(evidence['nickname']);self._loading=False
        self._clear_candidates();self._changed()
        if self.account_mode_combo.currentData()=='manual':
            self._say('查询完成，当前仍使用手动评分' if evidence.get('pr') is not None else '未取得 PR，当前手动评分已保留')
        else:self._say('账号评分已更新' if evidence.get('pr') is not None else
            'PR 暂无可用基准' if profile.get('status') in ('ok','partial') else query_status(profile,'PR 暂无可用基准'))

    def _show_candidates(self,rows,mode,target):
        self._candidates=[dict(row) for row in rows];self._candidate_mode=mode;self._candidate_query=target
        self.candidate_combo.clear()
        for row in rows:self.candidate_combo.addItem(f'{row.get("nickname") or row.get("account_id")} · {row.get("account_id")}')
        self.candidate_combo.setVisible(bool(rows));self.candidate_button.setVisible(bool(rows))
        self._say('请选择明确的个人账号' if rows else '没有对应的已存快照' if mode=='cache' else '未找到匹配账号')

    def apply_candidate(self):
        index=self.candidate_combo.currentIndex()
        if not 0<=index<len(self._candidates) or self._candidate_query!=self._query_identity():return
        row=self._candidates[index];region,name=self._query_identity();identifier=str(row['account_id']);generation=self._generation
        if self._candidate_mode=='cache':
            profile=self.career.profile(region,identifier,'pvp');self._accept_profile(profile,region,identifier,generation,(region,name))
        else:self._query_profile(region,identifier,generation,(region,name))

    def use_cached(self):
        if self.busy:return
        region,name=self._query_identity()
        if not name:self._say('缺少昵称或账号 ID');return
        self._generation+=1;generation=self._generation;rows=self.career.list_profiles()
        numeric=str(int(name)) if name.isascii() and name.isdigit() and int(name)>0 else None
        matches=[row for row in rows if row.get('region')==region and (str(row.get('account_id'))==numeric if numeric else str(row.get('nickname') or '').casefold()==name.casefold())]
        unique={str(row['account_id']):row for row in matches};matches=list(unique.values())
        if len(matches)==1:
            identifier=str(matches[0]['account_id']);profile=self.career.profile(region,identifier,'pvp')
            self._accept_profile(profile,region,identifier,generation,(region,name))
        else:self._show_candidates(matches,'cache',(region,name))

    def open_query_settings(self):
        self.pause();self.host.show_page(7);dossier=self.host.dossier_panel;dossier.show_sources()
        for section in dossier.findChildren(Disclosure):
            if section.toggle_button.text()=='查询与缓存设置':section.setExpanded(True)
        QTimer.singleShot(0,lambda:dossier.source_scroll.ensureWidgetVisible(dossier.network_enabled))

    def _interval_changed(self,value):
        self.store.save_settings(interval_seconds=value)
        if self._playing:self._timer.start(value*1000)

    def _show_playback(self,card):
        self.canvas.set_card(card,animate=True);self.stage_caption.setText('轮播中')
        if self.presentation:self.presentation.canvas.set_card(card,animate=True)
        self._update_position()

    def toggle_play(self):
        if self._playing:self._pause_timer();return
        if not self._cards:self._say('名单为空');return
        self._playing=True;self.editor.setExpanded(False);self.editor.body.setEnabled(False);self.play_button.setText('暂停轮播')
        if self.presentation:self.presentation.play.setText('暂停')
        identifier=self.selected_id if any(row['id']==self.selected_id for row in self._cards) else self._cards[0]['id']
        self.select_card(identifier);self._load_card(identifier,force_saved=True);self._timer.start(self.interval_input.value()*1000)

    def _pause_timer(self):
        was_playing=self._playing;self._playing=False;self._timer.stop();self.editor.body.setEnabled(True);self.play_button.setText('开始轮播')
        if self.presentation:self.presentation.play.setText('继续')
        if was_playing and self.selected_id:self._load_card(self.selected_id)

    def pause(self):
        self._pause_timer()
        presentation=self.presentation;self.presentation=None
        if presentation:presentation.close()

    def next_card(self,delta=1):
        if not self._cards:return
        ids=[card['id'] for card in self._cards]
        current=ids.index(self.selected_id) if self.selected_id in ids else (-1 if delta>=0 else 0)
        identifier=ids[(current+delta)%len(ids)];self.select_card(identifier)
        if self._playing:self._show_playback(self.store.get(identifier));self._timer.start(self.interval_input.value()*1000)
        elif self.presentation:self.presentation.canvas.set_card(self.store.get(identifier),animate=True)

    def show_presentation(self):
        if not self._cards:self._say('名单为空');return
        if self.presentation:self.presentation.raise_();return
        self.presentation=RadarPresentation(self);self._apply_comparison();self.presentation.showFullScreen()
        if not self._playing:self.toggle_play()
        elif self.selected_id:self._show_playback(self.store.get(self.selected_id))

    def _presentation_closed(self,presentation):
        if self.presentation is presentation:self.presentation=None;self._pause_timer()

    def export_selected(self,path=None):
        # A clicked(bool) signal is not an explicit export path.
        if isinstance(path,bool):path=None
        if self.selected_id is None:self._say('尚无已保存的选手');return None
        card=self.store.get(self.selected_id)
        if path is None:
            stem=re.sub(r'[<>:"/\\|?*\x00-\x1f]','_',card['player_name']).strip(' .') or '选手'
            suffix='_对照.png' if self._comparison_card() else '_八维图.png'
            path,_=QFileDialog.getSaveFileName(self,'导出选手八维图',str(Path(self.host.library.root)/(stem+suffix)),'PNG 图片 (*.png)')
        if not path:return None
        destination=Path(path)
        if destination.suffix.lower()!='.png':destination=destination.with_suffix('.png')
        export=RadarCanvas();export.set_card(card);export.set_comparison(self._comparison_card())
        export.set_background_enabled(self.canvas.background_enabled)
        try:
            result=export.export_png(destination,width=2560,height=1440)
        except (OSError,ValueError,RuntimeError) as exc:self._say('图片导出失败：'+str(exc));return None
        finally:export.deleteLater()
        self._say('图片已导出 · 2560 × 1440');self.notice.setToolTip(str(destination));return result

    def can_close(self):
        if not self._drafts:return True
        message=QMessageBox(self);message.setWindowTitle('保存八维图编辑');message.setText(f'还有 {len(self._drafts)} 份未保存编辑。')
        save=message.addButton('全部保存',QMessageBox.ButtonRole.AcceptRole)
        discard=message.addButton('放弃未保存编辑',QMessageBox.ButtonRole.DestructiveRole)
        message.addButton('继续编辑',QMessageBox.ButtonRole.RejectRole);message.exec()
        if message.clickedButton() is discard:self._drafts.clear();self._draft_revisions.clear();return True
        if message.clickedButton() is not save:return False
        try:
            for value in self._drafts.values():
                if not evaluate_card(value)['player_name']:raise ValueError('新选手尚未填写个人 ID，请完成后保存')
            for key,value in list(self._drafts.items()):
                self.store.save(value,card_id=None if key==NEW else key,expected_revision=None if key==NEW else self._draft_revisions.get(key))
                self._drafts.pop(key,None);self._draft_revisions.pop(key,None)
        except (ValueError,KeyError,RuntimeError,OSError,sqlite3.Error) as exc:self._say('档案未保存：'+str(exc));return False
        try:self.store.save_settings(default_pr_max=self.pr_max_input.value())
        except (ValueError,RuntimeError,OSError,sqlite3.Error):self._say('档案已保存，默认设置未更新')
        return True

    def closeEvent(self,event):
        self.pause()
        if self.busy:self.cancel();event.ignore();return
        super().closeEvent(event)
