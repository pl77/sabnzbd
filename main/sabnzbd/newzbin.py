#!/usr/bin/python -OO
# Copyright 2008-2009 The SABnzbd-Team <team@sabnzbd.org>
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
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.

"""
sabnzbd.newzbin - newzbin.com support functions
"""

import httplib
import urllib
import time
import logging
import re
import Queue
import socket
try:
    socket.ssl
    _HAVE_SSL = True
except:
    _HAVE_SSL = False

from threading import *

import sabnzbd
from sabnzbd.constants import *
from sabnzbd.decorators import synchronized
from sabnzbd.misc import cat_to_opts, sanitize_foldername, bad_fetch
from sabnzbd.nzbstuff import CatConvert
from sabnzbd.codecs import name_fixer
import sabnzbd.newswrapper
import sabnzbd.nzbqueue
import sabnzbd.cfg as cfg
from sabnzbd.lang import T
from sabnzbd.utils import osx

################################################################################
# BOOKMARK Wrappers
################################################################################

__BOOKMARKS = None

def bookmarks_init():
    global __BOOKMARKS
    if not __BOOKMARKS:
        __BOOKMARKS = Bookmarks()


def bookmarks_save():
    global __BOOKMARKS
    if __BOOKMARKS:
        __BOOKMARKS.save()


def getBookmarksNow():
    global __BOOKMARKS
    if __BOOKMARKS:
        __BOOKMARKS.run()


def getBookmarksList():
    global __BOOKMARKS
    if __BOOKMARKS:
        return __BOOKMARKS.bookmarksList()


def delete_bookmark(msgid):
    global __BOOKMARKS
    if __BOOKMARKS and cfg.NEWZBIN_BOOKMARKS.get() and cfg.NEWZBIN_UNBOOKMARK.get():
        __BOOKMARKS.del_bookmark(msgid)


################################################################################
# Msgid Grabber Wrappers
################################################################################
__MSGIDGRABBER = None

def init_grabber():
    global __MSGIDGRABBER
    if __MSGIDGRABBER:
        __MSGIDGRABBER.__init__()
    else:
        __MSGIDGRABBER = MSGIDGrabber()


def start_grabber():
    global __MSGIDGRABBER
    if __MSGIDGRABBER:
        logging.debug('Starting msgidgrabber')
        __MSGIDGRABBER.start()


def stop_grabber():
    global __MSGIDGRABBER
    if __MSGIDGRABBER:
        logging.debug('Stopping msgidgrabber')
        __MSGIDGRABBER.stop()
        try:
            __MSGIDGRABBER.join()
        except:
            pass


def grab(msgid, future_nzo):
    global __MSGIDGRABBER
    if __MSGIDGRABBER:
        __MSGIDGRABBER.grab(msgid, future_nzo)


################################################################################
# DirectNZB support
################################################################################

_gFailures = 0
def _warn_user(msg):
    """ Warn user if too many soft newzbin errors occurred
    """
    global _gFailures
    _gFailures += 1
    if _gFailures > 5:
        logging.warning(msg)
        _gFailures = 0
    else:
        logging.debug(msg)

def _access_ok():
    global _gFailures
    _gFailures = 0


