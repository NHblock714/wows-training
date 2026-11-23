"""Desktop controls and the scoped original tactical replay palette."""
from string import Template

from . import academy_palette as palette

REPLAY_BASE_STYLE = """
QWidget { background: transparent; color: #E4E6E6; font-family: 'Microsoft YaHei UI'; font-size: 13px; }
QMainWindow { background: #101725; }
QStackedWidget, QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }
QDialog, QMessageBox, QMenu { background: #182331; }
QMenu { border: 1px solid #586675; padding: 4px; }
QMenu::item { padding: 7px 20px; }
QMenu::item:selected { background: #365664; color: #F3E9CB; }
QLabel { background: transparent; }
QFrame#sidebar { background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #101827,stop:0.5 #17202E,stop:1 #101A25); border-right: 1px solid #53616D; }
QFrame#sidebar QLabel { background: transparent; }
QFrame#card { background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #263443,stop:1 #172332); border: 1px solid #3E5363; border-top: 2px solid #8F8060; border-radius: 3px; }
QFrame#card QLabel { background: transparent; }
QFrame#card[instrument="true"] { background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #263343,stop:1 #182532); border: 1px solid #43525F; border-left: 3px solid #C2A96F; border-radius: 2px; }
QLabel#title { font-size: 24px; font-weight: 600; color: #EEE3C7; border-bottom: 1px solid #5A6268; padding: 4px 0 9px; }
QLabel#sectionTitle { font-size: 18px; font-weight: 600; color: #E2CDA3; }
QLabel#subtitle { color: #A0B1BD; }
QLabel#navSection { color: #899DAA; font-size: 10px; padding: 6px 10px 3px; border-bottom: 1px solid #293A49; }
QLabel#metric { font-family: 'Bahnschrift','Segoe UI'; font-size: 29px; font-weight: 600; color: #E4C684; }
QLabel#notice { padding: 7px 10px; background: rgba(29,46,59,225); border-left: 2px solid #70A9B8; color: #CFDEE0; }
QFrame#disclosure { background: transparent; border: none; }
QLabel#helpText { background: #1B2B39; color: #A4B9C4; padding: 9px; border-left: 1px solid #53798C; }
QToolButton { color: #B0C0C9; background: transparent; border: none; padding: 5px 6px; text-align: left; }
QToolButton:hover { color: #F0D69D; background: #293B47; }
QToolButton:checked { color: #E4C48B; }
QPushButton { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #34495A,stop:1 #223441); border: 1px solid #536B7B; border-bottom: 2px solid #1A2935; border-radius: 2px; padding: 8px 13px; color: #E4EBEC; }
QPushButton:hover { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #425F6E,stop:1 #2B4352); border-color: #93B2BC; color: #FFFFFF; }
QPushButton:pressed { background: #142A39; border-color: #9BB8BE; }
QPushButton:disabled { color: #697F8A; background: #172430; border-color: #30414F; }
QPushButton#primary { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #99804C,stop:0.5 #796238,stop:1 #5B472B); border-color: #D3BA7E; border-bottom: 2px solid #48371F; color: #FFF2D2; font-weight: 600; }
QPushButton#primary:hover { background: #9B824C; border-color: #F0D99D; }
QPushButton#primary:disabled { background: #373529; border-color: #5E5740; color: #9B947D; }
QPushButton#nav { text-align: left; border: 1px solid transparent; background: transparent; padding: 8px 10px; font-size: 13px; }
QPushButton#nav:hover { background: #253544; border-color: #334B59; }
QPushButton#nav:checked { background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #4A4433,stop:0.7 #2C3439,stop:1 #1A2936); color: #F3DDA9; border: 1px solid #74684C; border-left: 3px solid #D7B976; border-radius: 0px; }
QLineEdit, QComboBox, QTextEdit, QPlainTextEdit, QTextBrowser, QSpinBox, QDoubleSpinBox { background: #111F2C; border: 1px solid #405B6C; border-bottom: 1px solid #547589; border-radius: 2px; padding: 6px; selection-background-color: #556F79; color: #E3EBEE; }
QLineEdit:focus, QComboBox:focus, QTextEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus { border-color: #BCA46D; background: #172938; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled { color: #6E8390; background: #16232E; border-color: #2E4350; }
QComboBox QAbstractItemView { background: #1C2B39; selection-background-color: #425E6E; selection-color: #F7E3B5; border: 1px solid #688393; }
QCheckBox { spacing: 7px; }
QCheckBox::indicator { width: 14px; height: 14px; border: 1px solid #6A8291; background: #112331; border-radius: 2px; }
QCheckBox::indicator:checked { background: #C7AA6A; border: 2px solid #E3D1A1; }
QCheckBox::indicator:disabled { background: #24323B; border-color: #3D4C56; }
QRadioButton { spacing: 7px; }
QTableWidget, QListWidget { background: #122230; alternate-background-color: #1B2C3B; border: 1px solid #496174; border-radius: 2px; gridline-color: #344C5D; selection-background-color: #3C5968; selection-color: #FFF0C8; }
QTableWidget::item { padding: 5px 8px; border-bottom: 1px solid #263E4E; }
QTableWidget::item:selected { background: #3B5664; color: #F9EAC7; border-bottom-color: #9C946E; }
QTableWidget::item:hover, QListWidget::item:hover { background: #2A4353; }
QListWidget::item { padding: 3px 6px; border-bottom: 1px solid #263B4A; }
QListWidget::item:selected { background: #394A4D; color: #F7E4B8; border-left: 3px solid #CDB173; }
QHeaderView::section { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #354D60,stop:1 #263C4D); color: #CCDDE2; border: none; border-right: 1px solid #425E70; border-bottom: 2px solid #8D805D; padding: 8px; font-weight: 600; }
QTableCornerButton::section { background: #2A4152; border: 1px solid #425E70; }
QTabWidget::pane { border: none; border-top: 1px solid #425868; padding-top: 4px; }
QTabBar::tab { background: #172937; padding: 8px 15px; margin-right: 3px; color: #9FB4C0; border: 1px solid #2D4555; border-bottom: 2px solid #2D4555; }
QTabBar::tab:hover { color: #EEE2C1; background: #283E4A; }
QTabBar::tab:selected { background: #33434A; color: #F0D399; border-color: #6D6D58; border-bottom: 2px solid #D2B473; }
QGroupBox { border: 1px solid #455D70; border-top-color: #8A7A57; border-radius: 2px; margin-top: 12px; padding-top: 10px; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; color: #D8C392; }
QProgressBar { border: 1px solid #3F596B; border-radius: 0px; background: #132534; text-align: center; color: #E6E7D7; }
QProgressBar::chunk { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #D2B77A,stop:1 #8D794D); border-radius: 0px; }
QSlider::groove:horizontal { background: #385567; height: 4px; }
QSlider::sub-page:horizontal { background: #B9A16E; }
QSlider::handle:horizontal { background: #DAC38A; border: 1px solid #F2DFAD; width: 12px; margin: -5px 0; border-radius: 1px; }
QSplitter::handle { background: #223B4B; }
QScrollBar:vertical { background: #142330; width: 9px; }
QScrollBar::handle:vertical { background: #526E7E; border-radius: 2px; min-height: 24px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
QScrollBar:horizontal { background: #142330; height: 9px; }
QScrollBar::handle:horizontal { background: #526E7E; border-radius: 2px; min-width: 24px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0px; }
QStatusBar { color: #9DB0BB; background: #0D1B28; border-top: 1px solid #344A59; }
QToolTip { background: #233D4B; color: #F0E9D5; border: 1px solid #B3A06F; padding: 6px; }
"""


