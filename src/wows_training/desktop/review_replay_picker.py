"""Select an imported battle for the single-battle analysis page."""

from datetime import datetime

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHeaderView, QLabel,
    QLineEdit, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from .fleet_shell import FleetPageHeader
from .ui_components import align_cell, style_table


def _display_date(raw):
    try:
        return datetime.strptime(raw, '%d.%m.%Y %H:%M:%S').strftime('%Y-%m-%d %H:%M')
    except (ValueError, TypeError):
        return str(raw or '')


class ReviewReplayPicker(QDialog):
    def __init__(self, host, current_battle_id=None):
        super().__init__(host)
        self.setObjectName('reviewReplayPicker')
        self.setWindowTitle('选择录像')
        self.resize(780, 450)
        self.setMinimumWidth(600)
        self.battle_id = None
        self._library = host.library

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        layout.addWidget(FleetPageHeader('选择录像', 'replays'))
        self.search = QLineEdit()
        self.search.setObjectName('reviewReplaySearch')
        self.search.setAccessibleName('搜索录像库')
        self.search.setPlaceholderText('搜索日期、地图、玩家、赛事或文件名')
        self.search.setClearButtonEnabled(True)
        layout.addWidget(self.search)

        self.table = QTableWidget(0, 5)
        self.table.setObjectName('reviewReplayTable')
        self.table.setAccessibleName('录像库比赛')
        self.table.setHorizontalHeaderLabels(('日期 / 时间', '地图', '模式', '录制账号', '录制数'))
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().hide()
        style_table(self.table)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column, width in ((0, 143), (2, 97), (3, 150), (4, 72)):
            self.table.setColumnWidth(column, width)
        self.table.setMinimumHeight(140)
        layout.addWidget(self.table, 1)

        self.results = QLabel()
        self.results.setObjectName('subtitle')
        self.results.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.results)

        buttons = QDialogButtonBox()
        self.open_button = buttons.addButton('打开分析', QDialogButtonBox.ButtonRole.AcceptRole)
        self.open_button.setObjectName('primary')
        self.open_button.setDefault(True)
        buttons.addButton('取消', QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        for widget in (self.search, self.table, self.results, *buttons.buttons()):
            widget.setProperty('clientTextRole', 'body')

        self._terms = []
        rows = self._library.list_battles()
        rows.sort(key=lambda row: _display_date(row['date_raw']), reverse=True)
        self.table.setRowCount(len(rows))
        selected_row = 0
        map_names = host.map_names
        for row_index, row in enumerate(rows):
            battle_id = row['id']
            if battle_id == current_battle_id:
                selected_row = row_index
            date = _display_date(row['date_raw'])
            map_name = map_names.get(row['map_id']) or row.get('map_label') or row['map_id']
            values = (date, map_name, row.get('battle_mode'), row.get('recorder'), row.get('replay_count'))
            searchable = (*values, row['date_raw'], row['map_id'], row.get('event_key'),
                          row.get('label'), row.get('notes'), row.get('source_name'))
            terms = ' '.join(str(value) for value in searchable if value is not None).casefold()
            self._terms.append((battle_id, terms))
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value) if value is not None and value != '' else '—')
                item.setData(Qt.ItemDataRole.UserRole, battle_id)
                item.setToolTip(item.text())
                if column == 3 and row.get('source_name'):
                    item.setToolTip(f"{item.text()}\n{row['source_name']}")
                align_cell(item, value)
                self.table.setItem(row_index, column, item)

        self.table.itemSelectionChanged.connect(self._update_open)
        self.table.cellDoubleClicked.connect(lambda *_: self.accept())
        self.search.textChanged.connect(self._filter)
        self.search.returnPressed.connect(self.accept)
        if rows:
            self.table.selectRow(selected_row)
        self._filter('')
        self.search.setFocus()

    def _selected_row(self):
        selected = self.table.selectionModel().selectedRows()
        if selected and not self.table.isRowHidden(selected[0].row()):
            return selected[0].row()
        return None

    def _update_open(self):
        self.open_button.setEnabled(self._selected_row() is not None)

    def _filter(self, value):
        prior = self._selected_row()
        tokens = set(value.casefold().split())
        # Match the same participants and recording filenames as the library,
        # including files attached to secondary viewpoints.
        library_matches = {
            token: {row['id'] for row in self._library.list_battles(search=token)}
            for token in tokens if any(token not in terms for _, terms in self._terms)
        }
        matched = []
        with QSignalBlocker(self.table):
            for row_index, (battle_id, terms) in enumerate(self._terms):
                matches = all(token in terms or battle_id in library_matches[token] for token in tokens)
                self.table.setRowHidden(row_index, not matches)
                if matches:
                    matched.append(row_index)
            if matched:
                self.table.selectRow(prior if prior in matched else matched[0])
            else:
                self.table.clearSelection()
        if not self._terms:
            self.results.setText('录像库为空')
        elif not matched:
            self.results.setText('未找到符合条件的录像')
        else:
            self.results.setText(f'{len(matched)} 场对局')
        self._update_open()

    def accept(self):
        row = self._selected_row()
        if row is None:
            return
        self.battle_id = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        super().accept()