class MSGIDGrabber(Thread):
    """ Thread for msgid-grabber queue """
    def __init__(self):
        Thread.__init__(self)
        self.queue = Queue.Queue()
        for tup in sabnzbd.nzbqueue.get_msgids():
            self.queue.put(tup)
        self.shutdown = False

    def grab(self, msgid, nzo):
        logging.debug("Adding msgid %s to the queue", msgid)
        self.queue.put((msgid, nzo))

    def stop(self):
        # Put None on the queue to stop "run"
        self.shutdown = True
        self.queue.put((None, None))

    def run(self):
        """ Process the queue (including waits and retries) """
        def sleeper(delay):
            for n in range(delay):
                if not self.shutdown:
                    time.sleep(1.0)

        self.shutdown = False
        msgid = None
        while not self.shutdown:
            if not msgid:
                (msgid, nzo) = self.queue.get()
                if self.shutdown or not msgid:
                    break
            logging.debug("Popping msgid %s", msgid)

            filename, data, newzbin_cat, nzo_info = _grabnzb(msgid)
            if filename and data:
                filename = name_fixer(filename)

                _r, _u, _d = nzo.get_repair_opts()
                pp = sabnzbd.opts_to_pp(_r, _u, _d)
                script = nzo.get_script()
                cat = nzo.get_cat()
                if not cat:
                    cat = CatConvert(newzbin_cat)

                priority = nzo.get_priority()

                cat, pp, script, priority = cat_to_opts(cat, pp, script, priority)

                try:
                    sabnzbd.nzbqueue.insert_future_nzo(nzo, filename, msgid, data, pp=pp, script=script, cat=cat, priority=priority, nzo_info=nzo_info)
                except:
                    logging.error(T('error-nbUpdate@1'), msgid)
                    sabnzbd.nzbqueue.remove_nzo(nzo.nzo_id, False)
                msgid = None
            else:
                if filename:
                    sleeper(int(filename))
                else:
                    # Fatal error, give up on this one
                    bad_fetch(nzo, msgid, retry=False)
                    msgid = None

            osx.sendGrowlMsg("NZB added to queue",filename)

            # Keep some distance between the grabs
            sleeper(5)

        logging.debug('Stopping MSGIDGrabber')


def _grabnzb(msgid):
    """ Grab one msgid from newzbin """

    nothing  = (None, None, None, None)
    retry = (300, None, None, None)
    nzo_info = {'msgid': msgid}

    logging.info('Fetching NZB for Newzbin report #%s', msgid)

    headers = { 'User-Agent': 'SABnzbd', }

    # Connect to Newzbin
    try:
        if _HAVE_SSL:
            conn = httplib.HTTPSConnection('www.newzbin.com')
        else:
            conn = httplib.HTTPConnection('www.newzbin.com')

        postdata = { 'username': cfg.USERNAME_NEWZBIN.get(), 'password': cfg.PASSWORD_NEWZBIN.get(), 'reportid': msgid }
        postdata = urllib.urlencode(postdata)

        headers['Content-type'] = 'application/x-www-form-urlencoded'

        fetchurl = '/api/dnzb/'
        conn.request('POST', fetchurl, postdata, headers)
        response = conn.getresponse()
    except:
        _warn_user('Problem accessing Newzbin server, wait 5 min.')
        return retry

    # Save debug info if we have to
    data = response.read()

    # Get the filename
    rcode = response.getheader('X-DNZB-RCode')
    rtext = response.getheader('X-DNZB-RText')
    try:
        nzo_info['more_info'] = response.getheader('X-DNZB-MoreInfo')
    except:
        # Only some reports will generate a moreinfo header
        pass
    if not (rcode or rtext):
        logging.error(T('error-nbProtocol'))
        return nothing

    # Official return codes:
    # 200 = OK, NZB content follows
    # 400 = Bad Request, please supply all parameters
    #       (this generally means reportid or fileid is missing; missing user/pass gets you a 401)
    # 401 = Unauthorised, check username/password?
    # 402 = Payment Required, not Premium
    # 404 = Not Found, data doesn't exist?
    #       (only working for reportids, see Technical Limitations)
    # 450 = Try Later, wait <x> seconds for counter to reset
    #       (for an explanation of this, see DNZB Rate Limiting)
    # 500 = Internal Server Error, please report to Administrator
    # 503 = Service Unavailable, site is currently down

    if rcode in ('500', '503'):
        _warn_user('Newzbin has a server problem (%s, %s), wait 5 min.' % (rcode, rtext))
        return retry

    _access_ok()

    if rcode == '450':
        wait_re = re.compile('wait (\d+) seconds')
        try:
            wait = int(wait_re.findall(rtext)[0])
        except:
            wait = 60
        if wait > 60:
            wait = 60
        logging.info("Newzbin says we should wait for %s sec", wait)
        return int(wait+1), None, None, None

    if rcode in ('402'):
        logging.warning(T('warn-nbCredit'))
        return nothing

    if rcode in ('401'):
        logging.warning(T('warn-nbNoAuth'))
        return nothing

    if rcode in ('400', '404'):
        logging.error(T('error-nbReport@1'), msgid)
        return nothing

    if rcode != '200':
        logging.error(T('error-nbUnkownError@2'), rcode, rtext)
        return nothing

    # Process data
    report_name = response.getheader('X-DNZB-Name')
    report_cat  = response.getheader('X-DNZB-Category')
    if not (report_name and report_cat):
        logging.error(T('error-nbInfo@1'), msgid)
        return nothing

    # sanitize report_name
    newname = sanitize_foldername(report_name)
    if len(newname) > 80:
        newname = newname[0:79].strip('. ')
    newname += ".nzb"

    logging.info('Successfully fetched report %s - %s (cat=%s) (%s)', msgid, report_name, report_cat, newname)

    return (newname, data, report_cat, nzo_info)


