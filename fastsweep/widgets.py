"""Composants WinUI 3 dessinés par FastSweep : navigation, sélection en pastille, tableaux à cases, recherche, barre d'information.

WinUI 3 components drawn by FastSweep: navigation, pill selection, checkbox tables, search, info bar.
"""
from __future__ import annotations

from contextlib import contextmanager

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
)

from .glyphs import glyph_icon
from .theme import colors, qcolor

PILL_WIDTH, PILL_HEIGHT = 3, 16


def _draw_pill(painter: QPainter, left: float, center_y: float, color: QColor) -> None:
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    painter.drawRoundedRect(QRectF(left, center_y - PILL_HEIGHT / 2, PILL_WIDTH, PILL_HEIGHT), 1.5, 1.5)


class NavButton(QPushButton):
    """Entrée du volet de navigation ; cochée, elle affiche la pastille d'accent à gauche.

    Navigation pane entry; when checked it shows the accent pill on its left.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.accent = QColor("#0067C0")
        self.setObjectName("navButton")
        self.setCheckable(True)
        self.setMinimumHeight(40)
        self.setIconSize(QSize(16, 16))
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_accent(self, color: QColor) -> None:
        self.accent = color
        self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self.isChecked():
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            _draw_pill(painter, 0, self.height() / 2, self.accent)
            painter.end()


class SelectionDelegate(QStyledItemDelegate):
    """Lignes à la WinUI : fond arrondi discret au survol et à la sélection, pastille d'accent sur la 1re colonne.

    WinUI-style rows: subtle rounded fill on hover and selection, accent pill on the first column.
    ``row_height`` fixe la hauteur des lignes des listes (les tableaux règlent la leur eux-mêmes).
    ``row_height`` sets the row height of lists (tables set their own).
    ``primary_column`` est écrite en couleur principale, les autres en secondaire.
    ``primary_column`` is drawn in the main text color, the others in the secondary one.
    """

    def __init__(self, parent, dark: bool, row_height: int = 0, primary_column: int = 0):
        super().__init__(parent)
        self.row_height = row_height
        self.primary_column = primary_column
        self.set_theme(dark)

    def set_theme(self, dark: bool) -> None:
        c = colors(dark)
        self.selected, self.hover = qcolor(c["sel"]), qcolor(c["subtleHover"])
        self.accent, self.text, self.text2 = QColor(c["accent"]), QColor(c["text"]), QColor(c["text2"])

    def sizeHint(self, option, index) -> QSize:
        size = super().sizeHint(option, index)
        return QSize(size.width(), self.row_height) if self.row_height else size

    def paint(self, painter: QPainter, option, index) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        first = index.column() == 0
        last = not index.siblingAtColumn(index.column() + 1).isValid()  # marche aussi pour les listes / also works for lists
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        hovered = bool(opt.state & QStyle.StateFlag.State_MouseOver)
        if selected or hovered:
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setClipRect(opt.rect)
            # Arrondir seulement les extrémités de la ligne : les cellules du milieu débordent sous la découpe.
            # Round only the row's ends: middle cells overflow under the clip.
            band = QRectF(opt.rect).adjusted(0 if first else -8, 1, 0 if last else 8, -1)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self.selected if selected else self.hover)
            painter.drawRoundedRect(band, 4, 4)
            if selected and first:
                _draw_pill(painter, opt.rect.left(), opt.rect.center().y() + 0.5, self.accent)
            painter.restore()
        # Le style ne redessine pas la sélection : la couleur du texte suit la colonne.
        # The style does not redraw the selection: the text color follows the column.
        opt.state &= ~(QStyle.StateFlag.State_Selected | QStyle.StateFlag.State_HasFocus | QStyle.StateFlag.State_MouseOver)
        opt.palette.setColor(QPalette.ColorRole.Text, self.text if index.column() == self.primary_column else self.text2)
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)


class CheckHeader(QHeaderView):
    """En-tête horizontal avec une case « tout cocher » dans la première colonne (cochée, partielle ou vide).

    Horizontal header with a "check all" box in the first column (checked, partial or empty).
    Un clic sur cette colonne émet ``toggled`` au lieu de trier.
    A click on that column emits ``toggled`` instead of sorting.
    """

    toggled = Signal()
    BOX = 18

    def __init__(self, parent, dark: bool):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.state = Qt.CheckState.Unchecked
        self.setSectionsClickable(True)
        self.set_theme(dark)

    def set_theme(self, dark: bool) -> None:
        c = colors(dark)
        self.accent, self.border, self.fill = QColor(c["accent"]), QColor(c["cbBorder"]), qcolor(c["ctl"])
        self.on_accent = QColor(c["onAccent"])
        self.check_icon = glyph_icon("check", c["onAccent"], size=12, stroke=2.2)
        self.viewport().update()

    def set_check_state(self, state: Qt.CheckState) -> None:
        if state != self.state:
            self.state = state
            self.viewport().update()

    def paintSection(self, painter: QPainter, rect, logical_index: int) -> None:
        super().paintSection(painter, rect, logical_index)
        if logical_index != 0:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = QRectF(rect.center().x() - self.BOX / 2 + 1, rect.center().y() - self.BOX / 2 + 1, self.BOX - 1, self.BOX - 1)
        if self.state == Qt.CheckState.Unchecked:
            painter.setPen(QPen(self.border, 1))
            painter.setBrush(self.fill)
        else:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self.accent)
        painter.drawRoundedRect(box, 4, 4)
        if self.state == Qt.CheckState.Checked:
            self.check_icon.paint(painter, box.adjusted(3, 3, -3, -3).toRect())
        elif self.state == Qt.CheckState.PartiallyChecked:
            painter.setPen(QPen(self.on_accent, 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            center = box.center()
            painter.drawLine(QPointF(center.x() - 4, center.y()), QPointF(center.x() + 4, center.y()))
        painter.restore()

    def mousePressEvent(self, event) -> None:
        if self.logicalIndexAt(event.position().toPoint()) == 0:
            event.accept()  # ni tri ni sélection de colonne / neither sorting nor column selection
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self.logicalIndexAt(event.position().toPoint()) == 0:
            event.accept()
            self.toggled.emit()
            return
        super().mouseReleaseEvent(event)


class InfoBar(QFrame):
    """Barre d'information WinUI (avertissement ou information) : icône + texte sur fond teinté.

    WinUI info bar (warning or informational): icon + text on a tinted background.
    """

    def __init__(self, text: str, dark: bool, severity: str = "warning", parent=None):
        super().__init__(parent)
        self.setObjectName("infoBar")
        self.severity = severity
        self.setProperty("severity", severity)  # fond neutre pour une simple information / neutral background for plain information
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)
        self.icon = QLabel()
        self.icon.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        self.label = QLabel(text)
        self.label.setWordWrap(True)
        layout.addWidget(self.label, 1)
        self.set_theme(dark)

    def set_theme(self, dark: bool) -> None:
        c = colors(dark)
        name, color = ("warn", c["warnIcon"]) if self.severity == "warning" else ("info", c["accent"])
        self.icon.setPixmap(glyph_icon(name, color, size=18).pixmap(18, 18))

    def set_text(self, text: str) -> None:
        self.label.setText(text)


class SortableItem(QTableWidgetItem):
    """Cellule triée sur sa valeur ``UserRole + 1`` (nombre, date…) quand elle existe, sinon sur son texte.

    Cell sorted on its ``UserRole + 1`` value (number, date…) when present, otherwise on its text.
    """

    def __lt__(self, other) -> bool:
        left = self.data(Qt.ItemDataRole.UserRole + 1)
        right = other.data(Qt.ItemDataRole.UserRole + 1)
        if left is not None and right is not None:
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                return left < right
            return str(left) < str(right)
        return self.text().casefold() < other.text().casefold()


class SearchBox(QLineEdit):
    """Champ de recherche WinUI ; ``search_changed`` n'est émis qu'après une courte pause dans la saisie.

    WinUI search field; ``search_changed`` is only emitted after a short pause in typing.
    """

    search_changed = Signal()

    def __init__(self, delay_ms: int = 150, parent=None):
        super().__init__(parent)
        self.setObjectName("searchBox")
        self.setMinimumHeight(32)
        self.setClearButtonEnabled(True)
        self.icon_action = self.addAction(QIcon(), QLineEdit.ActionPosition.TrailingPosition)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(delay_ms)
        self.timer.timeout.connect(self.search_changed)
        self.textChanged.connect(self.timer.start)  # temporiser la saisie / debounce typing

    def set_theme(self, dark: bool) -> None:
        self.icon_action.setIcon(glyph_icon("search", colors(dark)["text2"]))


class FluentTable(QTableWidget):
    """Tableau WinUI : pas de grille, une ligne par cellule, sélection de lignes en pastille, en-tête aligné à gauche.

    WinUI table: no grid, one line per cell, pill row selection, left-aligned header.
    """

    def __init__(self, columns: int, dark: bool, primary_column: int = 0, parent=None):
        super().__init__(0, columns, parent)
        header = self.make_header(dark)
        if header is not None:
            self.setHorizontalHeader(header)
        self.setShowGrid(False)
        self.setWordWrap(False)  # texte élidé / elided text
        self.setMouseTracking(True)  # survol des lignes / row hover
        self.delegate = SelectionDelegate(self, dark, primary_column=primary_column)
        self.setItemDelegate(self.delegate)
        self.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.verticalHeader().setVisible(False)
        header = self.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setHighlightSections(False)
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)

    def make_header(self, dark: bool) -> QHeaderView | None:
        return None  # en-tête standard / standard header

    def set_theme(self, dark: bool) -> None:
        self.delegate.set_theme(dark)
        self.viewport().update()


class CheckTable(FluentTable):
    """Tableau dont la 1re colonne porte des cases à cocher, avec case « tout cocher » dans l'en-tête et ``Espace``.

    Table whose first column holds checkboxes, with a "check all" box in the header and ``Space``.
    Chaque ligne garde son objet dans ``UserRole`` de la colonne 0 ; ``key(objet)`` donne l'identifiant mémorisé
    dans ``checked``, qui survit à une recherche ou à un rechargement. Une ligne sans case n'est jamais cochée.
    Each row keeps its object in column 0's ``UserRole``; ``key(object)`` gives the identifier stored in
    ``checked``, which survives a search or a reload. A row without a box is never checked.
    """

    checks_changed = Signal()
    CHECK_COLUMN_WIDTH = 44

    def __init__(self, columns: int, dark: bool, key, primary_column: int = 1, parent=None):
        super().__init__(columns, dark, primary_column, parent)
        self.key = key
        self.checked: set = set()
        self.check_header.toggled.connect(self.toggle_all_visible)
        header = self.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(0, self.CHECK_COLUMN_WIDTH)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.itemChanged.connect(self._item_changed)

    def make_header(self, dark: bool) -> QHeaderView:
        self.check_header = CheckHeader(self, dark)
        return self.check_header

    @contextmanager
    def rebuilding(self, rows: int):
        """Remplit le tableau d'un coup : ni tri, ni signaux, ni redessin pendant l'opération, puis cases resynchronisées.

        Fills the table in one go: no sorting, signals or repaint meanwhile, then boxes resynchronized.
        """
        was_blocked = self.blockSignals(True)
        self.setUpdatesEnabled(False)
        self.setSortingEnabled(False)
        try:
            self.setRowCount(rows)
            yield
        finally:
            self.setSortingEnabled(True)
            self.setUpdatesEnabled(True)
            self.blockSignals(was_blocked)
        self.setVisible(bool(rows))
        self.sync_checks()  # case d'en-tête, puis compteur et boutons du propriétaire / header box, then the owner's counter and buttons

    def set_theme(self, dark: bool) -> None:
        self.check_header.set_theme(dark)
        super().set_theme(dark)

    def check_item(self, obj, accessible_text: str, checkable: bool = True) -> SortableItem:
        """Cellule de la colonne 0 : case cochée selon ``checked``, ou simple cellule sans case.

        Column 0 cell: box checked according to ``checked``, or a plain cell without a box.
        """
        item = SortableItem()
        item.setData(Qt.ItemDataRole.UserRole, obj)
        item.setData(Qt.ItemDataRole.AccessibleTextRole, accessible_text)
        if checkable:
            item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            item.setCheckState(Qt.CheckState.Checked if self.key(obj) in self.checked else Qt.CheckState.Unchecked)
        else:
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        return item

    def row_object(self, row: int):
        item = self.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def is_checkable(self, row: int) -> bool:
        item = self.item(row, 0)
        return bool(item) and bool(item.flags() & Qt.ItemFlag.ItemIsUserCheckable)

    def row_is_checked(self, row: int) -> bool:
        return self.is_checkable(row) and self.item(row, 0).checkState() == Qt.CheckState.Checked

    def checkable_rows(self) -> list[int]:
        return [row for row in range(self.rowCount()) if self.is_checkable(row)]

    def selected_rows(self) -> list[int]:
        return sorted({index.row() for index in self.selectionModel().selectedRows()})

    def selected_objects(self) -> list:
        """Objets des lignes sélectionnées qui ont une case (les autres ne se cochent ni ne se traitent).

        Objects of the selected rows that have a box (the others are neither checked nor processed).
        """
        return [self.row_object(row) for row in self.selected_rows() if self.is_checkable(row)]

    def set_rows_checked(self, rows, checked: bool) -> None:
        """Coche ou décoche plusieurs lignes en une fois (un seul rafraîchissement).

        Checks or unchecks several rows at once (a single refresh).
        """
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        was_blocked = self.blockSignals(True)
        try:
            for row in rows:
                if not self.is_checkable(row):
                    continue
                self.item(row, 0).setCheckState(state)
                key = self.key(self.row_object(row))
                if checked:
                    self.checked.add(key)
                else:
                    self.checked.discard(key)
        finally:
            self.blockSignals(was_blocked)
        self.sync_checks()

    def toggle_all_visible(self) -> None:
        rows = self.checkable_rows()
        self.set_rows_checked(rows, not (rows and all(self.row_is_checked(row) for row in rows)))

    def toggle_selected_checks(self) -> None:
        selected = set(self.selected_rows()) or ({self.currentRow()} if self.currentRow() >= 0 else set())
        rows = [row for row in sorted(selected) if self.is_checkable(row)]
        if rows:
            self.set_rows_checked(rows, not all(self.row_is_checked(row) for row in rows))

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Space and event.modifiers() == Qt.KeyboardModifier.NoModifier:
            self.toggle_selected_checks()  # Espace coche les lignes sélectionnées / Space checks the selected rows
            event.accept()
            return
        super().keyPressEvent(event)

    def sync_checks(self) -> None:
        """Met à jour la case de l'en-tête (cochée, partielle ou vide) puis prévient le propriétaire.

        Updates the header box (checked, partial or empty), then notifies the owner.
        """
        rows = self.checkable_rows()
        checked = sum(self.row_is_checked(row) for row in rows)
        if checked == 0:
            state = Qt.CheckState.Unchecked
        elif checked == len(rows):
            state = Qt.CheckState.Checked
        else:
            state = Qt.CheckState.PartiallyChecked
        self.check_header.set_check_state(state)
        self.checks_changed.emit()

    def _item_changed(self, item) -> None:
        if item.column() != 0 or not item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
            return
        obj = item.data(Qt.ItemDataRole.UserRole)
        if obj is None:
            return
        if item.checkState() == Qt.CheckState.Checked:
            self.checked.add(self.key(obj))
        else:
            self.checked.discard(self.key(obj))
        self.sync_checks()
