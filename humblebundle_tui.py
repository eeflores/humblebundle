#!/usr/bin/env python3
#
# humblebundle_tui - Terminal user interface for Humble Bundle Manager
#
#    Copyright (C) 2014 Rodrigo Silva (MestreLion) <linux@rodrigosilva.com>
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program. See <http://www.gnu.org/licenses/gpl.html>

'''Browse the cached Humble Bundle games and bundles, and download them to
the download folder.

Browsing only reads the cache written by `hb --update` and the download
folder: it makes no network requests. Requests are made only to download,
when d is pressed and confirmed.
'''

import collections
import logging
import os
import sys
import threading
import time

try:
    from rich.console import Group
    from rich.markup import escape
    from rich.table import Table
    from rich.text import Text
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.screen import ModalScreen
    from textual.widgets import (Button, Footer, Header, Input, ProgressBar,
                                 RichLog, Static, Tree)
except ImportError:
    sys.exit("The TUI requires Textual. Install it with:"
             " pip install 'humblebundle-manager[tui]'")

import humblebundle
import humblebundle_library as library


log = logging.getLogger(__name__)

# Label of the tree branch for games that belong to no bundle,
# such as the ones from extras.json
NO_BUNDLE = "(not in any bundle)"

# Tree markers for the download status of a bundle or game
MARK_DONE = "[green]✓[/green] "
MARK_SOME = "[yellow]◐[/yellow] "
MARK_FLAG = "[red]![/red] "
MARK_NONE = "  "

STATUS_STYLE = {
    library.HAVE: "green",
    library.PARTIAL: "yellow",
    library.MISSING: "dim",
    library.UNTRACKED: "yellow",
    library.CHANGED: "red",
    library.MODIFIED: "red",
}

# How often download progress is shown, in seconds
PROGRESS_INTERVAL = 0.1


class TuiLogHandler(logging.Handler):
    '''Send log records to the app's log panel, from any thread'''

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setFormatter(logging.Formatter('%(levelname)-8s %(message)s'))

    def emit(self, record):
        try:
            message = self.format(record)
            if threading.get_ident() == self.app.thread_id:
                self.app.write_log(message)
            else:
                self.app.call_from_thread(self.app.write_log, message)
        except Exception:
            self.handleError(record)


# Dialogs -------------------------------------------------------------------

DIALOG_CSS = """
ConfirmScreen, InputScreen {
    align: center middle;
}
.dialog {
    width: 80%;
    max-width: 100;
    height: auto;
    max-height: 90%;
    border: thick $primary;
    background: $surface;
    padding: 1 2;
}
.dialog-title {
    text-style: bold;
    margin-bottom: 1;
}
.dialog-body {
    height: auto;
    max-height: 30;
}
.dialog-error {
    color: $error;
    height: auto;
}
.dialog-buttons {
    height: auto;
    margin-top: 1;
    align-horizontal: right;
}
.dialog-buttons Button {
    margin-left: 2;
}
"""


class ConfirmScreen(ModalScreen):
    '''Ask a yes/no question. Dismisses with True or False.'''

    BINDINGS = [
        Binding("y", "answer(True)", "Yes", show=False),
        Binding("n", "answer(False)", "No", show=False),
        Binding("escape", "answer(False)", "Cancel", show=False),
    ]

    def __init__(self, title, body, yes="Yes", no="Cancel"):
        super().__init__()
        self.title_text = title
        self.body = body
        self.yes = yes
        self.no = no

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self.title_text, classes="dialog-title")
            with VerticalScroll(classes="dialog-body"):
                yield Static(self.body)
            with Horizontal(classes="dialog-buttons"):
                yield Button(self.no + " (n)", id="no")
                yield Button(self.yes + " (y)", id="yes", variant="primary")

    def on_mount(self):
        self.query_one("#yes", Button).focus()

    def on_button_pressed(self, event: Button.Pressed):
        self.dismiss(event.button.id == "yes")

    def action_answer(self, answer):
        self.dismiss(answer)