################################################################################
# BookMark support
################################################################################
BOOK_LOCK = Lock()

class Bookmarks:
    """ Get list of bookmarks from www.newzbin.com
    """
    def __init__(self):
        self.bookmarks = sabnzbd.load_data(BOOKMARK_FILE_NAME)
        if not self.bookmarks:
            self.bookmarks = []

    @synchronized(BOOK_LOCK)
    def run(self, delete=None):

        headers = { 'User-Agent': 'SABnzbd', }

        # Connect to Newzbin
        try:
            if _HAVE_SSL:
                conn = httplib.HTTPSConnection('www.newzbin.com')
            else:
                conn = httplib.HTTPConnection('www.newzbin.com')

            if delete:
                logging.debug('Trying to delete Newzbin bookmark %s', delete)
                postdata = { 'username': cfg.USERNAME_NEWZBIN.get(), 'password': cfg.PASSWORD_NEWZBIN.get(), 'action': 'delete', \
                             'reportids' : delete }
            else:
                logging.info('Fetching Newzbin bookmarks')
                postdata = { 'username': cfg.USERNAME_NEWZBIN.get(), 'password': cfg.PASSWORD_NEWZBIN.get(), 'action': 'fetch'}
            postdata = urllib.urlencode(postdata)

            headers['Content-type'] = 'application/x-www-form-urlencoded'

            fetchurl = '/api/bookmarks/'
            conn.request('POST', fetchurl, postdata, headers)
            response = conn.getresponse()
        except:
            _warn_user('Problem accessing Newzbin server.')
            return

        data = response.read()

        # Get the status
        rcode = str(response.status)

        # Official return codes:
        # 200 = OK, NZB content follows
        # 204 = No content
        # 400 = Bad Request, please supply all parameters
        #       (this generally means reportid or fileid is missing; missing user/pass gets you a 401)
        # 401 = Unauthorised, check username/password?
        # 402 = Payment Required, not Premium
        # 403 = Forbidden (incorrect auth)
        # 500 = Internal Server Error, please report to Administrator
        # 503 = Service Unavailable, site is currently down

        if rcode not in ('500', '503'):
            _access_ok()

        if rcode == '204':
            logging.debug("No bookmarks set")
        elif rcode in ('401', '403'):
            logging.warning(T('warn-nbNoAuth'))
        elif rcode in ('402'):
            logging.warning(T('warn-nbCredit'))
        elif rcode in ('500', '503'):
            _warn_user('Newzbin has a server problem (%s).' % rcode)
        elif rcode == '200':
            if delete:
                if data.startswith('1'):
                    logging.info('Deleted newzbin bookmark %s', delete)
                    self.bookmarks.remove(delete)
                else:
                    logging.warning(T('warn-nbNoDelBM@1'), delete)
            else:
                for line in data.split('\n'):
                    try:
                        msgid, size, text = line.split('\t', 2)
                    except:
                        msgid = size = text = None
                    if msgid and (msgid not in self.bookmarks):
                        self.bookmarks.append(msgid)
                        logging.info("Found new bookmarked msgid %s (%s)", msgid, text)
                        sabnzbd.add_msgid(int(msgid), None, None, priority=cfg.DIRSCAN_PRIORITY.get())
        else:
            logging.error(T('error-nbUnkownError@1'), rcode)

        self.__busy = False

    @synchronized(BOOK_LOCK)
    def save(self):
        sabnzbd.save_data(self.bookmarks, BOOKMARK_FILE_NAME)

    def bookmarksList(self):
        return self.bookmarks

    def del_bookmark(self, msgid):
        msgid = str(msgid)
        if msgid in self.bookmarks:
            self.run(msgid)
