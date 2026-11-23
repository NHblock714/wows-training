"""Open embedded minimap thumbnails inside a report without external navigation."""
import base64
import re

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QDialog, QLabel, QScrollArea, QVBoxLayout


def attach_report_image_preview(browser, get_payload):
    def open_image(url):
        if url.scheme() != 'report-frame':
            return
        link = re.fullmatch(r'(verdict|decisions|dimensions)/(\d{1,2})/(\d)', url.path())
        if not link:
            return
        from ..report_visuals import read_report_visuals
        key, row, index = link.groups()
        images = read_report_visuals(get_payload() or {}).get((key, int(row)), [])
        if int(index) >= len(images):
            return
        image = images[int(index)]
        pixmap = QPixmap()
        if not pixmap.loadFromData(base64.b64decode(image['png_base64'], validate=True), 'PNG'):
            return
        dialog = QDialog(browser)
        dialog.setWindowTitle(image['caption'])
        layout = QVBoxLayout(dialog)
        surface = QLabel()
        surface.setPixmap(pixmap)
        scroll = QScrollArea()
        scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        scroll.setWidget(surface)
        layout.addWidget(scroll)
        dialog.resize(min(pixmap.width() + 32, 900), min(pixmap.height() + 46, 750))
        dialog.exec()

    browser.anchorClicked.connect(open_image)
