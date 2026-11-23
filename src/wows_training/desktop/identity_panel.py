"""Explicit identity confirmation and optional asynchronous public snapshots."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from uuid import UUID, uuid4

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSplitter, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from ..credential_store import SecretStore
from ..identity_rules import effective_mappings, parse_kots_name, report_battle_id
from ..public_stats import PublicStatsClient
from .ui_components import align_cell,style_table


STATUS_LABELS = {"not_fetched": "未获取", "private": "战绩隐藏", "not_found": "未找到", "no_battles": "无场次",
                 "unsupported": "来源不支持", "error": "请求失败", "ok": "已获取"}
IDENTITY_LABELS = {"confirmed": "已确认", "candidate": "候选·未确认", "conflict": "冲突·不能确认", "unmapped": "未关联"}
MODE_LABELS = {"pvp": "随机", "pvp_solo": "随机单人", "pvp_div2": "随机双人", "pvp_div3": "随机三人",
               "pve": "联合", "rank_solo": "排位单人", "club": "公会", "oper_solo": "战役单人", "oper_div": "战役组队"}


def _table(headers, name):
    table = QTableWidget(0, len(headers))
    table.setObjectName(name)
    table.setHorizontalHeaderLabels(headers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setAlternatingRowColors(True)
    table.verticalHeader().hide()
    table.horizontalHeader().setStretchLastSection(True)
    style_table(table)
    return table


def _row(table, index, values, data=None):
    for column, value in enumerate(values):
        item = QTableWidgetItem("未知" if value is None else str(value))
        item.setData(Qt.ItemDataRole.UserRole, data)
        item.setToolTip(item.text())
        align_cell(item,value)
        table.setItem(index, column, item)


def _button(text, name, slot):
    result = QPushButton(text)
    result.setObjectName(name)
    result.clicked.connect(slot)
    return result


class _PublicWorker(QThread):
    result = Signal(object)

    def __init__(self, operation, params, application_id, enabled, parent=None):
        super().__init__(parent)
        self.operation, self.params = operation, params
        self._application_id, self.enabled = application_id, enabled

    def run(self):
        try:
            client = PublicStatsClient(self._application_id, enabled=self.enabled, cancel=self.isInterruptionRequested)
            result = getattr(client, self.operation)(**self.params)
            if self.isInterruptionRequested():
                result.update(status="not_fetched", reason="cancelled", data=None)
            self.result.emit(result)
        except Exception:
            # Neither transport messages nor credentials enter status or logs.
            self.result.emit({"status": "error", "kind": self.operation, "data": None,
                              "region": self.params.get("region"), "account_id": self.params.get("account_id"),
                              "mode": self.params.get("mode"), "reason": "worker_failed", "source": {}})
        finally:
            self._application_id = None


class IdentityPanel(QWidget):
    changed = Signal()
    status = Signal(str)
    idle = Signal()

    def __init__(self, library, store, bundle, current_report_getter, parent=None):
        super().__init__(parent)
        self.library, self.store, self.bundle = library, store, bundle
        self.current_report_getter = current_report_getter
        self.secrets = SecretStore(store.root)
        self._worker = None
        self._preview = None
        self._settings_id = None
        self._report = {}
        self._edit_scope = None
        self._players, self._assignments, self._resolved = [], [], []
        self._build_ui()
        self._load_settings()
        self.refresh()

    @property
    def busy(self):
        return self._worker is not None and self._worker.isRunning()

    def cancel(self):
        if self.busy:
            self._worker.requestInterruption()
            self._notice("正在取消查询…")

    def _notice(self, text):
        self.notice.setText(text)
        self.notice.setVisible(bool(text))
        self.status.emit(text)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        self.notice = QLabel()
        self.notice.hide()
        self.notice.setObjectName("identityNotice")
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("identityTabs")
        layout.addWidget(self.tabs)
        identity = QWidget()
        self.tabs.addTab(identity, "赛事身份")
        column = QVBoxLayout(identity)
        split = QSplitter(Qt.Orientation.Horizontal)
        column.addWidget(split)
        self.roster_table = _table(["完整赛事赛号", "规则线索", "关联状态", "个人账号"], "identityRoster")
        self.roster_table.setColumnWidth(0, 260)
        self.roster_table.setColumnWidth(1, 180)
        self.roster_table.itemSelectionChanged.connect(self._select_roster)
        split.addWidget(self.roster_table)
        editor = QWidget()
        split.addWidget(editor)
        split.setSizes([630, 400])
        form = QFormLayout(editor)
        self.assignment_combo = QComboBox()
        self.assignment_combo.setObjectName("identityAssignment")
        self.assignment_combo.currentIndexChanged.connect(self._select_assignment)
        self.player_combo = QComboBox()
        self.player_combo.setObjectName("identityPlayer")
        self.player_combo.currentIndexChanged.connect(self._select_player)
        self.event_edit, self.tournament_edit, self.player_name, self.personal_name = (QLineEdit() for _ in range(4))
        for widget, name in ((self.event_edit, "identityEvent"), (self.tournament_edit, "identityTournamentName"),
                             (self.player_name, "identityPlayerName"), (self.personal_name, "identityPersonalName")):
            widget.setObjectName(name)
        self.tournament_edit.setReadOnly(True)
        self.region = QComboBox()
        self.region.setObjectName("identityRegion")
        for text, value in (("亚服", "asia"), ("欧服", "eu"), ("美服", "na")):
            self.region.addItem(text, value)
        self.account_id = QLineEdit()
        self.account_id.setObjectName("identityAccountId")
        self.account_id.setPlaceholderText("公开账号 ID（可留空）")
        self.notes = QPlainTextEdit()
        self.notes.setObjectName("identityNotes")
        self.notes.setMaximumHeight(85)
        self.scope = QComboBox()
        self.scope.setObjectName("identityScope")
        self.scope.addItem("仅当前比赛", "battle")
        self.scope.addItem("整个赛事", "event")
        self.scope.addItem("保留原分配的指定比赛范围", "existing")
        self.confirmed = QCheckBox("已核对：确认此赛事账号分配给所选选手")
        self.confirmed.setObjectName("identityConfirmed")
        self.save_button = _button("确认保存分配", "saveIdentity", self.save_assignment)
        for title, widget in (("分配记录", self.assignment_combo), ("稳定选手", self.player_combo),
                              ("赛事", self.event_edit), ("完整赛号", self.tournament_edit), ("显示名", self.player_name),
                              ("个人账号昵称", self.personal_name), ("区服", self.region), ("公开账号 ID", self.account_id),
                              ("适用范围", self.scope), ("依据 / 备注", self.notes)):
            form.addRow(title, widget)
        form.addRow(self.confirmed)
        form.addRow(self.save_button)
        form.addRow(_button("分配变更", "identityHistory", self.show_history))
        self.details = QPlainTextEdit()
        self.details.setObjectName("identityDetails")
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(130)
        column.addWidget(self.details)

        imports = QWidget()
        self.tabs.addTab(imports, "名单导入")
        imp = QVBoxLayout(imports)
        self.preview_button = _button("选择 JSON 并预览", "previewIdentityImport", self.choose_import)
        self.import_button = _button("确认导入全部有效行", "confirmIdentityImport", self.commit_import)
        self.import_button.setEnabled(False)
        actions = QHBoxLayout()
        actions.addWidget(self.preview_button)
        actions.addWidget(self.import_button)
        imp.addLayout(actions)
        self.import_preview = QPlainTextEdit()
        self.import_preview.setObjectName("identityImportPreview")
        self.import_preview.setReadOnly(True)
        imp.addWidget(self.import_preview)

        stats = QWidget()
        self.tabs.addTab(stats, "公开战绩")
        st = QVBoxLayout(stats)
        settings = QHBoxLayout()
        self.network_enabled = QCheckBox("启用手动公开战绩查询")
        self.network_enabled.setObjectName("publicStatsEnabled")
        self.application_id = QLineEdit()
        self.application_id.setObjectName("wgApplicationId")
        self.application_id.setEchoMode(QLineEdit.EchoMode.Password)
        self.application_id.setPlaceholderText("Application ID（留空保留已有凭据）")
        settings.addWidget(self.network_enabled)
        settings.addWidget(self.application_id, 1)
        self.settings_button = _button("保存设置", "savePublicStatsSettings", self.save_settings)
        settings.addWidget(self.settings_button)
        st.addLayout(settings)
        query = QHBoxLayout()
        self.search_name = QLineEdit()
        self.search_name.setObjectName("publicStatsSearch")
        self.search_name.setPlaceholderText("个人账号昵称（至少 3 个字符）")
        self.query_region = QComboBox()
        self.query_region.setObjectName("publicStatsRegion")
        for text, value in (("亚服", "asia"), ("欧服", "eu"), ("美服", "na")):
            self.query_region.addItem(text, value)
        self.query_account = QLineEdit()
        self.query_account.setObjectName("publicStatsAccountId")
        self.query_account.setPlaceholderText("精确公开账号 ID")
        self.mode = QComboBox()
        self.mode.setObjectName("publicStatsMode")
        modes = (("随机", "pvp"), ("随机单人", "pvp_solo"), ("随机双人", "pvp_div2"), ("随机三人", "pvp_div3"),
                 ("联合", "pve"), ("排位单人", "rank_solo"), ("公会", "club"), ("战役单人", "oper_solo"), ("战役组队", "oper_div"))
        for text, value in modes:
            self.mode.addItem(text, value)
        for widget in (self.search_name, self.query_region, self.query_account, self.mode):
            query.addWidget(widget)
        st.addLayout(query)
        buttons = QHBoxLayout()
        self.search_button = _button("搜索候选", "searchPublicAccounts", lambda: self.start_query("search"))
        self.account_button = _button("保存账号快照", "fetchAccountSnapshot", lambda: self.start_query("account"))
        self.ship_button = _button("保存当前用船快照", "fetchShipSnapshot", lambda: self.start_query("ships"))
        self.cancel_button = _button("取消查询", "cancelPublicStats", self.cancel)
        self.cancel_button.setEnabled(False)
        for widget in (self.search_button, self.account_button, self.ship_button, self.cancel_button):
            buttons.addWidget(widget)
        st.addLayout(buttons)
        self.candidate_table = _table(["搜索候选昵称", "区服", "精确账号 ID", "状态"], "publicStatsCandidates")
        for column, width in enumerate((260, 100, 180)):
            self.candidate_table.setColumnWidth(column, width)
        self.candidate_table.setMaximumHeight(160)
        self.candidate_table.itemSelectionChanged.connect(self.select_search_candidate)
        st.addWidget(self.candidate_table)
        self.snapshot_table = _table(["类型 / 模式", "区服 / 账号", "状态", "采集时间 UTC", "来源"], "publicStatsSnapshots")
        for column, width in enumerate((160, 160, 110, 220)):
            self.snapshot_table.setColumnWidth(column, width)
        self.snapshot_table.itemSelectionChanged.connect(self.show_snapshot)
        st.addWidget(self.snapshot_table)
        self.snapshot_details = QPlainTextEdit()
        self.snapshot_details.setObjectName("publicStatsDetails")
        self.snapshot_details.setReadOnly(True)
        self.snapshot_details.setMaximumHeight(160)
        st.addWidget(self.snapshot_details)

    def refresh(self):
        self._report = self.current_report_getter() or {}
        self._players = self.store.list_documents("player")
        self._assignments = self.store.list_documents("assignment")
        old_player = self.player_combo.currentData()
        self.player_combo.blockSignals(True)
        self.player_combo.clear()
        self.player_combo.addItem("新建稳定选手（不按同名合并）", None)
        for doc in self._players:
            self.player_combo.addItem(doc['payload'].get('display_name', doc['name']), doc["id"])
        self.player_combo.setCurrentIndex(max(0, self.player_combo.findData(old_player)))
        self.player_combo.blockSignals(False)
        old_name = self.tournament_edit.text()
        self._resolved = effective_mappings(self._report, self.library.list_mappings(), self._assignments, self._players)
        self.roster_table.blockSignals(True)
        self.roster_table.setRowCount(len(self._resolved))
        for index, row in enumerate(self._resolved):
            meta = row["resolved_identity"]
            hint = meta["candidate"]
            clue = " / ".join(part for part in (hint.get("clan"), hint.get("nickname_fragment")) if part)
            _row(self.roster_table, index, [row["tournament_name"], clue or "未匹配规则", IDENTITY_LABELS.get(meta["status"], meta["status"]),
                                           row["personal_nickname"] or "未确认"], row["tournament_name"])
            if meta["status"] == "conflict":
                for column in range(4):
                    self.roster_table.item(index, column).setForeground(QColor("#9F2C53"))
        self.roster_table.blockSignals(False)
        selected = next((i for i, row in enumerate(self._resolved) if row["tournament_name"] == old_name), 0)
        if self._resolved:
            self.roster_table.selectRow(selected)
            self._select_roster()
        else:
            self.tournament_edit.clear()
            self.event_edit.setText(self._report.get("event_key", "unassigned"))
            self.assignment_combo.clear()
            self.assignment_combo.addItem("新建分配", None)
            self.confirmed.setChecked(False)
        self.refresh_snapshots()

    def _select_roster(self):
        rows = self.roster_table.selectionModel().selectedRows()
        if not rows:
            return
        item = self._resolved[rows[0].row()]
        self.tournament_edit.setText(item["tournament_name"])
        self.event_edit.setText(self._report.get("event_key", "unassigned"))
        meta, hint = item["resolved_identity"], item["resolved_identity"]["candidate"]
        lines = [f"{IDENTITY_LABELS.get(meta['status'], meta['status'])} · {item['tournament_name']}",
                 f"名称线索：{hint.get('clan') or '未知队伍'} / {hint.get('nickname_fragment') or '未知昵称'}"]
        if meta.get("player_candidates"):
            lines.append("候选选手：" + "；".join(
                (self._player_doc(row['player_id']) or {}).get('name', '未知选手')
                for row in meta["player_candidates"]))
        self.details.setPlainText("\n".join(lines))
        self.assignment_combo.blockSignals(True)
        self.assignment_combo.clear()
        self.assignment_combo.addItem("新建分配", None)
        for doc in self._assignments:
            payload = doc["payload"]
            if payload.get("event_key") == self.event_edit.text() and payload.get("tournament_name") == item["tournament_name"]:
                scope = payload.get("battle_ids") or []
                label = "整个赛事" if not scope else f"指定 {len(scope)} 场"
                self.assignment_combo.addItem(f"第 {doc['revision']} 次分配 · {label}", doc["id"])
        self.assignment_combo.blockSignals(False)
        self.assignment_combo.setCurrentIndex(0)
        self.confirmed.setChecked(False)
        self.player_combo.setCurrentIndex(0)
        self.player_name.clear()
        self.personal_name.clear()
        self.account_id.clear()
        self.notes.clear()
        self.scope.setCurrentIndex(0)
        self._edit_scope = None
        self.search_name.setText(item["resolved_identity"]["candidate"].get("nickname_fragment") or "")
        self.query_account.clear()

    def _player_doc(self, identity):
        return next((row for row in self._players if row["id"] == identity or row["payload"].get("player_id") == identity), None)

    def _select_player(self):
        doc = self._player_doc(self.player_combo.currentData())
        self.confirmed.setChecked(False)
        if not doc:
            return
        payload = doc["payload"]
        self.player_name.setText(payload.get("display_name", doc["name"]))
        accounts = payload.get("accounts", [])
        self.personal_name.clear()
        self.account_id.clear()
        if len(accounts) == 1:
            account = accounts[0]
            self.personal_name.setText(account.get("nickname", ""))
            self.account_id.setText(str(account.get("account_id") or ""))
            self.region.setCurrentIndex(max(0, self.region.findData(account.get("region"))))
        elif len(accounts) > 1:
            self._notice("该选手有多个个人账号，请明确填写本次分配要关联的账号。")

    def _select_assignment(self):
        identity = self.assignment_combo.currentData()
        self.confirmed.setChecked(False)
        if not identity:
            return
        doc = self.store.get_document(identity)
        payload = doc["payload"]
        player = self._player_doc(payload.get("player_id"))
        self.player_combo.setCurrentIndex(max(0, self.player_combo.findData(player["id"] if player else None)))
        if player:
            self._select_player()
        self.event_edit.setText(payload.get("event_key", ""))
        self.personal_name.setText(payload.get("personal_nickname", ""))
        self.account_id.setText(str(payload.get("account_id") or ""))
        self.region.setCurrentIndex(max(0, self.region.findData(payload.get("region"))))
        self.notes.setPlainText(payload.get("notes", ""))
        self._edit_scope = deepcopy(payload.get("battle_ids") or [])
        self.scope.setCurrentIndex(2 if self._edit_scope else 1)
        self.details.setPlainText(self._history_text(doc))

    def save_assignment(self):
        try:
            if not self.confirmed.isChecked():
                raise ValueError("请先勾选已核对身份；候选不能直接绑定。")
            if self.scope.currentData() == "existing" and self._edit_scope is None:
                raise ValueError("新建分配没有原比赛范围，请选择仅当前比赛或整个赛事。")
            scope = (deepcopy(self._edit_scope) if self.scope.currentData() == "existing" else
                     [report_battle_id(self._report)] if self.scope.currentData() == "battle" else [])
            values = {"player_name": self.player_name.text().strip(), "tournament_name": self.tournament_edit.text(),
                      "event_key": self.event_edit.text().strip(), "personal_nickname": self.personal_name.text().strip(),
                      "region": self.region.currentData(), "account_id": self.account_id.text().strip() or None,
                      "battle_ids": scope,
                      "notes": self.notes.toPlainText()}
            self._validate_entry(values)
            if not self._report.get("sha256") or values["tournament_name"] not in {row["tournament_name"] for row in self._resolved}:
                raise ValueError("请先选择当前回放中的完整赛号。")
            player = self._player_doc(self.player_combo.currentData())
            player_document_id = player["id"] if player else str(uuid4())
            payload = deepcopy(player["payload"]) if player else {"aliases": [], "accounts": []}
            player_id = payload.get("player_id") or player_document_id
            payload["display_name"] = values["player_name"]
            accounts = [row for row in payload.get("accounts", []) if not (
                row.get("region") == values["region"] and str(row.get("account_id") or "") == str(values["account_id"] or "")
                and (values["account_id"] is not None or row.get("nickname") == values["personal_nickname"]))]
            accounts.append({"region": values["region"], "account_id": values["account_id"], "nickname": values["personal_nickname"], "confirmed": True})
            payload["accounts"] = accounts
            assignment_id = self.assignment_combo.currentData()
            assignment = {key: value for key, value in values.items() if key != "player_name"}
            assignment.update(player_id=player_id, confirmed=True, source="manual_panel_confirmation")
            result = self.store.save_documents([
                dict(kind="player", name=values["player_name"], payload=payload, document_id=player_document_id, create=player is None),
                dict(kind="assignment", name=values["tournament_name"], payload=assignment, document_id=assignment_id),
            ])
            self.refresh()
            self.assignment_combo.setCurrentIndex(max(0, self.assignment_combo.findData(result[1]["id"])))
            conflict = any(row["tournament_name"] == values["tournament_name"] and row["resolved_identity"]["status"] == "conflict" for row in self._resolved)
            self._notice("分配已保存" + ("；适用范围存在冲突，请核对。" if conflict else "；重新分析后生效。"))
            self.changed.emit()
        except Exception as error:
            self._notice("未保存：" + str(error))

    def show_history(self):
        identity = self.assignment_combo.currentData()
        if not identity:
            self._notice("请先选择已有分配记录。")
            return
        current = self.store.get_document(identity)
        history = [self.store.get_document(identity, revision) for revision in range(1, current["revision"] + 1)]
        self.details.setPlainText("\n\n".join(self._history_text(doc) for doc in history))

    @staticmethod
    def _history_text(doc):
        value = doc["payload"]
        scope = "整个赛事" if not value.get("battle_ids") else f"指定 {len(value['battle_ids'])} 场比赛"
        stamp=str(doc.get('created_at',''))[:16].replace('T',' ')
        return (f"第 {doc['revision']} 次分配 · {stamp} UTC\n"
                f"{value.get('event_key')} / {value.get('tournament_name')} → {value.get('personal_nickname')}（{value.get('region')}）\n"
                f"适用范围：{scope}\n备注：{value.get('notes') or '未填写'}")

    @staticmethod
    def _validate_entry(row):
        allowed = {"player_name", "tournament_name", "event_key", "personal_nickname", "region", "account_id", "battle_ids", "notes", "player_id"}
        if not isinstance(row, dict) or set(row) - allowed:
            raise ValueError("名单行包含未知字段或不是对象。")
        for field in ("player_name", "tournament_name", "event_key", "personal_nickname"):
            if not isinstance(row.get(field), str) or not row[field].strip() or len(row[field]) > 200:
                raise ValueError(f"{field} 必须是 1–200 字符的非空文本。")
        if row.get("region") not in ("asia", "eu", "na"):
            raise ValueError("region 必须为 asia / eu / na。")
        account = row.get("account_id")
        if account not in (None, "") and (type(account) not in (int, str) or not re.fullmatch(r"[0-9]{1,20}", str(account)) or int(account) <= 0):
            raise ValueError("account_id 必须为空或正整数。")
        if not isinstance(row.get("notes", ""), str):
            raise ValueError("notes 必须是文本。")
        battles = row.get("battle_ids", [])
        if not isinstance(battles, list) or len(battles) > 500 or any(not isinstance(item, str) or not item.strip() for item in battles):
            raise ValueError("battle_ids 必须为比赛标识字符串列表；空列表表示整个赛事。")
        if row.get("player_id") is not None:
            UUID(str(row["player_id"]))

    def choose_import(self):
        filename, _ = QFileDialog.getOpenFileName(self, "选择名单 JSON", "", "JSON (*.json)")
        if filename:
            self.preview_import(Path(filename))

    def preview_import(self, path):
        self._preview = None
        self.import_button.setEnabled(False)
        try:
            path = Path(path)
            if path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError("名单文件超过 2 MiB。")
            raw = path.read_bytes()
            data = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(data, dict) or set(data) != {"schema_version", "assignments"} or type(data["schema_version"]) is not int or data["schema_version"] != 1:
                raise ValueError("顶层必须只含 schema_version:1 和 assignments。")
            rows = data["assignments"]
            if not isinstance(rows, list) or not 1 <= len(rows) <= 500:
                raise ValueError("assignments 必须包含 1–500 行。")
            errors = []
            for index, row in enumerate(rows, 1):
                try:
                    self._validate_entry(row)
                    if row.get("player_id") and self._player_doc(row["player_id"]) is None:
                        raise ValueError("player_id 不对应现有选手。")
                    for earlier in rows[:index - 1] + [doc["payload"] for doc in self._assignments]:
                        if not isinstance(earlier, dict) or earlier.get("active") is False or earlier.get("deleted"):
                            continue
                        if (earlier.get("event_key"), earlier.get("tournament_name")) == (row["event_key"], row["tournament_name"]):
                            first, second = earlier.get("battle_ids", []), row.get("battle_ids", [])
                            if not first or not second or set(first) & set(second):
                                raise ValueError("与现有记录或名单前行范围重叠；请在身份页明确编辑。")
                except Exception as error:
                    errors.append(f"第 {index} 行：{error}")
            if errors:
                raise ValueError("\n".join(errors))
            self._preview = {"path": path, "sha256": hashlib.sha256(raw).hexdigest(), "rows": deepcopy(rows)}
            preview=[f"待导入 {len(rows)} 条身份分配"]
            for index,row in enumerate(rows,1):
                scope='整个赛事' if not row.get('battle_ids') else f"指定 {len(row['battle_ids'])} 场"
                preview.append(f"{index}. {row['event_key']} · {row['tournament_name']} → {row['player_name']}\n"
                               f"   {row['personal_nickname']} / {row['region']} · {scope}")
            self.import_preview.setPlainText('\n\n'.join(preview))
            self.import_button.setEnabled(True)
            self._notice(f"名单 {len(rows)} 行通过核验，待确认导入")
        except Exception as error:
            self.import_preview.setPlainText("名单未导入：\n" + str(error))
            self._notice("名单预览失败，未写入任何选手或分配。")

    def commit_import(self):
        preview = self._preview
        if preview is None:
            return
        try:
            if hashlib.sha256(preview["path"].read_bytes()).hexdigest() != preview["sha256"]:
                raise ValueError("名单文件在预览后发生变化，请重新预览。")
            # Revalidate against any changes made after the preview.
            self._players = self.store.list_documents("player")
            self._assignments = self.store.list_documents("assignment")
            self.preview_import(preview["path"])
            if self._preview is None:
                return
            if self._preview["sha256"] != preview["sha256"]:
                raise ValueError("名单在确认过程中发生变化，请核对新预览后重试。")
            items = []
            for raw in self._preview["rows"]:
                values = deepcopy(raw)
                player = self._player_doc(values.get("player_id")) if values.get("player_id") else None
                player_id = (player["payload"].get("player_id") or player["id"]) if player else str(uuid4())
                if not player:
                    payload = {"display_name": values["player_name"], "aliases": [], "accounts": [{
                        "nickname": values["personal_nickname"], "region": values["region"],
                        "account_id": str(values["account_id"]) if values.get("account_id") not in (None, "") else None, "confirmed": True}]}
                    items.append(dict(kind="player", name=values["player_name"], payload=payload, document_id=player_id, create=True))
                values.pop("player_name")
                values.update(player_id=player_id, confirmed=True, source="reviewed_json_import", import_sha256=preview["sha256"])
                values.setdefault("battle_ids", [])
                values.setdefault("notes", "")
                items.append(dict(kind="assignment", name=values["tournament_name"], payload=values))
            self.store.save_documents(items)
            count = len(self._preview["rows"])
            self._preview = None
            self.import_button.setEnabled(False)
            self.import_preview.appendPlainText(f"\n已导入 {count} 行。")
            self.refresh()
            self._notice(f"已导入 {count} 行身份分配；同名选手没有自动合并。")
            self.changed.emit()
        except Exception as error:
            self._notice("名单导入失败，未部分写入：" + str(error))

    def _load_settings(self):
        records = self.store.list_documents("public_stats_settings")
        doc = records[-1] if records else None
        self._settings_id = doc["id"] if doc else None
        self.network_enabled.setChecked(bool(doc and doc["payload"].get("enabled") is True))

    def save_settings(self):
        try:
            value = self.application_id.text().strip()
            if value:
                self.secrets.set("wg_application_id", value)
                self.application_id.clear()
            doc = self.store.save_document("public_stats_settings", "Wargaming public statistics", {
                "enabled": self.network_enabled.isChecked(), "credential_name": "wg_application_id",
            }, self._settings_id)
            self._settings_id = doc["id"]
            self._notice("查询设置已保存")
            self.changed.emit()
        except Exception:
            self._notice("设置未保存：本机加密存储不可用。")

    def start_query(self, operation):
        if self.busy:
            self._notice("已有公开查询正在运行，请先等待或取消。")
            return
        try:
            if self.application_id.text().strip():
                self._notice("请先点击保存设置，将 Application ID 加密保存后再查询。")
                return
            params = {"region": self.query_region.currentData()}
            if operation == "search":
                params["nickname"] = self.search_name.text().strip()
            else:
                params.update(account_id=self.query_account.text().strip(), mode=self.mode.currentData())
            if operation == "ships":
                name = self.tournament_edit.text()
                ship = next((row.get("shipParamsId") for row in self._report.get("roster", []) if row.get("name") == name), None)
                if ship is None:
                    self._notice("请先选择当前回放中具有舰船参数 ID 的选手。")
                    return
                params["ship_ids"] = [ship]
            enabled = self.network_enabled.isChecked()
            application_id = self.secrets.get("wg_application_id") if enabled else None
            self._worker = _PublicWorker(operation, params, application_id, enabled, self)
            self._worker.result.connect(self._query_result)
            self._worker.finished.connect(self._query_finished)
            for button in (self.search_button, self.account_button, self.ship_button, self.settings_button):
                button.setEnabled(False)
            self.cancel_button.setEnabled(True)
            self._notice("正在查询公开资料…")
            self._worker.start()
        except Exception:
            self._notice("查询未开始：无法读取本机加密凭据。")

    def _query_result(self, result):
        if self.sender() is not None and self.sender() is not self._worker:
            return
        try:
            if result.get("kind") == "search":
                candidates = (result.get("data") or {}).get("candidates", [])
                self.candidate_table.setRowCount(len(candidates))
                for index, row in enumerate(candidates):
                    _row(self.candidate_table, index, [row["nickname"], row["region"], row["account_id"], "候选·未确认"], row)
            else:
                self.store.save_run("public_stats", f"{result.get('region')} / {result.get('account_id')} / {result.get('kind')}", result)
                self.refresh_snapshots()
                self.changed.emit()
            self.snapshot_details.setPlainText(self._snapshot_text(result))
            self._notice("公开查询：" + STATUS_LABELS.get(result.get("status"), "未知") + ("（已取消）" if result.get("reason") == "cancelled" else ""))
        except Exception:
            self._notice("查询结束，但快照保存失败；未更改身份分配。")

    def _query_finished(self):
        if self.sender() is not None and self.sender() is not self._worker:
            return
        for button in (self.search_button, self.account_button, self.ship_button, self.settings_button):
            button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.idle.emit()

    def select_search_candidate(self):
        rows = self.candidate_table.selectionModel().selectedRows()
        if not rows:
            return
        item = self.candidate_table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        self.personal_name.setText(item["nickname"])
        self.account_id.setText(item["account_id"])
        self.region.setCurrentIndex(max(0, self.region.findData(item["region"])))
        self.query_account.setText(item["account_id"])
        self.confirmed.setChecked(False)
        self._notice("候选已填入，身份关联待确认")

    def refresh_snapshots(self):
        self._snapshots = self.store.list_runs("public_stats")
        self.snapshot_table.setRowCount(len(self._snapshots))
        for index, run in enumerate(self._snapshots):
            value = run["payload"]
            stamp = value.get("fetched_at_utc")
            display_time = stamp[:19].replace("T", " ") if isinstance(stamp, str) else "未请求"
            kind_label = {"account": "账号", "ships": "同船"}.get(value.get("kind"), "公开资料")
            _row(self.snapshot_table, index, [f"{kind_label} / {MODE_LABELS.get(value.get('mode'), '—')}",
                f"{value.get('region')} / {value.get('account_id') or '—'}", STATUS_LABELS.get(value.get("status"), "未知"),
                display_time, "公开战绩" if (value.get("source") or {}).get("url") else "未请求"], run["id"])

    def show_snapshot(self):
        rows = self.snapshot_table.selectionModel().selectedRows()
        if rows:
            self.snapshot_details.setPlainText(self._snapshot_text(self._snapshots[rows[0].row()]["payload"]))

    @staticmethod
    def _snapshot_text(value):
        lines = [f"状态：{STATUS_LABELS.get(value.get('status'), '未知')} · 账号：{value.get('region')} / {value.get('account_id') or '未指定'}",
                 f"采集时间 UTC：{str(value.get('fetched_at_utc') or '未请求')[:19].replace('T',' ')}"]
        data = value.get("data") or {}
        rows = data.get("ships", []) if value.get("kind") == "ships" else [data] if data.get("statistics") else []
        for row in rows:
            stats = row.get("statistics") or {}
            prefix = f"舰船 ID {row['ship_id']} · " if row.get("ship_id") else ""
            fields = " / ".join(f"{label}：{stats.get(key) if stats.get(key) is not None else '未知'}"
                                for label, key in (("场次", "battles"), ("胜场", "wins"), ("累计伤害", "damage_dealt")))
            lines.append(prefix + fields)
        if not rows:
            lines.append("暂无可用战绩统计")
        return "\n".join(lines)