BASE_STYLE = Template("""
QWidget { background:transparent; color:$INK; font-family:'Microsoft YaHei UI'; font-size:13px; }
QMainWindow { background:$CANVAS; }
QStackedWidget, QScrollArea, QScrollArea > QWidget > QWidget { background:transparent; }
QDialog, QMessageBox { background:$CANVAS; color:$INK; }
QToolTip { background:$NIGHT; color:$PAPER; border:1px solid #5284AF; padding:6px 9px; }
QLabel { background:transparent; }
QFrame#sidebar { background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #10192B,stop:0.55 $NIGHT,stop:1 #182B45); border-right:1px solid #354D6C; }
QFrame#sidebar QLabel { color:#D9E9FA; }
QFrame#sidebar QLabel#subtitle, QFrame#sidebar QLabel#navSection { color:#B2C9DE; }
QFrame#card { background:$PAPER; border:1px solid #BECEDD; border-top:2px solid #8EB9DD; border-radius:3px; }
QFrame#card[instrument="true"] { background:$PAPER; border:1px solid #B4C8DB; border-left:3px solid $BLUE; }
QFrame#card QLabel { background:transparent; }
QLabel#title { color:$INK; font-size:24px; font-weight:700; border-bottom:1px solid $LINE; padding:4px 0 9px; }
QLabel#sectionTitle { color:#194575; font-size:18px; font-weight:650; }
QLabel#subtitle { color:$MUTED; }
QLabel#metric { font-family:'Bahnschrift','Segoe UI'; font-size:29px; font-weight:650; color:#124F97; }
QLabel#notice { color:#25466B; background:#E1EEFC; border-left:3px solid $BLUE; padding:7px 10px; }
QLabel#helpText { color:$MUTED; background:#E5EFF7; border-left:2px solid #6895BD; padding:8px; }
QFrame#disclosure { border:0; background:transparent; }
QMenu { background:$PAPER; color:$INK; border:1px solid #8EAAC6; padding:5px 2px; }
QMenu::item { padding:7px 22px; }
QMenu::item:selected { background:#DCEBFB; color:#123F80; }
QMenu::item:disabled { color:#758699; }
QMenu::separator { margin:4px 8px; height:1px; background:$LINE; }
QToolButton { background:transparent; color:#2E526E; border:1px solid transparent; border-radius:2px; padding:4px 5px; text-align:left; }
QToolButton:hover { background:#D1E5F7; color:#094783; }
QToolButton:checked { color:#134F89; }
QToolButton:focus, QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus, QTextEdit:focus, QTextBrowser:focus,
QSpinBox:focus, QDoubleSpinBox:focus, QTableView:focus, QListView:focus, QTreeView:focus { border:1px solid #AB2876; }
QFrame#sidebar QToolButton { color:#C5D7E9; }
QFrame#sidebar QToolButton:hover { background:#273C56; color:$PAPER; }
QFrame#sidebar QToolButton:checked { color:#76D9F1; }
QToolButton#navGroup { font-weight:600; padding:3px 4px; }
QFrame#sidebar QToolButton#navGroup { background:transparent; border:0; color:#E8F4FF; }
QFrame#sidebar QToolButton#navGroup:hover { background:transparent; color:#A5EAFF; }
QFrame#sidebar QToolButton#navGroup:focus { background:transparent; color:#A5EAFF; }
QPushButton { background:rgba(212,230,243,155); color:$INK; border:1px solid #91A8BF; border-radius:2px; padding:8px 13px; }
QPushButton:hover { background:rgba(188,221,243,175); color:#123C73; border-color:#5584B7; }
QPushButton:pressed { background:rgba(165,205,233,165); color:#102A46; }
QPushButton:checked { background:#174574; color:$PAPER; }
QPushButton:focus { border-color:#AF286F; }
QPushButton:disabled { background:rgba(213,223,233,125); color:#506176; border-color:#B9C7D6; }
QPushButton#primary, QPushButton#radarSave { background:#1657AB; color:$PAPER; border-color:#134882; font-weight:650; }
QPushButton#primary:hover, QPushButton#radarSave:hover { background:#104A96; }
QPushButton#primary:pressed, QPushButton#radarSave:pressed { background:#123766; }
QPushButton#primary:disabled, QPushButton#radarSave:disabled { background:#D2DCE7; color:#536578; border-color:$LINE; }
QPushButton#homeImport { background:rgba(173,212,243,174); color:#153D65; border-color:#79A9D2; font-weight:650; }
QPushButton#homeImport:hover { background:rgba(191,228,251,200); border-color:#5894C7; }
QPushButton#homeImport:pressed { background:rgba(156,202,238,192); }
QPushButton#homeImport:disabled { background:rgba(213,223,233,125); color:#506176; border-color:#B9C7D6; }
QPushButton#nav { background:#172A44; color:#DCEBF8; border:1px solid transparent; padding:7px 10px; text-align:left; }
QPushButton#nav:hover { background:#294763; color:$PAPER; }
QPushButton#nav:checked { background:$BLUE; border-color:#609DE3; color:$PAPER; }
QPushButton#nav:focus, QPushButton#primary:focus, QPushButton#homeImport:focus { border-color:$PINK; }
QLineEdit, QComboBox, QPlainTextEdit, QTextEdit, QTextBrowser, QSpinBox, QDoubleSpinBox {
    color:$INK; background:$PAPER; border:1px solid #9CB2C9; border-radius:2px; padding:6px;
    selection-background-color:#1F5EAE; selection-color:$PAPER; placeholder-text-color:#637991;
}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover { border-color:#668CB1; }
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus, QTextEdit:focus, QTextBrowser:focus,
QSpinBox:focus, QDoubleSpinBox:focus { border-color:#AB2876; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled { background:#E6EEF5; color:#586F83; border-color:#C1D0DF; }
QFrame#sidebar QLineEdit { background:#20344D; border:1px solid #47617C; color:#EBF6FF; placeholder-text-color:#AEC5D9; }
QFrame#sidebar QLineEdit:focus { border-color:$CYAN; }
QFrame#sidebar QLineEdit#featureSearch { background:rgba(16,42,68,220); border:1px solid #678CA9; border-radius:4px; }
QFrame#sidebar QLineEdit#featureSearch:focus { border-color:$CYAN; }
QComboBox QAbstractItemView { background:$PAPER; color:$INK; border:1px solid #9DB1C7; padding:2px; selection-background-color:#C8E1FA; selection-color:#143C66; }
QCheckBox, QRadioButton { spacing:7px; }
QCheckBox::indicator { width:14px; height:14px; background:$PAPER; border:1px solid #7791AA; border-radius:2px; }
QCheckBox::indicator:checked { background:#155AB1; border:1px solid #1252A4; }
QCheckBox::indicator:indeterminate { background:#859AB1; border:1px solid #587589; }
QCheckBox::indicator:disabled { background:#E0E8F0; border-color:#B4C2D0; }
QCheckBox::indicator:checked:disabled { background:#687E92; border-color:#788CA0; }
QCheckBox::indicator:focus, QRadioButton::indicator:focus { border-color:#AB2876; }
QCheckBox:disabled, QRadioButton:disabled { color:#64768A; }
QTableView, QListView, QTreeView {
    color:$INK; background:$PAPER; alternate-background-color:#F1F6FB; border:1px solid #AABECD; border-radius:2px;
    gridline-color:#D4E0EC; selection-background-color:#D6EAFE; selection-color:#0B365C;
}
QTableWidget#libraryTable { background:rgba(255,255,255,220); alternate-background-color:rgba(236,244,251,230); }
QTableView::item { padding:5px 8px; border-bottom:1px solid #DAE4EE; }
QTableView::item:hover, QListView::item:hover, QTreeView::item:hover { background:#EAF4FD; }
QTableView::item:selected { background:#D4E8FC; color:#103A62; border-bottom:1px solid #86B3DD; }
QListView::item, QTreeView::item { padding:3px 6px; border-left:2px solid transparent; }
QListView::item:selected, QTreeView::item:selected { background:#D8EBFE; color:#10395F; border-left:2px solid $BLUE; }
QHeaderView { background:#E3EDF7; }
QHeaderView::section {
    color:#203E5E; background:qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #F5F9FD,stop:1 #DDEBF7);
    border:0; border-right:1px solid #C5D8E9; border-bottom:2px solid #95B7D4; padding:8px; font-weight:600;
}
QHeaderView::section:hover { background:#E1F0FC; }
QTableCornerButton::section { background:#E3EDF7; border:1px solid #C5D8E9; }
QTabWidget::pane { background:transparent; border:0; border-top:1px solid #9BB5CC; padding-top:4px; }
QTabBar::tab { color:#294C70; background:$ICE; border:1px solid #B4C7D9; padding:7px 14px; margin-right:2px; }
QTabBar::tab:hover { background:#CDE3F8; color:#124373; }
QTabBar::tab:selected { background:#D7E9F8; color:#164677; border:1px solid #9DBFDB; border-bottom:3px solid #3483C8; font-weight:600; }
QGroupBox { background:transparent; color:$INK; border:1px solid $LINE; border-radius:2px; margin-top:12px; padding-top:10px; }
QGroupBox::title { subcontrol-origin:margin; left:12px; padding:0 6px; color:#184F81; }
QProgressBar { background:#E7EFF7; color:#173B5C; border:1px solid #A9C2D8; border-radius:2px; text-align:center; }
QProgressBar::chunk { background:#9DC6E9; }
QFrame#aiGenerationProgress { background:rgba(250,253,255,230); border:1px solid #BDD3E9; border-left:3px solid #4185D2; border-radius:5px; }
QLabel#aiGenerationStage { color:#214F80; font-weight:650; }
QProgressBar#aiGenerationTrack { border-image:none; background:#D9E7F6; border:0; border-radius:3px; }
QProgressBar#aiGenerationTrack::chunk { border-image:none; border-radius:3px; background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #226BC3,stop:.82 #57A4E5,stop:1 #BA81BE); }
QSlider::groove:horizontal { background:#A3B6CC; height:5px; }
QSlider::sub-page:horizontal { background:$BLUE; }
QSlider::handle:horizontal { width:12px; margin:-5px 0; background:#24588B; border:1px solid $PAPER; border-radius:2px; }
QProgressBar#selectedShipHealthBar { background:#10181B; border:0; }
QProgressBar#selectedShipHealthBar::chunk { background:#73CFA4; }
QSplitter::handle { background:#A5BFD4; }
QScrollBar { background:#E0EAF3; border:0; }
QScrollBar:vertical { width:9px; }
QScrollBar:horizontal { height:9px; }
QScrollBar::handle { background:#849BB2; border-radius:3px; }
QScrollBar::handle:vertical { min-height:24px; }
QScrollBar::handle:horizontal { min-width:24px; }
QScrollBar::handle:hover { background:#557898; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width:0; }
QFrame#sidebar QScrollBar { background:#152338; }
QFrame#sidebar QScrollBar::handle { background:#456078; }
QStatusBar { background:#1F2023; color:#F3F4F6; border-top:1px solid #41434A; }
""").substitute(vars(palette))