class InputScreen(ModalScreen):
    '''Ask for a value. Dismisses with the value, or None if cancelled.

    validate, if given, is called with the value and returns the value to
    dismiss with. If it raises, its message is shown and the dialog stays.
    '''

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, title, body, placeholder="", value="",
                 password=False, validate=None):
        super().__init__()
        self.title_text = title
        self.body = body
        self.placeholder = placeholder
        self.value = value
        self.password = password
        self.validate = validate

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self.title_text, classes="dialog-title")
            yield Static(self.body, classes="dialog-body")
            yield Input(value=self.value, placeholder=self.placeholder,
                        password=self.password, id="value")
            yield Static("", classes="dialog-error", id="error")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel (Esc)", id="cancel")
                yield Button("OK (Enter)", id="ok", variant="primary")

    def on_mount(self):
        self.query_one("#value", Input).focus()

    def on_input_submitted(self, event: Input.Submitted):
        event.stop()
        self._submit()

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "ok":
            self._submit()
        else:
            self.dismiss(None)

    def action_cancel(self):
        self.dismiss(None)

    def _submit(self):
        value = self.query_one("#value", Input).value.strip()
        if not value:
            return
        if self.validate:
            try:
                value = self.validate(value)
            except Exception as e:
                self.query_one("#error", Static).update(escape(str(e)))
                return
        self.dismiss(value)


# Downloads -----------------------------------------------------------------

class DownloadItem:
    '''A bundle, or some games of it, waiting in the download queue'''

    def __init__(self, label, bundle, jobs):
        self.label = label
        self.bundle = bundle
        self.jobs = jobs
        self.result = None


# The app -------------------------------------------------------------------

