"""GTK4/libadwaita GUI for pipewire-multi-output.

Run with: python3 -m multi_output.gui
"""

from __future__ import annotations

import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, Gtk

from . import core


def _friendly_sink_type(sink_name: str) -> str:
    """Turn a PipeWire sink name into a short, human-readable transport label."""
    if sink_name.startswith("bluez_output."):
        return "Bluetooth"
    if sink_name.startswith("alsa_output.usb-"):
        return "USB Audio"
    if sink_name.startswith("alsa_output.pci-"):
        return "Built-in Audio"
    if sink_name.startswith("alsa_output."):
        return "ALSA Output"
    return sink_name


class SpeakerRow(Adw.ExpanderRow):
    """A row in the speaker list representing one output speaker."""

    def __init__(self, speaker: core.SpeakerConfig, index: int, app: MultiOutputApp):
        super().__init__()
        self.speaker = speaker
        self.index = index
        self.app = app
        self._debounce_id: int = 0

        self.set_title(speaker.label or speaker.sink_name)
        self.set_subtitle(_friendly_sink_type(speaker.sink_name))
        self.set_subtitle_lines(1)
        self.set_tooltip_text(speaker.sink_name)

        # Remove button
        remove_btn = Gtk.Button(icon_name="list-remove-symbolic", valign=Gtk.Align.CENTER)
        remove_btn.add_css_class("flat")
        remove_btn.set_tooltip_text("Remove speaker")
        remove_btn.connect("clicked", self._on_remove)
        self.add_suffix(remove_btn)

        # Delay slider as a child row inside the expander
        slider_row = Adw.ActionRow(title="Delay")

        self.delay_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 0, 500, 5,
        )
        self.delay_scale.set_value(speaker.delay_ms)
        self.delay_scale.set_hexpand(True)
        self.delay_scale.set_size_request(200, -1)
        self.delay_scale.set_valign(Gtk.Align.CENTER)
        slider_row.add_suffix(self.delay_scale)

        delay_adj = Gtk.Adjustment(
            value=speaker.delay_ms, lower=0, upper=500,
            step_increment=1, page_increment=10,
        )
        self.delay_spin = Gtk.SpinButton(
            adjustment=delay_adj, digits=0,
            valign=Gtk.Align.CENTER,
        )
        self.delay_spin.set_width_chars(4)

        delay_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=4, valign=Gtk.Align.CENTER,
        )
        delay_box.append(self.delay_spin)
        ms_label = Gtk.Label(label="ms")
        ms_label.add_css_class("dim-label")
        delay_box.append(ms_label)
        slider_row.add_suffix(delay_box)

        self._updating = False
        self.delay_scale.connect("value-changed", self._on_scale_changed)
        self.delay_spin.connect("value-changed", self._on_spin_changed)
        self.add_row(slider_row)

    def _on_scale_changed(self, scale: Gtk.Scale) -> None:
        if self._updating:
            return
        self._updating = True
        value = scale.get_value()
        self.speaker.delay_ms = value
        self.delay_spin.set_value(value)
        self._updating = False
        self._schedule_apply()

    def _on_spin_changed(self, spin: Gtk.SpinButton) -> None:
        if self._updating:
            return
        self._updating = True
        value = spin.get_value()
        self.speaker.delay_ms = value
        self.delay_scale.set_value(value)
        self._updating = False
        self._schedule_apply()

    def _schedule_apply(self) -> None:
        if self._debounce_id:
            GLib.source_remove(self._debounce_id)
        self._debounce_id = GLib.timeout_add(300, self._apply_delay)

    def _apply_delay(self) -> bool:
        self._debounce_id = 0
        if core.is_running():
            try:
                core.update_speaker_delay(self.index, self.speaker.delay_ms)
            except RuntimeError:
                pass
        return GLib.SOURCE_REMOVE

    def _on_remove(self, _btn: Gtk.Button) -> None:
        self.app.remove_speaker(self.index)


