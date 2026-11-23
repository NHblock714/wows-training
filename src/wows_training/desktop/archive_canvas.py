"""Archive-paper ornament limited to genuinely unused view space."""
import math

from PySide6.QtCore import QEvent,QObject,QRect,Qt,QTimer
from PySide6.QtGui import QColor,QLinearGradient,QPainter,QPainterPath,QPalette,QPen
from PySide6.QtWidgets import QListView,QListWidget,QTableWidget,QTextBrowser,QWidget
from shiboken6 import isValid

from . import academy_palette as colors
from .motion import motion_enabled,subscribe_motion


class _ArchiveCanvas(QWidget):
    def __init__(self,view):
        super().__init__(view.viewport())
        self.view=view
        self.phase=0.
        self._queued=False
        self._dark=False
        self.setObjectName('archiveBlankCanvas')
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setStyleSheet('background:transparent;border:none;')
        view.installEventFilter(self)
        view.viewport().installEventFilter(self)
        for scrollbar in (view.verticalScrollBar(),view.horizontalScrollBar()):
            scrollbar.valueChanged.connect(self.schedule)
            scrollbar.rangeChanged.connect(self.schedule)
        if isinstance(view,QTextBrowser):
            view.document().documentLayout().documentSizeChanged.connect(self.schedule)
            view.textChanged.connect(self.schedule)
        else:
            model=view.model()
            for signal in (model.rowsInserted,model.rowsRemoved,model.modelReset,model.layoutChanged,model.dataChanged):
                signal.connect(self.schedule)
            view.selectionModel().selectionChanged.connect(self.schedule)
            if isinstance(view,QTableWidget):
                view.verticalHeader().sectionResized.connect(self.schedule)
                view.verticalHeader().sectionMoved.connect(self.schedule)
        self.schedule()

    def schedule(self,*_):
        if not self._queued:
            self._queued=True
            QTimer.singleShot(0,self.refresh)

    def _data_bottom(self):
        view=self.view
        if isinstance(view,QTextBrowser):
            height=view.document().documentLayout().documentSize().height()
            return math.ceil(height-view.verticalScrollBar().value())+12
        if isinstance(view,QTableWidget):
            header=view.verticalHeader()
            for visual in range(view.rowCount()-1,-1,-1):
                row=header.logicalIndex(visual)
                if row>=0 and not view.isRowHidden(row):
                    return view.rowViewportPosition(row)+view.rowHeight(row)+2
            return 0
        # The project lists are vertical text/image rows. Other layouts keep their own canvas.
        if view.flow()!=QListView.Flow.TopToBottom or view.isWrapping():
            return view.viewport().height()
        for row in range(view.count()-1,-1,-1):
            item=view.item(row)
            if not item.isHidden():
                rect=view.visualItemRect(item)
                return rect.bottom()+3 if rect.isValid() else view.viewport().height()
        return 0

    def refresh(self):
        self._queued=False
        if not isValid(self) or not isValid(self.view):return
        viewport=self.view.viewport()
        start=max(0,self._data_bottom())
        remaining=viewport.height()-start
        wanted=self.view.isVisible() and remaining>=90 and viewport.width()>=170
        self.setGeometry(QRect(0,start,viewport.width(),max(0,remaining)))
        base=self.view.palette().color(QPalette.ColorRole.Base)
        self._dark=base.lightnessF()<.45
        self.setVisible(wanted)
        if wanted:self.raise_();self.update()

    def eventFilter(self,watched,event):
        if event.type() in (QEvent.Type.Show,QEvent.Type.Resize,QEvent.Type.LayoutRequest,QEvent.Type.StyleChange):
            self.schedule()
        elif watched is self.view.viewport() and event.type()==QEvent.Type.Paint:
            # Qt does not emit model signals when callers hide list/table rows.
            # Only reconcile a changed boundary, so normal paints cannot create a repaint loop.
            start=max(0,self._data_bottom())
            remaining=watched.height()-start
            wanted=self.view.isVisible() and remaining>=90 and watched.width()>=170
            if self.y()!=start or self.height()!=max(0,remaining) or self.isVisible()!=wanted:
                self.refresh()
        elif watched is self.view and event.type() in (QEvent.Type.Hide,QEvent.Type.Close):
            self.hide()
        return False

    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w,h=self.width(),self.height()
        ink=QColor('#B9D3EA' if self._dark else '#203D5C')
        accent=QColor(colors.PINK)
        # A partly open folder and two offset page leaves fill the free area without words.
        folder_w=min(560.,w*.76);folder_h=min(310.,h*.73)
        x=w-folder_w-18;y=max(22.,h-folder_h-18)
        outline=QPainterPath();outline.moveTo(x,y+23);outline.lineTo(x+folder_w*.22,y+23)
        outline.lineTo(x+folder_w*.29,y);outline.lineTo(x+folder_w*.58,y)
        outline.lineTo(x+folder_w*.65,y+23);outline.lineTo(x+folder_w-17,y+23)
        outline.lineTo(x+folder_w,y+40);outline.lineTo(x+folder_w,y+folder_h)
        outline.lineTo(x+18,y+folder_h);outline.lineTo(x,y+folder_h-18);outline.closeSubpath()
        shade=QLinearGradient(x,y,x+folder_w,y+folder_h)
        shade.setColorAt(0,QColor(68,219,243,8 if self._dark else 9))
        shade.setColorAt(1,QColor(240,87,166,18 if self._dark else 26))
        p.fillPath(outline,shade)
        ink.setAlpha(33 if self._dark else 29);p.setPen(QPen(ink,1));p.drawPath(outline)
        for offset in (9,18):
            leaf=QPainterPath();leaf.moveTo(x+offset,y+folder_h-13)
            leaf.lineTo(x+offset,y+42+offset*.3);leaf.lineTo(x+folder_w-31,y+42+offset*.3)
            p.drawPath(leaf)
        accent.setAlpha(60 if self._dark else 43);p.setPen(QPen(accent,2))
        p.drawLine(int(x+folder_w*.3),int(y+5),int(x+folder_w*.51),int(y+5))
        ink.setAlpha(23 if self._dark else 18);p.setPen(QPen(ink,1,Qt.PenStyle.DotLine))
        for row in range(4):
            ly=y+75+row*25
            if ly<y+folder_h-15:p.drawLine(int(x+35),int(ly),int(x+folder_w-35-row*17),int(ly))
        # A second distant page edge balances the free field rather than simulating table rows.
        if w>500 and h>230:
            ink.setAlpha(15 if self._dark else 11);p.setPen(QPen(ink,1))
            leaf=QPainterPath();leaf.moveTo(24,h*.25);leaf.lineTo(24,h*.65)
            leaf.lineTo(42,h*.65+18);leaf.lineTo(max(60,x-28),h*.65+18)
            p.drawPath(leaf)
            accent.setAlpha(28);p.setPen(QPen(accent,2));p.drawLine(24,int(h*.25),24,int(h*.25)+19)
        # A faint scan segment travels only within the ornament, using the host's ambient tick.
        if motion_enabled(self.view) and self.phase:
            dy=40+(self.phase%8)/8*max(1,folder_h-55)
            glow=QColor(colors.CYAN);glow.setAlpha(25 if self._dark else 19)
            p.setPen(QPen(glow,2));p.drawLine(int(x+25),int(y+dy),int(x+folder_w-28),int(y+dy))
        p.end()


