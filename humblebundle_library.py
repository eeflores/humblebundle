#!/usr/bin/env python3
#
# humblebundle_library - Local folder of files downloaded from Humble Bundle
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

'''Keep a local folder of files downloaded from Humble Bundle.

Files are stored as <folder>/<bundle>/<platform>/<file>, so a game
downloaded on its own goes to the same place as when its whole bundle is
downloaded. Games that are not in any bundle use the UNBUNDLED folder.
A bundle's folder is its machine name, or a shorter alias set in
config.ini, section [aliases], as <bundle machine name> = <alias>.

A manifest.json in the folder records what was downloaded, and the game
each file belongs to, so later runs can skip files already downloaded and
flag files that changed.

The folder is set once in config.ini, section [downloads], key folder.
'''

import datetime
import errno
import json
import logging
import os
import os.path as osp
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from urllib.error import URLError
from urllib.parse import unquote, urlsplit

import httpbot
import humblebundle


log = logging.getLogger(__name__)

CONFIG_SECTION = "downloads"
CONFIG_KEY = "folder"
# config.ini section of bundle machine name = shorter folder name
ALIAS_SECTION = "aliases"
MANIFEST = "manifest.json"
MANIFEST_VERSION = 1

# Pseudo-bundle for games that are not in any bundle, as shown in the TUI.
# Used both as its folder name and to select it in sync commands.
UNBUNDLED = "_unbundled"

# File statuses
MISSING   = "missing"    # not downloaded yet
PARTIAL   = "partial"    # an interrupted download can be resumed
HAVE      = "have"       # downloaded and matches the server's MD5
UNTRACKED = "untracked"  # a file is there but not in the manifest
CHANGED   = "changed"    # the server now has a different version
MODIFIED  = "modified"   # the local file differs from what was downloaded

# Statuses that sync() downloads or checks. The others are left untouched.
TO_DOWNLOAD = (MISSING, PARTIAL)
FLAGGED = (CHANGED, MODIFIED)

# Characters and names that are not valid in Windows file names
_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = re.compile(r'^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$',
                             re.IGNORECASE)


class LibraryError(humblebundle.HumbleBundleError):
    pass


def safe_name(name):
    '''Return name as a file name that is valid on Windows, macOS and Linux'''
    name = _INVALID_CHARS.sub('_', name).rstrip('. ')
    if _RESERVED_NAMES.match(name):
        name = '_' + name
    return name or '_'


# Settings ------------------------------------------------------------------

def get_folder(configdir=None):
    '''Return the download folder set in config.ini, or None'''
    config = humblebundle.read_config(configdir)
    return config.get(CONFIG_SECTION, CONFIG_KEY, fallback=None) or None


def set_folder(path, configdir=None):
    '''Set the download folder in config.ini and return its absolute path.
    The folder must exist, so a typo or an unmounted drive is not silently
    turned into a new, empty folder.
    '''
    path = osp.abspath(osp.expanduser(path))
    if not osp.isdir(path):
        raise LibraryError("Download folder does not exist: %s."
                           " Create it first." % path)
    config = humblebundle.read_config(configdir)
    previous = config.get(CONFIG_SECTION, CONFIG_KEY, fallback=None)
    if not config.has_section(CONFIG_SECTION):
        config.add_section(CONFIG_SECTION)
    config.set(CONFIG_SECTION, CONFIG_KEY, path)
    humblebundle.write_config(config, configdir)
    if previous and osp.normcase(previous) != osp.normcase(path):
        log.warning("Download folder changed from %s to %s. Files already"
                    " in the old folder are not moved.", previous, path)
    return path


def get_aliases(configdir=None):
    '''Return the bundle aliases set in config.ini, as a dict of
    bundle machine name -> folder name'''
    config = humblebundle.read_config(configdir)
    if not config.has_section(ALIAS_SECTION):
        return {}
    return {k: v for k, v in config.items(ALIAS_SECTION) if v}


def resolve_bundle(hb, name):
    '''Return the machine name of the bundle named name, which may be a
    machine name, an alias or UNBUNDLED'''
    if name == UNBUNDLED or name in hb.bundles:
        return name
    for bundle, alias in get_aliases(hb.configdir).items():
        if alias.lower() == name.lower():
            return bundle
    raise LibraryError("Bundle not found: %s" % name)


