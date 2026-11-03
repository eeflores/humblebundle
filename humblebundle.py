#!/usr/bin/env python3
#
# humblebundle - Manager for Humble Bundle games and bundles
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

# TODO:
# - INI-format config file for non-auth settings like arch-pref, debug level
# - Log to file, with debug or info level
# - Add --clear-cache, --clear-credentials, --clear-data, and --purge

import os
import os.path as osp
import sys
import re
import json
import logging
import argparse
import time
import threading
import subprocess
import shlex
import shutil
import getpass
import configparser
import tempfile
import http.cookiejar as cookielib
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlsplit, parse_qs

# PyPI: pyxdg
import xdg.BaseDirectory as xdg

# PyPI: keyring
import keyring
import keyring.errors


import httpbot


log = logging.getLogger(__name__)


# Defaults are suitable for usage as a library (imported module)
# They're changed when used as a script to adjust for __name__
APPNAME   = __name__
DATADIR   = osp.dirname(osp.realpath(__file__))
CONFIGDIR = osp.join(xdg.xdg_config_home, APPNAME)
CACHEDIR  = osp.join(xdg.xdg_cache_home, APPNAME)
COOKIEJAR = osp.join(CONFIGDIR, "cookies.txt")

# Keyring entry holding the _simpleauth_sess session cookie.
# The service name is APPNAME.
KEYRING_USER = "SESSION_COOKIE"

# Maximum number of simultaneous requests made by --update
DEFAULT_CONNECTIONS = 4


class HumbleBundleError(Exception):
    pass


class NoDownloadError(HumbleBundleError):
    '''No download matches the requested criteria'''
    def __init__(self, message, criteria=None):
        super().__init__(message)
        self.criteria = criteria or {}


class AmbiguousDownloadError(HumbleBundleError):
    '''More than one download matches the requested criteria.
    candidates holds the matching download dicts, so callers can pick one
    and pass it to download(selected=...).
    '''
    def __init__(self, message, candidates, criteria=None):
        super().__init__(message)
        self.candidates = candidates
        self.criteria = criteria or {}


def auth_expiry(auth):
    '''Return the expiry timestamp for a _simpleauth_sess cookie value.

    The value has the format "<data>|<timestamp>|<signature>".
    Raise HumbleBundleError if it does not.
    '''
    parts = auth.split('|')
    if len(parts) != 3 or not parts[1].isdigit():
        raise HumbleBundleError(
            "Invalid _simpleauth_sess cookie: expected 3 fields separated"
            " by '|', with a numeric timestamp in the middle")
    return int(parts[1]) + 730 * 24 * 60 * 60


def read_auth(appname=None):
    '''Return the session cookie saved in the keyring, or None'''
    return keyring.get_password(appname or APPNAME, KEYRING_USER)


def config_path(configdir=None):
    '''Return the path of the config.ini file'''
    return osp.join(configdir or CONFIGDIR, "config.ini")


def read_config(configdir=None):
    '''Return the settings in config.ini as a ConfigParser.
    A missing file gives empty settings.
    '''
    config = configparser.ConfigParser(interpolation=None)
    config.optionxform = str  # keep the case of keys, such as bundle names
    path = config_path(configdir)
    try:
        with open(path, encoding='utf-8') as fp:
            config.read_file(fp)
    except FileNotFoundError:
        pass
    except configparser.Error as e:
        raise HumbleBundleError("Invalid config file '%s': %s" % (path, e))
    return config


def write_config(config, configdir=None):
    '''Save a ConfigParser to config.ini, replacing it atomically'''
    path = config_path(configdir)
    os.makedirs(osp.dirname(path), 0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=osp.dirname(path), prefix=".config.")
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fp:
            config.write(fp)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        os.remove(tmp)
        raise
    log.info("Saved settings to '%s'", path)


def prompt_auth(appname=None):
    '''Prompt for the session cookie and save it in the keyring'''
    auth = getpass.getpass("Paste the value of the _simpleauth_sess cookie"
                           " (input is hidden): ").strip().strip('"')
    return save_auth(auth, appname)


def save_auth(auth, appname=None):
    '''Validate the session cookie value and save it in the keyring.
    Return it, stripped of spaces and quotes.'''
    auth = auth.strip().strip('"')
    if not auth:
        raise HumbleBundleError("No session cookie entered")
    auth_expiry(auth)  # validate before saving
    keyring.set_password(appname or APPNAME, KEYRING_USER, auth)
    log.info("Session cookie saved in the keyring")
    return auth

