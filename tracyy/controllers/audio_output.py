from __future__ import annotations

from pathlib import Path

import sounddevice as sd
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from tracyy.core import tracyy_resource_root
from tracyy.ui.transport_icons import transport_icon


class AudioOutputControllerMixin:
    def on_ltc_level_changed(self, value: int) -> None:
        self.ltc_level_value.setText(f"{value} %")
        with self.transport._lock:
            self.transport.ltc_level = max(
                0.05,
                min(1.0, value / 100.0),
            )

    def on_music_level_changed(self, value: int) -> None:
        self.music_level_value.setText(f"{value} %")
        gain = max(0.0, min(1.0, value / 100.0))
        with self.transport._lock:
            self.transport.music_gain = gain
        self.transport.audio_output.setVolume(gain)

    def available_output_devices(
        self,
    ) -> list[tuple[int, str, str, int]]:
        devices: list[tuple[int, str, str, int]] = []

        default_output = None
        try:
            value = sd.default.device[1]
            if value is not None and int(value) >= 0:
                default_output = int(value)
        except Exception:
            default_output = None

        for index, device in enumerate(sd.query_devices()):
            channels = int(
                device.get(
                    "max_output_channels",
                    0,
                )
            )
            if channels <= 0:
                continue

            name = str(
                device.get(
                    "name",
                    f"Device {index}",
                )
            )
            label = f"{name} — {channels} outputs"
            if index == default_output:
                label += "  [System Default]"

            devices.append(
                (
                    int(index),
                    label,
                    name,
                    channels,
                )
            )

        return devices

    def populate_output_device_combo(
        self,
        combo: QComboBox,
        preferred_index: int | None = None,
        preferred_name: str = "",
        prefer_name: bool = False,
    ) -> None:
        combo.blockSignals(True)
        combo.clear()

        devices = self.available_output_devices()

        for (
            device_index,
            label,
            device_name,
            channels,
        ) in devices:
            combo.addItem(
                label,
                {
                    "index": device_index,
                    "name": device_name,
                    "channels": channels,
                },
            )

        found = -1

        # A rescan renumbers the devices, so the remembered index can now
        # belong to a different card. The name is the only stable handle
        # across an unplug and replug.
        if prefer_name and preferred_name:
            preferred_lower = str(preferred_name).strip().lower()
            for row in range(combo.count()):
                data = combo.itemData(row)
                if (
                    isinstance(data, dict)
                    and str(data.get("name", "")).strip().lower() == preferred_lower
                ):
                    found = row
                    break

        if found < 0 and preferred_index is not None:
            for row in range(combo.count()):
                data = combo.itemData(row)
                if isinstance(
                    data,
                    dict,
                ) and int(
                    data.get(
                        "index",
                        -1,
                    )
                ) == int(preferred_index):
                    found = row
                    break

        if found < 0 and preferred_name:
            preferred_lower = str(preferred_name).strip().lower()
            for row in range(combo.count()):
                data = combo.itemData(row)
                if (
                    isinstance(
                        data,
                        dict,
                    )
                    and str(
                        data.get(
                            "name",
                            "",
                        )
                    )
                    .strip()
                    .lower()
                    == preferred_lower
                ):
                    found = row
                    break

        if found < 0 and combo.count() > 0:
            # Devices already claimed by OTHER rows. The row being populated is
            # itself already in output_device_rows, and its combo lands on item
            # 0 the moment the items are added — so a snapshot taken here
            # reported this row's own tentative pick as taken, and the search
            # skipped straight past it. That is why a fresh start selected the
            # second card in the list instead of the first.
            used: set[int] = set()
            for existing in self.output_device_rows:
                other = existing.get("device_combo")
                if not isinstance(other, QComboBox) or other is combo:
                    continue
                data = other.currentData()
                if isinstance(data, dict):
                    used.add(int(data.get("index", -1)))

            def _row_for_device(device_index: int) -> int:
                for row in range(combo.count()):
                    data = combo.itemData(row)
                    if isinstance(data, dict) and int(data.get("index", -1)) == device_index:
                        return row
                return -1

            # With nothing remembered, send audio where the machine already
            # sends it. Falling back to "first free card in enumeration order"
            # picked whatever the OS happened to list first — a virtual NDI
            # sink on this machine — and the operator only found out when the
            # speakers stayed silent.
            default_index = None
            try:
                value = sd.default.device[1]
                if value is not None and int(value) >= 0:
                    default_index = int(value)
            except Exception:
                default_index = None

            if default_index is not None and default_index not in used:
                found = _row_for_device(default_index)

            if found < 0:
                for row in range(combo.count()):
                    data = combo.itemData(row)
                    index = int(data.get("index", -1)) if isinstance(data, dict) else -1
                    if index not in used:
                        found = row
                        break

            if found < 0:
                found = 0

        if found >= 0:
            combo.setCurrentIndex(found)

        combo.blockSignals(False)

    def refresh_output_device_row_titles(
        self,
    ) -> None:
        for number, row in enumerate(
            self.output_device_rows,
            start=1,
        ):
            group = row.get("group")
            if isinstance(
                group,
                QGroupBox,
            ):
                group.setTitle(f"Output Device {number}")

            remove_button = row.get("remove_button")
            if isinstance(
                remove_button,
                QPushButton,
            ):
                remove_button.setEnabled(len(self.output_device_rows) > 1)

    def clear_output_device_rows(
        self,
    ) -> None:
        for row in list(self.output_device_rows):
            group = row.get("group")
            if isinstance(
                group,
                QWidget,
            ):
                self.output_device_rows_layout.removeWidget(group)
                group.deleteLater()

        self.output_device_rows = []

    def add_output_device_row(
        self,
        _checked: bool = False,
        device_index: int | None = None,
        assignments: list[str] | None = None,
        device_name: str = "",
        prefer_name: bool = False,
    ) -> None:
        del _checked

        self.suspend_output_live_apply(True)

        group = QGroupBox()
        group_layout = QVBoxLayout(group)

        header = QHBoxLayout()

        device_combo = QComboBox()
        device_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        # A 430px floor stopped the panel shrinking: below that the row could
        # not give the width back and the group clipped instead of reflowing.
        # Device names are long, so it still asks for room — it just yields.
        device_combo.setMinimumWidth(180)

        remove_button = QPushButton("Remove Device")
        remove_button.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Fixed,
        )

        device_power = QPushButton()
        device_power.setObjectName("channelPowerButton")
        device_power.setCheckable(True)
        device_power.setFixedWidth(56)
        device_power.setCursor(Qt.CursorShape.PointingHandCursor)

        soundcard_label = QLabel("Soundcard")
        soundcard_label.setMinimumWidth(self.OUTPUT_ROW_LABEL_WIDTH)
        soundcard_label.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Fixed,
        )
        header.addWidget(soundcard_label)
        header.addWidget(
            device_combo,
            1,
        )
        header.addWidget(device_power)
        header.addWidget(remove_button)
        group_layout.addLayout(header)

        channel_container = QWidget()
        channel_layout = QVBoxLayout(channel_container)
        channel_layout.setContentsMargins(
            0,
            4,
            0,
            0,
        )
        channel_layout.setSpacing(6)
        channel_layout.addStretch(1)

        group_layout.addWidget(channel_container)

        row: dict[str, object] = {
            "group": group,
            "device_combo": device_combo,
            "remove_button": remove_button,
            "channel_container": channel_container,
            "channel_layout": channel_layout,
            "selectors": [],
            "pending_assignments": list(assignments or []),
        }

        self.output_device_rows.append(row)

        insert_index = max(
            0,
            self.output_device_rows_layout.count() - 1,
        )
        self.output_device_rows_layout.insertWidget(
            insert_index,
            group,
        )

        self.populate_output_device_combo(
            device_combo,
            preferred_index=device_index,
            preferred_name=device_name,
            prefer_name=prefer_name,
        )

        row["device_power"] = device_power
        device_power.toggled.connect(
            lambda checked, current=row: self.on_output_device_power_toggled(
                current,
                checked,
            )
        )

        self.suspend_output_live_apply(False)

        device_combo.currentIndexChanged.connect(
            lambda _index, current=row: self.on_output_device_combo_changed(current)
        )
        remove_button.clicked.connect(
            lambda _checked=False, current=row: self.remove_output_device_row(current)
        )

        self.rebuild_output_device_row_channels(
            row,
            assignments=list(assignments or []),
        )
        self.refresh_output_device_row_titles()

        if hasattr(
            self,
            "mark_project_dirty",
        ):
            self.mark_project_dirty()

    def remove_output_device_row(
        self,
        row: dict[str, object],
    ) -> None:
        if row not in self.output_device_rows:
            return

        if len(self.output_device_rows) <= 1:
            self.set_status("AT LEAST ONE OUTPUT DEVICE IS REQUIRED")
            return

        self.output_device_rows.remove(row)

        group = row.get("group")
        if isinstance(
            group,
            QWidget,
        ):
            self.output_device_rows_layout.removeWidget(group)
            group.deleteLater()

        self.refresh_output_device_row_titles()
        self.mark_project_dirty()

    def rebuild_output_device_row_channels(
        self,
        row: dict[str, object],
        assignments: list[str] | None = None,
    ) -> None:
        layout = row.get("channel_layout")
        combo = row.get("device_combo")

        if not isinstance(
            layout,
            QVBoxLayout,
        ) or not isinstance(
            combo,
            QComboBox,
        ):
            return

        self.suspend_output_live_apply(True)

        previous = (
            list(assignments)
            if assignments is not None
            else [
                selector.currentText()
                for selector in row.get(
                    "selectors",
                    [],
                )
                if isinstance(
                    selector,
                    QComboBox,
                )
            ]
        )

        while layout.count() > 1:
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        selectors: list[QComboBox] = []

        data = combo.currentData()
        channels = (
            int(
                data.get(
                    "channels",
                    0,
                )
            )
            if isinstance(
                data,
                dict,
            )
            else 0
        )

        is_first_row = bool(self.output_device_rows) and self.output_device_rows[0] is row

        for channel_index in range(channels):
            row_widget = QWidget()
            channel_row = QHBoxLayout(row_widget)
            channel_row.setContentsMargins(
                0,
                0,
                0,
                0,
            )

            label = QLabel(f"Output Ch {channel_index + 1}")
            label.setMinimumWidth(self.OUTPUT_ROW_LABEL_WIDTH)
            label.setSizePolicy(
                QSizePolicy.Policy.Fixed,
                QSizePolicy.Policy.Fixed,
            )

            selector = QComboBox()
            selector.setSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Fixed,
            )
            selector.setMinimumWidth(120)
            selector.addItems(
                [
                    "Off",
                    "Music L",
                    "Music R",
                    "Music Mono",
                    "LTC",
                ]
            )

            if channel_index < len(previous):
                selected = previous[channel_index]
            elif is_first_row and channel_index == 0:
                selected = "Music L"
            elif is_first_row and channel_index == 1:
                selected = "Music R"
            else:
                selected = "Off"

            if selector.findText(selected) < 0:
                selected = "Off"

            selector.setCurrentText(selected)
            selector.currentTextChanged.connect(
                lambda _text, current=row: (
                    self.sync_output_device_power(current),
                    self.apply_output_config_live(),
                )
            )

            # One-click kill for the channel. It drives the routing that is
            # already there — muting sets the assignment to "Off" — rather than
            # adding a second piece of state the audio callback would have to
            # read. So it saves and reloads with the project for free, and the
            # realtime path is untouched. The previous assignment is remembered
            # on the button so switching back restores the routing.
            power_button = QPushButton()
            power_button.setObjectName("channelPowerButton")
            power_button.setCheckable(True)
            power_button.setFixedWidth(56)
            power_button.setCursor(Qt.CursorShape.PointingHandCursor)
            power_button.setProperty(
                "previousAssignment",
                selected if selected != "Off" else "Music L",
            )

            def sync_power(button=power_button, box=selector) -> None:
                active = box.currentText() != "Off"
                button.blockSignals(True)
                button.setChecked(active)
                button.blockSignals(False)
                button.setText("ON" if active else "OFF")
                button.setToolTip(
                    "Tắt đầu ra channel này" if active else "Bật lại đầu ra channel này"
                )
                style = button.style()
                style.unpolish(button)
                style.polish(button)

            def toggle_power(checked, button=power_button, box=selector) -> None:
                if checked:
                    restore = str(button.property("previousAssignment") or "").strip()
                    if restore and restore != "Off" and box.findText(restore) >= 0:
                        box.setCurrentText(restore)
                else:
                    current = box.currentText()
                    if current != "Off":
                        button.setProperty("previousAssignment", current)
                    box.setCurrentText("Off")
                sync_power()

            power_button.toggled.connect(toggle_power)
            selector.currentTextChanged.connect(lambda _text, fn=sync_power: fn())
            sync_power()

            channel_row.addWidget(label)
            channel_row.addWidget(
                selector,
                1,
            )
            channel_row.addWidget(power_button)

            layout.insertWidget(
                channel_index,
                row_widget,
            )
            selectors.append(selector)

        row["selectors"] = selectors
        row["pending_assignments"] = []

        self.suspend_output_live_apply(False)
        self.sync_output_device_power(row)

    def rebuild_output_channel_rows(
        self,
    ) -> None:
        if not getattr(
            self,
            "output_device_rows",
            None,
        ):
            return

        self.rebuild_output_device_row_channels(self.output_device_rows[0])

    def output_device_config_snapshot(
        self,
    ) -> list[dict[str, object]]:
        configs: list[dict[str, object]] = []

        for row in self.output_device_rows:
            combo = row.get("device_combo")
            if not isinstance(
                combo,
                QComboBox,
            ):
                continue

            data = combo.currentData()
            if not isinstance(
                data,
                dict,
            ):
                continue

            selectors = row.get("selectors", [])
            assignments = [
                selector.currentText()
                for selector in selectors
                if isinstance(
                    selector,
                    QComboBox,
                )
            ]

            configs.append(
                {
                    "device_index": int(
                        data.get(
                            "index",
                            -1,
                        )
                    ),
                    "device_name": str(
                        data.get(
                            "name",
                            "",
                        )
                    ),
                    "channels": int(
                        data.get(
                            "channels",
                            len(assignments),
                        )
                    ),
                    "assignments": assignments,
                }
            )

        return configs

    def restore_output_device_configs(
        self,
        configs: object,
        prefer_name: bool = False,
    ) -> None:
        if not isinstance(
            configs,
            list,
        ):
            return

        valid = [
            config
            for config in configs
            if isinstance(
                config,
                dict,
            )
        ]
        if not valid:
            return

        self.clear_output_device_rows()

        for config in valid:
            raw_index = config.get("device_index")
            try:
                device_index = int(raw_index) if raw_index is not None else None
            except Exception:
                device_index = None

            assignments = config.get("assignments", [])
            if not isinstance(
                assignments,
                list,
            ):
                assignments = []

            self.add_output_device_row(
                device_index=device_index,
                device_name=str(
                    config.get(
                        "device_name",
                        "",
                    )
                ),
                assignments=[str(value) for value in assignments],
                prefer_name=prefer_name,
            )

    def refresh_devices(
        self,
        *,
        prefer_name: bool = False,
    ) -> None:
        saved_configs = (
            self.output_device_config_snapshot()
            if getattr(self, "output_device_rows", None)
            else []
        )

        try:
            if saved_configs:
                self.restore_output_device_configs(
                    saved_configs,
                    prefer_name=prefer_name,
                )
            else:
                self.add_output_device_row()

        except Exception as exc:
            self.show_error(str(exc))

        self.refresh_input_timecode_devices()

    def rescan_output_devices(
        self,
        _checked: bool = False,
    ) -> None:
        """Re-read the sound devices from the OS, hot-plugged ones included.

        PortAudio takes its device list once, when it initialises. A card that
        is unplugged and plugged back in therefore stays invisible for the
        rest of the session no matter how often the combo boxes are refilled:
        the list only changes across a terminate/initialize pair, and that
        pair is only safe with every stream closed. So this closes the audio
        streams, rescans, then puts the outputs back.
        """
        del _checked

        had_output = self.transport.output_device is not None
        wanted_names = [
            str(config.get("device_name", "")).strip()
            for config in self.output_device_config_snapshot()
        ]

        engine = getattr(
            self,
            "_input_timecode_engine",
            None,
        )
        ltc_was_running = bool(engine is not None and engine.running)
        if ltc_was_running:
            self.stop_input_timecode_engine()

        self.transport.close_streams()

        rescanned = True
        try:
            sd._terminate()
            sd._initialize()
        except Exception as exc:
            # Without a working rescan the old list is still usable, so keep
            # going and let the operator see why nothing changed.
            rescanned = False
            self.show_error(f"DEVICE RESCAN FAILED — {exc}")

        self.refresh_devices(prefer_name=rescanned)

        if had_output:
            try:
                self.apply_restart()
            except Exception as exc:
                self.show_error(str(exc))

        if ltc_was_running:
            self.start_input_timecode_engine()

        if not rescanned:
            return

        # A card that did not come back is now silently routed somewhere else.
        # For a show that has to be said out loud, not left to be discovered.
        present = {
            str(config.get("device_name", "")).strip().lower()
            for config in self.output_device_config_snapshot()
        }
        missing = [name for name in wanted_names if name and name.lower() not in present]

        if missing:
            self.show_error(
                "These outputs did not come back and have been rerouted: " + ", ".join(missing)
            )
            return

        self.set_status(f"DEVICES RESCANNED — {len(self.available_output_devices())} OUTPUTS")

    def apply_config(
        self,
    ) -> bool:
        configs = self.output_device_config_snapshot()

        if not configs:
            self.show_error("Không tìm thấy thiết bị âm thanh output.")
            return False

        used_devices: set[int] = set()
        has_music = False

        normalized: list[dict[str, object]] = []

        for config in configs:
            device_index = int(
                config.get(
                    "device_index",
                    -1,
                )
            )

            if device_index < 0:
                continue

            if device_index in used_devices:
                self.show_error("Mỗi soundcard chỉ được thêm một lần trong Output Devices.")
                return False

            used_devices.add(device_index)

            assignments = [str(value) for value in config.get("assignments", [])]

            if any(assignment.startswith("Music") for assignment in assignments):
                has_music = True

            normalized.append(
                {
                    "device_index": device_index,
                    "device_name": str(
                        config.get(
                            "device_name",
                            "",
                        )
                    ),
                    "assignments": assignments,
                }
            )

        if not normalized:
            self.show_error("Không có Output Device hợp lệ.")
            return False

        if not has_music:
            self.show_error(
                "Phải gán ít nhất một output channel thành Music L, Music R hoặc Music Mono."
            )
            return False

        try:
            self.transport.configure_multiple(
                normalized,
                int(self.fps.currentText()),
                self.ltc_level.value(),
                self.music_level.value(),
            )
        except Exception as exc:
            self.show_error(str(exc))
            return False

        return True

    def play(self) -> None:
        if self.input_timecode_chase_enabled():
            self.set_status("INPUT TIMECODE CONTROLS THE TIMELINE")
            return

        pending_main = str(getattr(self, "_pending_activate_after_ram_path", "") or "").strip()
        selected_path = pending_main or self.main_track_path() or self.current_playlist_path()
        if not selected_path:
            self.show_error("Playlist chưa có bài được chọn.")
            return

        if self.transport.current_file != selected_path:
            if not self.load_current_track():
                return
            selected_path = self.main_track_path() or selected_path
        if self.apply_config():
            self.transport.play()
            self.now_playing.setText(
                "PLAYING: " + self.playlist_display_title_for_path(selected_path)
            )

    def apply_restart(self) -> None:
        if self.apply_config():
            self.transport.restart_outputs()

    def suspend_output_live_apply(self, suspend: bool) -> None:
        """Gate live apply while rows are being built or rebuilt.

        Constructing a row fires currentTextChanged for every selector it
        creates, so without this the transport would be reconfigured a dozen
        times against a half-built row.
        """
        depth = int(getattr(self, "_output_live_apply_depth", 0))
        self._output_live_apply_depth = max(0, depth + (1 if suspend else -1))

    def apply_output_config_live(self, restart: bool = False) -> None:
        """Push the routing to the transport the moment the operator changes it.

        Assignments are read by the audio callback on every block, so a routing
        change lands on the next block with no stream reopen and no gap. Only a
        different soundcard needs ``restart`` — that reopens the stream, which
        costs a short break by nature.

        Unlike apply_config() this never raises a dialog. It runs on every
        click, and a modal on the way to a valid setup would be unusable; the
        status line carries the warning instead. In particular it ALLOWS a
        configuration with no Music channel: switching the last output off is a
        deliberate act now that every channel has its own kill, and refusing it
        would leave the buttons saying one thing while audio did another.
        """
        if int(getattr(self, "_output_live_apply_depth", 0)) > 0:
            return

        configs = self.output_device_config_snapshot()
        if not configs:
            return

        used: set[int] = set()
        normalized: list[dict[str, object]] = []
        for config in configs:
            device_index = int(config.get("device_index", -1))
            if device_index < 0 or device_index in used:
                continue
            used.add(device_index)
            normalized.append(
                {
                    "device_index": device_index,
                    "device_name": str(config.get("device_name", "")),
                    "assignments": [str(value) for value in config.get("assignments", [])],
                }
            )

        if not normalized:
            return

        try:
            self.transport.configure_multiple(
                normalized,
                int(self.fps.currentText()),
                self.ltc_level.value(),
                self.music_level.value(),
            )
        except Exception as exc:
            self.set_status(f"OUTPUT CONFIG ERROR — {exc}")
            return

        if restart:
            try:
                self.transport.restart_outputs()
            except Exception as exc:
                self.set_status(f"OUTPUT RESTART FAILED — {exc}")
                return

        has_music = any(
            str(assignment).startswith("Music")
            for config in normalized
            for assignment in config["assignments"]
        )
        if not has_music:
            self.set_status("NO MUSIC OUTPUT — mọi channel Music đang tắt")
        else:
            self.set_status("OUTPUT UPDATED")

        self.mark_project_dirty()

    def sync_output_device_power(self, row: dict[str, object]) -> None:
        """Mirror the device button from the channels underneath it."""
        button = row.get("device_power")
        if not isinstance(button, QPushButton):
            return

        selectors = [
            selector
            for selector in row.get("selectors", [])
            if isinstance(selector, QComboBox)
        ]
        live = any(selector.currentText() != "Off" for selector in selectors)

        button.blockSignals(True)
        button.setChecked(live)
        button.blockSignals(False)
        button.setText("ON" if live else "OFF")
        button.setToolTip(
            "Tắt toàn bộ output của soundcard này"
            if live
            else "Bật lại soundcard này theo routing cũ"
        )
        style = button.style()
        style.unpolish(button)
        style.polish(button)

    def on_output_device_power_toggled(
        self,
        row: dict[str, object],
        checked: bool,
    ) -> None:
        """Kill or revive every channel on one soundcard in a single click.

        Like the per-channel button this drives the routing that already
        exists rather than adding state the audio callback would have to read.
        Live apply is suspended across the bulk change so the transport is
        reconfigured once at the end instead of once per channel.
        """
        selectors = [
            selector
            for selector in row.get("selectors", [])
            if isinstance(selector, QComboBox)
        ]
        if not selectors:
            return

        self.suspend_output_live_apply(True)
        try:
            if checked:
                remembered = row.get("power_assignments") or []
                for index, selector in enumerate(selectors):
                    restore = (
                        str(remembered[index]) if index < len(remembered) else ""
                    ).strip()
                    if restore and restore != "Off" and selector.findText(restore) >= 0:
                        selector.setCurrentText(restore)
            else:
                row["power_assignments"] = [
                    selector.currentText() for selector in selectors
                ]
                for selector in selectors:
                    selector.setCurrentText("Off")
        finally:
            self.suspend_output_live_apply(False)

        self.sync_output_device_power(row)
        self.apply_output_config_live()

    def on_output_device_combo_changed(self, row: dict[str, object]) -> None:
        """A different soundcard means a different stream, so this one reopens."""
        self.rebuild_output_device_row_channels(row)
        self.apply_output_config_live(restart=True)

    #: Label column for every row inside an Output Device group. "Soundcard"
    #: used to size itself to its own text while the channel labels were pinned
    #: at 110, so the soundcard field started left of the channel fields and the
    #: group read as two ragged columns.
    OUTPUT_ROW_LABEL_WIDTH = 110

    #: Neutral tone for the four secondary transport glyphs.
    TRANSPORT_GLYPH_COLOR = "#D6DCE4"

    #: The play/pause button is a filled disc, so its glyph is cut out of it.
    TRANSPORT_PLAY_GLYPH_COLOR = "#0B0F14"

    def icon_path(self, name: str) -> Path:
        return tracyy_resource_root() / "icon" / name

    def set_button_icon(
        self,
        button: QPushButton,
        glyph: str,
        fallback_text: str,
        tone: str | None = None,
    ) -> None:
        """Draw one transport glyph onto a button.

        Glyphs are drawn rather than loaded: the shipped PNGs were five
        unrelated clipart tiles at five weights and colours, and a QIcon built
        from a bitmap cannot be tinted. Cached per (glyph, tone) because this
        runs from the playback refresh, and reassigning an icon repaints.
        """
        tone = tone or self.TRANSPORT_GLYPH_COLOR
        cache = getattr(self, "_button_icon_cache", None)
        if cache is None:
            cache = {}
            self._button_icon_cache = cache

        key = (glyph, tone)
        if key not in cache:
            try:
                ratio = float(self.devicePixelRatioF())
            except Exception:
                ratio = 2.0
            try:
                cache[key] = transport_icon(glyph, tone, 20, ratio)
            except ValueError:
                cache[key] = None

        icon = cache[key]
        if icon is not None:
            button.setIcon(icon)
            button.setIconSize(QSize(20, 20) if tone == self.TRANSPORT_GLYPH_COLOR
                               else QSize(18, 18))
            button.setText("")
        else:
            button.setIcon(QIcon())
            button.setText(fallback_text)

    def refresh_transport_icons(self) -> None:
        self._button_icon_cache = {}
        self.set_button_icon(self.previous_button, "previous", "|<<")
        self.set_button_icon(self.stop_button, "stop", "[]")
        self.set_button_icon(self.zero_button, "zero", "|<")
        self.set_button_icon(self.next_button, "next", ">>|")
        self._play_pause_icon_state = None
        self.update_play_pause_icon()

    def update_play_pause_icon(self) -> None:
        # Called once per playback refresh. Reassigning the icon repaints the
        # button, so only an actual play/pause change is applied.
        running = bool(self.transport.running)
        if getattr(self, "_play_pause_icon_state", None) == running:
            return
        self._play_pause_icon_state = running

        glyph_tone = self.TRANSPORT_PLAY_GLYPH_COLOR
        if running:
            self.set_button_icon(self.play_pause_button, "pause", "||", glyph_tone)
            self.play_pause_button.setToolTip("Pause  (Space)")
        else:
            self.set_button_icon(self.play_pause_button, "play", ">", glyph_tone)
            self.play_pause_button.setToolTip("Play  (Space)")

        # Running state lives on the button, not on the glyph: a lit transport
        # button is the convention every operator already reads, and it keeps
        # the accent colour meaning exactly one thing.
        self.play_pause_button.setProperty("running", "true" if running else "false")
        style = self.play_pause_button.style()
        style.unpolish(self.play_pause_button)
        style.polish(self.play_pause_button)

    def toggle_play_pause(self) -> None:
        if self.input_timecode_chase_enabled():
            self.set_status("INPUT TIMECODE CONTROLS THE TIMELINE")
            return

        if not self.license_allows_operation():
            return

        if self.transport.running:
            self.transport.pause()
        else:
            self.play()
        self.update_play_pause_icon()

    def stop_playback(self) -> None:
        if self.input_timecode_chase_enabled():
            self.set_status("INPUT TIMECODE CONTROLS THE TIMELINE")
            return
        self.transport.stop()
        self.update_play_pause_icon()

    def restart_from_beginning(self) -> None:
        if self.input_timecode_chase_enabled():
            self.set_status("INPUT TIMECODE CONTROLS THE TIMELINE")
            return
        was_running = self.transport.running
        self.transport.seek(0.0)
        if was_running:
            self.transport.play()
        self.refresh_position()
        self.update_play_pause_icon()
        self.set_status("BACK TO START")

    def play_next(self) -> None:
        was_playing = self.transport.running

        if self.standby_path:
            self.transport.pause()
            self.promote_standby(
                autoplay=was_playing,
            )
            return

        count = self.playlist.count()
        if not count:
            return

        row = self.playing_playlist_row
        next_row = 0 if row < 0 else min(row + 1, count - 1)

        if next_row == row:
            return

        self.activate_playlist_row(
            next_row,
            autoplay=was_playing,
        )

    def play_previous(self) -> None:
        count = self.playlist.count()
        if not count:
            return

        was_playing = self.transport.running
        row = self.playing_playlist_row

        if row < 0:
            row = self.playlist_row_for_path(self.transport.current_file)
        if row < 0:
            row = self.playlist.currentRow()

        previous_row = max(0, row - 1)

        if previous_row == row:
            self.transport.seek(0.0)
            if was_playing:
                self.play()
            return

        self.activate_playlist_row(
            previous_row,
            autoplay=was_playing,
        )

    def on_track_finished(
        self,
    ) -> None:
        """End of track: stop the clock and hand control back to the operator.

        The transport stops feeding the output on its own, but everything
        around it used to stay armed: LTC continuity was still held, the
        play/pause button still read PLAYING, the playhead label and the web
        viewer kept the last playing state, and the next track could not be
        selected into Main. Pausing here makes the end an actual end — the
        playhead parks on the final frame, LTC goes off, and a click on the
        next track loads it into Main.
        """
        path = self.main_track_path() or ""

        if self.input_timecode_chase_enabled():
            # Chase owns the timeline: incoming timecode decides where the
            # playhead sits, including running off the end of a track. Pausing
            # here would fight it.
            self.update_play_pause_icon()
            title = self.playlist_display_title_for_path(path) if path else ""
            self.set_status(f"FINISHED — {title}" if title else "FINISHED")
            return

        # pause() emits its own "PAUSED — LTC OFF"; the real status is set
        # after it so the operator sees why the transport stopped.
        self.transport.pause()

        title = self.playlist_display_title_for_path(path) if path else ""
        if title:
            self.now_playing.setText(f"NOW: {title}")

        self.update_play_pause_icon()
        self.refresh_position()
        self.set_status(f"FINISHED — {title}" if title else "FINISHED")

    def seek_from_waveform(self, ratio: float) -> None:
        if self.input_timecode_chase_enabled():
            self.set_status("PLAYHEAD IS CONTROLLED BY INPUT TIMECODE")
            return
        if self.transport.duration <= 0:
            return
        self.transport.seek(ratio * self.transport.duration)

    def begin_seek(self) -> None:
        self._seeking = True

    def end_seek(self) -> None:
        self._seeking = False
        if self.input_timecode_chase_enabled():
            self.refresh_position()
            return
        if self.transport.duration:
            seconds = self.position.value() / self.position.maximum() * self.transport.duration
            self.transport.seek(seconds)
