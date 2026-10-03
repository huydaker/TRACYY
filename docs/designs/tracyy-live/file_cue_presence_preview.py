"""Visual proposals only: native Tracyy widgets with illustrative presence."""
import os
import sys
import ast
from pathlib import Path
from types import SimpleNamespace

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
os.environ['TRACYY_WAVEFORM_SOFTWARE'] = '1'
PROJECT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).parent
sys.path.insert(0, str(PROJECT))
import numpy as np
from PySide6.QtCore import Qt, QRectF, QPointF, QLineF
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QGroupBox,
    QPushButton, QTableWidget, QHeaderView, QAbstractItemView,
)
from tracyy.ui.theme import apply_theme
from tracyy.widgets.waveform import WaveformWidget
from tracyy.widgets.cue_loading import VirtualCueLoadingWidget
from tracyy.widgets.cue_widgets import CueTableDelegate
from tracyy.controllers.cue import CueControllerMixin

PINK, BLUE = '#F297C1', '#84B7FF'


def tag(p, x, y, text, color):
    p.setFont(QFont('Helvetica Neue', 10))
    w = p.fontMetrics().horizontalAdvance(text)+16
    p.setPen(QPen(QColor(color), .7))
    p.setBrush(QColor('#18201d'))
    p.drawRoundedRect(QRectF(x, y, w, 22), 6, 6)
    p.setPen(QColor(color))
    p.drawText(QRectF(x+8, y, w-16, 22), Qt.AlignmentFlag.AlignVCenter, text)


def cursor(p, x, y, name, color):
    p.setPen(QPen(QColor('#070A05'), 1))
    p.setBrush(QColor(color))
    p.drawPolygon(QPolygonF([QPointF(x,y), QPointF(x+2,y+18),
                            QPointF(x+8,y+12), QPointF(x+14,y+11)]))
    tag(p, x+17, y+4, name, color)


class Wave(WaveformWidget):
    def __init__(self, scene):
        super().__init__()
        self.scene = scene
        self.setFixedHeight(263)

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.wave_rect()
        for pos, color in [(.4, PINK), (26/30, BLUE)]:
            x = r.left()+r.width()*pos
            p.setPen(QPen(QColor(color), 1.1, Qt.PenStyle.DashLine))
            p.drawLine(QLineF(x, r.top()+12, x, r.bottom()))
            p.setBrush(QColor(color))
            p.drawEllipse(QPointF(x,r.top()+8), 3, 3)
        if self.scene != 'editing':
            cursor(p, r.left()+r.width()*.72, r.top()+r.height()*.58, 'Huy', BLUE)
        p.end()