class HumbleBundle(httpbot.HttpBot):

    name = "Humble Bundle"
    url = "https://www.humblebundle.com"
    auth_urls = ("/login", "/user/humbleguard")

    def __init__(self, auth=None,
                 datadir=None, configdir=None, cachedir=None, cookiejar=None,
                 connections=DEFAULT_CONNECTIONS, debug=False,
                 autoupdate=True):
        '''auth is the _simpleauth_sess cookie value.
        If omitted, it is read from the keyring.
        connections is the maximum number of simultaneous requests
        made by update().
        autoupdate controls what happens when there is no cached data:
        if True, fetch it from the server now with update(); if False,
        start with no games and bundles and leave fetching to the caller.
        Check has_data() to see if the cache was loaded.
        '''
        if connections < 1:
            raise HumbleBundleError("connections must be at least 1")
        self.connections = connections
        # Serializes writes to the cookies file from concurrent requests
        self._cookie_lock = threading.Lock()
        # Serializes changes to bundles and games from other threads
        self._data_lock = threading.Lock()

        self.datadir   = datadir   or DATADIR
        self.configdir = configdir or CONFIGDIR
        self.cachedir  = cachedir  or CACHEDIR

        if not os.path.isdir(self.configdir):
            # Create the config dir as xdg would. Let exceptions bubble up
            os.makedirs(self.configdir, 0o700)

        self.cookiejar = cookielib.MozillaCookieJar(filename=(cookiejar or
                                                              COOKIEJAR))
        try:
            self.cookiejar.load()
        except (IOError, cookielib.LoadError) as e:
            log.error('Error reading cookies: %s', e)

        if auth is None:
            auth = read_auth()
            if not auth:
                log.warning("No session cookie in the keyring."
                            " Run with --auth to set it")

        if auth:
            self.set_auth(auth)

        super(HumbleBundle, self).__init__(self.url,
                                           tag=APPNAME,
                                           cookiejar=self.cookiejar,
                                           debug=debug)

        # "purchases" in the website. May be non-bundle like Store Purchases
        self.bundles = {}
        # "subproducts" in json. May be not a game, like Soundtracks and eBooks
        self.games   = {}

        self.gamedata = osp.join(self.datadir, "gamedata.json")

        if not self.load() and autoupdate:
            self.update()


    def set_auth(self, auth):
        '''Use auth, the _simpleauth_sess cookie value, for later requests.
        Raise HumbleBundleError if it is not a valid cookie value.
        '''
        log.info("Injecting authenticated cookie")
        expires = auth_expiry(auth)
        cookie = cookielib.Cookie(version = 0,
                                  name = '_simpleauth_sess',
                                  value = auth,
                                  port = None,
                                  port_specified = False,
                                  domain = urlsplit(self.url)[1],
                                  domain_specified = False,
                                  domain_initial_dot = False,
                                  path = '/',
                                  path_specified = False,
                                  secure = True,
                                  expires = expires,
                                  discard = False,
                                  comment = None,
                                  comment_url = None,
                                  rest={},)
        self.cookiejar.set_cookie(cookie)


    def load(self):
        '''Load bundles and games from the cache files, without network access.
        Return True if loaded, False if the cache files do not exist.
        '''
        try:
            with open(osp.join(self.configdir, "bundles.json")) as fp1:
                with open(osp.join(self.configdir, "games.json")) as fp2:
                    self.bundles = json.load(fp1)
                    self.games   = json.load(fp2)
                    log.info("Loaded %d games from %d bundles",
                             len(self.games),
                             len(self.bundles))
        except IOError:
            log.info("No cached data in '%s'", self.configdir)
            return False
        self._merge()
        return True


    def has_data(self):
        '''Return True if there are any games or bundles loaded'''
        return bool(self.games or self.bundles)


    def _merge(self):
        # Merge extras
        extras = osp.join(self.datadir, "extras.json")
        log.debug("Merging extras from %s", extras)
        try:
            with open(extras) as fp:
                games = json.load(fp)
            self.games.update(games)
        except (IOError, ValueError) as e:
            log.warning("Error merging extras: %s", e)

        # Merge install instructions
        log.debug("Merging games install data from %s", self.gamedata)
        try:
            with open(self.gamedata) as fp:
                games = json.load(fp)
            for game in self.games:
                self.games[game].update(games.get(game, {}))
        except (IOError, ValueError) as e:
            log.warning("Error merging games install data: %s", e)


    def update(self, progress=None, cancel=None):
        '''Fetch all bundles and games from the server, rebuilding the cache.

        progress, if given, is called as progress(done, total, name) after
        each order is retrieved, where name is the bundle's human name.
        cancel, if given, is a threading.Event. Setting it stops the update
        with HumbleBundleError, leaving the current data untouched.
        '''
        # Get the keys
        log.info("Retrieving keys from '%s/home/keys'", self.url)
        match = re.search(r'^.*[\'"]?gamekeys[\'"]?\s*[:=]\s*(\[.*\])',
                          self.get('/home/keys').read().decode('utf-8'),
                          re.MULTILINE)
        if not match:
            raise HumbleBundleError("GameKeys list not found")

        # Loop the bundles, with at most self.connections requests at a time.
        # Build into new dicts so a failure leaves the current data untouched.
        keys = json.loads(match.groups()[0])
        bundles = {}
        games = {}
        log.info("Retrieving %d orders using %d connection(s)",
                 len(keys), self.connections)
        with ThreadPoolExecutor(max_workers=self.connections) as pool:
            futures = [pool.submit(self._load_key, key, True) for key in keys]
            # Collect in the original order of keys
            for i, (key, future) in enumerate(zip(keys, futures), 1):
                if cancel is not None and cancel.is_set():
                    pool.shutdown(wait=True, cancel_futures=True)
                    raise HumbleBundleError(
                        "Update cancelled at order %d of %d."
                        " Cached data was not changed." % (i, len(keys)))
                try:
                    bundle, bundle_games = future.result()
                except (httpbot.HTTPError, OSError, ValueError) as e:
                    pool.shutdown(wait=True, cancel_futures=True)
                    log.debug("Failed order key: %s", key)
                    raise HumbleBundleError(
                        "Update aborted at order %d of %d: %s."
                        " Cached data was not changed." % (i, len(keys), e))
                bundles.update(bundle)
                games.update(bundle_games)
                for info in bundle.values():
                    name = (info.get('human_name') or
                            info.get('machine_name', '?'))
                    log.info("[%d/%d] %s (%d items)", i, len(keys),
                             name, len(bundle_games))
                    if progress:
                        progress(i, len(keys), name)

        self.bundles = bundles
        self.games   = games

        log.info("Updated %d games from %d bundles",
                 len(self.games),
                 len(self.bundles))
        self._merge()
        self._save_data()


    def refresh_order(self, key):
        '''Reload one order (bundle) from the server, which also gives fresh
        download links for its games, and save the cache'''
        with self._data_lock:
            bundle, games = self._load_key(key, batch=True)
            self.bundles.update(bundle)
            self.games.update(games)
            self._merge()
            self._save_data()


    def _save_data(self):
        for obj in ['bundles', 'games']:
            path = osp.join(self.configdir, "%s.json" % obj)
            try:
                with open(path, 'w') as f:
                    json.dump(getattr(self, obj), f,
                              indent=2, separators=(',', ': '), sort_keys=True)
                os.chmod(path, 0o600)
            except IOError as e:
                log.error("Error saving cache data: %s", e)


    def _load_key(self, key, batch=False):

        url = "/api/v1/order/%s" % key
        log.debug("Retrieving purchase info from '%s%s'", self.url, url)
        bundle = json.load(self.get(url))
        bundle['games'] = []  # made-up field: list of games it contains
        bundlekey = bundle['product']['machine_name']

        # Loop each game in the bundle
        games = {}
        for game in bundle['subproducts']:
            gamekey = game['machine_name']

            # Add game name to its bundle game list
            bundle['games'].append(gamekey)

            # Add custom fields and insert game in games dict (overwriting)
            game['bundle'] = bundlekey  # made-up field: bundle it was retrieved from
            games[gamekey] = game

        # remove the now redundant "subproducts" list
        del bundle['subproducts']

        # Remove useless fields, that may not be present anyway
        bundle.pop('subscriptions', None)

        # Move 'products' sub-dict to root
        for k, v in bundle['product'].items():
            bundle[k] = v
        del bundle['product']

        # Sort games list
        bundle['games'].sort()

        # Batch-processing: do not update nor save bundles and games dict
        if batch:
            return ({bundlekey:bundle}, games)

        # Add bundle to bundles list
        self.games.update(games)
        self.bundles[bundlekey] = bundle
        self._save_data()


    def download(self, name, path=None, bittorrent=False,
                 dtype=None, arch=None, platform=None, serverfile=None,
                 type_pref=".deb", arch_pref="64",
                 retry=True, selected=None, progress=None, cancel=None):
        '''Download a file of game name and return its saved path,
        or None if the download was aborted or is corrupt.

        The file is chosen by dtype, arch, platform and serverfile, falling
        back to type_pref and arch_pref. Raise NoDownloadError if none match,
        and AmbiguousDownloadError (with the candidates) if many do.
        selected is a download dict, as returned by find_downloads(), to
        use instead of choosing one. Pass its platform too, as it is needed
        to find it again if the download URL has expired.

        progress, if given, is called as progress(done, total) while
        downloading, and replaces the console output and progress bar.
        cancel, if given, is a threading.Event that aborts the download.
        '''
        game = self.get_game(name)
        if selected is not None:
            d = selected
        else:
            d = self._choose_download(name=name, dtype=dtype, arch=arch,
                                      platform=platform, serverfile=serverfile,
                                      type_pref=type_pref, arch_pref=arch_pref)

        # MD5 sum matches direct download files, not .torrent ones.
        if bittorrent:
            md5 = None
        else:
            md5 = d.get('md5', '').lower() or None

        url = d.get('url', {}).get('bittorrent' if bittorrent else 'web','')
        if not url:
            raise NoDownloadError("Selected download for '%s' has no %s URL" %
                                  (name, 'bittorrent' if bittorrent else 'web'))

        # Check if URL has expired
        try:
            ttl = int(parse_qs(urlsplit(url).query)['ttl'][0])
        except KeyError:
            ttl = 0  # No TTL
        except (IndexError, ValueError) as e:
            ttl = -1  # Invalid TTL
        if ttl and ttl < time.time():
            if not retry:
                raise HumbleBundleError("Game data for '%s' expired %s." %
                                        (name, time.ctime(ttl)))

            log.debug("Game data for '%s' expired %s, will update and retry.",
                      name, time.ctime(ttl))
            self._load_key(self.bundles.get(game.get('bundle', ''),
                                            {}).get('gamekey', ''))
            if selected is not None:
                # The reloaded game has new download dicts, so find the
                # selected one again by its server file name
                serverfile = self._download_basename(selected)
            return self.download(name=name,
                                 path=path,
                                 dtype=dtype,
                                 arch=arch,
                                 platform=platform,
                                 bittorrent=bittorrent,
                                 type_pref=type_pref,
                                 arch_pref=arch_pref,
                                 serverfile=serverfile,
                                 retry=False,
                                 progress=progress,
                                 cancel=cancel)

        message = "Downloading '%s' [%s]\t%s" % (
            game['human_name'], game['machine_name'], self._download_info(d))
        if progress is None:
            print(message)
        else:
            log.info(message)
        try:
            return super(HumbleBundle, self).download(url, path, md5,
                                                      callback=progress,
                                                      cancel=cancel)
        except httpbot.HTTPError as e:
            # Handle Unauthorized/Not Found (most likely outdated download URL)
            if not e.code in (403, 404):
                raise
            raise HumbleBundleError(
                "Download error: %d %s. URL may be outdated, try --update." %
                (e.code, e.reason))

    def _download_basename(self, d):
        basename = osp.basename(urlsplit(d.get('url', {}).get('web', "")).path)
        return basename

    def _download_info(self, d):
        a = "\t(%s-bit)" % d['arch'] if d.get('arch', None) else ""
        return "\t\t%-20s%s\t%8s\t%s" % (d['name'], a, d['human_size'],
                                         self._download_basename(d))


    def download_info(self, d):
        '''Return a one-line description of a download dict'''
        return self._download_info(d).strip()


    def find_downloads(self, name, platform=None, dtype=None, arch=None,
                       serverfile=None):
        '''Return the list of download dicts of game name for platform that
        have a URL and match dtype, arch and serverfile, when given.
        Downloads with no explicit architecture get one guessed from their
        name, when possible.
        '''
        game = self.get_game(name)
        candidates = []

        for plat in game.get('downloads', []):
            if plat.get('platform', '') == platform:
                for download in plat.get('download_struct', []):
                    if download in candidates:
                        continue
                    if not download.get('url', ''):
                        continue
                    if (serverfile and
                        serverfile.lower() !=
                            self._download_basename(download).lower()):
                        continue
                    if (dtype and
                        dtype.lower() not in download.get('name','').lower()):
                        continue
                    if not download.get('arch', ''):
                        if re.search('(?:32|64)[- ]?bit|i386|x86_64',
                                     download.get('name','')):
                            if '64' in download['name']:
                                download['arch'] = "64"
                            else:
                                download['arch'] = "32"
                    if (arch and
                        download.get('arch', '') and
                        download['arch'] != arch):
                        continue

                    candidates.append(download)

        return candidates


    def _choose_download(self, name, dtype=None, arch=None, platform=None,
                         serverfile=None, type_pref=None, arch_pref=None):
        '''Return the single download dict matching the criteria and
        preferences. Raise NoDownloadError or AmbiguousDownloadError.
        '''
        game = self.get_game(name)
        criteria = {'dtype':dtype,
                    'arch':arch,
                    'serverfile':serverfile,
                    'platform':platform}
        candidates = self.find_downloads(name, platform=platform, dtype=dtype,
                                         arch=arch, serverfile=serverfile)
        finalists = []

        if len(candidates) == 1:
            return candidates[0]

        if len(candidates) == 0:
            raise NoDownloadError(
                "No valid downloads for game '%s' [%s]\n\t criteria %r" %
                (game['human_name'], game['machine_name'], criteria),
                criteria)

        log.debug("Many download candidates for '%s' [%s]\n\tcriteria %r:\n%s",
                  game['human_name'], game['machine_name'], criteria,
                  json.dumps(candidates, indent=2))

        # Try type (download name) preference
        if not dtype and type_pref:
            for download in candidates:
                if type_pref.lower() in download.get('name', '').lower():
                    finalists.append(download)

        if len(finalists) == 1:
            return finalists[0]

        # Multiple finalists. Set them as next candidates
        # If no finalists, candidates remain the same
        if len(finalists) > 1:
            candidates = finalists[:]
        finalists = []

        # Try arch preference
        if not arch and arch_pref:
            for download in candidates:
                if download.get('arch', '') and download['arch'] == arch_pref:
                    finalists.append(download)

        if len(finalists) == 1:
            return finalists[0]

        # Try type again, with more restrictive matching
        if dtype:
            for rule in ('starts', 'is'):
                if finalists:
                    candidates = finalists[:]
                    finalists = []
                for download in candidates:
                    name = download.get('name', '').lower()
                    if ((rule == 'is'     and name == dtype.lower()) or
                        (rule == 'starts' and name.startswith(dtype.lower()))):
                        finalists.append(download)
                if len(finalists) == 1:
                    return finalists[0]

        # Give up
        remaining = finalists or candidates
        raise AmbiguousDownloadError(
            "Too many download candidates for '%s' [%s]."
            " Improve criteria to narrow it down.%s" %
            (game['human_name'], game['machine_name'],
             "".join(["\n%s" % self._download_info(x) for x in remaining])),
            remaining, criteria)


    def install(self, name, method=None):
        # References:
        # Steam: https://developer.valvesoftware.com/wiki/Steam_browser_protocol
        # USC:   https://software-center.ubuntu.com/subscriptions/
        # Mojo:  scripts/mojosetup_mainline.lua

        game = self.get_game(name)
        method = method or game.get('install', "").lower()

        def execute(command, cwd=None):
            return executecmd(shlex.split(command), cwd)

        def executecmd(command, cwd=None):
            try:
                log.debug("Executing: %s", command)
                subprocess.check_call(command, cwd=cwd)
            except (subprocess.CalledProcessError, OSError) as e:
                if getattr(e, 'errno', 0) == 2:  # OSError, No such file or directory
                    log.error("Error installing '%s': %s: %s",
                              name, e.strerror, shlex.split(command)[0])
                else:
                    log.error("Error installing '%s': %s", name, e)

        def download(specs):
            for spec in specs:
                specs[spec] = game.get(spec, specs[spec])
            specs['dtype'] = specs.pop('download', None)
            # make sure cachedir ends with a trailing slash, see HttpBot.download()
            return self.download(name, path=osp.join(self.cachedir, ''), **specs)

        if not method:
            raise HumbleBundleError("No install data for '%s', please check '%s'"
                                    " or use --method" %
                                    (name, self.gamedata))

        elif method == "deb":
            specs = dict(download=None, arch=None, platform="linux",
                         type_pref=".deb", arch_pref="64")
            deb = download(specs)
            if deb:
                execute("sudo dpkg --force-depends --install '%s'" % deb)
                execute("sudo apt-get --yes --fix-broken install")

        elif method == "apt":
            package = game.get("package", name)
            execute('sudo apt-get install --yes "%s"' % package)

        elif method == "steam":
            try:
                execute("steam steam://install/%d" % game['steamid'])
            except KeyError:
                raise HumbleBundleError(
                    "No steamid for steam-installable game '%s'" % name)

        elif method == "mojo":
            specs = dict(download=None, arch=None, platform="linux",
                         type_pref="sh", arch_pref="64")
            installer = download(specs)
            if not installer:
                raise HumbleBundleError(
                    "Could not download installer for game '%s'" % name)

            path = osp.join(osp.expanduser("~/.local/opt"),
                            game.get('mojoname', name.split("_", 1)[0].title()))
            execute("chmod +x '%s'" % installer)
            execute("'%s' -- --destination '%s' --noreadme --noprompt"
                    " --nooptions --i-agree-to-all-licenses" %
                    (installer, path))

        elif method == "air":
            specs = dict(download=None, arch=None, platform="linux",
                         type_pref="air", arch_pref="64")
            installer = download(specs)
            if not installer:
                raise HumbleBundleError(
                    "Could not download installer for game '%s'" % name)

            adobeair = "/usr/bin/Adobe AIR Application Installer"
            if not osp.isfile(adobeair):
                self.install('adobeair')
            execute("'%s' '%s'" % (adobeair, installer))

        elif method == "custom":
            specs = dict(download=None, arch=None, platform="linux",
                         type_pref=None, arch_pref="64")
            archive = download(specs)
            if not archive:
                raise HumbleBundleError(
                    "Could not download installer for game '%s'" % name)

            hookdir = osp.join(self.datadir, "hooks", name)
            hookfile = osp.join(hookdir, "%s.install.hook" % name)
            basename = game.get('basename', name.split("_", 1)[0])
            # FIXME: Make sure basename is valid: single word, no punc, etc
            installdir = osp.join(osp.expanduser("~"), '.local', 'opt',
                                  game.get('dirname', basename))
            executecmd([hookfile,
                        basename,
                        installdir,
                        osp.abspath(archive),
                        name,
                        game.get('human_name', ''),
                        game.get('icon', '')],
                       cwd=hookdir)

        else:
            log.error("Invalid install method for '%s': '%s'", name, method)


    def uninstall(self, name, method=None):
        game = self.get_game(name)
        method = method or game.get('install', "").lower()
        command = ""
        installdir = ""
        popenargs = {}

        if not method:
            raise HumbleBundleError("No install data for '%s',"
                                    " please check '%s'" %
                                    (name, self.gamedata))

        elif method in ["deb", "apt", "air"]:
            package = game.get("package", name)
            command = 'sudo apt-get remove --auto-remove --yes "%s"' % package

        elif method == "steam":
            try:
                command = "steam steam://uninstall/%d" % game['steamid']
            except KeyError:
                raise HumbleBundleError(
                    "No steamid for steam-installable game '%s'" % name)

        elif method == "mojo":
            mojoname = game.get('mojoname', name.split("_", 1)[0].title())
            installdir = osp.join(osp.expanduser("~"), '.local', 'opt', mojoname)
            uninstaller = osp.join(installdir, "uninstall-%s.sh" % mojoname)
            command = "'%s' --noprompt" % uninstaller
            # "Disable" install dir removal as user can cancel uninstall in GUI
            installdir = ""

        elif method == "custom":
            basename = game.get('basename', name.split("_", 1)[0])
            uninstaller = osp.join(osp.expanduser("~"), '.local', 'opt',
                                  game.get('dirname', basename), "uninstall")
            if not osp.isfile(uninstaller):
                hookdir = osp.join(self.datadir, "hooks", name)
                uninstaller = osp.join(hookdir, "%s.uninstall.hook" % name)
                popenargs['cwd'] = hookdir
            command = "'%s'" % uninstaller

        else:
            log.error("Invalid uninstall method for '%s': '%s'", name, method)

        if not command:
            return

        try:
            log.debug("Executing: %s", command)
            subprocess.check_call(shlex.split(command), **popenargs)
            if installdir:
                log.debug("Removing '%s'", installdir)
                shutil.rmtree(installdir, ignore_errors=True)
        except (subprocess.CalledProcessError, OSError) as e:
            if getattr(e, 'errno', 0) == 2:  # OSError, No such file or directory
                log.error("Error uninstalling '%s': %s: %s",
                          name, e.strerror, shlex.split(command)[0])
            else:
                log.error("Error uninstalling '%s': %s", name, e)


    def get_game(self, name):
        # Get game, if exists
        log.info("Retrieving game info for '%s'", name)
        try:
            return self.games[name]
        except KeyError:
            raise HumbleBundleError("Game not found: %s" % name)


    def get_bundle(self, name):
        # Get bundle, if exists
        log.info("Retrieving bundle info for '%s'", name)
        try:
            return self.bundles[name]
        except KeyError:
            raise HumbleBundleError("Bundle not found: %s" % name)


    def list_games(self, regex=None, platform=None):
        '''Return the sorted machine names of all games, optionally filtered
        by a regex search on the machine name and by having downloads
        for platform.
        '''
        names = self.games.keys()
        if regex:
            names = [k for k in names if re.search(regex, k)]
        if platform is not None:
            names = [k for k in names
                     if any(d.get('platform') == platform
                            for d in self.games[k].get('downloads', []))]
        return sorted(names)


    def list_bundles(self):
        '''Return all bundle dicts, sorted by machine name'''
        return [self.bundles[k] for k in sorted(self.bundles)]


    def get_game_bundles(self, name):
        '''Return the bundle dicts that contain game name'''
        game = self.get_game(name)
        machine_name = game.get('machine_name', '')
        return [bundle for bundle in self.bundles.values()
                if machine_name in bundle.get('games', [])]


    def get_bundle_games(self, name):
        '''Return the game dicts of bundle name, sorted by machine name'''
        bundle = self.get_bundle(name)
        return [self.get_game(game) for game in sorted(bundle['games'])]


    def get_downloads(self, name):
        '''Return the downloads of game name as a list of
        (platform, [download dicts]) tuples, sorted by platform.
        Only downloads with a URL are listed, so a list may be empty.
        A platform may appear more than once.
        '''
        game = self.get_game(name)
        return [(plat.get('platform', ''),
                 [d for d in plat.get('download_struct', []) if 'url' in d])
                for plat in sorted(game.get('downloads', []),
                                   key=lambda k: k['platform'])]


    def get(self, url, postdata=None, headers=None):

        def urlabspath(url):
            return urlsplit(urljoin('/', url)).path.lower()

        def save_cookies(res, force=False):
            if force or 'Set-Cookie' in res.info():
                log.debug("Saving cookies to '%s'", self.cookiejar.filename)
                try:
                    with self._cookie_lock:
                        self.cookiejar.save()
                        os.chmod(self.cookiejar.filename, 0o600)
                except IOError as e:
                    log.error("Error saving cookies: %s", e)

        requested_url = urlabspath(url)
        current_url = ""
        try:
            res = super(HumbleBundle, self).get(url, postdata, headers)
            save_cookies(res)
            # Was it successful? That is, not redirected TO an authorization
            # page that requires special handling.
            current_url = urlabspath(res.geturl())
            if (current_url == requested_url or
                current_url not in self.auth_urls):
                return res
        except httpbot.HTTPError as e:
            # Unauthorized (requires login) or something else?
            if not e.code == 401:
                raise

        # Login required: the session cookie is missing or expired
        raise HumbleBundleError(
            "Not logged in. Log in using a real browser, copy the value of"
            " the _simpleauth_sess cookie and run again with --auth")



