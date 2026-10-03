from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtNetwork import QNetworkInterface
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from tracyy.core import (
    local_lan_address,
    seconds_to_clock,
    silent_message_box,
    source_root,
)
from tracyy.core.ids import cue_key
from tracyy.ui.qr import qr_pixmap
from tracyy.ui.surfaces import ToggleSwitch
from tracyy.widgets import frames_to_timecode


class ServerControllerMixin:
    def server_event_signature(
        self,
        snapshot: dict[str, object],
    ) -> tuple:
        return (
            str(snapshot.get("state", "")),
            str(snapshot.get("track", "")),
            str(snapshot.get("standby", "")),
            str(snapshot.get("offset", "")),
            str(
                snapshot.get(
                    "manifest_revision",
                    "",
                )
            ),
            int(
                snapshot.get(
                    "transport_sequence",
                    0,
                )
                or 0
            ),
            str(
                snapshot.get(
                    "viewer_name",
                    "",
                )
            ),
            tuple(
                snapshot.get(
                    "selected_cue_types",
                    [],
                )
            ),
        )

    def write_server_state_file(
        self,
        force: bool = False,
    ) -> None:
        now = time.monotonic()

        with self._server_state_lock:
            snapshot = dict(self._server_state)

        signature = self.server_event_signature(snapshot)
        position = float(
            snapshot.get(
                "position",
                0.0,
            )
            or 0.0
        )
        play_state = str(
            snapshot.get(
                "state",
                "STOPPED",
            )
        )

        event_changed = signature != self._last_server_event_signature

        elapsed = max(
            0.0,
            now - self._last_server_position_sent_at,
        )
        expected_position = self._last_server_position
        if self._last_server_play_state == "PLAYING":
            expected_position += elapsed

        timeline_jump = (
            self._last_server_position_sent_at > 0.0
            and abs(position - expected_position) > 0.18
        )

        if not (force or event_changed or timeline_jump):
            return

        # app_epoch_ms already corresponds to snapshot["position"].
        # Do not replace it at file-write time.
        snapshot["state_written_epoch_ms"] = time.time() * 1000.0

        self._last_server_state_write = now
        self._last_server_event_signature = signature
        self._last_server_position = position
        self._last_server_position_sent_at = now
        self._last_server_play_state = play_state

        payload = json.dumps(
            snapshot,
            ensure_ascii=False,
            separators=(",", ":"),
        )

        temporary = self.server_state_path.with_suffix(".tmp")
        try:
            temporary.write_text(
                payload,
                encoding="utf-8",
            )
            os.replace(
                temporary,
                self.server_state_path,
            )
        except Exception:
            pass

    def server_cue_type_catalog(
        self,
    ) -> list[tuple[str, str]]:
        cache_key = (
            self._cue_manifest_revision,
            tuple(
                (
                    str(name),
                    str(color),
                )
                for name, color in self.cue_type_defs
            ),
        )

        if cache_key == self._server_catalog_cache_key:
            return list(self._server_catalog_cache)

        catalog: dict[str, str] = {str(name): str(color) for name, color in self.cue_type_defs}

        for track_cues in self.cues_by_track.values():
            for cue in track_cues:
                cue_type = str(cue.get("type", "")).strip() or "Unassigned"
                cue_color = str(cue.get("color", "")).strip()

                if cue_type not in catalog or not catalog[cue_type]:
                    catalog[cue_type] = cue_color if cue_color else "#6b7280"

        self._server_catalog_cache_key = cache_key
        self._server_catalog_cache = list(catalog.items())
        return list(self._server_catalog_cache)

    def sync_server_profile_cue_types(self) -> None:
        """Auto-enable Cue Types only when first discovered."""
        catalog_names = {name for name, _color in self.server_cue_type_catalog()}

        changed = False

        for profile in self.server_profiles:
            selected = set(profile.get("cue_types", set()))
            known = set(profile.get("known_cue_types", set()))

            newly_discovered = catalog_names - known
            if newly_discovered:
                selected.update(newly_discovered)
                known.update(newly_discovered)
                profile["cue_types"] = selected
                profile["known_cue_types"] = known
                changed = True

        if changed and hasattr(self, "server_cards_layout"):
            self.rebuild_server_table()

    def available_server_networks(
        self,
    ) -> list[tuple[str, str]]:
        options: list[tuple[str, str]] = [
            (
                "Auto — All Networks",
                "0.0.0.0",
            )
        ]
        discovered: list[tuple[bool, str, str]] = []
        seen_addresses: set[str] = set()

        try:
            interfaces = QNetworkInterface.allInterfaces()
        except Exception:
            interfaces = []

        for interface in interfaces:
            try:
                flags = interface.flags()
                if not (flags & QNetworkInterface.InterfaceFlag.IsUp):
                    continue

                interface_name = (
                    interface.humanReadableName().strip()
                    or interface.name().strip()
                    or "Network"
                )

                for entry in interface.addressEntries():
                    raw_address = (
                        entry.ip()
                        .toString()
                        .split(
                            "%",
                            1,
                        )[0]
                    )

                    try:
                        address = ipaddress.ip_address(raw_address)
                    except ValueError:
                        continue

                    if address.version != 4 or address.is_unspecified or address.is_multicast:
                        continue

                    address_text = str(address)
                    if address_text in seen_addresses:
                        continue
                    seen_addresses.add(address_text)

                    is_loopback = address.is_loopback
                    label_prefix = "Local" if is_loopback else interface_name
                    discovered.append(
                        (
                            is_loopback,
                            label_prefix,
                            address_text,
                        )
                    )
            except Exception:
                continue

        fallback = local_lan_address()
        try:
            fallback_ip = ipaddress.ip_address(fallback)
        except ValueError:
            fallback_ip = None

        if (
            fallback_ip is not None
            and fallback_ip.version == 4
            and fallback not in seen_addresses
        ):
            seen_addresses.add(fallback)
            discovered.append(
                (
                    fallback_ip.is_loopback,
                    ("Detected Network" if not fallback_ip.is_loopback else "Local"),
                    fallback,
                )
            )

        if "127.0.0.1" not in seen_addresses:
            discovered.append(
                (
                    True,
                    "Local",
                    "127.0.0.1",
                )
            )

        discovered.sort(
            key=lambda item: (
                item[0],
                item[1].lower(),
                tuple(int(part) for part in item[2].split(".")),
            )
        )

        options.extend(
            (
                f"{label} — {address}",
                address,
            )
            for (
                _is_loopback,
                label,
                address,
            ) in discovered
        )
        return options

    def server_network_label(
        self,
        bind_address: str,
    ) -> str:
        normalized = str(bind_address).strip() or "0.0.0.0"
        for label, address in self.available_server_networks():
            if address == normalized:
                return label

        return f"Unavailable — {normalized}"

    def server_profile_urls(
        self,
        profile: dict[str, object],
    ) -> list[str]:
        port = int(profile["port"])
        bind_address = (
            str(
                profile.get(
                    "bind_address",
                    "0.0.0.0",
                )
            ).strip()
            or "0.0.0.0"
        )

        if bind_address != "0.0.0.0":
            return [f"http://{bind_address}:{port}"]

        addresses = [
            address
            for _label, address in self.available_server_networks()
            if address
            not in {
                "0.0.0.0",
                "127.0.0.1",
            }
        ]
        if not addresses:
            addresses = ["127.0.0.1"]

        return [f"http://{address}:{port}" for address in addresses]

    def server_profile_url_text(
        self,
        profile: dict[str, object],
    ) -> str:
        urls = self.server_profile_urls(profile)
        if not urls:
            return "Unavailable"
        if len(urls) == 1:
            return urls[0]
        return f"{urls[0]} (+{len(urls) - 1})"

    def refresh_server_networks(
        self,
    ) -> None:
        self.rebuild_server_table()
        count = max(
            0,
            len(self.available_server_networks()) - 1,
        )
        self.set_status(f"NETWORKS REFRESHED — {count} IPv4 address(es)")

    def update_server_profile_bind_address(
        self,
        profile_id: int,
        bind_address: str,
    ) -> None:
        profile = self.server_profile_by_id(profile_id)
        if profile is None:
            return

        process = profile.get("process")
        running = process is not None and process.poll() is None
        if running:
            self.rebuild_server_table()
            return

        normalized = str(bind_address).strip() or "0.0.0.0"
        profile["bind_address"] = normalized
        self.rebuild_server_table()

    def add_server_profile(self) -> None:
        profile_id = self._next_server_profile_id
        self._next_server_profile_id += 1

        used_ports = {int(profile.get("port", 0)) for profile in self.server_profiles}
        port = 8080
        while port in used_ports:
            port += 1

        profile = {
            "id": profile_id,
            "name": f"Viewer {profile_id}",
            "port": port,
            "bind_address": "0.0.0.0",
            "cue_types": {name for name, _color in self.server_cue_type_catalog()},
            "known_cue_types": {name for name, _color in self.server_cue_type_catalog()},
            "process": None,
            "state_path": (
                Path(tempfile.gettempdir()) / (f"tracyy_state_{os.getpid()}_{profile_id}.json")
            ),
            "last_signature": None,
            "last_position": 0.0,
            "last_sent_at": 0.0,
            "last_play_state": "STOPPED",
        }
        self.server_profiles.append(profile)
        self.rebuild_server_table()

    @staticmethod
    def server_profile_viewers_path(profile: dict[str, object]) -> Path:
        """Where the server process reports how many viewers it is serving.

        Derived from the state file rather than stored, so the two paths cannot
        drift apart: the server derives its side the same way from the
        ``--state-file`` it was given.
        """
        state_path = Path(profile["state_path"])
        return state_path.with_name(state_path.name + ".viewers")

    def clear_server_profile_viewers(self, profile: dict[str, object]) -> None:
        """Drop a stale count so a stopped server cannot keep reporting one."""
        try:
            self.server_profile_viewers_path(profile).unlink(missing_ok=True)
        except OSError:
            pass

    def read_server_profile_viewers(self, profile: dict[str, object]) -> int | None:
        """How many devices this server is serving, or ``None`` if unknown.

        ``None`` is a real answer, not a failure: it means the server has not
        reported yet — the first moments after start, or an older server binary
        that does not write the file. Showing "0 devices" then would be a lie
        the user cannot distinguish from the truth.
        """
        try:
            raw = self.server_profile_viewers_path(profile).read_bytes()
        except OSError:
            return None

        try:
            count = int(json.loads(raw).get("viewers"))
        except (ValueError, TypeError, AttributeError):
            return None

        return max(0, count)

    def refresh_server_viewer_counts(self, force: bool = False) -> None:
        """Update the per-card device counts from what the servers report.

        Only the labels are touched. Rebuilding the cards here would destroy
        the name field mid-typing and close the network dropdown, several times
        a second.
        """
        now = time.monotonic()
        # The servers republish at most every few seconds; reading the files at
        # the network tick's rate would be forty pointless reads a second.
        if not force and (now - self._server_viewers_last_read) < 1.0:
            return
        self._server_viewers_last_read = now

        for profile in self.server_profiles:
            label = self._server_viewer_labels.get(int(profile["id"]))
            if label is None:
                continue

            process = profile.get("process")
            running = process is not None and process.poll() is None
            count = self.read_server_profile_viewers(profile) if running else None

            try:
                label.setText(self.server_viewers_text(running, count))
                label.setProperty("live", "true" if count else "false")
                label.style().unpolish(label)
                label.style().polish(label)
            except RuntimeError:
                # The card was rebuilt between the lookup and the write; the
                # rebuild has already registered a fresh label.
                self._server_viewer_labels.pop(int(profile["id"]), None)

    @staticmethod
    def server_viewers_text(running: bool, count: int | None) -> str:
        """What the device pill says, for every state it can be in.

        A stopped server gets a dash rather than a zero: nobody is connected
        either way, but "0 thiết bị" reads as a measurement, and this one has
        not been measured. The dash also holds the row's shape, so starting the
        server changes the words without moving anything.
        """
        if not running:
            return "—"
        if count is None:
            return "Đang đếm…"
        if count == 0:
            return "Chưa có thiết bị"
        return f"{count} thiết bị"

    def server_profile_by_id(
        self,
        profile_id: int,
    ) -> dict[str, object] | None:
        for profile in self.server_profiles:
            if int(profile["id"]) == profile_id:
                return profile
        return None

    def rebuild_server_table(self) -> None:
        """Rebuild the server cards.

        Named for the table it replaced — a dozen call sites across the
        controllers ask for a refresh by this name, and renaming them all would
        be churn with no reader benefit.
        """
        layout = self.server_cards_layout

        # Every label in here is about to be destroyed with its card.
        self._server_viewer_labels.clear()

        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # setParent(None) first: deleteLater only schedules the
                # destruction, and until the event loop gets to it the old card
                # still has a parent and still paints at its last geometry —
                # the new cards were drawing straight on top of the old ones.
                widget.setParent(None)
                widget.deleteLater()

        network_options = self.available_server_networks()
        available_addresses = {address for _label, address in network_options}

        for index, profile in enumerate(self.server_profiles):
            card = self.build_server_card(
                profile,
                network_options,
                available_addresses,
            )
            layout.addWidget(card, index // 2, index % 2)

        # The trailing row soaks up the leftover height so two cards sit at the
        # top of the page instead of stretching to fill it.
        layout.setRowStretch(layout.rowCount(), 1)
        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(1, 1)

    def build_server_card(
        self,
        profile: dict[str, object],
        network_options: list,
        available_addresses: set,
    ) -> QWidget:
        profile_id = int(profile["id"])
        process = profile.get("process")
        running = process is not None and process.poll() is None

        card = QFrame()
        card.setObjectName("serverCard")
        card.setProperty("running", "true" if running else "false")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(18, 16, 18, 16)
        card_layout.setSpacing(14)

        # -- header: state, name, switch, remove ---------------------------
        header = QHBoxLayout()
        header.setSpacing(10)

        dot = QLabel()
        dot.setObjectName("serverCardDot")
        dot.setProperty("running", "true" if running else "false")
        dot.setFixedSize(9, 9)

        name_edit = QLineEdit(str(profile["name"]))
        name_edit.setObjectName("serverCardName")
        name_edit.editingFinished.connect(
            lambda profile_id=profile_id, edit=name_edit: self.update_server_profile_name(
                profile_id,
                edit.text(),
            )
        )

        state_pill = QLabel("ĐANG CHẠY" if running else "ĐANG TẮT")
        state_pill.setObjectName("serverCardState")
        state_pill.setProperty("running", "true" if running else "false")

        power = ToggleSwitch()
        power.set_checked_silently(running)
        power.toggled.connect(
            lambda checked, profile_id=profile_id: self.set_server_profile_running(
                profile_id,
                checked,
            )
        )

        remove_button = QPushButton("✕")
        remove_button.setObjectName("serverCardRemove")
        remove_button.setFixedSize(26, 26)
        remove_button.setCursor(Qt.CursorShape.PointingHandCursor)
        remove_button.clicked.connect(
            lambda _checked=False, profile_id=profile_id: self.remove_server_profile(
                profile_id
            )
        )

        header.addWidget(dot)
        header.addWidget(name_edit, 1)
        header.addWidget(state_pill)
        header.addWidget(power)
        header.addWidget(remove_button)
        card_layout.addLayout(header)

        # -- the address, which is the whole point of the page --------------
        address_box = QVBoxLayout()
        address_box.setSpacing(6)

        address_caption = QLabel("ĐỊA CHỈ CHO CREW")
        address_caption.setObjectName("serverCardCaption")

        url_text = self.server_profile_url_text(profile)
        address_value = QLabel(url_text)
        address_value.setObjectName("serverCardAddress")
        address_value.setProperty("running", "true" if running else "false")
        address_value.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )

        address_actions = QHBoxLayout()
        address_actions.setSpacing(8)

        copy_button = QPushButton("Chép link")
        copy_button.setObjectName("serverCardAction")
        copy_button.setCursor(Qt.CursorShape.PointingHandCursor)
        copy_button.clicked.connect(
            lambda _checked=False, text=url_text: self.copy_server_url(text)
        )

        open_button = QPushButton("Mở thử")
        open_button.setObjectName("serverCardAction")
        open_button.setCursor(Qt.CursorShape.PointingHandCursor)
        open_button.setEnabled(running)
        open_button.clicked.connect(
            lambda _checked=False, text=url_text: self.open_server_url(text)
        )

        # How many crew are actually on this address — the answer to "did the
        # link work?", which is otherwise a question you can only ask out loud.
        viewer_count = self.read_server_profile_viewers(profile) if running else None
        viewers_label = QLabel(self.server_viewers_text(running, viewer_count))
        viewers_label.setObjectName("serverCardViewers")
        viewers_label.setProperty("running", "true" if running else "false")
        # Set here as well as in the refresh: a card built with viewers already
        # connected would otherwise paint dim until the next tick and read as a
        # flicker on every rebuild.
        viewers_label.setProperty("live", "true" if viewer_count else "false")
        self._server_viewer_labels[profile_id] = viewers_label

        address_actions.addWidget(copy_button)
        address_actions.addWidget(open_button)
        address_actions.addStretch()
        address_actions.addWidget(viewers_label)

        address_box.addWidget(address_caption)
        address_box.addWidget(address_value)
        address_box.addLayout(address_actions)

        address_row = QHBoxLayout()
        address_row.setSpacing(16)
        address_row.addLayout(address_box, 1)
        address_row.addWidget(self.build_server_qr(url_text, running))
        card_layout.addLayout(address_row)

        # -- settings, one row, three fields --------------------------------
        divider = QFrame()
        divider.setObjectName("serverCardDivider")
        divider.setFrameShape(QFrame.Shape.HLine)
        card_layout.addWidget(divider)

        settings = QGridLayout()
        settings.setHorizontalSpacing(14)
        settings.setVerticalSpacing(5)

        port_edit = QLineEdit(str(profile["port"]))
        port_edit.setObjectName("serverCardField")
        port_edit.setEnabled(not running)
        port_edit.editingFinished.connect(
            lambda profile_id=profile_id, edit=port_edit: self.update_server_profile_port(
                profile_id,
                edit.text(),
            )
        )

        selected_address = (
            str(profile.get("bind_address", "0.0.0.0")).strip() or "0.0.0.0"
        )
        network_combo = QComboBox()
        network_combo.setObjectName("serverCardField")
        network_combo.setView(QListView())
        network_combo.setEnabled(not running)
        for label, address in network_options:
            network_combo.addItem(label, address)
        if selected_address not in available_addresses:
            network_combo.addItem(f"Unavailable — {selected_address}", selected_address)
        selected_index = network_combo.findData(selected_address)
        network_combo.setCurrentIndex(max(0, selected_index))
        network_combo.currentIndexChanged.connect(
            lambda _index, profile_id=profile_id, combo=network_combo: (
                self.update_server_profile_bind_address(
                    profile_id,
                    str(combo.currentData() or "0.0.0.0"),
                )
            )
        )

        types_button = QPushButton(self.server_profile_types_text(profile))
        types_button.setObjectName("serverCardField")
        types_button.setCursor(Qt.CursorShape.PointingHandCursor)
        types_button.clicked.connect(
            lambda _checked=False, profile_id=profile_id: self.edit_server_profile_types(
                profile_id
            )
        )

        for column, (caption, widget) in enumerate(
            (
                ("PORT", port_edit),
                ("NETWORK", network_combo),
                ("CUE TYPES", types_button),
            )
        ):
            label = QLabel(caption)
            label.setObjectName("serverCardCaption")
            settings.addWidget(label, 0, column)
            settings.addWidget(widget, 1, column)
            settings.setColumnStretch(column, 2 if column == 1 else 1)

        card_layout.addLayout(settings)

        if running:
            lock_hint = QLabel("Đang chạy — tắt server trước khi đổi Port hoặc Network.")
            lock_hint.setObjectName("serverCardHint")
            lock_hint.setWordWrap(True)
            card_layout.addWidget(lock_hint)

        return card

    def build_server_qr(self, url: str, running: bool) -> QWidget:
        """The address as a QR, so nobody has to type an IP into a phone.

        Only while the server is up: a code that resolves to a refused
        connection is worse than no code — the person who scanned it has no way
        to tell a stopped server from a wrong address.
        """
        holder = QLabel()
        holder.setObjectName("serverCardQr")
        holder.setFixedSize(108, 108)
        holder.setAlignment(Qt.AlignmentFlag.AlignCenter)

        if not running:
            holder.setProperty("state", "idle")
            holder.setText("QR hiện\nkhi bật")
            return holder

        try:
            ratio = max(1.0, float(self.devicePixelRatioF()))
        except Exception:
            ratio = 2.0

        pixmap = qr_pixmap(url, 100, ratio=ratio)
        if pixmap is None:
            holder.setProperty("state", "idle")
            holder.setText("QR không\nkhả dụng")
            return holder

        holder.setProperty("state", "live")
        holder.setPixmap(pixmap)
        return holder

    def copy_server_url(self, url: str) -> None:
        text = str(url or "").strip()
        if not text:
            return
        QApplication.clipboard().setText(text)
        self.set_status(f"ĐÃ CHÉP — {text}")

    def open_server_url(self, url: str) -> None:
        text = str(url or "").strip()
        if not text:
            return
        QDesktopServices.openUrl(QUrl(text))

    def remove_server_profile(self, profile_id: object) -> None:
        """Delete one server by identity rather than by selected row."""
        target = int(profile_id)
        for index, profile in enumerate(self.server_profiles):
            if int(profile["id"]) != target:
                continue

            self.stop_server_profile(target)
            self.server_profiles.pop(index)

            try:
                Path(profile["state_path"]).unlink(missing_ok=True)
            except Exception:
                pass
            self.clear_server_profile_viewers(profile)

            self.rebuild_server_table()
            return

    def server_profile_types_text(
        self,
        profile: dict[str, object],
    ) -> str:
        selected = set(profile.get("cue_types", set()))
        catalog = self.server_cue_type_catalog()
        ordered = [name for name, _color in catalog if name in selected]

        if not ordered:
            return "No cue types"

        if len(ordered) == len(catalog) and catalog:
            return "All cue types"

        return ", ".join(ordered)

    def update_server_profile_name(
        self,
        profile_id: int,
        value: str,
    ) -> None:
        profile = self.server_profile_by_id(profile_id)
        if profile is None:
            return
        profile["name"] = value.strip() or f"Viewer {profile_id}"

    def update_server_profile_port(
        self,
        profile_id: int,
        value: str,
    ) -> None:
        profile = self.server_profile_by_id(profile_id)
        if profile is None:
            return

        try:
            port = int(value.strip())
            if not 1 <= port <= 65535:
                raise ValueError
            if any(
                int(other["id"]) != profile_id and int(other["port"]) == port
                for other in self.server_profiles
            ):
                raise ValueError
        except ValueError:
            self.show_error("Port không hợp lệ hoặc đang trùng với server khác.")
            self.rebuild_server_table()
            return

        profile["port"] = port
        self.rebuild_server_table()

    def edit_server_profile_types(
        self,
        profile_id: int,
    ) -> None:
        profile = self.server_profile_by_id(profile_id)
        if profile is None:
            return

        dialog = QDialog(self)
        dialog.setWindowTitle(f"Cue Types — {profile['name']}")
        dialog.resize(420, 500)
        layout = QVBoxLayout(dialog)

        instruction = QLabel("Chọn Cue Type được phép hiện trên server này:")
        layout.addWidget(instruction)

        checkboxes: dict[str, QCheckBox] = {}

        self.sync_server_profile_cue_types()
        catalog = self.server_cue_type_catalog()
        selected = set(profile.get("cue_types", set()))

        for cue_type, color in catalog:
            checkbox = QCheckBox(cue_type)
            checkbox.setChecked(cue_type in selected)
            checkbox.setStyleSheet(f"QCheckBox {{ color: {color}; }}")
            checkboxes[cue_type] = checkbox
            layout.addWidget(checkbox)

        buttons = QHBoxLayout()
        all_button = QPushButton("Select All")
        none_button = QPushButton("Select None")
        done_button = QPushButton("Done")
        buttons.addWidget(all_button)
        buttons.addWidget(none_button)
        buttons.addStretch()
        buttons.addWidget(done_button)
        layout.addLayout(buttons)

        all_button.clicked.connect(
            lambda: [checkbox.setChecked(True) for checkbox in checkboxes.values()]
        )
        none_button.clicked.connect(
            lambda: [checkbox.setChecked(False) for checkbox in checkboxes.values()]
        )
        done_button.clicked.connect(dialog.accept)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        profile["cue_types"] = {
            cue_type for cue_type, checkbox in checkboxes.items() if checkbox.isChecked()
        }
        profile["known_cue_types"] = set(checkboxes)
        self.rebuild_server_table()
        self.write_server_profile_state(
            profile,
            force=True,
        )

    def remove_selected_server_profile(self) -> None:
        """Retained for callers that still ask by name; removes the last card."""
        if self.server_profiles:
            self.remove_server_profile(int(self.server_profiles[-1]["id"]))

    def set_server_profile_running(
        self,
        profile_id: object,
        running: bool,
    ) -> None:
        """Route the switch to whichever half of the old pair it replaced."""
        if running:
            self.start_server_profile(profile_id)
        else:
            self.stop_server_profile(profile_id)

    def start_server_profile(
        self,
        profile_id: int,
    ) -> None:
        status = str(self.license_result.status).strip().upper()
        if status in {
            "BLOCKED",
            "REVOKED",
        }:
            silent_message_box(
                self,
                "Tracyy License",
                (f"Server đã bị khóa vì trạng thái license là {status}."),
            )
            return

        if not self.license_allows_operation():
            return

        profile = self.server_profile_by_id(profile_id)
        if profile is None:
            return

        process = profile.get("process")
        if process is not None and process.poll() is None:
            return

        bind_address = (
            str(
                profile.get(
                    "bind_address",
                    "0.0.0.0",
                )
            ).strip()
            or "0.0.0.0"
        )

        available_addresses = {address for _label, address in self.available_server_networks()}
        if bind_address != "0.0.0.0" and bind_address not in available_addresses:
            self.show_error(
                "Network Interface không còn khả dụng: "
                f"{bind_address}. "
                "Nhấn Refresh Networks và chọn lại mạng."
            )
            self.rebuild_server_table()
            return

        if getattr(sys, "frozen", False):
            server_filename = "TracyyCueServer.exe" if os.name == "nt" else "TracyyCueServer"
            server_program = Path(sys.executable).resolve().parent / server_filename
            server_command = [
                str(server_program),
                "--bind-address",
                bind_address,
                "--port",
                str(int(profile["port"])),
                "--state-file",
                str(profile["state_path"]),
            ]
            server_cwd = server_program.parent
        else:
            # Run the viewer as a module so it resolves through the package
            # rather than a loose script path. V1 pointed at a file beside
            # main.py; in V2 the server lives inside ``tracyy.server``.
            server_program = source_root() / "tracyy" / "server" / "cue_viewer.py"
            server_command = [
                sys.executable,
                "-m",
                "tracyy.server.cue_viewer",
                "--bind-address",
                bind_address,
                "--port",
                str(int(profile["port"])),
                "--state-file",
                str(profile["state_path"]),
            ]
            server_cwd = source_root()

        if not server_program.exists():
            self.show_error("Không tìm thấy chương trình Tracyy Cue Server.")
            return

        current_position = self.transport.current_position()
        current_duration = self.transport.duration
        current_fps = int(self.fps.currentText())
        current_timecode = frames_to_timecode(
            round(self.media_to_timecode_seconds(current_position) * current_fps),
            current_fps,
        )
        self.update_server_snapshot(
            current_position,
            current_duration,
            current_fps,
            current_timecode,
            force=True,
        )
        self.write_server_profile_state(
            profile,
            force=True,
        )

        # Before the process starts, never after: the server publishes a zero
        # the moment it binds, and deleting the file afterwards would throw
        # that away and leave the card counting nothing.
        self.clear_server_profile_viewers(profile)

        creation_flags = 0
        if os.name == "nt":
            creation_flags = (
                subprocess.CREATE_NO_WINDOW
                if hasattr(
                    subprocess,
                    "CREATE_NO_WINDOW",
                )
                else 0
            )

        try:
            process = subprocess.Popen(
                server_command,
                cwd=str(server_cwd),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creation_flags,
            )
        except Exception as exc:
            self.show_error(f"Không thể chạy server: {exc}")
            return

        profile["process"] = process
        self.rebuild_server_table()

        QTimer.singleShot(
            350,
            lambda profile_id=profile_id: self.finish_server_profile_start(profile_id),
        )

    def finish_server_profile_start(
        self,
        profile_id: int,
    ) -> None:
        profile = self.server_profile_by_id(profile_id)
        if profile is None:
            return

        process = profile.get("process")
        if process is None:
            return

        bind_address = str(
            profile.get(
                "bind_address",
                "0.0.0.0",
            )
        )
        if process.poll() is not None:
            profile["process"] = None
            self.rebuild_server_table()
            self.show_error(
                f"Server {profile['name']} không mở được {bind_address}:{profile['port']}."
            )
            return

        address = self.server_profile_url_text(profile)
        self.rebuild_server_table()
        self.set_status(f"SERVER RUNNING — {profile['name']} — {address}")

    def stop_server_profile(
        self,
        profile_id: int,
    ) -> None:
        profile = self.server_profile_by_id(profile_id)
        if profile is None:
            return

        process = profile.get("process")
        profile["process"] = None
        self.clear_server_profile_viewers(profile)

        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=1.0)
                except Exception:
                    pass

        self.rebuild_server_table()

    def stop_all_server_profiles(self) -> None:
        for profile in list(self.server_profiles):
            process = profile.get("process")
            profile["process"] = None
            self.clear_server_profile_viewers(profile)
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1.5)
                except subprocess.TimeoutExpired:
                    process.kill()

    def filtered_snapshot_for_profile(
        self,
        profile: dict[str, object],
    ) -> dict[str, object]:
        self.sync_server_profile_cue_types()

        snapshot = dict(self._last_server_snapshot)
        selected_types = set(
            profile.get(
                "cue_types",
                set(),
            )
        )
        selected_key = tuple(sorted(str(value) for value in selected_types))
        base_revision = str(
            snapshot.get(
                "manifest_revision",
                "0",
            )
        )
        filter_key = (
            base_revision,
            selected_key,
        )

        if profile.get("_filtered_manifest_key") != filter_key:
            filtered_cues = [
                cue
                for cue in snapshot.get(
                    "cues",
                    [],
                )
                if (
                    str(
                        cue.get(
                            "type",
                            "",
                        )
                    ).strip()
                    or "Unassigned"
                )
                in selected_types
            ]
            profile["_filtered_manifest_key"] = filter_key
            profile["_filtered_manifest_cues"] = filtered_cues
        else:
            filtered_cues = list(
                profile.get(
                    "_filtered_manifest_cues",
                    [],
                )
            )

        filter_digest = hashlib.sha1("\x1f".join(selected_key).encode("utf-8")).hexdigest()[:12]

        snapshot["viewer_name"] = str(profile["name"])
        snapshot["selected_cue_types"] = list(selected_key)
        snapshot["manifest_revision"] = f"{base_revision}:{filter_digest}"
        snapshot["cues"] = filtered_cues
        return snapshot

    def write_server_profile_state(
        self,
        profile: dict[str, object],
        force: bool = False,
    ) -> None:
        snapshot = self.filtered_snapshot_for_profile(profile)
        now = time.monotonic()
        signature = self.server_event_signature(snapshot)
        position = float(
            snapshot.get(
                "position",
                0.0,
            )
            or 0.0
        )
        play_state = str(
            snapshot.get(
                "state",
                "STOPPED",
            )
        )

        last_signature = profile.get("last_signature")
        last_position = float(
            profile.get(
                "last_position",
                0.0,
            )
        )
        last_sent_at = float(
            profile.get(
                "last_sent_at",
                0.0,
            )
        )
        last_play_state = str(
            profile.get(
                "last_play_state",
                "STOPPED",
            )
        )

        elapsed = max(
            0.0,
            now - last_sent_at,
        )
        expected_position = last_position
        if last_play_state == "PLAYING":
            expected_position += elapsed

        timeline_jump = last_sent_at > 0.0 and abs(position - expected_position) > 0.18

        if not (force or signature != last_signature or timeline_jump):
            return

        # app_epoch_ms belongs to the transport anchor.
        snapshot["state_written_epoch_ms"] = time.time() * 1000.0

        profile["last_signature"] = signature
        profile["last_position"] = position
        profile["last_sent_at"] = now
        profile["last_play_state"] = play_state

        state_path = Path(profile["state_path"])
        temporary = state_path.with_suffix(".tmp")
        try:
            temporary.write_text(
                json.dumps(
                    snapshot,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            os.replace(
                temporary,
                state_path,
            )
        except Exception:
            pass

    def build_server_cue_manifest(
        self,
        path: str,
        fps: int,
    ) -> list[dict[str, object]]:
        offset_frames = self.track_offset_frames(path) if path else 0
        cache_key = (
            path,
            int(fps),
            int(offset_frames),
            self._cue_manifest_revision,
            tuple(
                (
                    str(name),
                    str(color),
                )
                for name, color in self.cue_type_defs
            ),
        )

        if cache_key == self._server_manifest_cache_key:
            return self._server_manifest_cache

        catalog_colors = dict(self.server_cue_type_catalog())
        manifest: list[dict[str, object]] = []

        for cue in self._cue_sorted_cache:
            cue_seconds = float(cue.get("seconds", 0.0))
            cue_type = str(cue.get("type", "")).strip() or "Unassigned"
            cue_color = str(
                cue.get("color")
                or catalog_colors.get(
                    cue_type,
                    "#6b7280",
                )
            )
            cue_tc_seconds = self.media_to_timecode_seconds(
                cue_seconds,
                path,
            )
            manifest.append(
                {
                    "id": cue_key(cue.get("id")),
                    "seconds": cue_seconds,
                    "name": str(cue.get("name", "")),
                    "label": str(cue.get("label") or cue.get("name") or ""),
                    "type": cue_type,
                    "color": cue_color,
                    "timecode": frames_to_timecode(
                        round(cue_tc_seconds * fps),
                        fps,
                    ),
                }
            )

        self._server_manifest_cache_key = cache_key
        self._server_manifest_cache = manifest
        return self._server_manifest_cache

    def update_server_snapshot(
        self,
        position: float,
        duration: float,
        fps: int,
        timecode: str,
        force: bool = False,
    ) -> None:
        now = time.monotonic()
        active_profiles = [
            profile
            for profile in self.server_profiles
            if (profile.get("process") is not None and profile["process"].poll() is None)
        ]

        refresh_interval = 0.05 if active_profiles else 0.25
        if not force and (now - self._server_snapshot_last_refresh) < refresh_interval:
            return

        self._server_snapshot_last_refresh = now
        self.refresh_server_viewer_counts()
        self.sync_server_profile_cue_types()

        path = self.main_track_path()
        offset_frames = self.track_offset_frames(path) if path else 0
        offset_tc = frames_to_timecode(
            offset_frames,
            fps,
        )
        manifest = (
            self.build_server_cue_manifest(
                path,
                fps,
            )
            if path
            else []
        )

        if (
            self.input_timecode_chase_enabled()
            and self._input_timecode_locked
            and self.main_track_path()
        ) or self.transport.running:
            play_state = "PLAYING"
        elif duration > 0.0 and position >= duration - 0.05:
            # Ran to the end rather than being paused mid-show. The viewer
            # paints this red, so the back of a dark room can tell a finished
            # track from one somebody stopped on purpose.
            play_state = "ENDED"
        elif position > 0.001:
            play_state = "PAUSED"
        else:
            play_state = "STOPPED"

        track_title = (
            self.playlist_display_title_for_path(path) if path else "No track selected"
        )
        standby_title = (
            self.playlist_display_title_for_path(self.standby_path) if self.standby_path else ""
        )
        manifest_revision = f"{self._cue_manifest_revision}:{fps}:{offset_frames}"

        transport_signature = (
            path,
            play_state,
            manifest_revision,
        )
        anchor_ready = self._server_transport_anchor_monotonic > 0.0
        expected_position = self._server_transport_anchor_position
        if anchor_ready and self._server_transport_anchor_state == "PLAYING":
            expected_position += max(
                0.0,
                now - self._server_transport_anchor_monotonic,
            )

        timeline_jump = anchor_ready and abs(position - expected_position) > 0.18
        transport_event = (
            force
            or not anchor_ready
            or (transport_signature != self._server_transport_signature)
            or timeline_jump
        )

        if transport_event:
            self._server_transport_sequence += 1
            self._server_transport_signature = transport_signature
            self._server_transport_anchor_position = float(position)
            self._server_transport_anchor_monotonic = now
            self._server_transport_anchor_state = play_state

        app_epoch_ms = time.time() * 1000.0

        self._last_server_snapshot = {
            "timecode": timecode,
            "state": play_state,
            "fps": fps,
            "offset": offset_tc,
            "offset_seconds": (self.track_offset_seconds(path) if path else 0.0),
            "elapsed": seconds_to_clock(position),
            "duration": seconds_to_clock(duration),
            "duration_seconds": duration,
            "track": track_title,
            "track_path_key": path,
            "standby": standby_title,
            "position": float(position),
            "app_epoch_ms": app_epoch_ms,
            # Compatibility with the existing web clock code.
            "server_epoch_ms": app_epoch_ms,
            "server_monotonic": now,
            "session_id": (self._server_session_id),
            "transport_sequence": (self._server_transport_sequence),
            "manifest_revision": (manifest_revision),
            "cues": manifest,
        }

        with self._server_state_lock:
            self._server_state = dict(self._last_server_snapshot)

        for profile in active_profiles:
            self.write_server_profile_state(profile)