def build_style(asset_root=None):
    from .native_ui_assets import NativeUiAssets,default_native_ui_assets
    assets=NativeUiAssets(asset_root) if asset_root is not None else default_native_ui_assets()
    return BASE_STYLE+'\n'+assets.stylesheet()


REPLAY_CHROME_STYLE = """
QWidget#replayPanel { background:transparent; color:#12263D; }
QWidget#replayPanel QLabel { color:#31516D; background:transparent; }
QLabel#replayStatusLabel { color:#174B80; font-weight:600; }
QLabel#replayScoresLabel { color:#174B80; font-weight:600; }
QLabel#selectedShipTitle { color:#153F68; font-weight:600; }
QLabel#replayClockLabel, QLabel#replayBattleTimeLabel { color:#26567E; }
QLabel#replaySelectedLabel { color:#254863; }
QWidget#replayMap { font-family:'Microsoft YaHei UI'; font-size:13px; }
QListWidget#replayEvents {
    color:#12263D; background:rgba(247,251,255,225); border:1px solid #B2C8DC;
    border-top:2px solid #8FBCE0; border-radius:3px;
    selection-background-color:#DDEDFB; selection-color:#143E66;
}
QListWidget#replayEvents::item {
    border:0; border-bottom:1px solid #DCE6EF; border-left:2px solid transparent;
    padding:8px 6px;
}
QListWidget#replayEvents::item:hover { background:#EBF5FD; }
QListWidget#replayEvents::item:selected {
    background:#DDEDFB; color:#143E66; border-left:2px solid #ED69A9;
}
QWidget#replayLayers, QWidget#replayLayerViewport, QScrollArea#replayLayerScroll { background:transparent; }
QWidget#combatLegend { background:#F1F7FE; border:1px solid #AEC7DF; border-radius:5px; }
QWidget#selectedShipCard { background:transparent; border:0; }
QProgressBar#selectedShipHealthBar { background:#DCE8EF; border-image:none; border:0; }
QProgressBar#selectedShipHealthBar::chunk { border-image:none; background:#73CFA4; }
QTabWidget#replayDetailTabs::pane { border:0; background:transparent; padding-top:4px; }
QSplitter#replayContentSplitter::handle { background:#C6D8E7; }
QWidget#replayPanel QCheckBox { color:#31516D; }
QWidget#replayPanel QCheckBox:disabled { color:#74879A; }
QWidget#replayPanel QComboBox {
    color:#173E62; background:rgba(219,235,247,190); border:1px solid #94B5CF;
    border-radius:2px; padding:6px;
}
QWidget#replayPanel QComboBox:disabled { color:#74879A; background:#E9F0F6; border-color:#C8D6E3; }
QWidget#replayPanel QComboBox QAbstractItemView {
    color:#173E62; background:#F7FBFF; border:1px solid #95B4CD;
    selection-background-color:#D6EAF9; selection-color:#143E66;
}
QWidget#replayPanel QScrollBar { background:transparent; }
QWidget#replayPanel QScrollBar::handle { background:#8FAABE; }
QWidget#replayPanel QScrollBar::handle:hover { background:#5A84A6; }
"""


def apply_native_controls(widget):
    """Modernise the replay chrome while its map owns the tactical palette."""
    from .native_ui_assets import default_native_ui_assets
    # The main window supplies the common typography and client-size preference.
    # Only material/state assets and local surfaces are scoped here; the loaded
    # tactical canvas is still painted entirely by its original renderer.
    widget.setStyleSheet(default_native_ui_assets().stylesheet()+'\n'+REPLAY_CHROME_STYLE)


STYLE=build_style()