def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    args, parser = parseargs(argv)
    logging.basicConfig(level=getattr(logging, args.loglevel.upper(), None),
                        format='%(asctime)s\t%(levelname)-8s\t%(message)s')
    log.debug(args)

    # Needs no game data, so do it before loading (and maybe fetching) it
    if args.download_folder is not None:
        import humblebundle_library as library
        if args.download_folder is True:
            folder = library.get_folder()
            print(folder or "No download folder set")
        else:
            print("Download folder: %s" %
                  library.set_folder(args.download_folder))
        if (not args.sync and not args.update and not args.auth and
                args.alias is None):
            return

    auth = prompt_auth() if args.auth else None
    hb = HumbleBundle(auth=auth, connections=args.connections,
                      debug=args.debug)

    if args.update:
        hb.update()

    if args.clear:
        clear_auth()

    # Without --platform, --list and --download use linux, --sync uses all
    platform = args.platform or "linux"

    if args.alias is not None:
        import humblebundle_library as library
        if not args.alias:
            aliases = library.get_aliases(hb.configdir)
            for bundle in sorted(aliases):
                print("%s\t%s" % (aliases[bundle], bundle))
            if not aliases:
                print("No aliases set")
        elif len(args.alias) > 2:
            raise HumbleBundleError("--alias takes a BUNDLE and an optional"
                                    " NAME")
        else:
            bundle = library.resolve_bundle(hb, args.alias[0])
            folder = library.set_alias(hb, bundle, (args.alias[1:] or
                                                    [None])[0])
            print("Bundle %s now uses folder %s" % (bundle, folder))

    elif args.sync:
        import humblebundle_library as library
        return sync(hb, library.resolve_bundle(hb, args.sync[0]),
                    args.sync[1:], args.platform, dry_run=args.dry_run)

    elif args.list is not None:
        regex = args.list if isinstance(args.list, str) else None
        for key in hb.list_games(regex=regex, platform=platform):
            print("%s" % (hb.games[key]['human_name'] if args.human else key))

    elif args.show:
        def print_key(key, alias=None, obj=None):
            print("%-10s: %s" % (alias or key,
                                 getattr(obj or game, 'get')(key.lower(), '')))

        game = hb.get_game(args.show)
        if args.json:
            print(json.dumps(game, indent=2, separators=(',', ': '),
                             sort_keys=True))
            return
        print_key('machine_name', 'Game')
        print_key('human_name', 'Name')
        print_key('human_name', 'Developer', obj=game.get('payee',{}))
        print_key('URL')
        print_key('', 'Bundles')
        for bundle in hb.get_game_bundles(args.show):
            print("\t%s [%s]" % (bundle['human_name'],
                                 bundle['machine_name']))
        print_key('', 'Downloads')
        platform_prev = None
        for platform, downloads in hb.get_downloads(args.show):
            if platform_prev != platform:
                print("\t%s" % platform)
                platform_prev = platform
            for d in downloads:
                print(hb._download_info(d))

    elif args.show_bundle:
        def print_key(key, alias=None, obj=None):
            print("%-10s: %s" % (alias or key,
                                 getattr(obj or bundle, 'get')(key.lower(), '')))

        bundle = hb.get_bundle(args.show_bundle)
        if args.json:
            print(json.dumps(bundle, indent=2, separators=(',', ': '),
                             sort_keys=True))
            return
        print_key('machine_name', 'Bundle')
        print_key('human_name', 'Name')
        print_key('Category')
        print_key('familyamount', 'Price US$')
        print_key('', 'Games')
        for game in hb.get_bundle_games(args.show_bundle):
            print("\t%s\t[%s]" % (game['human_name'], game['machine_name']))

    elif args.list_bundles:
        for bundle in hb.list_bundles():
            if args.human:
                print(bundle['human_name'])
            else:
                print("%s\t%s" % (bundle['machine_name'],
                                  bundle['human_name']))

    elif args.download:
        if not hb.download(name=args.download,
                           path=args.path,
                           dtype=args.type,
                           arch=args.arch,
                           bittorrent=args.bittorrent,
                           platform=platform,
                           serverfile=args.serverfile):
            return 1

    elif args.install:
        hb.install(args.install, args.method)

    elif args.uninstall:
        hb.uninstall(args.uninstall, args.method)

    else:
        if not (args.update or args.clear or
                args.download_folder is not None or args.alias is not None):
            parser.print_usage()