def set_alias(hb, bundle, alias=None):
    '''Set the folder name of bundle to alias, or remove its alias if alias
    is None, so it goes back to the bundle's machine name.

    If the bundle already has files in the download folder, its folder is
    renamed and the manifest updated, so nothing is downloaded again.
    Return the new folder name.
    '''
    bundle = resolve_bundle(hb, bundle)
    if bundle == UNBUNDLED:
        raise LibraryError("%s cannot have an alias" % UNBUNDLED)
    aliases = get_aliases(hb.configdir)
    old = aliases.get(bundle) or safe_name(bundle)
    new = alias or safe_name(bundle)

    if alias is not None:
        if safe_name(alias) != alias or alias in ('.', '..'):
            raise LibraryError("Invalid alias '%s': use a name that is valid"
                               " as a folder on Windows, macOS and Linux"
                               % alias)
        # Folder names must stay unique, ignoring case as macOS and
        # Windows do
        taken = {UNBUNDLED.lower(): UNBUNDLED}
        for other in hb.bundles:
            if other != bundle:
                name = aliases.get(other) or safe_name(other)
                taken[name.lower()] = other
                taken[other.lower()] = other
        if alias.lower() in taken:
            raise LibraryError("Alias '%s' is already used by %s" %
                               (alias, taken[alias.lower()]))

    folder = get_folder(hb.configdir)
    if folder and old != new:
        lib = Library(hb, folder)
        lib.check_folder()
        src, dst = osp.join(lib.folder, old), osp.join(lib.folder, new)
        moved = {k: v for k, v in lib.files.items()
                 if k.split('/', 1)[0] == old}
        if osp.exists(src):
            # samefile: a change of case only, on a case-insensitive disk
            if osp.exists(dst) and not osp.samefile(src, dst):
                raise LibraryError("Folder already exists: %s" % dst)
            log.info("Renaming %s to %s", src, dst)
            os.rename(src, dst)
        if moved:
            for key, entry in moved.items():
                del lib.files[key]
                lib.files[new + key[len(old):]] = entry
            lib._save_manifest()

    config = humblebundle.read_config(hb.configdir)
    if not config.has_section(ALIAS_SECTION):
        config.add_section(ALIAS_SECTION)
    if alias is None:
        config.remove_option(ALIAS_SECTION, bundle)
    else:
        config.set(ALIAS_SECTION, bundle, alias)
    humblebundle.write_config(config, hb.configdir)
    return new


# Download plan ---------------------------------------------------------------

@dataclass
class FileJob:
    '''One file of a game, where it goes in the library and its status'''
    bundle: str     # bundle machine name, or UNBUNDLED
    game: str       # game machine name
    platform: str
    name: str       # download name, such as "64-bit .deb" or "EPUB"
    relpath: str    # path in the library, with "/" separators
    path: str       # absolute path
    size: int
    md5: str
    status: str = MISSING
    partial_size: int = 0
    human_size: str = ""

    @property
    def remaining(self):
        '''Bytes still to download'''
        if self.status not in TO_DOWNLOAD:
            return 0
        return max(0, self.size - self.partial_size)


@dataclass
class SyncResult:
    downloaded: list = field(default_factory=list)
    skipped: list = field(default_factory=list)    # already downloaded
    flagged: list = field(default_factory=list)    # CHANGED or MODIFIED
    failed: list = field(default_factory=list)     # (job, error message)
    cancelled: bool = False


def _basename(download):
    url = download.get('url', {}).get('web', '')
    return unquote(osp.basename(urlsplit(url).path))