class TablePresence(QWidget):
    def __init__(self, table, scene):
        super().__init__(table.viewport())
        self.table, self.scene = table, scene
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        t = self.table
        if self.scene != 'outside':
            row = QRectF(t.visualItemRect(t.item(4, 0)))
            row.setRight(t.viewport().width()-4)
            row.adjust(2,2,-2,-2)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(PINK), 1.2))
            p.drawRoundedRect(row, 5, 5)
            cell = t.visualItemRect(t.item(4,3))
            cursor(p, cell.right()-94, cell.top()+10, 'Linh', PINK)
        if self.scene == 'editing':
            cell = QRectF(t.visualItemRect(t.item(5,3))).adjusted(2,2,-3,-2)
            p.setPen(QPen(QColor(BLUE), 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(cell, 5,5)
            # Editing indicator sits after the existing label, with no colour
            # replacement of the cue type or of the user's own selected row.
            cursor(p, cell.right()-93, cell.top()+7, 'Huy', BLUE)
        p.end()


def label(text, style=''):
    w = QLabel(text)
    if style:
        w.setStyleSheet(style)
    return w


app = QApplication([])
apply_theme(app)
# Read the actual table style without constructing MainWindow or starting
# licence checks, audio devices, or a project session.
tree = ast.parse((PROJECT/'tracyy/ui/main_window.py').read_text())
table_style = next(node.args[0].value for node in ast.walk(tree)
                   if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                   and node.func.attr == 'setStyleSheet' and node.args
                   and isinstance(node.args[0], ast.Constant)
                   and isinstance(node.args[0].value, str)
                   and 'gridline-color: #243515' in node.args[0].value)
rng = np.random.default_rng(14)
t = np.linspace(0,1,18000)
samples = ((.2+.42*np.sin(t*32)**2)*(.48+.52*rng.random(len(t)))+.24*np.sin(t*133)**14).astype(np.float32)
names = ['Mở sân khấu', 'Intro', 'Verse', 'Chuyển cảnh', 'Khói vào', 'Blackout',
         'Mở visual', 'Chorus', 'Solo', 'Dừng khói', 'Bridge', 'Kết đoạn']
types = [('Choreo','#ff752b'), ('Audio','#23b7e5'), ('Video','#9255ed'),
         ('Notes','#5682df'), ('Pyro','#00bf42'), ('Lighting','#ed4040')]
seconds = [52,58,64,70,77,82.4,88,95,102,110,117,125]
entries = []
for i in range(28):
    typ, color = types[i%6]
    entries.append(dict(id=f'cue-{i+1}', name=f'Cue {i+1:02}',
                        label=names[i%12], seconds=seconds[i] if i<12 else 130+(i-12)*7,
                        type=typ,color=color))

for scene, filename in [('pointing','file-cue-cursor'), ('editing','file-cue-editing'),
                        ('outside','file-cue-outside-view')]:
    window = QWidget()
    window.setObjectName('filePreview')
    window.setStyleSheet('#filePreview {background:#070A05;}')
    outer = QVBoxLayout(window)
    outer.setContentsMargins(14,12,14,12)
    header = QHBoxLayout()
    header.addWidget(label('FILE  /  Phiên dựng cue', 'color:#edf5e8;font-size:16px;font-weight:600'))
    header.addStretch()
    header.addWidget(label('●  Đã kết nối   ·   3 người', 'color:#b8d99f;font-size:12px'))
    outer.addLayout(header)
    body = QHBoxLayout()
    body.setSpacing(12)
    outer.addLayout(body,1)
    left = QWidget()
    ll = QVBoxLayout(left)
    ll.setContentsMargins(0,0,0,0)
    ll.setSpacing(8)
    chips = QHBoxLayout()
    for title,color in [('1 · Choreo','#ff752b'), ('2 · Audio','#20b5ee'), ('3 · Video','#9255ed'),
                        ('4 · Notes','#5682df'), ('5 · Lighting','#ef4741')]:
        b = QPushButton(title)
        b.setStyleSheet(f'color:{color};border:1px solid {color};border-radius:13px;background:#070A05;padding:6px;font-size:11px')
        chips.addWidget(b)
    ll.addLayout(chips)
    lane = QWidget()
    lane.setStyleSheet('background:#131b12;border-radius:8px')
    lane_l = QVBoxLayout(lane)
    lane_l.setContentsMargins(10,7,10,7)
    lane_l.setSpacing(4)
    status_linh = 'Cue 20 · ngoài vùng bảng đang xem' if scene=='outside' else 'chọn Cue 05 · Khói vào'
    status_huy = 'sửa LABEL · Cue 06' if scene=='editing' else 'đang xem waveform'
    lane_l.addWidget(label(f'● Linh · 01:12  ·  {status_linh}', f'color:{PINK};font-size:12px'))
    lane_l.addWidget(label(f'● Huy · 01:26  ·  {status_huy}', f'color:{BLUE};font-size:12px'))
    ll.addWidget(lane)
    timeline = QGroupBox('Timeline')
    tl = QVBoxLayout(timeline)
    tl.setContentsMargins(5,10,5,5)
    wave = Wave(scene)
    wave.set_track('CHỜ THÊM MỘT PHÚT · 01:00 — 01:30', samples)
    wave.set_markers([(e['id'],(e['seconds']-60)/30,e['label'],e['color'],False,'rectangle')
                      for e in entries if 60<=e['seconds']<=90])
    wave.set_progress(13/30,False)
    tl.addWidget(wave)
    zoom = QHBoxLayout()
    zoom.addStretch()
    for text in ['Follow','Zoom +','Zoom −','Fit']:
        zoom.addWidget(QPushButton(text))
    tl.addLayout(zoom)
    ll.addWidget(timeline)
    loading = VirtualCueLoadingWidget()
    loading.setFixedHeight(142)
    loading.set_entries(entries[4:8], 'cue-5', 5)
    loading.set_transport_anchor(73,False)
    ll.addWidget(loading)
    ll.addStretch()
    body.addWidget(left, 1)

    group = QGroupBox('Cue / Marker Editor')
    gl = QVBoxLayout(group)
    clockrow = QHBoxLayout()
    clockrow.addWidget(label('Cue Time'))
    clockrow.addStretch()
    clockrow.addWidget(label('00:01:13:00 / 00:04:30:00','color:#8bff67;font-size:15px;font-weight:600'))
    gl.addLayout(clockrow)
    buttons = QHBoxLayout()
    for text in ['Add Cue at Playhead','Import Cue','Export Cue']:
        buttons.addWidget(QPushButton(text))
    buttons.addStretch()
    gl.addLayout(buttons)
    if scene=='outside':
        hintrow = QHBoxLayout()
        hintrow.addWidget(label('● Linh đang ở Cue 20', f'color:{PINK};font-size:12px'))
        hintrow.addStretch()
        hintrow.addWidget(QPushButton('Đi tới cue'))
        gl.addLayout(hintrow)
    else:
        hint = '● Linh chọn Cue 05' if scene=='pointing' else '● Linh chọn Cue 05    ·    Huy sửa LABEL của Cue 06'
        gl.addWidget(label(hint,'color:#c9b7c7;font-size:12px;padding:5px 0'))
    table = QTableWidget(len(entries),4)
    table.setStyleSheet(table_style)
    table.setItemDelegate(CueTableDelegate(table))
    table.setHorizontalHeaderLabels(['TIME','CUE TYPE','CUE','LABEL'])
    table.verticalHeader().hide()
    table.verticalHeader().setDefaultSectionSize(40)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
    stub = SimpleNamespace(cue_table=table,media_to_timecode_seconds=lambda v:v)
    for i,e in enumerate(entries):
        CueControllerMixin.populate_cue_table_row(stub,i,e,30)
    table.selectRow(2)
    table.clearFocus()
    gl.addWidget(table,1)
    gl.addWidget(label('Votre sélection : Cue 03'.replace('Votre sélection','Bạn đang chọn'), 'color:#8f9f85;font-size:12px'))
    body.addWidget(group,1)
    footer = QHBoxLayout()
    footer.addWidget(label('Nhạc đã khớp  ·  Cue đã cập nhật','color:#aabb9b;font-size:12px'))
    footer.addStretch()
    footer.addWidget(label('Phát nhạc độc lập trên từng máy','color:#a2aba0;font-size:12px'))
    outer.addLayout(footer)
    window.resize(1380,760)
    window.show()
    for _ in range(5):
        app.processEvents()
    width=table.viewport().width()-4
    for col, ratio in enumerate([.23,.22,.16,.39]):
        table.setColumnWidth(col,int(width*ratio))
    overlay = TablePresence(table,scene)
    overlay.setGeometry(table.viewport().rect())
    overlay.show()
    overlay.raise_()
    for _ in range(5):
        app.processEvents()
    path=OUT/f'{filename}.png'
    if not window.grab().save(str(path)):
        raise RuntimeError(path)
    print(path)
    window.close()