def sync(hb, bundle, games=None, platform=None, dry_run=False):
    '''Download the files of a bundle, or of some of its games, to the
    download folder. Return the exit code.'''
    import humblebundle_library as library
    lib = library.Library(hb)
    lib.check_folder()
    jobs = lib.plan(bundle, games, platform)
    counts = lib.summary(jobs)
    name = ("Games in no bundle" if bundle == library.UNBUNDLED
            else hb.get_bundle(bundle).get('human_name', bundle))

    print("%s [%s]%s: %d files" % (
        name, bundle, " (%s)" % platform if platform else "", len(jobs)))
    print("  %s" % ", ".join("%d %s" % (counts[s], s)
                             for s in (library.MISSING, library.PARTIAL,
                                       library.HAVE, library.UNTRACKED,
                                       library.CHANGED, library.MODIFIED)
                             if counts[s]))
    print("  To download: %s, free: %s in %s" % (
        library.human_bytes(counts['bytes']),
        library.human_bytes(counts['free']), lib.folder))

    if dry_run:
        for job in jobs:
            print("%-10s %10s  %s" % (job.status, job.human_size, job.relpath))
        return 0

    def on_file(job, index, count):
        if job.status in library.TO_DOWNLOAD:
            print("[%d/%d] %s%s (%s)" % (
                index, count, job.relpath,
                " (resuming)" if job.status == library.PARTIAL else "",
                job.human_size))

    result = lib.sync(jobs, on_file=on_file)

    print("Downloaded %d, already had %d, flagged %d, failed %d" % (
        len(result.downloaded), len(result.skipped),
        len(result.flagged), len(result.failed)))
    for job in result.flagged:
        print("  %s: %s" % (job.status, job.relpath))
    for job, error in result.failed:
        print("  failed: %s: %s" % (job.relpath, error))
    if result.cancelled:
        print("Cancelled. Run again to resume.")
    return 1 if result.failed or result.cancelled else 0