class Library:
    '''The download folder and its manifest'''

    def __init__(self, hb, folder=None):
        '''hb is a HumbleBundle instance.
        folder defaults to the one set in config.ini.
        '''
        self.hb = hb
        folder = folder or get_folder(hb.configdir)
        if not folder:
            raise LibraryError("No download folder set. Set one with:"
                               " hb --download-folder PATH")
        self.folder = osp.abspath(osp.expanduser(folder))
        self.manifest_path = osp.join(self.folder, MANIFEST)
        self.aliases = get_aliases(hb.configdir)
        self._lock = threading.Lock()
        self._files = None  # loaded on first use

    def bundle_folder(self, bundle):
        '''Return the folder name of bundle: its alias, if set, or its
        machine name'''
        return self.aliases.get(bundle) or safe_name(bundle)

    def check_folder(self):
        '''Raise LibraryError if the folder is not there, such as when it is
        on a drive that is not mounted'''
        if not osp.isdir(self.folder):
            raise LibraryError("Download folder not found: %s" % self.folder)

    # Manifest ----------------------------------------------------------

    @property
    def files(self):
        '''Manifest entries: relpath -> dict of md5, size, bundle, game,
        platform, name and downloaded (ISO date)'''
        if self._files is None:
            self._files = self._load_manifest()
        return self._files

    def _load_manifest(self):
        try:
            with open(self.manifest_path, encoding='utf-8') as fp:
                data = json.load(fp)
        except FileNotFoundError:
            return {}
        except ValueError as e:
            raise LibraryError("Invalid manifest %s: %s" %
                               (self.manifest_path, e))
        return data.get('files', {})

    def _save_manifest(self):
        data = {'version': MANIFEST_VERSION, 'files': self.files}
        fd, tmp = tempfile.mkstemp(dir=self.folder, prefix=".manifest.")
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as fp:
                json.dump(data, fp, indent=1, sort_keys=True)
            os.replace(tmp, self.manifest_path)
        except BaseException:
            os.remove(tmp)
            raise

    def _record(self, job):
        with self._lock:
            self.files[job.relpath] = {
                'md5': job.md5,
                'size': job.size,
                'bundle': job.bundle,
                'game': job.game,
                'platform': job.platform,
                'name': job.name,
                'downloaded': datetime.datetime.now().isoformat(
                    timespec='seconds'),
            }
            self._save_manifest()

    # Planning ----------------------------------------------------------

    def bundle_games(self, bundle):
        '''Return the game machine names of bundle, or of the games in no
        bundle for UNBUNDLED'''
        if bundle == UNBUNDLED:
            bundled = {g for b in self.hb.bundles.values()
                       for g in b.get('games', [])}
            return sorted(k for k in self.hb.games if k not in bundled)
        return [g for g in self.hb.get_bundle(bundle).get('games', [])
                if g in self.hb.games]

    def plan(self, bundle, games=None, platform=None):
        '''Return the FileJobs for bundle, with their current status.

        games, if given, limits it to those game machine names, which must
        be in the bundle. platform, if given, limits it to that platform.
        '''
        available = self.bundle_games(bundle)
        if games:
            unknown = [g for g in games if g not in available]
            if unknown:
                raise LibraryError("Not in bundle '%s': %s" %
                                   (bundle, ", ".join(unknown)))
            available = [g for g in available if g in games]

        jobs = []
        seen = {}
        for game in available:
            for plat, downloads in self.hb.get_downloads(game):
                if platform and plat != platform:
                    continue
                for d in downloads:
                    job = self._job(bundle, game, plat, d, seen)
                    if job:
                        jobs.append(job)
        return jobs

    def _job(self, bundle, game, platform, download, seen):
        basename = _basename(download)
        if not basename:
            return None
        md5 = download.get('md5', '').lower()
        parts = [self.bundle_folder(bundle), safe_name(platform),
                 safe_name(basename)]
        relpath = "/".join(parts)
        key = relpath.lower()  # macOS and Windows ignore case
        if key in seen:
            if seen[key] == md5:
                return None  # the same file, listed twice or by two games
            # A different file with the same name: keep both
            stem, ext = osp.splitext(parts[-1])
            parts[-1] = "%s-%s%s" % (stem, md5[:8] or "dup", ext)
            relpath = "/".join(parts)
            key = relpath.lower()
        seen[key] = md5

        job = FileJob(bundle=bundle, game=game, platform=platform,
                      name=download.get('name', ''), relpath=relpath,
                      path=osp.join(self.folder, *parts),
                      size=int(download.get('file_size') or 0), md5=md5,
                      human_size=download.get('human_size', ''))
        self.update_status(job)
        return job

    def update_status(self, job):
        '''Set job.status from the files on disk and the manifest'''
        entry = self.files.get(job.relpath)
        job.partial_size = 0
        if osp.isfile(job.path):
            if entry is None:
                job.status = UNTRACKED
            elif (job.size and entry.get('size') and
                  osp.getsize(job.path) != entry['size']):
                job.status = MODIFIED
            elif job.md5 and entry.get('md5') and entry['md5'] != job.md5:
                job.status = CHANGED
            else:
                job.status = HAVE
            return job.status

        part = job.path + httpbot.PART_SUFFIX
        if osp.isfile(part):
            job.status = PARTIAL
            job.partial_size = osp.getsize(part)
        else:
            job.status = MISSING
        return job.status

    def summary(self, jobs):
        '''Return a dict of status -> number of files, plus "bytes" still to
        download and "free" bytes in the folder'''
        counts = {s: 0 for s in (MISSING, PARTIAL, HAVE, UNTRACKED,
                                 CHANGED, MODIFIED)}
        for job in jobs:
            counts[job.status] += 1
        counts['bytes'] = sum(job.remaining for job in jobs)
        counts['free'] = (shutil.disk_usage(self.folder).free
                          if osp.isdir(self.folder) else 0)
        return counts

    # Downloading -----------------------------------------------------

    def sync(self, jobs, progress=None, cancel=None, on_file=None):
        '''Download the jobs that are MISSING or PARTIAL, one at a time, and
        check UNTRACKED ones. Return a SyncResult.

        progress(job, done, total) is called while downloading. Without it,
        a console progress bar is shown.
        on_file(job, index, count), if given, is called before each file.
        cancel, if given, is a threading.Event that stops the sync. The
        file being downloaded is kept as a partial file to resume later.

        Files that fail are reported in the result and the sync goes on,
        except for errors that would fail every file, such as not being
        logged in or a full disk.
        '''
        self.check_folder()
        result = SyncResult()
        need = sum(job.remaining for job in jobs)
        free = shutil.disk_usage(self.folder).free
        if need > free:
            raise LibraryError("Not enough disk space: need %s, %s free in %s"
                               % (human_bytes(need), human_bytes(free),
                                  self.folder))

        for index, job in enumerate(jobs, 1):
            if cancel is not None and cancel.is_set():
                result.cancelled = True
                break
            if on_file:
                on_file(job, index, len(jobs))
            status = self.update_status(job)
            if status == HAVE:
                result.skipped.append(job)
                continue
            if status in FLAGGED:
                result.flagged.append(job)
                continue
            try:
                if status == UNTRACKED:
                    self._adopt(job)
                    (result.flagged if job.status in FLAGGED
                     else result.skipped).append(job)
                    continue
                callback = None
                if progress:
                    callback = lambda done, total, job=job: progress(
                        job, done, total)
                if self.sync_file(job, callback, cancel):
                    result.downloaded.append(job)
                else:
                    result.cancelled = True
                    break
            except humblebundle.HumbleBundleError as e:
                if isinstance(e, LibraryError):
                    result.failed.append((job, str(e)))
                    log.error("%s: %s", job.relpath, e)
                    continue
                raise  # not logged in: every file would fail
            except httpbot.HTTPError as e:
                result.failed.append((job, "%d %s" % (e.code, e.reason)))
                log.error("%s: %d %s", job.relpath, e.code, e.reason)
            except URLError as e:
                # No connection: every file would fail
                raise LibraryError("Network error: %s" % e.reason)
            except OSError as e:
                if e.errno == errno.ENOSPC:
                    raise LibraryError("Disk full while downloading %s" %
                                       job.relpath)
                result.failed.append((job, str(e)))
                log.error("%s: %s", job.relpath, e)
            except (httpbot.DownloadError, ValueError) as e:
                result.failed.append((job, str(e)))
                log.error("%s: %s", job.relpath, e)
        return result

    def _adopt(self, job):
        '''Add an existing file that is not in the manifest, if it is the
        same file the server has. Otherwise flag it as MODIFIED.'''
        log.info("Checking existing file %s", job.relpath)
        if ((not job.size or osp.getsize(job.path) == job.size) and
                (not job.md5 or
                 httpbot.filehash(job.path) == job.md5)):
            self._record(job)
            job.status = HAVE
        else:
            job.status = MODIFIED

    def sync_file(self, job, callback=None, cancel=None):
        '''Download one file to its place in the library, resuming a partial
        download. Return True if done, False if cancelled.'''
        self.check_folder()
        refreshed = False
        while True:
            url = self._current_url(job)
            try:
                path = self.hb.fetch(url, job.path, md5sum=job.md5 or None,
                                     expected_size=job.size,
                                     progress=callback is None,
                                     callback=callback, cancel=cancel)
                break
            except httpbot.HTTPError as e:
                # Download links expire. Get fresh ones and retry once.
                if e.code not in (403, 404, 410) or refreshed:
                    raise LibraryError("Download failed: %d %s" %
                                       (e.code, e.reason))
                self._refresh(job)
                refreshed = True
        if path is None:
            job.status = PARTIAL
            return False
        self._record(job)
        job.status = HAVE
        return True

    def _current_url(self, job):
        '''Return the web URL of job's file from the current game data'''
        for plat, downloads in self.hb.get_downloads(job.game):
            if plat != job.platform:
                continue
            for d in downloads:
                if ((job.md5 and d.get('md5', '').lower() == job.md5) or
                        (not job.md5 and _basename(d) ==
                         job.relpath.rsplit('/', 1)[-1])):
                    url = d.get('url', {}).get('web')
                    if url:
                        return url
        raise LibraryError("File is no longer offered by the server")

    def _refresh(self, job):
        '''Reload the order of job's bundle from the server, which gives
        new download links for all its games'''
        if job.bundle == UNBUNDLED:
            raise LibraryError("Download link expired and the game is not in"
                               " a bundle, so it cannot be refreshed")
        gamekey = self.hb.get_bundle(job.bundle).get('gamekey')
        if not gamekey:
            raise LibraryError("Bundle has no order key to refresh links")
        log.info("Refreshing download links for bundle %s", job.bundle)
        self.hb.refresh_order(gamekey)


def human_bytes(size):
    '''Return size in bytes as a short human-readable string'''
    for unit in ("bytes", "KB", "MB", "GB"):
        if abs(size) < 1024 or unit == "GB":
            break
        size /= 1024.0
    if unit == "bytes":
        return "%d bytes" % size
    if unit == "GB" and abs(size) >= 1024:
        return "%.1f TB" % (size / 1024.0)
    return "%.1f %s" % (size, unit)