class _ArchiveCanvases(QObject):
    def __init__(self,window):
        super().__init__(window)
        self.window=window;self.canvases=[];self._last_tick=-1
        backdrop=getattr(window,'backdrop',None)
        self.backdrop=backdrop
        if backdrop is not None:backdrop._scene_timer.timeout.connect(self._tick)
        subscribe_motion(window,self._motion_changed)
        self.refresh()

    def refresh(self):
        retained=[]
        for canvas in self.canvases:
            if not isValid(canvas) or not isValid(canvas.view):
                continue
            if canvas.view.property('customEmptyPresentation') is True:
                canvas.hide()
                canvas.deleteLater()
            else:
                retained.append(canvas)
        self.canvases=retained
        found=set(canvas.view for canvas in self.canvases)
        for view in self.window.findChildren(QWidget):
            if not isinstance(view,(QTableWidget,QListWidget,QTextBrowser)) or view in found:
                continue
            if view.property('customEmptyPresentation') is True:
                continue
            parent=view
            excluded=False
            while parent is not None and parent is not self.window:
                if parent.objectName()=='replayPanel':excluded=True;break
                parent=parent.parentWidget()
            if excluded:continue
            previous=getattr(view,'_empty_table_art',None)
            if previous is not None:
                previous.hide();previous.deleteLater();view._empty_table_art=None
            canvas=_ArchiveCanvas(view);self.canvases.append(canvas)
            found.add(view)

    def _motion_changed(self,enabled):
        if not enabled:
            for canvas in self.canvases:
                if isValid(canvas):canvas.phase=0.;canvas.update()

    def _tick(self):
        if not motion_enabled(self.window) or self.window.isMinimized():return
        seconds=self.backdrop._scene_time
        tick=int(seconds*8)
        if tick==self._last_tick:return
        self._last_tick=tick
        for canvas in self.canvases:
            if isValid(canvas) and canvas.isVisible():canvas.phase=seconds;canvas.update()


def install_archive_canvases(window):
    """Decorate unused table/list/browser view space. Call refresh() for later-created views."""
    owner=getattr(window,'_archive_canvases',None)
    if owner is None:owner=window._archive_canvases=_ArchiveCanvases(window)
    else:owner.refresh()
    return owner
