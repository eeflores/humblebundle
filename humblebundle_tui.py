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

'''Read-only browser for the cached Humble Bundle games and bundles.

It only reads the cache written by `hb --update`: it makes no
network requests and does not download, install or uninstall anything.
'''

import logging
import sys
import threading

try:
    from rich.console import Group
    from rich.markup import escape
    from rich.table import Table
    from rich.text import Text
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.widgets import Footer, Header, Input, RichLog, Static, Tree
except ImportError:
    sys.exit("The TUI requires Textual. Install it with:"
             " pip install 'humblebundle-manager[tui]'")

import humblebundle


log = logging.getLogger(__name__)

# Label of the tree branch for games that belong to no bundle,
# such as the ones from extras.json
NO_BUNDLE = "(not in any bundle)"


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


class HumbleBundleApp(App):

    TITLE = "Humble Bundle Manager"

    CSS = """
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
        Binding("p", "next_platform", "Platform"),
        Binding("P", "previous_platform", "Platform", show=False),
        Binding("h", "toggle_empty", "No-download items"),
        Binding("e", "expand_all", "Expand"),
        Binding("c", "collapse_all", "Collapse"),
        Binding("l", "toggle_log", "Log"),
        Binding("q", "quit", "Quit"),
    ]

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
            # auth="" skips reading the keyring: no requests are made here
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
            node = tree.root.add(
                "%s [dim](%d)[/dim]" % (escape(name), len(games)),
                data=("bundle", bundle['machine_name']),
                expand=expand, allow_expand=bool(games))
            for key in self._sorted_games(games):
                node.add_leaf(escape(self.hb.games[key].get('human_name', key)),
                              data=("game", key))
            shown_bundles += 1
            seen_games.update(games)

        others = matching([k for k in self.hb.games if k not in bundled],
                          self.filter_text)
        if others:
            node = tree.root.add("[i]%s[/i] [dim](%d)[/dim]" %
                                 (NO_BUNDLE, len(others)),
                                 data=None, expand=expand)
            for key in self._sorted_games(others):
                node.add_leaf(escape(self.hb.games[key].get('human_name', key)),
                              data=("game", key))
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
        self.filter_text = event.value.strip().lower()
        self.rebuild_tree()

    def on_input_submitted(self, event: Input.Submitted):
        self.query_one("#tree", Tree).focus()

    # Details ----------------------------------------------------------------

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted):
        data = event.node.data
        if not data or not self.hb:
            return
        kind, key = data
        try:
            if kind == "game":
                text = self.format_game(key)
            else:
                text = self.format_bundle(key)
        except humblebundle.HumbleBundleError as e:
            text = escape(str(e))
        self._show_message(text)
        self.query_one("#details-scroll", VerticalScroll).scroll_home(
            animate=False)

    @staticmethod
    def _field(label, value):
        return "[b]%-10s[/b] %s" % (label, escape(str(value or "")))

    def format_game(self, key):
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
        for bundle in bundles:
            lines.append("  %s [dim]\\[%s][/dim]" % (
                escape(bundle.get('human_name', '')),
                escape(bundle.get('machine_name', ''))))
        if not bundles:
            lines.append("  [dim]none[/dim]")

        lines += ["", "[b]Downloads[/b]"]
        downloads = hb.get_downloads(key)
        if not downloads:
            lines.append("  [dim]none[/dim]")
            return "\n".join(lines)

        table = Table(box=None, show_header=False, pad_edge=False,
                      padding=(0, 1), expand=True)
        table.add_column("name", ratio=3, overflow="fold")
        table.add_column("arch", no_wrap=True)
        table.add_column("size", justify="right", no_wrap=True)
        table.add_column("file", ratio=4, overflow="fold", style="dim")
        previous = None
        for platform, files in downloads:
            if platform != previous:
                style = "bold reverse" if platform == self.platform else "underline"
                table.add_row(Text.assemble("  ", (platform, style)))
                previous = platform
            if not files:
                table.add_row(Text("    none", style="dim"))
            for d in files:
                table.add_row(Text("    " + d.get('name', '')),
                              "%s-bit" % d['arch'] if d.get('arch') else "",
                              d.get('human_size', ''),
                              hb._download_basename(d))
        return Group(Text.from_markup("\n".join(lines)), table)

    def format_bundle(self, key):
        hb = self.hb
        bundle = hb.get_bundle(key)
        lines = [
            self._field("Bundle", bundle.get('machine_name')),
            self._field("Name", bundle.get('human_name')),
            self._field("Category", bundle.get('category')),
            self._field("Price US$", bundle.get('familyamount')),
            "",
            "[b]Games[/b] (%d)" % len(bundle.get('games', [])),
        ]
        for name in sorted(bundle.get('games', [])):
            game = hb.games.get(name)
            if game is None:
                lines.append("  [dim]%s (missing from games data)[/dim]" %
                             escape(name))
                continue
            lines.append("  %s [dim]\\[%s][/dim]" % (
                escape(game.get('human_name', '')), escape(name)))
        return "\n".join(lines)

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
        # Let printable keys reach the filter box while typing in it
        if (action in ("next_platform", "previous_platform", "toggle_empty",
                       "expand_all",
                       "collapse_all", "toggle_log", "quit", "focus_filter")
                and isinstance(self.focused, Input)):
            return False
        return True


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(
        description="Browse cached Humble Bundle games and bundles."
                    " Read-only: run 'hb --update' to refresh"
                    " the cache.")
    parser.add_argument('-D', '--debug', dest='loglevel',
                        action="store_const", const=logging.DEBUG,
                        default=logging.INFO,
                        help="Show debug messages in the log panel")
    args = parser.parse_args(argv)
    HumbleBundleApp(loglevel=args.loglevel).run()


if __name__ == '__main__':
    main()
