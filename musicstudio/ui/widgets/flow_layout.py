"""A layout that wraps its widgets onto the next line when they don't fit.

Rows of buttons were plain QHBoxLayouts. Once the window (or the interface
size) left less width than the buttons needed, Qt squeezed them below their
text width -- "Jpdate all artwork", "uTube Music form" -- and the search box
collapsed to "Se...". Wrapping keeps every button at its natural size and
simply uses a second line when the width runs out.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QLayout, QSizePolicy, QWidget, QWidgetItem


class FlowLayout(QLayout):
    def __init__(self, parent: QWidget | None = None, spacing: int = 8,
                 align_right: bool = False) -> None:
        super().__init__(parent)
        self._items: list = []
        self._align_right = align_right
        self.setSpacing(spacing)
        self.setContentsMargins(0, 0, 0, 0)

    # -- QLayout plumbing ------------------------------------------------
    def addItem(self, item) -> None:  # noqa: N802 (Qt API)
        self._items.append(item)

    def addWidget(self, widget: QWidget) -> None:  # noqa: N802
        self.addChildWidget(widget)
        self.addItem(QWidgetItem(widget))

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):  # noqa: N802
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._arrange(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._arrange(rect, apply=True)

    def sizeHint(self) -> QSize:  # noqa: N802
        # Preferred: everything on one line.
        width = sum(i.sizeHint().width() for i in self._visible())
        width += self.spacing() * max(0, len(self._visible()) - 1)
        height = max((i.sizeHint().height() for i in self._visible()), default=0)
        margins = self.contentsMargins()
        return QSize(width + margins.left() + margins.right(),
                     height + margins.top() + margins.bottom())

    def minimumSize(self) -> QSize:  # noqa: N802
        # Only as wide as the widest single item: the rest can wrap.
        size = QSize()
        for item in self._visible():
            size = size.expandedTo(item.minimumSize().expandedTo(item.sizeHint()))
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(), margins.top() + margins.bottom())

    # -- layout -------------------------------------------------------------
    def _visible(self) -> list:
        return [i for i in self._items if i.widget() is None or not i.widget().isHidden()]

    def _arrange(self, rect: QRect, apply: bool) -> int:
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        spacing = self.spacing()
        lines: list[list] = [[]]
        line_width = 0
        for item in self._visible():
            w = item.sizeHint().width()
            if lines[-1] and line_width + spacing + w > area.width():
                lines.append([])
                line_width = 0
            line_width += (spacing if lines[-1] else 0) + w
            lines[-1].append(item)

        y = area.y()
        for line in lines:
            if not line:
                continue
            line_height = max(i.sizeHint().height() for i in line)
            used = sum(i.sizeHint().width() for i in line) + spacing * (len(line) - 1)
            x = area.x() + (area.width() - used if self._align_right else 0)
            for item in line:
                hint = item.sizeHint()
                if apply:
                    item.setGeometry(QRect(QPoint(x, y), QSize(hint.width(), line_height)))
                x += hint.width() + spacing
            y += line_height + spacing
        return y - spacing - rect.y() + margins.bottom()


def flow(*widgets: QWidget, spacing: int = 8, align_right: bool = False) -> QWidget:
    """A container that lays ``widgets`` out left to right, wrapping as needed."""
    container = QWidget()
    layout = FlowLayout(container, spacing=spacing, align_right=align_right)
    for widget in widgets:
        layout.addWidget(widget)
    policy = QSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
    policy.setHeightForWidth(True)
    container.setSizePolicy(policy)
    return container
