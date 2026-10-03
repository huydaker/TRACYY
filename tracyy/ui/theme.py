"""Application-wide Qt style sheet.

V1 pasted nine hundred lines of QSS into the body of ``main()``, which is
why nobody could find a colour without scrolling past the bootstrap. The
sheet is unchanged; it just lives where a style sheet belongs.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPointF, Qt

__all__ = ["TRACYY_QSS", "apply_theme", "resolved_stylesheet", "set_tooltips_enabled"]

TRACYY_QSS = """
    QWidget {
        background: #0f0d15;
        color: white;
        font-size: 13px;
    }
    /* Transport: four quiet satellites around one solid disc. Uniform buttons
       read as five equal options; a player has one primary action and the eye
       should find it without reading any of the others. */
    QPushButton#transportIconButton {
        min-width: 34px;
        min-height: 34px;
        max-width: 34px;
        max-height: 34px;
        padding: 0px;
        border-radius: 17px;
        background: transparent;
        border: 1px solid transparent;
    }
    QPushButton#transportIconButton:hover {
        background: rgba(255, 255, 255, 0.09);
    }
    QPushButton#transportIconButton:pressed {
        background: rgba(255, 255, 255, 0.15);
    }

    /* Filled disc, glyph cut out of it. Bright enough to be the one thing
       found by touch in a dark room. */
    QPushButton#transportPlayButton {
        min-width: 48px;
        min-height: 48px;
        max-width: 48px;
        max-height: 48px;
        padding: 0px;
        border-radius: 24px;
        border: none;
        background: qlineargradient(
            x1: 0, y1: 0, x2: 0, y2: 1,
            stop: 0 #FFFFFF, stop: 1 #DCE3EC
        );
    }
    QPushButton#transportPlayButton:hover {
        background: qlineargradient(
            x1: 0, y1: 0, x2: 0, y2: 1,
            stop: 0 #FFFFFF, stop: 1 #F0F4F9
        );
    }
    QPushButton#transportPlayButton:pressed {
        background: qlineargradient(
            x1: 0, y1: 0, x2: 0, y2: 1,
            stop: 0 #E4EAF2, stop: 1 #C6CFDA
        );
    }
    /* Running: the disc takes the accent, so the state is visible from the
       far side of a room without reading the glyph. */
    QPushButton#transportPlayButton[running="true"] {
        background: qlineargradient(
            x1: 0, y1: 0, x2: 0, y2: 1,
            stop: 0 #6BF23A, stop: 1 #3CB80E
        );
    }
    QPushButton#transportPlayButton[running="true"]:hover {
        background: qlineargradient(
            x1: 0, y1: 0, x2: 0, y2: 1,
            stop: 0 #8AF95F, stop: 1 #46DC00
        );
    }
    /* Glass again, but at 0.90-0.94 alpha rather than 0.02-0.075. The cue
       list runs underneath this bar, so the rows have to be sensed and not
       read: at this weight they come through as a faint shift in the panel,
       and every control on top of them stays legible. A lighter base gives
       the surface somewhere to catch the top hairline. */
    QGroupBox#transportGroupBar {
        background: qlineargradient(
            x1: 0, y1: 0, x2: 0, y2: 1,
            stop: 0 rgba(46, 53, 63, 0.945),
            stop: 1 rgba(28, 33, 41, 0.965)
        );
        border: 0px;
        border-top: 1px solid rgba(255, 255, 255, 0.16);
        border-radius: 14px;
        margin-top: 0px;
        padding-top: 0px;
    }
    QGroupBox#transportGroupBar::title {
        subcontrol-origin: margin;
        left: 0px;
        top: 0px;
        width: 0px;
        height: 0px;
        color: transparent;
    }
    /* Elapsed is the live number and reads bright; duration is reference and
       steps back, so the two stop looking like the same reading twice. Both
       monospace, so digits do not shuffle sideways as they count. */
    QLabel#transportMiniTime {
        color: #eef1f5;
        font-family: Consolas;
        font-size: 13px;
        font-weight: 600;
        background: transparent;
    }
    QLabel#transportMiniTimeMuted {
        color: #6f747e;
        font-family: Consolas;
        font-size: 13px;
        font-weight: 500;
        background: transparent;
    }
    QLabel#transportTrackTitle {
        color: #e8eaee;
        font-size: 12px;
        font-weight: 600;
        letter-spacing: 0px;
        background: transparent;
        padding: 0px 6px;
    }
    QLabel#transportVolumeLabel {
        color: #6f747e;
        font-size: 10px;
        font-weight: 700;
        letter-spacing: 1px;
        background: transparent;
    }
    QLabel#transportVolumeValue {
        color: #a4a9b3;
        font-family: Consolas;
        font-size: 12px;
        font-weight: 600;
        background: transparent;
    }
    QSlider#transportTimeline {
        background: transparent;
    }
    /* The played part of the timeline carries the accent, because the
       playhead is the live value on this bar. Volume does not: it was the
       loudest thing on screen and it is the least urgent. */
    QSlider#transportTimeline::groove:horizontal {
        border: 0px;
        height: 4px;
        background: rgba(255, 255, 255, 0.14);
        border-radius: 2px;
    }
    QSlider#transportTimeline::sub-page:horizontal {
        background: #FFFFFF;
        border-radius: 2px;
    }
    QSlider#transportTimeline::add-page:horizontal {
        background: rgba(255, 255, 255, 0.14);
        border-radius: 2px;
    }
    QSlider#transportTimeline::handle:horizontal {
        background: #FFFFFF;
        border: 0px;
        width: 12px;
        height: 12px;
        margin: -4px 0;
        border-radius: 6px;
    }
    /* Without this the slider paints the app background over the transport
       bar and reads as a black box floating behind the fader. */
    QSlider#transportVolumeSlider {
        background: transparent;
    }
    QSlider#transportVolumeSlider::groove:horizontal {
        border: 0px;
        height: 3px;
        background: #23262c;
        border-radius: 1px;
    }
    QSlider#transportVolumeSlider::sub-page:horizontal {
        background: #6f747e;
        border-radius: 1px;
    }
    QSlider#transportVolumeSlider::add-page:horizontal {
        background: #23262c;
        border-radius: 1px;
    }
    QSlider#transportVolumeSlider::handle:horizontal {
        background: #c8ccd4;
        border: 0px;
        width: 9px;
        height: 9px;
        margin: -3px 0;
        border-radius: 4px;
    }
    QPushButton#transportSmallRoundButton {
        min-width: 34px;
        min-height: 34px;
        max-width: 38px;
        max-height: 38px;
        font-size: 9px;
        font-weight: 700;
        border-radius: 19px;
        background: #20232a;
        border: 1px solid #3b3f47;
        color: #f5f7fa;
        padding: 0px;
    }
    QPushButton#transportSmallRoundButton:hover {
        background: #2a2e37;
        border-color: #585d68;
    }
    QPushButton#transportSmallRoundButton:pressed {
        background: #0f72d7;
        border-color: #1487ff;
    }


    QPushButton[licenseLocked="true"] {
        color: #f7cf72;
        background: #282117;
        border: 1px solid #6d5726;
    }
    QPushButton[licenseLocked="true"]:disabled {
        color: #d8b45f;
        background: #211c15;
        border: 1px solid #554621;
    }

    QWidget#navPanel {
        background: #14121b;
        border: none;
    }
    QPushButton#licenseStatusButton {
        background: #2a1010;
        border: 1px solid #7f1d1d;
        border-radius: 6px;
        color: #fecaca;
        padding: 8px;
        font-size: 10px;
        font-weight: 700;
        text-align: center;
    }
    QPushButton#licenseStatusButton:hover {
        border-color: #cbd5e1;
    }
    QPushButton#licenseStatusButton:pressed {
        padding-top: 9px;
        padding-bottom: 7px;
    }
    QPushButton#licenseStatusButton[licenseState="active"] {
        background: #0f2a1b;
        border-color: #166534;
        color: #bbf7d0;
    }
    QPushButton#licenseStatusButton[licenseState="expired"] {
        background: #332b0b;
        border-color: #ca8a04;
        color: #fef08a;
    }
    QPushButton#licenseStatusButton[licenseState="blocked"] {
        background: #2a1010;
        border-color: #7f1d1d;
        color: #fecaca;
    }

    QDialog#licenseInformationDialog {
        background: #111019;
    }
    QLabel#licenseInformationTitle {
        color: #ffffff;
        font-size: 18px;
        font-weight: 800;
    }
    QLabel#licenseFieldName {
        color: #9d9aa8;
        font-size: 12px;
        font-weight: 600;
    }
    QLabel#licenseFieldValue,
    QLabel#licenseRemainingValue {
        color: #f3f4f6;
        font-size: 12px;
        font-weight: 600;
    }
    QLabel#licenseFieldValue[licenseState="active"] {
        color: #86efac;
    }
    QLabel#licenseFieldValue[licenseState="expired"] {
        color: #fde047;
    }
    QLabel#licenseFieldValue[licenseState="blocked"] {
        color: #fca5a5;
    }
    QLabel#licenseRemainingValue[urgency="normal"] {
        color: #f3f4f6;
    }
    QLabel#licenseRemainingValue[urgency="notice"] {
        color: #fde047;
    }
    QLabel#licenseRemainingValue[urgency="warning"] {
        color: #fb923c;
    }
    QLabel#licenseRemainingValue[urgency="critical"] {
        color: #f87171;
        font-weight: 800;
    }
    QLabel#licenseRemainingValue[urgency="inactive"] {
        color: #9d9aa8;
    }
    QLabel#licenseMessage {
        color: #cbd5e1;
        background: #17151f;
        border: 1px solid #2f2b39;
        border-radius: 6px;
        padding: 10px;
    }

    QWidget#leftColumn {
        background: transparent;
        border: none;
    }
    QWidget#brandRow {
        background: transparent;
        border: none;
        min-height: 64px;
        max-height: 64px;
        padding-top: 4px;
    }
    QLabel#navTitle {
        font-size: 24px;
        font-weight: 900;
        letter-spacing: 2px;
        color: #ffffff;
    }
    QLabel#inputTimecodeStatus {
        padding: 6px 10px;
        border: 1px solid #3F463C;
        border-radius: 5px;
        background: #20241F;
        color: #D8DDD4;
        font-weight: 700;
    }
    QLabel#inputTimecodeStatus[state="locked"] {
        background: #173B24;
        border-color: #2F7A44;
        color: #DDFBE5;
    }
    QLabel#inputTimecodeStatus[state="waiting"] {
        background: #3B3217;
        border-color: #8A7225;
        color: #FFF1B8;
    }
    QLabel#inputTimecodeStatus[state="error"] {
        background: #481D1D;
        border-color: #9A3B3B;
        color: #FFDADA;
    }
    QCheckBox#inputTimecodeEnable,
    QCheckBox#autoBackupCheckbox {
        font-weight: 700;
        spacing: 8px;
    }

    QCheckBox#autoBackupCheckbox {
        color: #C9D7BE;
        padding: 4px 2px;
    }

    QLineEdit#inputTimecodeEdit {
        min-height: 34px;
        padding: 4px 10px;
        font-family: "SF Mono", "Menlo", "Consolas", monospace;
        font-size: 15px;
        font-weight: 700;
        color: #E7EEE3;
        background: #10150F;
        border: 1px solid #3C5231;
        border-radius: 5px;
    }

    QLineEdit#inputTimecodeEdit:focus {
        border-color: #46DC00;
    }

    QPushButton#inputTimecodeButton {
        min-height: 34px;
        font-weight: 700;
    }

    QLabel#settingsTitle {
        font-size: 18px;
        font-weight: 700;
    }
    QLabel#brandLogo {
        background: transparent;
        border: none;
    }
    QLabel#serverStatus {
        color: #22c55e;
        font-weight: 700;
    }
    QWidget#navPanel QPushButton {
        text-align: left;
        padding: 10px;
        margin-top: 2px;
    }
    QWidget#navPanel QPushButton:checked {
        background: #086acf;
    }
    QGroupBox {
        border: 1px solid #34313f;
        border-radius: 8px;
        margin-top: 10px;
        padding-top: 12px;
    }
    QGroupBox#topDock {
        margin-top: 0px;
    }
    QFrame#workspacePlaceholder {
        background: #0f0d15;
        border: 1px dashed #2f2b38;
        border-radius: 8px;
    }
    QLabel#standbyTitle {
        color: #facc15;
        font-size: 12px;
        font-weight: 800;
        padding: 0px 2px;
    }
    QListWidget {
        background: #14121b;
        border: 1px solid #34313f;
        border-radius: 6px;
        padding: 4px;
    }
    QListWidget::item {
        padding: 8px;
        border-radius: 5px;
    }
    QListWidget::item:selected {
        background: #086acf;
    }
    QTableWidget {
        background: #111019;
        alternate-background-color: #17151e;
        border: 1px solid #34313f;
        gridline-color: #292632;
    }
    QHeaderView::section {
        background: #22202b;
        color: white;
        padding: 7px;
        border: none;
        border-right: 1px solid #34313f;
    }
    QDialog {
        background: #111019;
        border-radius: 12px;
    }
    QLabel#tc {
        font-family: Consolas;
        font-size: 52px;
        color: #1686ff;
    }
    QLabel#sidebarTimeTitle {
        font-size: 13px;
        color: #9d9aa8;
        font-weight: 600;
    }
    QLabel#sidebarTcLabel {
        font-family: Consolas;
        font-size: 18px;
        font-weight: 600;
        color: #f3f4f6;
        background: transparent;
        border: none;
        padding: 0px;
    }
    QLineEdit#tcEditor {
        font-family: Consolas;
        font-size: 52px;
        color: #1686ff;
        background: transparent;
        border: 1px solid #1686ff;
        border-radius: 6px;
        padding: 0px 4px;
    }
    QLabel#playlistLoadProgress {
        font-size: 11px;
        color: #facc15;
        padding: 2px 4px;
    }
    QLabel#playlistTitle {
        font-size: 13px;
        font-weight: 700;
        color: #cfcfd6;
        min-height: 28px;
        max-height: 28px;
        padding: 0px;
        margin: 0px;
        qproperty-alignment: 'AlignCenter';
    }



    QLabel#nowPlaying {
        font-size: 17px;
        font-weight: 600;
    }
    QSlider::groove:horizontal {
        height: 6px;
        background: #2b2834;
        border-radius: 3px;
    }
    QSlider::handle:horizontal {
        width: 16px;
        margin: -5px 0;
        background: #1686ff;
        border-radius: 8px;
    }

    /* =========================================================
       TRACYY LIME / OLIVE THEME
       Visual-only override. No application behavior is changed.
       ========================================================= */

    QWidget {
        background: #050605;
        color: #E9E9E9;
        selection-background-color: #2B7400;
        selection-color: #FFFFFF;
    }

    QMainWindow,
    QDialog {
        background: #050605;
    }

    QWidget#leftColumn,
    QWidget#navPanel {
        background: #080D05;
        border: none;
    }

    QWidget#brandRow {
        background: #080D05;
    }

    QLabel#navTitle {
        color: #FFFFFF;
    }

    QLabel#settingsTitle,
    QLabel#playlistTitle,

    QGroupBox {
        background: #090D06;
        border: 1px solid #263916;
        border-radius: 8px;
        margin-top: 10px;
        padding-top: 12px;
    }

    QGroupBox::title {
        color: #B8C9A8;
        subcontrol-origin: margin;
        left: 10px;
        padding: 0px 5px;
    }

    /* The single definition for every ordinary control. There used to be two
       — a neutral one near the top of this sheet and this one overriding it —
       so editing the first had no effect at all. The neutral block is gone.

       Hover is the whole language: corners round, the label goes white, and
       the accent moves to the edge. Same idea as the nav tiles, expressed in
       the only terms a stylesheet has. */
    QPushButton,
    QComboBox,
    QLineEdit {
        background: #141A11;
        color: #C3CBBA;
        border: 1px solid #29381F;
        border-radius: 9px;
        /* Padding stays at 7px. Every layout in this window was sized around
           it, and the playlist panel is capped at 430px — widening it by five
           pixels a side pushed Save/Load/Relink past their own sizeHint and
           clipped the labels. The roundness is what changed, not the box. */
        padding: 7px;
    }

    QPushButton:hover,
    QComboBox:hover,
    QLineEdit:hover {
        background: #1B2616;
        border-color: #46DC00;
        color: #FFFFFF;
    }

    QPushButton:pressed {
        background: #0D1309;
        border-color: #46DC00;
        color: #FFFFFF;
    }

    /* The combo popup had no rules at all, so macOS drew it natively: the
       list inherited the closed control's 92px cap, spent most of it on a
       native check column, and clipped every item to one digit — "2 / 2 / 3"
       where 24 / 25 / 30 belong. Styling the view (and handing the combo a
       QListView in code) puts it back on Qt's own item painting. */
    QComboBox::drop-down {
        subcontrol-origin: padding;
        subcontrol-position: center right;
        width: 22px;
        border: none;
        background: transparent;
    }

    /* A fully styled QComboBox stops drawing the platform arrow, and every
       combo in the app had been reading as a dead label because of it. The
       triangle is built from borders on a zero-size box — the one shape Qt
       stylesheets can draw without shipping an image. */
    QComboBox::down-arrow {
        image: url(__CHEVRON_IDLE__);
        width: 9px;
        height: 6px;
    }

    QComboBox::down-arrow:on,
    QComboBox::down-arrow:hover {
        image: url(__CHEVRON_HOVER__);
    }

    QComboBox::down-arrow:disabled {
        image: url(__CHEVRON_OFF__);
    }

    QComboBox QAbstractItemView {
        background: #121B0B;
        color: #E9E9E9;
        border: 1px solid #30451D;
        border-radius: 8px;
        padding: 4px;
        outline: none;
        selection-background-color: #2B7400;
        selection-color: #FFFFFF;
    }

    QComboBox QAbstractItemView::item {
        min-height: 26px;
        padding: 0px 10px;
        border-radius: 5px;
    }

    QComboBox QAbstractItemView::item:selected,
    QComboBox QAbstractItemView::item:hover {
        background: #2B7400;
        color: #FFFFFF;
    }

    QPushButton:checked {
        background: #1E2C15;
        color: #FFFFFF;
        border-color: #46DC00;
    }

    QPushButton:disabled,
    QComboBox:disabled,
    QLineEdit:disabled {
        background: #0B0E09;
        color: #606A58;
        border-color: #1D2815;
    }

    QWidget#topHeader {
        background: #070A06;
        border: none;
        border-bottom: 1px solid #223315;
        border-bottom-left-radius: 12px;
        border-bottom-right-radius: 12px;
    }

    QLabel#brandLogoHeader {
        background: transparent;
        border: none;
    }

    QLabel#headerBrandTitle {
        color: #F4F7F0;
        font-size: 20px;
        font-weight: 800;
        letter-spacing: 1px;
        background: transparent;
        border: none;
    }

    QLabel#headerBrandSubtitle {
        color: #738468;
        font-size: 8px;
        font-weight: 700;
        letter-spacing: 2px;
        background: transparent;
        border: none;
    }

    QWidget#leftColumn,
    QWidget#navPanel {
        background: #070A06;
        border: none;
    }

    /* The nav tiles paint themselves (tracyy.ui.surfaces.RaisedIconButton):
       a raised neumorphic face needs two opposed shadows and a stylesheet can
       draw neither, so anything set here would only sit behind the tile. */
    QWidget#navPanel QToolButton#sidebarNavButton,
    QToolButton#sidebarNavButton {
        background: transparent;
        border: none;
        padding: 0px;
        margin: 0px;
    }

    QPushButton#licenseStatusButton {
        min-width: 42px;
        max-width: 42px;
        min-height: 34px;
        max-height: 34px;
        padding: 0px;
        margin: 0px;
        border-radius: 10px;
        font-size: 10px;
        font-weight: 700;
    }

    /* transportSmallRoundButton keeps the green skin; the transport row does
       not — its styling lives in one block near the top of this sheet. */
    QPushButton#transportSmallRoundButton {
        background: #11180D;
        border: 1px solid #334A20;
        color: #F1F1F1;
    }

    QPushButton#transportSmallRoundButton:hover {
        background: #1A2D0B;
        border-color: #4B7926;
    }

    QPushButton#transportSmallRoundButton:pressed {
        background: #234900;
        border-color: #46DC00;
    }

    QLabel#tc {
        color: #46DC00;
    }

    QLabel#sidebarTcLabel {
        color: #46DC00;
        font-family: Consolas;
        font-size: 18px;
        font-weight: 600;
    }

    QLineEdit#tcEditor {
        color: #46DC00;
        background: #060805;
        border: 1px solid #46DC00;
    }

    QLabel#sidebarTimeTitle,

    QLabel#playlistLoadProgress {
        color: #A7E786;
    }

    QLabel#standbyTitle {
        color: #B3EA91;
    }

    QLabel#serverStatus {
        color: #46DC00;
    }

    QListWidget {
        background: #070A05;
        color: #E9E9E9;
        border: 1px solid #263916;
        border-radius: 10px;
    }

    QListWidget::item:hover {
        background: #142307;
    }

    QListWidget::item:selected {
        background: #2B7400;
        color: #FFFFFF;
    }

    QTableWidget {
        background: #070A05;
        alternate-background-color: #0D1409;
        color: #E9E9E9;
        border: 1px solid #263916;
        border-radius: 10px;
        gridline-color: #223313;
        selection-background-color: #2B7400;
        selection-color: #FFFFFF;
    }

    QTableWidget::item {
        border-bottom: 1px solid #1A2810;
    }

    QTableWidget::item:hover {
        background: #142307;
    }

    QHeaderView::section {
        background: #142307;
        color: #F2F2F2;
        border: none;
        border-right: 1px solid #2C4518;
        border-bottom: 1px solid #315017;
    }

    /* The corner sections carry the table's own radius so the header does not
       square off a rounded frame. */
    QHeaderView::section:first {
        border-top-left-radius: 9px;
    }

    QHeaderView::section:last {
        border-top-right-radius: 9px;
        border-right: none;
    }

    QScrollBar:vertical {
        background: #080B06;
        width: 10px;
        margin: 0px;
    }

    QScrollBar::handle:vertical {
        background: #294414;
        min-height: 26px;
        border-radius: 5px;
    }

    QScrollBar::handle:vertical:hover {
        background: #3B6D18;
    }

    QScrollBar:horizontal {
        background: #080B06;
        height: 10px;
        margin: 0px;
    }

    QScrollBar::handle:horizontal {
        background: #294414;
        min-width: 26px;
        border-radius: 5px;
    }

    QScrollBar::handle:horizontal:hover {
        background: #3B6D18;
    }

    QScrollBar::add-line,
    QScrollBar::sub-line {
        width: 0px;
        height: 0px;
    }

    QSlider::groove:horizontal {
        height: 6px;
        background: #26351B;
        border-radius: 3px;
    }

    QSlider::sub-page:horizontal {
        background: #46DC00;
        border-radius: 3px;
    }

    QSlider::add-page:horizontal {
        background: #26351B;
        border-radius: 3px;
    }

    QSlider::handle:horizontal {
        width: 16px;
        margin: -5px 0;
        background: #E9E9E9;
        border: 1px solid #46DC00;
        border-radius: 8px;
    }


    QFrame#workspacePlaceholder {
        background: #070A05;
        border: 1px dashed #315017;
    }


    QDialog#licenseInformationDialog {
        background: #070A05;
    }

    QLabel#licenseInformationTitle {
        color: #46DC00;
    }

    QLabel#licenseFieldName {
        color: #9EAE93;
    }

    QLabel#licenseMessage {
        color: #DDE5D4;
        background: #0E150A;
        border: 1px solid #2C421B;
    }

    QPushButton#scanUsbLicenseButton {
        color: #DFFFD0;
        background: #173C08;
        border: 1px solid #46DC00;
        border-radius: 6px;
        padding: 7px 14px;
        font-weight: 700;
    }

    QPushButton#scanUsbLicenseButton:hover {
        background: #21580A;
    }

    QPushButton#scanUsbLicenseButton:pressed {
        background: #102D05;
    }

    QPushButton#scanUsbLicenseButton:disabled {
        color: #829079;
        background: #10170D;
        border-color: #2C421B;
    }

    QPushButton#scanGoogleSheetButton {
        color: #E6F1FF;
        background: #102A43;
        border: 1px solid #4DA3FF;
        border-radius: 6px;
        padding: 7px 14px;
        font-weight: 700;
    }

    QPushButton#scanGoogleSheetButton:hover {
        background: #163B5F;
    }

    QPushButton#scanGoogleSheetButton:pressed {
        background: #0B2035;
    }

    QPushButton#scanGoogleSheetButton:disabled {
        color: #7D8994;
        background: #11171D;
        border-color: #2D3A45;
    }

    /* The build number sits here now, so the tile has to be readable rather
       than loud: same surface as the nav tiles above it, with the licence
       state carried by the hairline. A saturated fill next to three quiet
       tiles just looked like something had gone wrong. */
    QPushButton#licenseStatusButton[licenseState="active"] {
        background: #23262C;
        border-color: #3D8F14;
        color: #A9CE96;
    }

    QPushButton#licenseStatusButton[licenseState="expired"] {
        background: #23262C;
        border-color: #A08C0A;
        color: #D9CE86;
    }

    QPushButton#licenseStatusButton[licenseState="blocked"] {
        background: #2A2226;
        border-color: #9B3131;
        color: #E0AEAE;
    }

    /* Per-channel output kill. Reads as a state, not a command: lit green
       while the channel is live, flat and dim once it is muted. */
    QPushButton#channelPowerButton {
        background: #23262C;
        border: 1px solid #33373F;
        border-radius: 6px;
        color: #7E8590;
        font-size: 10px;
        font-weight: 800;
        letter-spacing: 1px;
        padding: 6px 0px;
    }

    QPushButton#channelPowerButton:hover {
        border-color: #4A505C;
        color: #C8CCD4;
    }

    QPushButton#channelPowerButton:checked {
        background: rgba(70, 220, 0, 0.13);
        border-color: #3D8F14;
        color: #8FD46A;
    }

    QPushButton#channelPowerButton:checked:hover {
        background: rgba(70, 220, 0, 0.20);
        border-color: #46DC00;
        color: #B6EE96;
    }

    /* ---- Server page ----------------------------------------------- */

    QLabel#serverPageTitle {
        font-size: 19px;
        font-weight: 700;
        color: #F2F5EF;
        background: transparent;
    }

    QLabel#serverPageSubtitle,
    QLabel#serverPageHelp {
        font-size: 12px;
        color: #7E8C74;
        background: transparent;
    }

    QPushButton#serverPrimaryButton {
        background: #2B7400;
        border: 1px solid #46DC00;
        color: #FFFFFF;
        font-weight: 600;
        border-radius: 9px;
        padding: 7px 14px;
    }

    QPushButton#serverPrimaryButton:hover {
        background: #369600;
    }

    /* ---- Server cards --------------------------------------------- */

    QScrollArea#serverCardsScroll,
    QWidget#serverCardsContainer {
        background: transparent;
        border: none;
    }

    /* Same glass as the transport bar, at the weight a panel can afford when
       nothing scrolls underneath it: the page behind is flat, so the fill can
       stay light and the hairline does the lifting. */
    QFrame#serverCard {
        background: qlineargradient(
            x1: 0, y1: 0, x2: 0, y2: 1,
            stop: 0 rgba(255, 255, 255, 0.055),
            stop: 1 rgba(255, 255, 255, 0.016)
        );
        border: 1px solid rgba(255, 255, 255, 0.09);
        border-radius: 14px;
    }

    /* A running server is the one thing on this page worth finding across a
       room, so the whole card takes the accent rather than a badge on it. */
    QFrame#serverCard[running="true"] {
        background: qlineargradient(
            x1: 0, y1: 0, x2: 0, y2: 1,
            stop: 0 rgba(70, 220, 0, 0.075),
            stop: 1 rgba(70, 220, 0, 0.018)
        );
        border-color: rgba(70, 220, 0, 0.42);
    }

    QLabel#serverCardDot {
        border-radius: 4px;
        background: #48504A;
    }

    QLabel#serverCardDot[running="true"] {
        background: #46DC00;
    }

    QLineEdit#serverCardName {
        background: transparent;
        border: 1px solid transparent;
        border-radius: 7px;
        padding: 3px 6px;
        font-size: 16px;
        font-weight: 650;
        color: #F2F5EF;
    }

    QLineEdit#serverCardName:hover {
        background: rgba(255, 255, 255, 0.05);
        border-color: rgba(255, 255, 255, 0.12);
    }

    QLineEdit#serverCardName:focus {
        background: rgba(255, 255, 255, 0.07);
        border-color: #46DC00;
    }

    QLabel#serverCardState {
        font-size: 10px;
        font-weight: 800;
        letter-spacing: 1px;
        color: #7E8C74;
        border: 1px solid #2C3626;
        border-radius: 999px;
        padding: 3px 9px;
        background: transparent;
    }

    QLabel#serverCardState[running="true"] {
        color: #8FD46A;
        border-color: #2F6B12;
    }

    QPushButton#serverCardRemove {
        background: transparent;
        border: 1px solid transparent;
        border-radius: 7px;
        padding: 0px;
        font-size: 13px;
        font-weight: 700;
        color: #6F7D66;
    }

    QPushButton#serverCardRemove:hover {
        background: #3A1414;
        border-color: #7A3333;
        color: #FF8F8F;
    }

    QLabel#serverCardCaption {
        font-size: 10px;
        font-weight: 800;
        letter-spacing: 1.4px;
        color: #6F7D66;
        background: transparent;
    }

    QLabel#serverCardAddress {
        font-family: Consolas;
        font-size: 21px;
        font-weight: 600;
        color: #8E978A;
        background: transparent;
    }

    QLabel#serverCardAddress[running="true"] {
        color: #FFFFFF;
    }

    QPushButton#serverCardAction {
        font-size: 12px;
        padding: 6px 12px;
        border-radius: 8px;
    }

    QFrame#serverCardDivider {
        border: none;
        border-top: 1px solid rgba(255, 255, 255, 0.075);
        max-height: 1px;
    }

    QLineEdit#serverCardField,
    QComboBox#serverCardField,
    QPushButton#serverCardField {
        font-size: 13px;
        padding: 7px 10px;
        border-radius: 8px;
        background: rgba(0, 0, 0, 0.26);
        border: 1px solid rgba(255, 255, 255, 0.10);
        color: #D9DDCF;
    }

    QLineEdit#serverCardField:hover,
    QComboBox#serverCardField:hover,
    QPushButton#serverCardField:hover {
        border-color: #46DC00;
        color: #FFFFFF;
    }

    QLineEdit#serverCardField:disabled,
    QComboBox#serverCardField:disabled {
        color: #6B7364;
        background: rgba(0, 0, 0, 0.16);
        border-color: rgba(255, 255, 255, 0.05);
    }

    QLabel#serverCardQr {
        background: #F2F5EF;
        border-radius: 10px;
        padding: 4px;
    }

    /* Stopped, or no encoder: a labelled empty plate rather than a white
       square that looks like the code failed to load. */
    QLabel#serverCardQr[state="idle"] {
        background: rgba(255, 255, 255, 0.05);
        border: 1px solid rgba(255, 255, 255, 0.09);
        color: #6F7D66;
        font-size: 11px;
    }

    QLabel#serverCardHint {
        font-size: 11px;
        color: #6F7D66;
        background: transparent;
    }

    /* Connected devices. Sits quiet at zero and lights up only when somebody
       is actually watching, so the colour itself carries the answer from
       across the room. */
    QLabel#serverCardViewers {
        font-size: 11px;
        font-weight: 600;
        color: #6F7D66;
        background: rgba(255, 255, 255, 0.04);
        border: 1px solid rgba(255, 255, 255, 0.07);
        border-radius: 9px;
        padding: 4px 10px;
    }

    QLabel#serverCardViewers[live="true"] {
        color: #C8F5A0;
        background: rgba(139, 224, 90, 0.13);
        border-color: rgba(139, 224, 90, 0.30);
    }

    /* ---- Cue Point Types dialog ---------------------------------- */

    QLabel#cueTypeDialogTitle {
        font-size: 19px;
        font-weight: 700;
        color: #F2F5EF;
        background: transparent;
    }

    QLabel#cueTypeDialogSubtitle {
        font-size: 12px;
        color: #7E8C74;
        background: transparent;
    }

    QPushButton#cueTypePrimaryButton {
        background: #2B7400;
        border: 1px solid #46DC00;
        color: #FFFFFF;
        font-weight: 700;
        border-radius: 8px;
        padding: 9px 26px;
    }

    QPushButton#cueTypePrimaryButton:hover {
        background: #369600;
    }

    QPushButton#cueTypeSecondaryButton {
        background: transparent;
        border: 1px solid #30451D;
        color: #C3D1B7;
        font-weight: 600;
        border-radius: 8px;
        padding: 9px 18px;
    }

    QPushButton#cueTypeSecondaryButton:hover {
        background: #16220E;
        border-color: #4A7B22;
    }

    /* ---- Tracyy Live (Sync page) ---------------------------------- */

    QWidget#syncPage {
        background: transparent;
    }

    QLabel#syncTitle {
        font-size: 21px;
        font-weight: 700;
        color: #F2F5EF;
        background: transparent;
    }

    QLabel#syncSubtitle {
        font-size: 12px;
        color: #7E8C74;
        background: transparent;
    }

    QLabel#syncCaption {
        font-size: 10px;
        font-weight: 700;
        letter-spacing: 1.1px;
        color: #6E7D63;
        background: transparent;
    }

    QFrame#syncCard {
        background: rgba(255, 255, 255, 0.035);
        border: 1px solid rgba(255, 255, 255, 0.09);
        border-radius: 14px;
    }

    QLabel#syncCardTitle {
        font-size: 15px;
        font-weight: 700;
        color: #F2F5EF;
        background: transparent;
    }

    QLabel#syncCardNote,
    QLabel#syncEmptyNote {
        font-size: 12px;
        color: #7E8C74;
        background: transparent;
    }

    QFrame#syncDivider {
        border: none;
        border-top: 1px solid rgba(255, 255, 255, 0.07);
        max-height: 1px;
    }

    QLineEdit#syncNameField,
    QLineEdit#syncField {
        background: rgba(255, 255, 255, 0.05);
        border: 1px solid rgba(255, 255, 255, 0.1);
        border-radius: 8px;
        padding: 7px 10px;
        color: #E7EBE3;
        selection-background-color: #2B7400;
        selection-color: #FFFFFF;
    }

    QLineEdit#syncNameField:focus,
    QLineEdit#syncField:focus {
        border-color: #55B21E;
        background: rgba(139, 224, 90, 0.09);
    }

    QPushButton#syncPrimaryButton {
        background: #2B7400;
        border: 1px solid #46DC00;
        color: #FFFFFF;
        font-weight: 700;
        border-radius: 8px;
        padding: 8px 20px;
    }

    QPushButton#syncPrimaryButton:hover { background: #369600; }

    QPushButton#syncSecondaryButton {
        background: transparent;
        border: 1px solid #30451D;
        color: #C3D1B7;
        font-weight: 600;
        border-radius: 8px;
        padding: 7px 14px;
    }

    QPushButton#syncSecondaryButton:hover {
        background: rgba(139, 224, 90, 0.1);
        border-color: #4A7B22;
    }

    /* Leaving a session is not destructive, but it is abrupt for everyone
       still in it, so it reads warmer than the plain secondary. */
    QPushButton#syncDangerButton {
        background: transparent;
        border: 1px solid #4A2A20;
        color: #D2A08F;
        font-weight: 600;
        border-radius: 8px;
        padding: 7px 14px;
    }

    QPushButton#syncDangerButton:hover {
        background: rgba(224, 90, 60, 0.12);
        border-color: #7A4030;
    }

    QLabel#syncLiveDot {
        border-radius: 4px;
        background: #4CE05A;
    }

    QLabel#syncLiveDot[connecting="true"] {
        background: #EDE04A;
    }
    QLabel#syncLiveDot[disconnected="true"] { background: #E8615A; }
    QLabel#syncStatePill[disconnected="true"] { color: #FF9088; background: rgba(232, 97, 90, 30); }

    QLabel#syncStatePill {
        font-size: 10px;
        font-weight: 700;
        letter-spacing: 1px;
        padding: 4px 10px;
        border-radius: 9px;
        color: #8A9880;
        background: rgba(255, 255, 255, 0.05);
        border: 1px solid rgba(255, 255, 255, 0.08);
    }

    QLabel#syncStatePill[hosting="true"] {
        color: #C6F0A4;
        background: rgba(139, 224, 90, 0.13);
        border-color: rgba(139, 224, 90, 0.3);
    }

    /* The code is read aloud across a room, so it is set large and spaced. */
    QLabel#syncCodeValue {
        font-family: Consolas;
        font-size: 26px;
        font-weight: 700;
        letter-spacing: 4px;
        color: #C6F0A4;
        background: transparent;
    }

    QLabel#syncAddressValue {
        font-family: Consolas;
        font-size: 13px;
        color: #D9DDCF;
        background: transparent;
    }

    QLabel#syncQr {
        background: #F2F5EF;
        border-radius: 10px;
        padding: 4px;
    }

    QLabel#syncQr[state="idle"] {
        background: rgba(255, 255, 255, 0.05);
        border: 1px solid rgba(255, 255, 255, 0.09);
        color: #6F7D66;
        font-size: 11px;
    }

    QFrame#syncPeerRow {
        background: rgba(255, 255, 255, 0.03);
        border: 1px solid rgba(255, 255, 255, 0.07);
        border-radius: 10px;
    }

    /* Your own row is the one you look for first. */
    QFrame#syncPeerRow[local="true"] {
        background: rgba(139, 224, 90, 0.07);
        border-color: rgba(139, 224, 90, 0.22);
    }

    QLabel#syncPeerName {
        font-size: 13.5px;
        font-weight: 600;
        color: #E7EBE3;
        background: transparent;
    }

    QLabel#syncPeerTrack {
        font-size: 11.5px;
        color: #7E8C74;
        background: transparent;
    }

    QLabel#syncPeerSwatch {
        background: transparent;
    }

    QLabel#syncRolePill {
        font-size: 10px;
        font-weight: 700;
        letter-spacing: 0.6px;
        padding: 4px 10px;
        border-radius: 9px;
        color: #8A9880;
        background: rgba(255, 255, 255, 0.05);
        border: 1px solid rgba(255, 255, 255, 0.08);
    }

    QLabel#syncRolePill[role="owner"] {
        color: #C6F0A4;
        background: rgba(139, 224, 90, 0.13);
        border-color: rgba(139, 224, 90, 0.3);
    }

    QLabel#syncRolePill[role="viewer"] {
        color: #6E7D63;
    }

    QComboBox#syncRoleCombo {
        background: rgba(255, 255, 255, 0.05);
        border: 1px solid rgba(255, 255, 255, 0.1);
        border-radius: 8px;
        padding: 5px 10px;
        min-width: 118px;
        color: #D9DDCF;
    }

    QComboBox#syncRoleCombo:hover {
        border-color: #4A7B22;
    }

    QFrame#syncRecentRow {
        background: rgba(255, 255, 255, 0.03);
        border: 1px solid rgba(255, 255, 255, 0.06);
        border-radius: 9px;
    }

    QLabel#syncRecentName {
        font-size: 13px;
        font-weight: 600;
        color: #E7EBE3;
        background: transparent;
    }

    QLabel#syncRecentAddress {
        font-family: Consolas;
        font-size: 12px;
        color: #7E8C74;
        background: transparent;
    }

    QLabel#syncRecentSize {
        font-family: Consolas;
        font-size: 12px;
        color: #C3D1B7;
        background: transparent;
    }

    /* A show with nothing downloaded is the normal state, not a warning, so
       the number recedes instead of announcing itself. */
    QLabel#syncRecentSize[empty="true"] {
        color: #5E6B55;
    }

    QFrame#syncStorageRow {
        background: rgba(255, 255, 255, 0.02);
        border: 1px solid rgba(255, 255, 255, 0.05);
        border-radius: 9px;
    }

    QLabel#syncStoragePath {
        font-family: Consolas;
        font-size: 11.5px;
        color: #7E8C74;
        background: transparent;
    }

    /* ---- Cue type cards ------------------------------------------ */

    QScrollArea#cueTypeScroll,
    QScrollArea#cueTypeScroll > QWidget > QWidget {
        background: transparent;
        border: none;
    }

    QFrame#cueTypeCard {
        background: #0F130C;
        border: 1px solid #26331B;
        border-radius: 12px;
    }

    /* A hidden type still has to be findable, so the card dims rather than
       disappearing — enough to read as off across the dialog, not so much
       that its name stops being legible. */
    QFrame#cueTypeCard[visible_state="false"] {
        background: #0C0F0A;
        border-color: #1C2415;
    }

    QLineEdit#cueTypeName {
        background: transparent;
        border: 1px solid transparent;
        border-radius: 6px;
        padding: 3px 6px;
        font-size: 14px;
        font-weight: 600;
        color: #E7EBE3;
        selection-background-color: #2B7400;
        selection-color: #FFFFFF;
    }

    QLineEdit#cueTypeName[visible_state="false"] {
        color: #71806A;
    }

    /* The field has to look like a label until it is worth touching, or every
       card carries a text box competing with the name it holds. */
    QLineEdit#cueTypeName:hover {
        background: #141B0E;
        border-color: #2C3D1D;
    }

    QLineEdit#cueTypeName:focus {
        background: #101609;
        border-color: #55B21E;
    }

    QLabel#cueTypeHex {
        font-size: 11px;
        color: #5E6B55;
        background: transparent;
    }

    QLabel#cueTypeCaption {
        font-size: 10px;
        font-weight: 700;
        letter-spacing: 1px;
        color: #6E7D63;
        background: transparent;
    }

    QPushButton#cueTypeSwatch {
        background: transparent;
        border: none;
        padding: 0px;
    }

    QPushButton#cueTypeDelete {
        background: transparent;
        border: none;
        border-radius: 6px;
    }

    /* Destructive, so it stays quiet until the pointer is actually on it — a
       red box on every card reads as seven warnings. */
    QPushButton#cueTypeDelete:hover {
        background: #3A1414;
    }

    QPushButton#cueTypeDelete:pressed {
        background: #521A1A;
    }

    QPushButton#cueTypeFlag {
        font-size: 10px;
        font-weight: 700;
        letter-spacing: 0.9px;
        padding: 6px 0px;
        border-radius: 7px;
        background: #101609;
        border: 1px solid #2C3D1D;
        color: #5E6B55;
    }

    QPushButton#cueTypeFlag:hover {
        border-color: #46662A;
        color: #8A9880;
    }

    QPushButton#cueTypeFlag:checked {
        background: #26400F;
        border-color: #55B21E;
        color: #C6F0A4;
    }

    QPushButton#cueTypeShape {
        background: #101609;
        border: 1px solid #2C3D1D;
        border-radius: 7px;
        padding: 0px;
    }

    QPushButton#cueTypeShape:hover {
        border-color: #46662A;
    }

    QPushButton#cueTypeShape:checked {
        background: #26400F;
        border-color: #55B21E;
    }

    QPushButton#cueTypeColorButton {
        background: #101609;
        border: 1px solid #30451D;
        border-radius: 6px;
        padding: 0px;
    }

    QPushButton#cueTypeColorButton:hover {
        border-color: #5C8A34;
    }

    /* Checkboxes were rendering as native macOS blue against a green sheet,
       and the unchecked ones as bare grey squares — three columns, two
       different control languages. One box, filled with the accent when on. */
    QCheckBox::indicator {
        width: 16px;
        height: 16px;
        border-radius: 4px;
        border: 1px solid #3A4B2C;
        background: #101609;
    }

    QCheckBox::indicator:hover {
        border-color: #5C8A34;
    }

    QCheckBox::indicator:checked {
        background: #3D8F14;
        border-color: #55B21E;
    }

    QCheckBox::indicator:disabled {
        border-color: #212B1A;
        background: #0B0E09;
    }

    QToolTip {
        background: #111A0B;
        color: #FFFFFF;
        border: 1px solid #46DC00;
        border-radius: 7px;
        padding: 6px 9px;
    }
