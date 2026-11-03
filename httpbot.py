#!/usr/bin/env python3
#
#    Copyright (C) 2012 Rodrigo Silva (MestreLion) <linux@rodrigosilva.com>
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
#
# urllib2 wrapper for a simpler, higher-level API

import os
import logging
import hashlib
import urllib.request
import urllib.parse
from urllib.error import HTTPError


from lxml import html   # PyPI: lxml
try:
    import progressbar  # PyPI: progressbar2
except ImportError:
    progressbar = None


log = logging.getLogger(__name__)

# Suffix of files being downloaded by HttpBot.fetch()
PART_SUFFIX = ".part"


class DownloadError(Exception):
    """ A downloaded file does not match its expected size or MD5 """
    pass


class HttpBot:
    """ Base class for other handling basic http tasks like requesting a page,
        download a file and cache content. Not to be used directly
    """
    def __init__(self, base_url="", tag="", cookiejar=None, debug=False):
        self.tag = tag
        hh  = urllib.request.HTTPHandler( debuglevel=1 if debug else 0)
        hsh = urllib.request.HTTPSHandler(debuglevel=1 if debug else 0)
        cp  = urllib.request.HTTPCookieProcessor(cookiejar)
        self._opener = urllib.request.build_opener(hh, hsh, cp)
        scheme, netloc, path, q, f  = urllib.parse.urlsplit(base_url, "http")
        if not netloc:
            netloc, _, path = path.partition('/')
        self.base_url = urllib.parse.urlunsplit((scheme, netloc, path, q, f))

    def get(self, url, postdata=None, headers=None):
        """ Send an HTTP request, either GET (if no postdata) or POST
            Keeps session and other cookies.
            postdata is a dict with name/value pairs
            headers is a dict of extra request headers
            url can be absolute or relative to base_url
        """
        url = urllib.parse.urljoin(self.base_url, url)
        data = None
        if postdata:
            data = urllib.parse.urlencode(postdata).encode('ascii')
        if headers:
            url = urllib.request.Request(url, data=data, headers=headers)
            return self._opener.open(url)
        return self._opener.open(url, data)

    def fetch(self, url, path, md5sum=None, expected_size=0,
              progress=True, callback=None, cancel=None, chunk_size=0):
        """ Download url to exactly path, resuming a previous attempt.

            Data goes to path + PART_SUFFIX, which is renamed to path once
            complete and verified against expected_size and md5sum, when
            given. If the partial file exists, only the remaining bytes are
            requested; if the server does not support that, the download
            restarts from the beginning.

            Return path, or None if cancelled. On cancel or Ctrl+C the
            partial file is kept so a later call resumes it.
            Raise DownloadError, removing the partial file, if the result
            does not match expected_size or md5sum.

            callback, if given, is called as callback(done, total) and
            replaces the console progress bar. total is 0 if unknown.
            cancel, if given, is a threading.Event that stops the download.
        """
        chunk_size = chunk_size or 256*1024
        part = path + PART_SUFFIX
        dirname = os.path.dirname(path)
        if dirname:
            os.makedirs(dirname, exist_ok=True)

        offset = os.path.getsize(part) if os.path.isfile(part) else 0
        if expected_size and offset > expected_size:
            log.warning("Partial file is larger than expected, restarting: %s",
                        part)
            os.remove(part)
            offset = 0

        # Hash the data already on disk, then each new chunk, so the file is
        # read only once
        hashobj = hashlib.md5() if md5sum else None
        response = None
        if not (expected_size and offset == expected_size):
            headers = {'Range': 'bytes=%d-' % offset} if offset else None
            try:
                response = self.get(url, headers=headers)
            except HTTPError as e:
                # 416: nothing left to send, so the partial file is complete
                if not (e.code == 416 and offset):
                    raise
            if response is not None and offset and response.getcode() != 206:
                log.info("Server does not support resuming, restarting %s",
                         os.path.basename(path))
                offset = 0

        if offset:
            log.info("Resuming %s at %d bytes", os.path.basename(path), offset)
            if hashobj:
                filehash(part, hashobj)

        done = offset
        total = expected_size
        if response is not None and not total:
            length = int(response.info().get('Content-Length', 0))
            total = offset + length if length else 0

        show = (progress and progressbar and not callback and
                response is not None)
        if show:
            pbar = progressbar.ProgressBar(widgets=[
                ' ', progressbar.Percentage(),
                ' of %.1f MiB' % (total/1024.0**2),
                ' ', progressbar.Bar('.'),
                ' ', progressbar.FileTransferSpeed(),
                ' ', progressbar.ETA(),
                ' '], max_value=total or progressbar.UnknownLength,
                initial_value=offset).start()
        if callback:
            callback(done, total)

        cancelled = False
        try:
            if response is not None:
                with open(part, 'ab' if offset else 'wb') as f:
                    for data in iter(lambda: response.read(chunk_size), b''):
                        if cancel is not None and cancel.is_set():
                            cancelled = True
                            break
                        f.write(data)
                        if hashobj:
                            hashobj.update(data)
                        done += len(data)
                        if show:
                            pbar.update(min(total, done) if total else done)
                        if callback:
                            callback(done, total)
        finally:
            if response is not None:
                response.close()
            if show:
                pbar.finish()

        if cancelled:
            log.info("Download cancelled, keeping %s", part)
            return None

        size = os.path.getsize(part)
        if expected_size and size != expected_size:
            os.remove(part)
            raise DownloadError("Size of %s is %d bytes, expected %d" %
                                (os.path.basename(path), size, expected_size))
        if hashobj and hashobj.hexdigest() != md5sum.lower():
            os.remove(part)
            raise DownloadError("MD5 of %s does not match: expected %s, got %s"
                                % (os.path.basename(path), md5sum,
                                   hashobj.hexdigest()))
        os.replace(part, path)
        return path

    def download(self, url, path=None, md5sum=None, expected_size=0,
                 progress=True, keep_partial=False, chunk_size=0,
                 callback=None, cancel=None):
        """ Download url to path and return the saved path, or None if the
            download was aborted or its MD5 does not match md5sum.

            callback, if given, is called as callback(done, total) after each
            chunk, where total is 0 if the size is unknown. It replaces the
            console progress bar.
            cancel, if given, is a threading.Event. Setting it aborts the
            download as Ctrl+C would.
        """
        show = progress and progressbar and not callback
        chunk_size = chunk_size or 32*1024  # 32K is arbitrary

        download = self.get(url)

        # Set download path
        # If save name is not set, use the downloaded file name
        # "Not set" means either path is an existing dir or ends with a trailing '/'
        path = os.path.expanduser(path or ".")
        if os.path.isdir(path) or not os.path.basename(path):
            #TODO: Parse Content-Disposition header for filename
            basename = urllib.parse.unquote(os.path.basename(
                urllib.parse.urlsplit(download.geturl()).path))
            path = os.path.join(path, basename)
        log.info("Downloading to %s", path)

        # Handle dir
        dirname, _ = os.path.split(path)
        try:
            log.debug("Creating destination directory %s", dirname)
            os.makedirs(dirname)
        except OSError as e:
            # Ignore if destination directory exists, raise otherwise
            if not (e.errno == 17 and os.path.isdir(dirname)):
                raise

        size = expected_size or int(download.info().get('Content-Length', 0))
        if md5sum and os.path.isfile(path) and os.path.getsize(path) == size:
            log.debug("File already exists, checking its MD5")
            if filehash(path, hashlib.md5()) == md5sum:
                log.debug("MD5 matches, skipping download and using cached file")
                return path
            else:
                log.debug("MD5 does not match, downloading")

        if show:
            pbar = progressbar.ProgressBar(widgets=[
                ' ', progressbar.Percentage(), ' of %.1f MiB' % (size/1024.0**2),
                ' ', progressbar.Bar('.'),
                ' ', progressbar.FileTransferSpeed(),
                ' ', progressbar.ETA(),
                ' '], max_value=size or progressbar.UnknownLength).start()

        # TODO: Perhaps overkill, but network read and md5/sha1/disk write could be done async
        # Could save around 30s for a 1-GiB file
        completed = False
        done = 0
        if callback:
            callback(done, size)
        try:
            with open(path, 'wb') as f:
                for data in iter(lambda: download.read(chunk_size), b''):
                    if cancel is not None and cancel.is_set():
                        break
                    f.write(data)
                    done += len(data)
                    if show:
                        pbar.update(min(size, done) if size else done)
                    if callback:
                        callback(done, size)
                else:
                    completed = True
        except KeyboardInterrupt:
            pass
        finally:
            if show:
                pbar.finish()
            if not completed:
                log.warning("Download aborted")
                if not keep_partial:
                    log.debug("Removing partial file")
                    os.remove(path)

        if completed:
            if not md5sum:
                return path

            realhash = filehash(path, hashlib.md5())
            if md5sum == realhash:
                log.debug("Download MD5 match: %s", md5sum)
                return path
            else:
                log.warning("Download MD5 does not match - file is likely corrupt.")
                log.debug("Expected and downloaded MD5:\n%s\n%s", md5sum, realhash)

    def quote(self, text):
        """ Quote a text for URL usage, similar to urllib.parse.quote_plus.
            Also encodes "/"
        """
        return urllib.parse.quote_plus(text, safe='')

    def parse(self, url, postdata=None):
        """ Parse an URL and return an etree ElementRoot.
            Assumes UTF-8 encoding
        """
        return html.parse(self.get(url, postdata),
                          parser=html.HTMLParser(encoding='utf-8'))


def filehash(path, hashobj=None, chunk_size=0):
    hashobj = hashobj or hashlib.md5()
    chunk = chunk_size or 32*1024
    with open(path, 'rb') as f:
        for data in iter(lambda: f.read(chunk), b''):
            hashobj.update(data)
    return hashobj.hexdigest()