class MultiOutputApp(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id="io.github.pipewire_multi_output",
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        self.config = core.MultiOutputConfig()
        self.speaker_rows: list[SpeakerRow] = []

    def do_activate(self) -> None:
        # Load saved config
        saved = core.load_config()
        if saved:
            self.config = saved

        # Window
        self.win = Adw.ApplicationWindow(application=self)
        self.win.set_title("Multi-Output Audio")
        self.win.set_default_size(500, 600)

        # Header bar
        header = Adw.HeaderBar()

        # Save button
        save_btn = Gtk.Button(icon_name="document-save-symbolic")
        save_btn.set_tooltip_text("Save configuration")
        save_btn.connect("clicked", self._on_save)
        header.pack_start(save_btn)

        # Start/stop button
        self.toggle_btn = Gtk.Button()
        self.toggle_btn.add_css_class("suggested-action")
        self.toggle_btn.connect("clicked", self._on_toggle)
        header.pack_end(self.toggle_btn)

        # Main content
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        main_box.append(header)

        clamp = Adw.Clamp(maximum_size=600)
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            margin_start=12,
            margin_end=12,
            margin_top=12,
            margin_bottom=12,
            spacing=18,
        )
        clamp.set_child(content)

        scrolled = Gtk.ScrolledWindow(vexpand=True)
        scrolled.set_child(clamp)
        main_box.append(scrolled)

        # Device name group
        name_group = Adw.PreferencesGroup(title="Output Device")
        self.name_entry = Adw.EntryRow(title="Name shown in GNOME")
        self.name_entry.set_text(self.config.name)
        self.name_entry.connect("changed", self._on_name_changed)
        name_group.add(self.name_entry)
        content.append(name_group)

        # Speakers group
        speakers_group_header = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            margin_bottom=0,
        )
        self.speakers_group = Adw.PreferencesGroup(title="Speakers")

        # Add speaker button
        add_btn = Gtk.Button(icon_name="list-add-symbolic")
        add_btn.add_css_class("flat")
        add_btn.set_tooltip_text("Add speaker")
        add_btn.connect("clicked", self._on_add_speaker)
        self.speakers_group.set_header_suffix(add_btn)

        content.append(self.speakers_group)

        # Placeholder when no speakers
        self.empty_label = Gtk.Label(
            label="No speakers added. Click + to add one.",
            margin_top=12,
            margin_bottom=12,
        )
        self.empty_label.add_css_class("dim-label")

        # Settings group
        settings_group = Adw.PreferencesGroup(title="Settings")
        self.autostart_row = Adw.SwitchRow(
            title="Auto-start on login",
            subtitle="Enable systemd user service",
        )
        try:
            self.autostart_row.set_active(core.is_service_enabled())
        except Exception:
            self.autostart_row.set_sensitive(False)
            self.autostart_row.set_subtitle("Service not installed")
        self.autostart_row.connect("notify::active", self._on_autostart_toggled)
        settings_group.add(self.autostart_row)
        content.append(settings_group)

        # Status bar
        self.status_label = Gtk.Label(halign=Gtk.Align.CENTER, margin_top=6)
        self.status_label.add_css_class("dim-label")
        content.append(self.status_label)

        self.win.set_content(main_box)
        self._rebuild_speaker_list()
        self._update_status()
        self.win.present()

    def _rebuild_speaker_list(self) -> None:
        """Rebuild the speaker list UI from current config."""
        # Remove existing rows
        for row in self.speaker_rows:
            self.speakers_group.remove(row)
        self.speaker_rows.clear()

        if not self.config.speakers:
            self.speakers_group.add(self.empty_label)
        else:
            try:
                self.speakers_group.remove(self.empty_label)
            except Exception:
                pass
            for i, speaker in enumerate(self.config.speakers):
                row = SpeakerRow(speaker, i, self)
                self.speakers_group.add(row)
                self.speaker_rows.append(row)

    def _update_status(self) -> None:
        """Update the UI to reflect running state."""
        running = core.is_running()
        if running:
            self.toggle_btn.set_label("Stop")
            self.toggle_btn.remove_css_class("suggested-action")
            self.toggle_btn.add_css_class("destructive-action")
            state = core.load_state()
            n = len(state.speakers) if state else 0
            self.status_label.set_text(f"Running ({n} speakers)")
        else:
            self.toggle_btn.set_label("Start")
            self.toggle_btn.remove_css_class("destructive-action")
            self.toggle_btn.add_css_class("suggested-action")
            self.status_label.set_text("Stopped")

    def _on_name_changed(self, entry: Adw.EntryRow) -> None:
        self.config.name = entry.get_text()

    def _on_toggle(self, _btn: Gtk.Button) -> None:
        if core.is_running():
            core.stop()
            self._update_status()
        else:
            if not self.config.speakers:
                self.status_label.set_text("Add at least one speaker first.")
                return
            self.toggle_btn.set_sensitive(False)
            self.status_label.set_text("Starting...")

            def do_start():
                try:
                    core.start(self.config)
                    GLib.idle_add(self._on_start_done, None)
                except Exception as e:
                    GLib.idle_add(self._on_start_done, str(e))

            threading.Thread(target=do_start, daemon=True).start()

    def _on_start_done(self, error: str | None) -> None:
        self.toggle_btn.set_sensitive(True)
        if error:
            self.status_label.set_text(f"Error: {error}")
        else:
            self._update_status()

    def _on_save(self, _btn: Gtk.Button) -> None:
        core.save_config(self.config)
        self.status_label.set_text(f"Config saved.")

    def _on_autostart_toggled(self, row: Adw.SwitchRow, _pspec) -> None:
        if getattr(self, "_toggling_autostart", False):
            return
        try:
            core.set_service_enabled(row.get_active())
            action = "enabled" if row.get_active() else "disabled"
            self.status_label.set_text(f"Auto-start {action}.")
        except Exception as e:
            self.status_label.set_text(f"Service error: {e}")
            self._toggling_autostart = True
            row.set_active(not row.get_active())
            self._toggling_autostart = False

    def _on_add_speaker(self, _btn: Gtk.Button) -> None:
        """Show a dialog to pick a sink to add."""
        # Get currently selected sink names
        selected = {s.sink_name for s in self.config.speakers}
        available = core.get_available_sinks(list(selected))

        if not available:
            dialog = Adw.AlertDialog(
                heading="No Speakers Available",
                body="All detected audio outputs are already added, or no outputs were found.",
            )
            dialog.add_response("ok", "OK")
            dialog.present(self.win)
            return

        # Build a selection dialog
        dialog = Adw.AlertDialog(
            heading="Add Speaker",
            body="Select an audio output to add:",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("add", "Add")
        dialog.set_response_appearance("add", Adw.ResponseAppearance.SUGGESTED)

        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        listbox.add_css_class("boxed-list")
        listbox.set_margin_start(12)
        listbox.set_margin_end(12)

        for sink in available:
            row = Adw.ActionRow(
                title=sink["description"],
                subtitle=_friendly_sink_type(sink["name"]),
            )
            row.set_subtitle_lines(1)
            row.set_tooltip_text(sink["name"])
            row.sink_data = sink
            listbox.append(row)

        # Select first row
        listbox.select_row(listbox.get_row_at_index(0))

        dialog.set_extra_child(listbox)

        dialog.connect("response", self._on_add_dialog_response, listbox, available)
        dialog.present(self.win)

    def _on_add_dialog_response(
        self,
        dialog: Adw.AlertDialog,
        response: str,
        listbox: Gtk.ListBox,
        available: list[dict],
    ) -> None:
        if response != "add":
            return
        selected_row = listbox.get_selected_row()
        if selected_row is None:
            return

        # Get the sink data from the selected row's index
        idx = selected_row.get_index()
        sink = available[idx]

        speaker = core.SpeakerConfig(
            sink_name=sink["name"],
            delay_ms=0,
            label=sink["description"],
        )
        self.config.speakers.append(speaker)
        self._rebuild_speaker_list()

    def remove_speaker(self, index: int) -> None:
        """Remove a speaker from the config and rebuild UI."""
        if 0 <= index < len(self.config.speakers):
            self.config.speakers.pop(index)
            self._rebuild_speaker_list()


def main() -> None:
    app = MultiOutputApp()
    app.run()


if __name__ == "__main__":
    main()