class HumbleBundleApp(App):

    TITLE = "Humble Bundle Manager"

    CSS = DIALOG_CSS + """
    #left {
        width: 2fr;
        min-width: 30;
    }
    #filter-status {
        height: auto;
        padding: 0 1;
        color: $text-muted;
    }
    #tree {
        height: 1fr;
    }
    #details-scroll {
        width: 3fr;
        border-left: solid $primary;
    }
    #details {
        padding: 0 1;
    }
    #downloads {
        height: auto;
        border-top: solid $accent;
        padding: 0 1;
        display: none;
    }
    #downloads.active {
        display: block;
    }
    #downloads ProgressBar {
        width: 1fr;
    }
    .dl-row {
        height: 1;
    }
    .dl-label {
        width: 8;
        color: $text-muted;
    }
    #log {
        height: 7;
        border-top: solid $primary;
    }
    #log.hidden {
        display: none;
    }
    """

    BINDINGS = [
        Binding("slash", "focus_filter", "Filter"),
        Binding("escape", "clear_filter", "Clear filter", show=False),
        Binding("d", "download", "Download"),
        Binding("x", "cancel_downloads", "Cancel downloads"),
        Binding("p", "next_platform", "Platform"),
        Binding("P", "previous_platform", "Platform", show=False),
        Binding("h", "toggle_empty", "No-download items"),
        Binding("e", "expand_all", "Expand"),
        Binding("c", "collapse_all", "Collapse"),
        Binding("l", "toggle_log", "Log"),
        Binding("q", "quit", "Quit"),
    ]

    # Actions of the main screen. They are disabled while typing in the
    # filter box, so its letters reach it, and while a dialog is open.
    MAIN_ACTIONS = ("download", "cancel_downloads", "next_platform",
                    "previous_platform", "toggle_empty", "expand_all",
                    "collapse_all", "toggle_log", "quit", "focus_filter",
                    "clear_filter")

    def __init__(self, loglevel=logging.INFO):
        super().__init__()
        self.loglevel = loglevel
        self.hb = None
        self.platforms = [None]  # None means all platforms
        self.platform_index = 0
        self.filter_text = ""
        # Show games with no downloadable files, and bundles with no games
        # left to show. Toggled with h.
        self.show_empty = False
        # Machine name -> set of platforms with at least one file to download
        self.download_platforms = {}
        self.thread_id = None

        # Download folder, or None if not set or not usable
        self.lib = None
        self.lib_error = None
        # Bundle machine name (or UNBUNDLED) -> FileJobs of all its files.
        # Their status is kept up to date as files are downloaded.
        self.jobs = {}
        self.has_auth = False
        self.queue = collections.deque()
        self.current = None  # DownloadItem being downloaded
        self.cancel_event = threading.Event()
        self.worker = None
        self._last_progress = 0

    # Layout ----------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical(id="left"):
                yield Input(placeholder="Filter by name ( / )", id="filter")
                yield Static(id="filter-status")
                tree = Tree("Bundles", id="tree")
                tree.show_root = False
                yield tree
            with VerticalScroll(id="details-scroll"):
                yield Static("Loading cached data...", id="details")
        with Vertical(id="downloads"):
            yield Static(id="dl-title")
            with Horizontal(classes="dl-row"):
                yield Static("File", classes="dl-label")
                yield ProgressBar(id="dl-file", show_eta=True)
            with Horizontal(classes="dl-row"):
                yield Static("Total", classes="dl-label")
                yield ProgressBar(id="dl-total", show_eta=True)
            yield Static(id="dl-queue")
        yield RichLog(id="log", wrap=True, markup=False)
        yield Footer()

    def on_mount(self):
        self.thread_id = threading.get_ident()
        handler = TuiLogHandler(self)
        root = logging.getLogger()
        root.addHandler(handler)
        root.setLevel(self.loglevel)
        if self.loglevel > logging.DEBUG:
            # The library logs every game and bundle lookup at INFO level
            logging.getLogger(humblebundle.__name__).setLevel(logging.WARNING)
        self._log_handler = handler
        self.query_one("#tree", Tree).focus()
        self.load_data()

    def on_unmount(self):
        logging.getLogger().removeHandler(self._log_handler)

    def write_log(self, message):
        self.query_one("#log", RichLog).write(message)

    # Data loading -----------------------------------------------------------

    def load_data(self):
        # Parsing a large games.json takes a moment, so keep the UI responsive
        self.run_worker(self._load, thread=True, exit_on_error=False)

    def _load(self):
        try:
            # auth="" skips reading the keyring: no requests are made here.
            # The session cookie is read only when downloading.
            hb = humblebundle.HumbleBundle(auth="", autoupdate=False)
        except Exception as e:
            log.error("Could not load data: %s", e)
            self.call_from_thread(self._show_message,
                                  "Could not load data:\n%s" % escape(str(e)))
            return
        self.call_from_thread(self._loaded, hb)

    def _loaded(self, hb):
        self.hb = hb
        if not hb.has_data():
            self._show_message(
                "No cached data in %s\n\nRun [b]hb --update[/b]"
                " to fetch your games and bundles, then start this again."
                % escape(hb.configdir))
            return
        self.platforms = [None] + sorted(
            {d.get('platform', '')
             for game in hb.games.values()
             for d in game.get('downloads', [])} - {''})
        self.download_platforms = {
            key: {platform for platform, files in hb.get_downloads(key)
                  if files}
            for key in hb.games}
        log.info("Loaded %d games from %d bundles",
                 len(hb.games), len(hb.bundles))
        self.rebuild_tree()
        self._show_message("Select a bundle or game to see its details.")
        self.open_library()

    def open_library(self):
        '''Open the download folder and read the status of its files in the
        background. Without one, the tree shows no download status.'''
        self.lib = None
        self.jobs = {}
        try:
            lib = library.Library(self.hb)
            lib.check_folder()
        except humblebundle.HumbleBundleError as e:
            self.lib_error = str(e)
            log.info("%s", e)
            return
        self.lib_error = None
        self.run_worker(lambda: self._scan(lib), thread=True,
                        exit_on_error=False, group="scan")

    def _scan(self, lib):
        try:
            jobs = {bundle: lib.plan(bundle)
                    for bundle in list(self.hb.bundles) + [library.UNBUNDLED]}
        except Exception as e:
            log.error("Could not read the download folder: %s", e)
            self.call_from_thread(self._scan_failed, str(e))
            return
        self.call_from_thread(self._scanned, lib, jobs)

    def _scan_failed(self, error):
        self.lib_error = error
        self.refresh_details()

    def _scanned(self, lib, jobs):
        self.lib = lib
        self.jobs = jobs
        files = sum(len(j) for j in jobs.values())
        have = sum(1 for js in jobs.values() for j in js
                   if j.status == library.HAVE)
        log.info("Download folder %s: %d of %d files downloaded",
                 lib.folder, have, files)
        self.refresh_markers()
        self.refresh_details()

    def _show_message(self, text):
        self.query_one("#details", Static).update(text)

    # Filtering ------------------------------------------------------------

    @property
    def platform(self):
        return self.platforms[self.platform_index]

    def _game_matches(self, key, text, show_empty):
        game = self.hb.games[key]
        if show_empty:
            # Any download entry for the platform, even with no files
            if self.platform and not any(
                    d.get('platform') == self.platform
                    for d in game.get('downloads', [])):
                return False
        else:
            # At least one file to download, for the platform if one is set
            platforms = self.download_platforms.get(key, set())
            if not platforms or (self.platform and
                                 self.platform not in platforms):
                return False
        if not text:
            return True
        return (text in key.lower() or
                text in game.get('human_name', '').lower())

    def _sorted_games(self, keys):
        return sorted(keys, key=lambda k: (
            self.hb.games[k].get('human_name', k).lower(), k))

    def rebuild_tree(self):
        if not self.hb:
            return
        tree = self.query_one("#tree", Tree)
        tree.clear()
        expand = bool(self.filter_text)
        bundled = set()
        shown_bundles = 0
        seen_games = set()
        # Games and bundles that only show_empty would add to the tree
        hidden_games = set()
        hidden_bundles = 0

        def matching(keys, text):
            '''Return the keys to show, and record the ones hidden only
            for having no downloads'''
            games = [k for k in keys
                     if self._game_matches(k, text, self.show_empty)]
            if not self.show_empty:
                shown = set(games)
                hidden_games.update(
                    k for k in keys
                    if k not in shown and self._game_matches(k, text, True))
            return games

        bundles = sorted(self.hb.bundles.values(),
                         key=lambda b: (b.get('human_name', '').lower(),
                                        b.get('machine_name', '')))
        for bundle in bundles:
            keys = [k for k in bundle.get('games', []) if k in self.hb.games]
            bundled.update(keys)
            name = bundle.get('human_name') or bundle.get('machine_name', '?')
            # If the bundle name matches, show all its games for the platform
            text = self.filter_text
            name_match = bool(text) and text in name.lower()
            if name_match:
                text = ""
            games = matching(keys, text)
            if not games:
                # Some purchases list no games at all (such as key-only
                # ones). With show_empty, show those unless a filter
                # excludes them.
                excluded = bool(keys or self.platform or
                                (self.filter_text and not name_match))
                if not self.show_empty:
                    if not excluded or any(
                            self._game_matches(k, text, True) for k in keys):
                        hidden_bundles += 1
                    continue
                if excluded:
                    continue
            key = bundle['machine_name']
            node = tree.root.add(
                self.bundle_label(key, len(games)),
                data=("bundle", key, len(games)),
                expand=expand, allow_expand=bool(games))
            for game in self._sorted_games(games):
                node.add_leaf(self.game_label(key, game),
                              data=("game", game, key))
            shown_bundles += 1
            seen_games.update(games)

        others = matching([k for k in self.hb.games if k not in bundled],
                          self.filter_text)
        if others:
            node = tree.root.add(
                self.bundle_label(library.UNBUNDLED, len(others)),
                data=("bundle", library.UNBUNDLED, len(others)),
                expand=expand)
            for game in self._sorted_games(others):
                node.add_leaf(self.game_label(library.UNBUNDLED, game),
                              data=("game", game, library.UNBUNDLED))
            seen_games.update(others)

        status = ("Platform: [b]%s[/b]   %d games in %d bundles" %
                  (escape(self.platform or "all"), len(seen_games),
                   shown_bundles))
        if self.show_empty:
            status += "\n[dim]Showing items without downloads (h)[/dim]"
        elif hidden_games or hidden_bundles:
            status += ("\n[dim]Hidden: %d games, %d bundles without"
                       " downloads (h)[/dim]" %
                       (len(hidden_games), hidden_bundles))
        self.query_one("#filter-status", Static).update(status)

    def on_input_changed(self, event: Input.Changed):
        if event.input.id != "filter":
            return
        self.filter_text = event.value.strip().lower()
        self.rebuild_tree()

    def on_input_submitted(self, event: Input.Submitted):
        if event.input.id == "filter":
            self.query_one("#tree", Tree).focus()

    # Download status ------------------------------------------------------

    def jobs_for(self, bundle, game=None):
        '''Return the FileJobs of bundle, or of one game in it, for the
        selected platform'''
        return [j for j in self.jobs.get(bundle, [])
                if (game is None or game in j.games) and
                (not self.platform or j.platform == self.platform)]

    def marker(self, jobs):
        '''Return the tree marker for the status of jobs'''
        if not self.lib or not jobs:
            return MARK_NONE if self.lib else ""
        statuses = {j.status for j in jobs}
        if statuses & set(library.FLAGGED):
            return MARK_FLAG
        if statuses == {library.HAVE}:
            return MARK_DONE
        if statuses - {library.MISSING}:
            return MARK_SOME
        return MARK_NONE

    def bundle_name(self, bundle):
        if bundle == library.UNBUNDLED:
            return NO_BUNDLE
        b = self.hb.bundles.get(bundle, {})
        return b.get('human_name') or b.get('machine_name', bundle)

    def bundle_label(self, bundle, count):
        name = escape(self.bundle_name(bundle))
        if bundle == library.UNBUNDLED:
            name = "[i]%s[/i]" % name
        return "%s%s [dim](%d)[/dim]" % (
            self.marker(self.jobs_for(bundle)), name, count)

    def game_label(self, bundle, game):
        return "%s%s" % (self.marker(self.jobs_for(bundle, game)),
                         escape(self.hb.games[game].get('human_name', game)))

    def refresh_markers(self):
        '''Update the tree labels after download status changes, keeping
        what is expanded and selected'''
        if not self.hb:
            return
        tree = self.query_one("#tree", Tree)
        for node in tree.root.children:
            _, bundle, count = node.data
            node.set_label(self.bundle_label(bundle, count))
            for leaf in node.children:
                leaf.set_label(self.game_label(bundle, leaf.data[1]))

    # Details ----------------------------------------------------------------

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted):
        self.refresh_details(event.node)
        self.query_one("#details-scroll", VerticalScroll).scroll_home(
            animate=False)

    def refresh_details(self, node=None):
        if not self.hb:
            return
        node = node or self.query_one("#tree", Tree).cursor_node
        if node is None or not node.data:
            return
        kind, key, extra = node.data
        try:
            if kind == "game":
                text = self.format_game(key, extra)
            else:
                text = self.format_bundle(key)
        except humblebundle.HumbleBundleError as e:
            text = escape(str(e))
        self._show_message(text)

    @staticmethod
    def _field(label, value):
        return "[b]%-10s[/b] %s" % (label, escape(str(value or "")))

    def _folder_lines(self, bundle, game=None):
        '''Return the details lines about the download folder of bundle,
        with the status of its files, or of game's files in it'''
        if self.lib:
            path = os.path.join(self.lib.folder, self.lib.bundle_folder(bundle))
            jobs = self.jobs_for(bundle, game)
            counts = self.lib.summary(jobs)
            done = "%d of %d files downloaded" % (counts[library.HAVE],
                                                  len(jobs))
            for status in (library.PARTIAL, library.UNTRACKED,
                           library.CHANGED, library.MODIFIED):
                if counts[status]:
                    done += ", %d %s" % (counts[status], status)
            if self.platform:
                done += " (%s)" % self.platform
            return [self._field("Folder", path),
                    "[b]%-10s[/b] %s" % ("Status", escape(done))]
        if self.lib_error:
            return ["[b]%-10s[/b] [dim]%s[/dim]" % (
                "Folder", escape(self.lib_error) + " Press d to set it."
                if "No download folder" in self.lib_error
                else escape(self.lib_error))]
        return ["[b]%-10s[/b] [dim]reading...[/dim]" % "Folder"]

    def format_game(self, key, bundle):
        hb = self.hb
        game = hb.get_game(key)
        lines = [
            self._field("Game", game.get('machine_name')),
            self._field("Name", game.get('human_name')),
            self._field("Developer", game.get('payee', {}).get('human_name')),
            self._field("URL", game.get('url')),
        ]
        if game.get('install'):
            lines.append(self._field("Install", game['install']))
        lines += ["", "[b]Bundles[/b]"]
        bundles = hb.get_game_bundles(key)
        for b in bundles:
            current = " [b]←[/b]" if b.get('machine_name') == bundle else ""
            lines.append("  %s [dim]\\[%s][/dim]%s" % (
                escape(b.get('human_name', '')),
                escape(b.get('machine_name', '')), current))
        if not bundles:
            lines.append("  [dim]none[/dim]")

        lines += ["", "[b]Downloads[/b]"]
        downloads = hb.get_downloads(key)
        if not downloads:
            lines.append("  [dim]none[/dim]")
            return "\n".join(lines)

        # Status of each file, as part of the selected bundle
        status = {}
        for job in self.jobs_for(bundle, key) if self.lib else []:
            status[(job.platform, job.md5)] = job

        table = Table(box=None, show_header=False, pad_edge=False,
                      padding=(0, 1), expand=True)
        table.add_column("name", ratio=3, overflow="fold")
        table.add_column("arch", no_wrap=True)
        table.add_column("size", justify="right", no_wrap=True)
        table.add_column("file", ratio=4, overflow="fold", style="dim")
        if self.lib:
            table.add_column("status", no_wrap=True)
        previous = None
        for platform, files in downloads:
            if platform != previous:
                style = ("bold reverse" if platform == self.platform
                         else "underline")
                table.add_row(Text.assemble("  ", (platform, style)))
                previous = platform
            if not files:
                table.add_row(Text("    none", style="dim"))
            for d in files:
                row = [Text("    " + d.get('name', '')),
                       "%s-bit" % d['arch'] if d.get('arch') else "",
                       d.get('human_size', ''),
                       hb._download_basename(d)]
                if self.lib:
                    job = status.get((platform, d.get('md5', '').lower()))
                    row.append(Text(job.status,
                                    style=STATUS_STYLE.get(job.status, ""))
                               if job else Text(""))
                table.add_row(*row)
        return Group(Text.from_markup("\n".join(lines)), table,
                     Text.from_markup("\n" + "\n".join(
                         self._folder_lines(bundle, key))))

    def format_bundle(self, key):
        hb = self.hb
        if key == library.UNBUNDLED:
            games = self.lib.bundle_games(key) if self.lib else [
                g for g in hb.games if not hb.get_game_bundles(g)]
            lines = [self._field("Bundle", library.UNBUNDLED),
                     self._field("Name", "Games that are not in any bundle")]
        else:
            bundle = hb.get_bundle(key)
            games = bundle.get('games', [])
            lines = [
                self._field("Bundle", bundle.get('machine_name')),
                self._field("Name", bundle.get('human_name')),
                self._field("Category", bundle.get('category')),
                self._field("Price US$", bundle.get('familyamount')),
            ]
        lines += self._folder_lines(key)
        lines += ["", "[b]Games[/b] (%d)" % len(games)]
        for name in sorted(games):
            game = hb.games.get(name)
            if game is None:
                lines.append("  [dim]%s (missing from games data)[/dim]" %
                             escape(name))
                continue
            lines.append("  %s%s [dim]\\[%s][/dim]" % (
                self.marker(self.jobs_for(key, name)),
                escape(game.get('human_name', '')), escape(name)))
        return "\n".join(lines)

    # Downloading ------------------------------------------------------------

    def action_download(self):
        '''Download the selected bundle, or game in its bundle'''
        if not self.hb or not self.hb.has_data():
            return
        node = self.query_one("#tree", Tree).cursor_node
        if node is None or not node.data:
            return
        kind, key, extra = node.data
        if kind == "game":
            bundle, games = extra, [key]
            label = "%s (%s)" % (self.hb.games[key].get('human_name', key),
                                 self.bundle_name(bundle))
        else:
            bundle, games = key, None
            label = self.bundle_name(bundle)

        if not self.lib:
            if self.lib_error and "No download folder" not in self.lib_error:
                self.notify(self.lib_error, severity="error")
                return
            self.ask_folder(lambda: self.action_download())
            return
        if bundle not in self.jobs:
            self.notify("Still reading the download folder", severity="warning")
            return

        jobs = self.jobs_for(bundle, games[0] if games else None)
        for job in jobs:
            self.lib.update_status(job)
        if not jobs:
            self.notify("Nothing to download%s" % (
                " for %s" % self.platform if self.platform else ""))
            return
        queued = {id(j) for item in self.queue for j in item.jobs}
        if self.current:
            queued |= {id(j) for j in self.current.jobs}
        if queued & {id(j) for j in jobs}:
            self.notify("Already in the download queue", severity="warning")
            return

        counts = self.lib.summary(jobs)
        todo = counts[library.MISSING] + counts[library.PARTIAL]
        lines = [
            self._field("Folder", os.path.join(
                self.lib.folder, self.lib.bundle_folder(bundle))),
            self._field("Platform", self.platform or "all"),
            self._field("Files", "%d: %s" % (len(jobs), ", ".join(
                "%d %s" % (counts[s], s)
                for s in (library.MISSING, library.PARTIAL, library.HAVE,
                          library.UNTRACKED, library.CHANGED,
                          library.MODIFIED) if counts[s]))),
            self._field("Download", library.human_bytes(counts['bytes'])),
            self._field("Free", library.human_bytes(counts['free'])),
        ]
        flagged = [j for j in jobs if j.status in library.FLAGGED]
        if flagged:
            lines += ["", "[b]Left untouched[/b] (changed on the server"
                      " or locally):"]
            lines += ["  [red]%s[/red] %s" % (j.status, escape(j.relpath))
                      for j in flagged]
        if counts['bytes'] > counts['free']:
            lines += ["", "[red b]Not enough free disk space.[/red b]"]
            self.push_screen(ConfirmScreen("Download %s" % escape(label),
                                           "\n".join(lines), yes="OK",
                                           no="Close"))
            return
        if not todo and not counts[library.UNTRACKED]:
            lines += ["", "Everything is already downloaded."]
            self.push_screen(ConfirmScreen("Download %s" % escape(label),
                                           "\n".join(lines), yes="OK",
                                           no="Close"))
            return

        def confirmed(yes):
            if yes:
                self.ensure_auth(bundle, lambda: self.enqueue(
                    DownloadItem(label, bundle, jobs)))

        self.push_screen(ConfirmScreen("Download %s?" % escape(label),
                                       "\n".join(lines), yes="Download"),
                         confirmed)

    def ask_folder(self, then):
        '''Ask for the download folder, save it and open it, then call then'''

        def validate(path):
            return library.set_folder(path, self.hb.configdir)

        def done(path):
            if not path:
                return
            log.info("Download folder set to %s", path)
            self.open_library()
            if self.lib_error:
                return
            self.notify("Download folder set to %s" % path)
            # The folder is read in the background; continue once it is
            self.set_timer(0.2, lambda: self._when_scanned(then))

        self.push_screen(InputScreen(
            "Set the download folder",
            "All downloads go to this folder, as BUNDLE/PLATFORM/FILE."
            " It is saved in config.ini and must already exist.",
            placeholder="/path/to/folder", validate=validate), done)

    def _when_scanned(self, then, tries=50):
        if self.lib:
            then()
        elif tries and not self.lib_error:
            self.set_timer(0.2, lambda: self._when_scanned(then, tries - 1))

    def ensure_auth(self, bundle, then):
        '''Make sure the session cookie is set, asking for it if needed,
        then call then. Games in no bundle do not need it.'''
        if self.has_auth or bundle == library.UNBUNDLED:
            then()
            return
        auth = None
        try:
            auth = humblebundle.read_auth()
        except Exception as e:
            log.warning("Could not read the keyring: %s", e)
        if auth:
            try:
                self.hb.set_auth(auth)
                self.has_auth = True
                then()
                return
            except humblebundle.HumbleBundleError as e:
                log.warning("Saved session cookie is invalid: %s", e)
        self.ask_auth(then)

    def ask_auth(self, then, expired=False):
        def validate(value):
            value = humblebundle.save_auth(value)
            self.hb.set_auth(value)
            return value

        def done(value):
            if value:
                self.has_auth = True
                then()

        self.push_screen(InputScreen(
            "Session cookie expired" if expired else "Log in",
            "Log in to humblebundle.com in a browser, then copy the value of"
            " its _simpleauth_sess cookie and paste it here. It is saved in"
            " the system keyring.",
            placeholder="_simpleauth_sess value", password=True,
            validate=validate), done)

    def enqueue(self, item):
        self.queue.append(item)
        log.info("Queued %s: %d files", item.label, len(item.jobs))
        self.start_downloads()

    def start_downloads(self):
        if self.worker is not None or not self.queue:
            self.update_panel()
            return
        self.cancel_event.clear()
        self.query_one("#downloads").add_class("active")
        self.worker = self.run_worker(self._download_queue, thread=True,
                                      exit_on_error=False, group="download")

    def _download_queue(self):
        '''Download the queued items one at a time. Runs in a thread.'''
        stop = None
        while True:
            if self.cancel_event.is_set():
                break
            try:
                item = self.queue.popleft()
            except IndexError:
                break
            self.current = item
            self.call_from_thread(self.update_panel)
            try:
                item.result = self.lib.sync(item.jobs,
                                            progress=self._on_progress,
                                            cancel=self.cancel_event,
                                            on_file=self._on_file)
            except library.LibraryError as e:
                # Missing folder, full disk, no network: stop the queue
                log.error("%s: %s", item.label, e)
                stop = ("error", str(e))
                break
            except humblebundle.HumbleBundleError as e:
                # Not logged in: ask again, then resume this item
                log.error("%s: %s", item.label, e)
                self.queue.appendleft(item)
                stop = ("auth", str(e))
                break
            except Exception as e:
                log.exception("Download of %s failed: %s", item.label, e)
                stop = ("error", str(e))
                break
            finally:
                self.call_from_thread(self._file_done)
            r = item.result
            log.info("%s: downloaded %d, already had %d, flagged %d,"
                     " failed %d%s", item.label, len(r.downloaded),
                     len(r.skipped), len(r.flagged), len(r.failed),
                     ", cancelled" if r.cancelled else "")
            for job, error in r.failed:
                log.error("  %s: %s", job.relpath, error)
            if r.cancelled:
                break
        self.call_from_thread(self._queue_stopped, stop)

    def _on_file(self, job, index, count):
        self._last_progress = 0
        self.call_from_thread(self._file_started, job, index, count)

    def _on_progress(self, job, done, total):
        now = time.monotonic()
        if now - self._last_progress < PROGRESS_INTERVAL and done != total:
            return
        self._last_progress = now
        self.call_from_thread(self._show_progress, job, done, total)

    def _file_started(self, job, index, count):
        self._file_done()
        item = self.current
        if item is None:
            return
        verb = {library.PARTIAL: "Resuming",
                library.UNTRACKED: "Checking"}.get(job.status, "Downloading")
        if job.status in (library.HAVE,) + library.FLAGGED:
            verb = "Skipping"
        self.query_one("#dl-title", Static).update(
            "[b]%s[/b]  [%d/%d] %s %s [dim](%s)[/dim]" % (
                escape(item.label), index, count, verb, escape(job.relpath),
                escape(job.human_size)))
        bar = self.query_one("#dl-file", ProgressBar)
        bar.update(total=job.size or None, progress=job.partial_size)
        self._update_total()

    def _show_progress(self, job, done, total):
        self.query_one("#dl-file", ProgressBar).update(
            total=total or None, progress=done)
        self._update_total(job, done)

    def _update_total(self, current=None, done=0):
        item = self.current
        if item is None:
            return
        total = sum(j.size for j in item.jobs)
        have = sum(j.size for j in item.jobs
                   if j is not current and j.status == library.HAVE)
        if current is not None:
            have += done
        self.query_one("#dl-total", ProgressBar).update(
            total=total or None, progress=have)

    def _file_done(self):
        '''A file finished: update the status shown in the tree'''
        self.refresh_markers()
        self.refresh_details()

    def _queue_stopped(self, stop):
        self.worker = None
        self.current = None
        if self.cancel_event.is_set():
            dropped = len(self.queue)
            self.queue.clear()
            log.info("Downloads cancelled%s. Partial files are kept and"
                     " resumed next time.", " and %d queued items removed"
                     % dropped if dropped else "")
        self.cancel_event.clear()
        self._file_done()
        if stop and stop[0] == "auth":
            self.ask_auth(self.start_downloads, expired=True)
        elif stop:
            self.notify("Downloads stopped: %s" % stop[1], severity="error",
                        timeout=10)
        elif self.queue:
            self.start_downloads()
            return
        self.update_panel()

    def update_panel(self):
        panel = self.query_one("#downloads")
        waiting = len(self.queue)
        if self.current is None and not waiting:
            panel.remove_class("active")
            return
        panel.add_class("active")
        text = ("Queued: %s" % ", ".join(escape(i.label) for i in self.queue)
                if waiting else "Nothing else queued")
        self.query_one("#dl-queue", Static).update(
            "[dim]%s   x to cancel[/dim]" % text)

    def action_cancel_downloads(self):
        if self.worker is None and not self.queue:
            self.notify("No downloads running")
            return
        if self.worker is None:
            self.queue.clear()
            self.update_panel()
            return
        self.cancel_event.set()
        self.notify("Cancelling downloads...")

    async def action_quit(self):
        if self.worker is None:
            self.exit()
            return

        def answer(yes):
            if yes:
                self.cancel_event.set()
                self.queue.clear()
                self._exit_when_stopped()

        self.push_screen(ConfirmScreen(
            "Quit while downloading?",
            "The current download is stopped. Its partial file is kept and"
            " resumed the next time you download it.", yes="Quit"), answer)

    def _exit_when_stopped(self):
        if self.worker is None:
            self.exit()
        else:
            self.set_timer(0.1, self._exit_when_stopped)

    # Actions ----------------------------------------------------------------

    def action_focus_filter(self):
        self.query_one("#filter", Input).focus()

    def action_clear_filter(self):
        filter_input = self.query_one("#filter", Input)
        if filter_input.value:
            filter_input.value = ""  # triggers Input.Changed
        self.query_one("#tree", Tree).focus()

    def _cycle_platform(self, step):
        self.platform_index = (self.platform_index + step) % len(self.platforms)
        self.rebuild_tree()
        self.refresh_details()

    def action_next_platform(self):
        self._cycle_platform(1)

    def action_previous_platform(self):
        self._cycle_platform(-1)

    def action_toggle_empty(self):
        self.show_empty = not self.show_empty
        self.rebuild_tree()

    def action_expand_all(self):
        for node in self.query_one("#tree", Tree).root.children:
            node.expand()

    def action_collapse_all(self):
        for node in self.query_one("#tree", Tree).root.children:
            node.collapse()

    def action_toggle_log(self):
        self.query_one("#log", RichLog).toggle_class("hidden")

    def check_action(self, action, parameters):
        if action in self.MAIN_ACTIONS:
            if isinstance(self.screen, ModalScreen):
                return False
            # Let printable keys reach the filter box while typing in it
            if action != "clear_filter" and isinstance(self.focused, Input):
                return False
        return True


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(
        description="Browse cached Humble Bundle games and bundles, and"
                    " download them to the download folder. Run"
                    " 'hb --update' to refresh the cache.")
    parser.add_argument('-D', '--debug', dest='loglevel',
                        action="store_const", const=logging.DEBUG,
                        default=logging.INFO,
                        help="Show debug messages in the log panel")
    args = parser.parse_args(argv)
    HumbleBundleApp(loglevel=args.loglevel).run()


if __name__ == '__main__':
    main()