"""


class _ToolTipSuppressor(QObject):
    """Swallows tooltip requests before Qt can show one.

    Installed on the application rather than stripping fifty setToolTip calls:
    the strings stay in the source, where they still document what a control
    does for whoever reads it next, and turning tooltips back on is one call
    instead of fifty edits. Nothing else in the event stream is touched.
    """

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.ToolTip:
            return True
        return super().eventFilter(watched, event)


_TOOLTIP_SUPPRESSOR: _ToolTipSuppressor | None = None


def set_tooltips_enabled(app, enabled: bool) -> None:
    """Turn hover tooltips on or off for the whole application."""
    global _TOOLTIP_SUPPRESSOR

    if enabled:
        if _TOOLTIP_SUPPRESSOR is not None:
            app.removeEventFilter(_TOOLTIP_SUPPRESSOR)
            _TOOLTIP_SUPPRESSOR = None
        return

    if _TOOLTIP_SUPPRESSOR is None:
        _TOOLTIP_SUPPRESSOR = _ToolTipSuppressor()
        app.installEventFilter(_TOOLTIP_SUPPRESSOR)


def _chevron_file(colour: str, name: str) -> str:
    """Write a down chevron to the cache and return its path.

    A fully styled ``QComboBox`` stops drawing the platform arrow, and every
    combo in the application had been reading as a dead label because of it.
    Qt stylesheets cannot draw the replacement: a zero-size box with borders
    renders as a flat dash rather than a triangle, and there is no transform
    to rotate a corner into a chevron. An image is the only route, so the
    theme draws one once instead of the project carrying an asset file.

    Written at 2x alongside the 1x, which is the filename convention Qt's
    pixmap loader already looks for on a HiDPI screen.
    """
    from PySide6.QtGui import QColor, QPainter, QPen, QPixmap

    from tracyy.core.paths import cache_root

    directory = cache_root() / "chrome"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{name}.png"

    for path, scale in ((target, 1), (directory / f"{name}@2x.png", 2)):
        pixmap = QPixmap(9 * scale, 6 * scale)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.scale(scale, scale)
            painter.setPen(
                QPen(
                    QColor(colour),
                    1.5,
                    Qt.PenStyle.SolidLine,
                    Qt.PenCapStyle.RoundCap,
                    Qt.PenJoinStyle.RoundJoin,
                )
            )
            painter.drawPolyline([QPointF(1.2, 1.6), QPointF(4.5, 4.6), QPointF(7.8, 1.6)])
        finally:
            painter.end()
        pixmap.save(str(path), "PNG")

    # Qt stylesheets take forward slashes on every platform, and a Windows
    # backslash in url() is read as an escape.
    return str(target).replace("\\", "/")


def resolved_stylesheet() -> str:
    """The sheet with its generated image paths filled in."""
    return (
        TRACYY_QSS.replace("__CHEVRON_IDLE__", _chevron_file("#8A9880", "chevron"))
        .replace("__CHEVRON_HOVER__", _chevron_file("#C6F0A4", "chevron-hover"))
        .replace("__CHEVRON_OFF__", _chevron_file("#4A5741", "chevron-off"))
    )


def apply_theme(app) -> None:
    """Install the Tracyy palette and the platform UI font on a ``QApplication``.

    The sheet used to hardcode ``font-family: Segoe UI``, which does not exist
    on macOS: Qt scanned the whole family alias table on every start (~53 ms,
    and it says so in the log) before falling back to a default that is not the
    system font either. Asking the font database for the platform's own UI face
    gives SF Pro on macOS and Segoe UI on Windows, with no lookup and no guess.
    """
    from PySide6.QtGui import QFontDatabase

    app.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont))
    app.setStyleSheet(resolved_stylesheet())

    # A tooltip popping up because someone brushed a control is the last thing
    # anyone needs mid-show. Flip this to True to bring them back.
    set_tooltips_enabled(app, False)
