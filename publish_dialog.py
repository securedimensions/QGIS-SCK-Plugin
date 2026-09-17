# Copyright (C) 2026 Secure Dimensions GmbH, Munich, Germany.
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, see <https://www.gnu.org/licenses/>.

"""Consent dialog shown before STAplus publishing: Party name, license, attribution."""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .sta import is_attribution_license, is_template_license


class PublishConsentDialog(QDialog):
    """Collect displayName, a template License, optional attribution text, and consent."""

    def __init__(self, licenses, display_name=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Start publishing")
        self.setMinimumWidth(460)
        self._licenses = [item for item in (licenses or []) if is_template_license(item)]
        if not self._licenses:
            self._licenses = list(licenses or [])

        intro = QLabel(
            "These settings are written to the STAplus service with your observations."
        )
        intro.setWordWrap(True)

        self.name_edit = QLineEdit()
        self.name_edit.setText(str(display_name or "").strip())
        self.name_edit.setPlaceholderText("Public name for your Party")

        self.license_combo = QComboBox()
        for item in self._licenses:
            iot_id = str(item.get("@iot.id") or "")
            name = str(item.get("name") or iot_id)
            label = "%s (%s)" % (name, iot_id) if iot_id and name != iot_id else name
            self.license_combo.addItem(label, iot_id)
        default = self.license_combo.findData("CC_BY")
        if default >= 0:
            self.license_combo.setCurrentIndex(default)

        self.attribution_edit = QTextEdit()
        self.attribution_edit.setMaximumHeight(72)
        self.attribution_label = QLabel("Attribution text")
        self.attribution_hint = QLabel(
            "This license requires attribution. The text is stored on a License instance "
            "created from the selected template."
        )
        self.attribution_hint.setWordWrap(True)

        self.consent = QCheckBox()
        consent_text = QLabel(
            "I agree that this display name is published to the SensorThings service "
            "and that anyone using the service can read it."
        )
        consent_text.setWordWrap(True)
        consent_text.setBuddy(self.consent)
        try:
            align_top = Qt.AlignmentFlag.AlignTop
        except AttributeError:
            align_top = Qt.AlignTop
        consent_row = QWidget()
        consent_layout = QHBoxLayout(consent_row)
        consent_layout.setContentsMargins(0, 0, 0, 0)
        consent_layout.addWidget(self.consent, 0, align_top)
        consent_layout.addWidget(consent_text, 1)
        consent_text.mouseReleaseEvent = lambda _event: self.consent.toggle()

        self.buttons = QDialogButtonBox()
        try:
            accept_role = QDialogButtonBox.ButtonRole.AcceptRole
            cancel = QDialogButtonBox.StandardButton.Cancel
        except AttributeError:
            accept_role = QDialogButtonBox.AcceptRole
            cancel = QDialogButtonBox.Cancel
        self.ok_btn = self.buttons.addButton("Start publishing", accept_role)
        self.buttons.addButton(cancel)
        self.ok_btn.setEnabled(False)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        form = QFormLayout()
        form.addRow("Party display name", self.name_edit)
        form.addRow("License", self.license_combo)
        form.addRow(self.attribution_label, self.attribution_edit)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addLayout(form)
        layout.addWidget(self.attribution_hint)
        layout.addWidget(consent_row)
        layout.addWidget(self.buttons)

        self.name_edit.textChanged.connect(self._sync)
        self.license_combo.currentIndexChanged.connect(self._sync)
        self.attribution_edit.textChanged.connect(self._sync)
        self.consent.toggled.connect(self._sync)
        self._sync()

    def selected_license(self):
        iot_id = self.license_combo.currentData()
        for item in self._licenses:
            if str(item.get("@iot.id") or "") == str(iot_id or ""):
                return item
        return None

    def values(self):
        license_entity = self.selected_license() or {}
        return {
            "display_name": (self.name_edit.text() or "").strip(),
            "license_id": str(license_entity.get("@iot.id") or ""),
            "attribution_text": (self.attribution_edit.toPlainText() or "").strip(),
            "needs_attribution": is_attribution_license(license_entity),
        }

    def _sync(self):
        license_entity = self.selected_license() or {}
        needs = is_attribution_license(license_entity)
        self.attribution_label.setVisible(needs)
        self.attribution_edit.setVisible(needs)
        self.attribution_hint.setVisible(needs)
        values = self.values()
        ready = bool(values["display_name"] and values["license_id"] and self.consent.isChecked())
        if needs:
            ready = ready and bool(values["attribution_text"])
        self.ok_btn.setEnabled(ready)