def clear_auth(appname=None, cookiejar=None):
    '''Clear all authentication data and delete related files'''

    appname   = appname   or APPNAME
    cookiejar = cookiejar or COOKIEJAR

    log.info("Clearing all authentication data")

    try:
        log.debug("Removing session cookie from the keyring")
        keyring.delete_password(appname, KEYRING_USER)
    except keyring.errors.PasswordDeleteError as e:
        log.debug(e)

    try:
        log.debug("Deleting '%s'", cookiejar)
        os.remove(cookiejar)
    except OSError as e:
        log.debug(e)

    log.info("Finished clearing authentication")


def parseargs(argv=None):
    parser = argparse.ArgumentParser(
        description="Humble Bundle Manager",)

    parser.add_argument('-v', '--verbose', dest='loglevel',
                        action="store_const",
                        const="info", default="warn",
                        help="Output informative messages")
    parser.add_argument('-D', '--debug', dest='loglevel',
                        action="store_const", const="debug",
                        help="Output debug information")
    parser.add_argument('-u', '--update', dest='update',
                        default=False, action="store_true",
                        help="Fetch all games and bundles data from the server,"
                            " rebuilding the cache")

    def positive_int(value):
        try:
            number = int(value)
        except ValueError:
            number = 0
        if number < 1:
            raise argparse.ArgumentTypeError(
                "must be a whole number of 1 or more: %r" % value)
        return number

    parser.add_argument('-n', '--connections', dest='connections',
                        type=positive_int, default=DEFAULT_CONNECTIONS,
                        metavar="N",
                        help="Maximum number of simultaneous connections"
                            " used by --update. Use 1 for one request at a"
                            " time. Default is %d" % DEFAULT_CONNECTIONS)

    group = parser.add_argument_group("Authentication Options")
    group.add_argument('-A', '--auth', dest='auth',
                        default=False, action="store_true",
                        help="Prompt for the _simpleauth_sess session cookie"
                            " and save it in the keyring")
    group.add_argument('-C', '--clear',
                       default=False, action="store_true",
                       help="Clear all authentication data, deleting related files")

    group = parser.add_argument_group("Commands")
    group.add_argument('-l', '--list', dest='list',
                        const=True, nargs='?', metavar="REGEX",
                        help="List all available Games (Products),"
                            " including Soundtracks and eBooks,"
                            " optionally filtered by REGEX. If --platform is given,"
                            " filter by platform also.")
    group.add_argument('-L', '--list-bundles', dest='list_bundles',
                        default=False, action="store_true",
                        help="List all available Bundles (Purchases), "
                            "including Store Front (single product) purchases")
    group.add_argument('-s', '--show', dest='show', metavar="GAME",
                        help="Show all info about selected game")
    group.add_argument('-S', '--show-bundle', dest='show_bundle',
                        metavar="BUNDLE",
                        help="Show all info about selected bundle")
    group.add_argument('-d', '--download', dest='download', metavar="GAME",
                        help="Download the selected game")
    group.add_argument('-i', '--install', dest='install', metavar="GAME",
                        help="Install selected game")
    group.add_argument('-I', '--uninstall', dest='uninstall', metavar="GAME",
                        help="Uninstall selected game")
    group.add_argument('-Y', '--sync', dest='sync', nargs='+',
                        metavar=("BUNDLE", "GAME"),
                        help="Download all files of BUNDLE, or only of the"
                            " given GAMEs in it, to the download folder,"
                            " as BUNDLE/PLATFORM/FILE. BUNDLE may be its"
                            " alias. Files already"
                            " downloaded are skipped, interrupted ones are"
                            " resumed and files changed on the server are"
                            " reported but not replaced. Use _unbundled"
                            " for games in no bundle. All platforms, unless"
                            " --platform is given.")

    group = parser.add_argument_group("Download Folder Options")
    group.add_argument('-o', '--download-folder', dest='download_folder',
                        nargs='?', const=True, metavar="PATH",
                        help="Set the folder used by --sync, saving it in"
                            " config.ini. The folder must exist. Without"
                            " PATH, show the current folder.")
    group.add_argument('-K', '--alias', dest='alias', nargs='*',
                        metavar=("BUNDLE", "NAME"),
                        help="Use NAME as the folder of BUNDLE in the"
                            " download folder, instead of its long machine"
                            " name, saving it in config.ini. Files already"
                            " downloaded are moved. Without NAME, remove"
                            " the alias. With no arguments, list aliases.")
    group.add_argument('-N', '--dry-run', dest='dry_run',
                        default=False, action="store_true",
                        help="With --sync, list each file and its status"
                            " without downloading")

    group = parser.add_argument_group("List Options")
    group.add_argument('-H', '--human', dest='human',
                        default=False, action="store_true",
                        help="Output human-friendly game names in --list/--list-bundles")

    group = parser.add_argument_group("Show Options")
    group.add_argument('-j', '--json', dest='json',
                        default=False, action="store_true",
                        help="Output --show/--show-bundle in machine-readable,"
                            " JSON format")

    group = parser.add_argument_group("Download Options")
    group.add_argument('-t', '--type', dest='type', metavar="NAME",
                        help="Type (name) of the download,"
                            " for example '.deb', 'mojo', 'flash', etc")
    group.add_argument('-a', '--arch', dest='arch', choices=['32', '64'],
                        help="Download architecture: 32-bit (also known as i386)"
                            " or 64-bit (amd64, x86_64, etc)")
    group.add_argument('-p', '--platform', dest='platform',
                        default=None,
                        choices=['windows', 'mac', 'linux', 'android', 'audio',
                                 'ebook', 'comedy', 'video', 'other'],
                        help="Download platform. Default is 'linux' for"
                            " --list and --download, and all platforms"
                            " for --sync")
    group.add_argument('-F', '--server-file', dest='serverfile', metavar="FILE",
                        help="Basename of the server file to download."
                            " Useful when no combination of --type, --arch"
                            " and --platform is enough to narrow down choices"
                            " to a single download.")
    group.add_argument('-b', '--bittorrent', dest='bittorrent',
                        default=False, action="store_true",
                        help="Download bittorrent file instead of direct download")
    group.add_argument('-f', '--path', dest='path',
                        help="Path to download. If PATH is a directory,"
                            " default download basename will be used."
                            " If omitted, download to current directory.")

    group = parser.add_argument_group("Install Options")
    group.add_argument('-m', '--method', dest='method',
                        choices=['custom', 'deb', 'apt', 'mojo', 'air', 'steam'],
                        help="Use this method instead of the default"
                            " for (un-)installing a game")

    args = parser.parse_args(argv)
    args.debug = args.loglevel=='debug'
    return args, parser


def cli():
    global APPNAME, CONFIGDIR, CACHEDIR, COOKIEJAR

    APPNAME   = osp.basename(osp.splitext(__file__)[0])
    CONFIGDIR = xdg.save_config_path(APPNAME)  # creates the dir
    CACHEDIR  = osp.join(xdg.xdg_cache_home, APPNAME)
    COOKIEJAR = osp.join(CONFIGDIR, "cookies.txt")
    # No changes in DATADIR

    try:
        sys.exit(main())

    except KeyboardInterrupt:
        pass
    except HumbleBundleError as e:
        log.critical(e)
        sys.exit(1)
    except Exception as e:
        log.critical(e, exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    cli()
