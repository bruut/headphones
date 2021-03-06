#  This file is part of Headphones.
#
#  Headphones is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  Headphones is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with Headphones.  If not, see <http://www.gnu.org/licenses/>.

import urllib, urllib2, urlparse
import lib.feedparser as feedparser
import lib.whatapi as whatapi
from xml.dom import minidom
from xml.parsers.expat import ExpatError
from StringIO import StringIO
import gzip

import os, re, time
import string

import headphones, exceptions
from headphones import logger, db, helpers, classes, sab

import lib.bencode as bencode

class NewzbinDownloader(urllib.FancyURLopener):

    def __init__(self):
        urllib.FancyURLopener.__init__(self)

    def http_error_default(self, url, fp, errcode, errmsg, headers):

        # if newzbin is throttling us, wait seconds and try again
        if errcode == 400:

            newzbinErrCode = int(headers.getheader('X-DNZB-RCode'))

            if newzbinErrCode == 450:
                rtext = str(headers.getheader('X-DNZB-RText'))
                result = re.search("wait (\d+) seconds", rtext)

                logger.info("Newzbin throttled our NZB downloading, pausing for " + result.group(1) + " seconds")
                time.sleep(int(result.group(1)))
                raise exceptions.NewzbinAPIThrottled()

            elif newzbinErrCode == 401:
                logger.info("Newzbin error 401")
                #raise exceptions.AuthException("Newzbin username or password incorrect")

            elif newzbinErrCode == 402:
                #raise exceptions.AuthException("Newzbin account not premium status, can't download NZBs")
                logger.info("Newzbin error 402")

#this should be in a class somewhere
def getNewzbinURL(url):

    myOpener = classes.AuthURLOpener(headphones.NEWZBIN_UID, headphones.NEWZBIN_PASSWORD)
    try:
        f = myOpener.openit(url)
    except (urllib.ContentTooShortError, IOError), e:
        logger.info("Error loading search results: ContentTooShortError ")
        return None

    data = f.read()
    f.close()

    return data
    
def url_fix(s, charset='utf-8'):
    if isinstance(s, unicode):
        s = s.encode(charset, 'ignore')
    scheme, netloc, path, qs, anchor = urlparse.urlsplit(s)
    path = urllib.quote(path, '/%')
    qs = urllib.quote_plus(qs, ':&=')
    return urlparse.urlunsplit((scheme, netloc, path, qs, anchor))    
    
    
def searchforalbum(albumid=None, new=False, lossless=False):
    
    if not albumid:

        myDB = db.DBConnection()
    
        results = myDB.select('SELECT AlbumID, Status from albums WHERE Status="Wanted" OR Status="Wanted Lossless"')
        new = True
         
        for result in results:
            foundNZB = "none"
            if (headphones.NZBMATRIX or headphones.NEWZNAB or headphones.NZBSORG or headphones.NEWZBIN) and (headphones.SAB_HOST or headphones.BLACKHOLE):
                if result['Status'] == "Wanted Lossless":
                    foundNZB = searchNZB(result['AlbumID'], new, losslessOnly=True)
                else:
                    foundNZB = searchNZB(result['AlbumID'], new)

            if (headphones.KAT or headphones.ISOHUNT or headphones.MININOVA or headphones.WAFFLES or headphones.WHATCD) and foundNZB == "none":
                if result['Status'] == "Wanted Lossless":
                    searchTorrent(result['AlbumID'], new, losslessOnly=True)
                else:
                    searchTorrent(result['AlbumID'], new)
            
    else:        
    
        foundNZB = "none"
        if (headphones.NZBMATRIX or headphones.NEWZNAB or headphones.NZBSORG or headphones.NEWZBIN) and (headphones.SAB_HOST or headphones.BLACKHOLE):
            foundNZB = searchNZB(albumid, new, lossless)

        if (headphones.KAT or headphones.ISOHUNT or headphones.MININOVA or headphones.WAFFLES or headphones.WHATCD) and foundNZB == "none":
            searchTorrent(albumid, new, lossless)

def searchNZB(albumid=None, new=False, losslessOnly=False):

    myDB = db.DBConnection()
    
    if albumid:
        results = myDB.select('SELECT ArtistName, AlbumTitle, AlbumID, ReleaseDate, Type from albums WHERE AlbumID=?', [albumid])
    else:
        results = myDB.select('SELECT ArtistName, AlbumTitle, AlbumID, ReleaseDate, Type from albums WHERE Status="Wanted" OR Status="Wanted Lossless"')
        new = True
        
    for albums in results:
        
        albumid = albums[2]
        reldate = albums[3]
        
        try:
            year = reldate[:4]
        except TypeError:
            year = ''
        
        dic = {'...':'', ' & ':' ', ' = ': ' ', '?':'', '$':'s', ' + ':' ', '"':'', ',':'', '*':'', '.':'', ':':''}

        cleanalbum = helpers.latinToAscii(helpers.replace_all(albums[1], dic))
        cleanartist = helpers.latinToAscii(helpers.replace_all(albums[0], dic))

        # FLAC usually doesn't have a year for some reason so I'll leave it out
        # Various Artist albums might be listed as VA, so I'll leave that out too
        # Only use the year if the term could return a bunch of different albums, i.e. self-titled albums
        if albums[0] in albums[1] or len(albums[0]) < 4 or len(albums[1]) < 4:
            term = cleanartist + ' ' + cleanalbum + ' ' + year
        elif albums[0] == 'Various Artists':
            term = cleanalbum + ' ' + year
        else:
            term = cleanartist + ' ' + cleanalbum
            
        # Replace bad characters in the term and unicode it
        term = re.sub('[\.\-\/]', ' ', term).encode('utf-8')
        artistterm = re.sub('[\.\-\/]', ' ', cleanartist).encode('utf-8')
        
        logger.info("Searching for %s since it was marked as wanted" % term)
        
        resultlist = []
        
        if headphones.NZBMATRIX:
            provider = "nzbmatrix"
            if headphones.PREFERRED_QUALITY == 3 or losslessOnly:
                categories = "23" 
            elif headphones.PREFERRED_QUALITY:
                categories = "23,22"
            else:
                categories = "22"
                
            # Search Audiobooks/Singles/etc
            if albums['Type'] == "Other":
                categories = "49"
                logger.info("Album type is audiobook/spokenword. Using audiobook category")
            if albums['Type'] == "Single":
                categories = "47"
                logger.info("Album type is 'Single'. Using singles category")
                
            # For some reason NZBMatrix is erroring out/timing out when the term starts with a "The" right now
            # so we'll strip it out for the time being. This may get fixed on their end, it may not, but
            # hopefully this will fix it for now. If you notice anything else it gets stuck on, please post it
            # on Github so it can be added
            if term.lower().startswith("the "):
                term = term[4:]
            
            
            params = {    "page": "download",
                        "username": headphones.NZBMATRIX_USERNAME,
                        "apikey": headphones.NZBMATRIX_APIKEY,
                        "subcat": categories,
                        "maxage": headphones.USENET_RETENTION,
                        "english": 1,
                        "ssl": 1,
                        "scenename": 1,
                        "term": term
                        }
                        
            searchURL = "http://rss.nzbmatrix.com/rss.php?" + urllib.urlencode(params)
            logger.info(u'Parsing results from <a href="%s">NZBMatrix</a>' % searchURL)
            try:
                data = urllib2.urlopen(searchURL, timeout=20).read()
            except urllib2.URLError, e:
                logger.warn('Error fetching data from NZBMatrix: %s' % e)
                data = False   
                
            if data:
            
                d = feedparser.parse(data)
                
                for item in d.entries:
                    try:
                        url = item.link
                        title = item.title
                        size = int(item.links[1]['length'])
                        
                        resultlist.append((title, size, url, provider))
                        logger.info('Found %s. Size: %s' % (title, helpers.bytes_to_mb(size)))
                    
                    except AttributeError, e:
                        logger.info(u"No results found from NZBMatrix for %s" % term)
            
        if headphones.NEWZNAB:
            
            newznab_hosts = [(headphones.NEWZNAB_HOST, headphones.NEWZNAB_APIKEY, headphones.NEWZNAB_ENABLED)]
            
            for newznab_host in headphones.EXTRA_NEWZNABS:
                if newznab_host[2] == '1' or newznab_host[2] == 1:
                    newznab_hosts.append(newznab_host)
            
            provider = "newznab"
            if headphones.PREFERRED_QUALITY == 3 or losslessOnly:
                categories = "3040"
            elif headphones.PREFERRED_QUALITY:
                categories = "3040,3010"
            else:
                categories = "3010"
                
            if albums['Type'] == 'Other':
                categories = "3030"
                logger.info("Album type is audiobook/spokenword. Using audiobook category")
                
            for newznab_host in newznab_hosts:

                params = {    "t": "search",
                            "apikey": newznab_host[1],
                            "cat": categories,
                            "maxage": headphones.USENET_RETENTION,
                            "q": term
                            }
            
                searchURL = newznab_host[0] + '/api?' + urllib.urlencode(params)
                    
                logger.info(u'Parsing results from <a href="%s">%s</a>' % (searchURL, newznab_host[0]))
                
                try:
                    data = urllib2.urlopen(searchURL, timeout=20).read()
                except urllib2.URLError, e:
                    logger.warn('Error fetching data from %s: %s' % (newznab_host[0], e))
                    data = False
                    
                if data:
                
                    d = feedparser.parse(data)
                    
                    if not len(d.entries):
                        logger.info(u"No results found from %s for %s" % (newznab_host[0], term))
                        pass
                    
                    else:
                        for item in d.entries:
                            try:
                                url = item.link
                                title = item.title
                                size = int(item.links[1]['length'])
                                
                                resultlist.append((title, size, url, provider))
                                logger.info('Found %s. Size: %s' % (title, helpers.bytes_to_mb(size))) 
                            
                            except Exception, e:
                                logger.error(u"An unknown error occurred trying to parse the feed: %s" % e)
                    
        if headphones.NZBSORG:
            provider = "nzbsorg"
            if headphones.PREFERRED_QUALITY == 3 or losslessOnly:
                categories = "3040"
            elif headphones.PREFERRED_QUALITY:
                categories = "3040,3010"
            else:
                categories = "3010"
                
            if albums['Type'] == 'Other':
                categories = "3030"
                logger.info("Album type is audiobook/spokenword. Using audiobook category")

            params = {    "t": "search",
                        "apikey": headphones.NZBSORG_HASH,
                        "cat": categories,
                        "maxage": headphones.USENET_RETENTION,
                        "q": term
                        }
        
            searchURL = 'http://beta.nzbs.org/api?' + urllib.urlencode(params)
                
            logger.info(u'Parsing results from <a href="%s">nzbs.org</a>' % searchURL)
            
            try:
                data = urllib2.urlopen(searchURL, timeout=20).read()
            except urllib2.URLError, e:
                logger.warn('Error fetching data from nzbs.org: %s' % e)
                data = False
                
            if data:
            
                d = feedparser.parse(data)
                
                if not len(d.entries):
                    logger.info(u"No results found from nzbs.org for %s" % term)
                    pass
                
                else:
                    for item in d.entries:
                        try:
                            url = item.link
                            title = item.title
                            size = int(item.links[1]['length'])
                            
                            resultlist.append((title, size, url, provider))
                            logger.info('Found %s. Size: %s' % (title, helpers.bytes_to_mb(size)))
                            
                        except Exception, e:
                            logger.error(u"An unknown error occurred trying to parse the feed: %s" % e)

        if headphones.NEWZBIN:
            provider = "newzbin"    
            providerurl = "https://www.newzbin2.es/"
            if headphones.PREFERRED_QUALITY == 3 or losslessOnly:
                categories = "7"        #music
                format = "2"             #flac
            elif headphones.PREFERRED_QUALITY:
                categories = "7"        #music
                format = "10"            #mp3+flac
            else:
                categories = "7"        #music
                format = "8"            #mp3      

            if albums['Type'] == 'Other':
                categories = "13"
                format = "16"
                logger.info("Album type is audiobook/spokenword. Using audiobook category")
            
            params = {   
                        "fpn": "p",
                        'u_nfo_posts_only': 0,
                        'u_url_posts_only': 0,
                        'u_comment_posts_only': 0,
                        'u_show_passworded': 0,
                        "searchaction": "Search",
                        #"dl": 1,
                        "category": categories,
                        "retention": headphones.USENET_RETENTION,
                        "ps_rb_audio_format": format,
                        "feed": "rss",
                        "u_post_results_amt": 50,        #this can default to a high number per user
                        "hauth": 1,
                        "q": term
                      }
            searchURL = providerurl + "search/?%s" % urllib.urlencode(params)
            try:
                data = getNewzbinURL(searchURL)
            except exceptions.NewzbinAPIThrottled:
                #try again if we were throttled
                data = getNewzbinURL(searchURL)
            if data:
                logger.info(u'Parsing results from <a href="%s">%s</a>' % (searchURL, providerurl))
                
                try:    
                    d = minidom.parseString(data)
                    node = d.documentElement
                    items = d.getElementsByTagName("item")
                except ExpatError:
                    logger.info('Unable to get the NEWZBIN feed. Check that your settings are correct - post a bug if they are')
                    items = []
            
            if len(items):
            
                for item in items:
        
                    sizenode = item.getElementsByTagName("report:size")[0].childNodes
                    titlenode = item.getElementsByTagName("title")[0].childNodes
                    linknode = item.getElementsByTagName("link")[0].childNodes
    
                    for node in sizenode:
                        size = int(node.data)
                    for node in titlenode:
                        title = node.data
                    for node in linknode:
                        url = node.data
                        
                        #exract the reportid from the link nodes
                        id_regex = re.escape(providerurl) + 'browse/post/(\d+)/'
                        id_match = re.match(id_regex, url)
                        if not id_match:
                            logger.info("Didn't find a valid Newzbin reportid in linknode")
                        else:
                            url = id_match.group(1) #we have to make a post request later, need the id                            
                    if url:
                        resultlist.append((title, size, url, provider))
                        logger.info('Found %s. Size: %s' % (title, helpers.bytes_to_mb(size)))
                    else:
                        logger.info('No url link found in nzb. Skipping.')    
                
            else:
                logger.info('No results found from NEWZBIN for %s' % term)

        #attempt to verify that this isn't a substring result
        #when looking for "Foo - Foo" we don't want "Foobar"
        #this should be less of an issue when it isn't a self-titled album so we'll only check vs artist
        if len(resultlist):
            resultlist[:] = [result for result in resultlist if verifyresult(result[0], artistterm, term)]
        
        if len(resultlist):    
                       
            if headphones.PREFERRED_QUALITY == 2 and headphones.PREFERRED_BITRATE:

                logger.debug('Target bitrate: %s kbps' % headphones.PREFERRED_BITRATE)

                tracks = myDB.select('SELECT TrackDuration from tracks WHERE AlbumID=?', [albumid])

                try:
                    albumlength = sum([pair[0] for pair in tracks])

                    targetsize = albumlength/1000 * int(headphones.PREFERRED_BITRATE) * 128
                    
                    if not targetsize:
                        logger.info('No track information for %s - %s. Defaulting to highest quality' % (albums[0], albums[1]))
                        nzblist = sorted(resultlist, key=lambda title: title[1], reverse=True)
                    
                    else:
                        logger.info('Target size: %s' % helpers.bytes_to_mb(targetsize))
                        newlist = []
                        
                        if headphones.PREFERRED_BITRATE_HIGH_BUFFER:
                            high_size_limit = targetsize * int(headphones.PREFERRED_BITRATE_HIGH_BUFFER)/100
                        else:
                            high_size_limit = None
                        if headphones.PREFERRED_BITRATE_LOW_BUFFER:
                            low_size_limit = targetsize * int(headphones.PREFERRED_BITRATE_LOW_BUFFER)/100
                        else:
                            low_size_limit = None
                            
                        for result in resultlist:
                            
                            if high_size_limit and (result[1] > high_size_limit):
                                logger.info(result[0] + "is too large for this album - not considering it. (Size: " + helpers.bytes_to_mb(result[1]) + ", Maxsize: " + helpers.bytes_to_mb(high_size_limit))
                                continue
                                
                            if low_size_limit and (result[1] < low_size_limit):
                                logger.info(result[0] + "is too small for this album - not considering it. (Size: " + helpers.bytes_to_mb(result[1]) + ", Minsize: " + helpers.bytes_to_mb(low_size_limit))
                                continue
                                                                
                            delta = abs(targetsize - result[1])
                            newlist.append((result[0], result[1], result[2], result[3], delta))
            
                        nzblist = sorted(newlist, key=lambda title: title[4])
                
                except Exception, e:
                    
                    logger.debug('Error: %s' % str(e))
                    logger.info('No track information for %s - %s. Defaulting to highest quality' % (albums[0], albums[1]))
                    
                    nzblist = sorted(resultlist, key=lambda title: title[1], reverse=True)
            
            else:
            
                nzblist = sorted(resultlist, key=lambda title: title[1], reverse=True)
            
            
            if new:
    
                while True:
                    
                    if len(nzblist):
                    
                        alreadydownloaded = myDB.select('SELECT * from snatched WHERE URL=?', [nzblist[0][2]])
                        
                        if len(alreadydownloaded):
                            logger.info('%s has already been downloaded. Skipping.' % nzblist[0][0])
                            nzblist.pop(0)
                        
                        else:
                            break
                    else:
                        logger.info('No more results found for %s' % term)
                        return "none"

            logger.info(u"Pre-processing result")
            
            (data, bestqual) = preprocess(nzblist)
            
            if data and bestqual:
                logger.info(u'Found best result: <a href="%s">%s</a> - %s' % (bestqual[2], bestqual[0], helpers.bytes_to_mb(bestqual[1])))
                nzb_folder_name = '%s - %s [%s]' % (helpers.latinToAscii(albums[0]).encode('UTF-8').replace('/', '_'), helpers.latinToAscii(albums[1]).encode('UTF-8').replace('/', '_'), year) 
                if headphones.SAB_HOST and not headphones.BLACKHOLE:

                    nzb = classes.NZBDataSearchResult()
                    nzb.extraInfo.append(data)
                    nzb.name = nzb_folder_name
                    sab.sendNZB(nzb)

                elif headphones.BLACKHOLE:
                
                    nzb_name = nzb_folder_name + '.nzb'
                    download_path = os.path.join(headphones.BLACKHOLE_DIR, nzb_name)
                    try:
                        f = open(download_path, 'w')
                        f.write(data)
                        f.close()
                        logger.info('File saved to: %s' % nzb_name)
                    except Exception, e:
                        logger.error('Couldn\'t write NZB file: %s' % e)
                        break
                        
                myDB.action('UPDATE albums SET status = "Snatched" WHERE AlbumID=?', [albums[2]])
                myDB.action('INSERT INTO snatched VALUES( ?, ?, ?, ?, DATETIME("NOW", "localtime"), ?, ?)', [albums[2], bestqual[0], bestqual[1], bestqual[2], "Snatched", nzb_folder_name])
                return "found"
            else:
                return "none"
        else:      
            return "none"



def verifyresult(title, artistterm, term):
    
    title = re.sub('[\.\-\/\_]', ' ', title)
    
    #if artistterm != 'Various Artists':
    #    
    #    if not re.search('^' + re.escape(artistterm), title, re.IGNORECASE):
    #        #logger.info("Removed from results: " + title + " (artist not at string start).")
    #        #return False
    #    elif re.search(re.escape(artistterm) + '\w', title, re.IGNORECASE | re.UNICODE):
    #        logger.info("Removed from results: " + title + " (post substring result).")
    #        return False
    #    elif re.search('\w' + re.escape(artistterm), title, re.IGNORECASE | re.UNICODE):
    #        logger.info("Removed from results: " + title + " (pre substring result).")
    #        return False

    #another attempt to weed out substrings. We don't want "Vol III" when we were looking for "Vol II"
    
    tokens = re.split('\W', term, re.IGNORECASE | re.UNICODE)
    for token in tokens:

        if not token:
            continue
        if token == 'Various' or token == 'Artists' or token == 'VA':
            continue
        if not re.search('(?:\W|^)+' + token + '(?:\W|$)+', title, re.IGNORECASE | re.UNICODE):
            cleantoken = ''.join(c for c in token if c not in string.punctuation)
            if not not re.search('(?:\W|^)+' + cleantoken + '(?:\W|$)+', title, re.IGNORECASE | re.UNICODE):
                dic = {'!':'i', '$':'s'}
                dumbtoken = helpers.replace_all(token, dic)
                if not not re.search('(?:\W|^)+' + dumbtoken + '(?:\W|$)+', title, re.IGNORECASE | re.UNICODE):
                    logger.info("Removed from results: " + title + " (missing tokens: " + token + " and " + cleantoken + ")")
                    return False
                    
    return True

def getresultNZB(result):
    
    nzb = None
    
    if result[3] == 'newzbin':
        params = urllib.urlencode({"username": headphones.NEWZBIN_UID, "password": headphones.NEWZBIN_PASSWORD, "reportid": result[2]})
        url = "https://www.newzbin2.es" + "/api/dnzb/"
        urllib._urlopener = NewzbinDownloader()
        try:
            nzb = urllib.urlopen(url, data=params).read()
        except urllib2.URLError, e:
            logger.warn('Error fetching nzb from url: %s. Error: %s' % (url, e))
        except exceptions.NewzbinAPIThrottled:
            #TODO: This has created a potentially infinite loop? As long as they keep throttling we keep trying.
            logger.info("Done waiting for Newzbin API throttle limit, starting downloads again")
            getresultNZB(result)
        except AttributeError:
            logger.warn("AttributeError in getresultNZB.")
    else:
        try:
            nzb = urllib2.urlopen(result[2], timeout=30).read()
        except urllib2.URLError, e:
            logger.warn('Error fetching nzb from url: ' + result[2] + ' %s' % e)
    return nzb
    
def preprocess(resultlist):

    if not headphones.USENET_RETENTION:
        usenet_retention = 2000
    else:
        usenet_retention = int(headphones.USENET_RETENTION)
    
    for result in resultlist:
        nzb = getresultNZB(result)
        if nzb:
            try:    
                d = minidom.parseString(nzb)
                node = d.documentElement
                nzbfiles = d.getElementsByTagName("file")
                skipping = False
                for nzbfile in nzbfiles:
                    if int(nzbfile.getAttribute("date")) < (time.time() - usenet_retention * 86400):
                        logger.info('NZB contains a file out of your retention. Skipping.')
                        skipping = True
                        break
                if skipping:
                    continue

                    #TODO: Do we want rar checking in here to try to keep unknowns out?
                    #or at least the option to do so?
            except ExpatError:
                logger.error('Unable to parse the best result NZB. Skipping.')
                continue
            return nzb, result
        else:
            logger.error("Couldn't retrieve the best nzb. Skipping.")
    return (False, False)



def searchTorrent(albumid=None, new=False, losslessOnly=False):

    myDB = db.DBConnection()
    
    if albumid:
        results = myDB.select('SELECT ArtistName, AlbumTitle, AlbumID, ReleaseDate from albums WHERE AlbumID=?', [albumid])
    else:
        results = myDB.select('SELECT ArtistName, AlbumTitle, AlbumID, ReleaseDate from albums WHERE Status="Wanted" OR Status="Wanted Lossless"')
        new = True
        
    for albums in results:
        
        albumid = albums[2]
        reldate = albums[3]
        
        try:
            year = reldate[:4]
        except TypeError:
            year = ''
        
        dic = {'...':'', ' & ':' ', ' = ': ' ', '?':'', '$':'s', ' + ':' ', '"':'', ',':'', '*':''}

        cleanalbum = helpers.latinToAscii(helpers.replace_all(albums[1], dic))
        cleanartist = helpers.latinToAscii(helpers.replace_all(albums[0], dic))

        # FLAC usually doesn't have a year for some reason so I'll leave it out
        # Various Artist albums might be listed as VA, so I'll leave that out too
        # Only use the year if the term could return a bunch of different albums, i.e. self-titled albums
        if albums[0] in albums[1] or len(albums[0]) < 4 or len(albums[1]) < 4:
            term = cleanartist + ' ' + cleanalbum + ' ' + year
        elif albums[0] == 'Various Artists':
            term = cleanalbum + ' ' + year
        else:
            term = cleanartist + ' ' + cleanalbum
            
        # Replace bad characters in the term and unicode it
        term = re.sub('[\.\-\/]', ' ', term).encode('utf-8')
        artistterm = re.sub('[\.\-\/]', ' ', cleanartist).encode('utf-8')
        albumterm  = re.sub('[\.\-\/]', ' ', cleanalbum).encode('utf-8')

        logger.info("Searching torrents for %s since it was marked as wanted" % term)
        
        resultlist = []
        pre_sorted_results = False
        minimumseeders = int(headphones.NUMBEROFSEEDERS) - 1

        if headphones.KAT:
            provider = "Kick Ass Torrent"
            providerurl = url_fix("http://www.kat.ph/search/" + term)
            if headphones.PREFERRED_QUALITY == 3 or losslessOnly:
                categories = "7"        #music
                format = "2"             #flac
                maxsize = 10000000000
            elif headphones.PREFERRED_QUALITY:
                categories = "7"        #music
                format = "10"            #mp3+flac
                maxsize = 10000000000
            else:
                categories = "7"        #music
                format = "8"            #mp3
                maxsize = 300000000        

            params = {   
                        "categories[0]": "music",
                        "field": "seeders",
                        "sorder": "desc",
                        "rss": "1"
                      }
            searchURL = providerurl + "/?%s" % urllib.urlencode(params)
            
            try:
                data = urllib2.urlopen(searchURL, timeout=20).read()
            except urllib2.URLError, e:
                logger.warn('Error fetching data from %s: %s' % (provider, e))
                data = False
            
            if data:
            
                logger.info(u'Parsing results from <a href="%s">KAT</a>' % searchURL)
                
                d = feedparser.parse(data)
                if not len(d.entries):
                    logger.info(u"No results found from %s for %s" % (provider, term))
                    pass
                
                else:
                    for item in d.entries:
                        try:
                            rightformat = True
                            title = item.title
                            seeders = item.seeds
                            url = item.links[1]['url']
                            size = int(item.links[1]['length'])
                            try:
                                if format == "2":
                                    request = urllib2.Request(url)
                                    request.add_header('Accept-encoding', 'gzip')
                                    request.add_header('Referer', 'http://kat.ph/')
                                    response = urllib2.urlopen(request)
                                    if response.info().get('Content-Encoding') == 'gzip':
                                        buf = StringIO( response.read())
                                        f = gzip.GzipFile(fileobj=buf)
                                        torrent = f.read()
                                    else:
                                        torrent = response.read()
                                    if int(torrent.find(".mp3")) > 0 and int(torrent.find(".flac")) < 1:
                                        rightformat = False
                            except Exception, e:
                                rightformat = False
                            if rightformat == True and size < maxsize and minimumseeders < int(seeders):
                                resultlist.append((title, size, url, provider))
                                logger.info('Found %s. Size: %s' % (title, helpers.bytes_to_mb(size)))
                            else:
                                logger.info('%s is larger than the maxsize, the wrong format or has too little seeders for this category, skipping. (Size: %i bytes, Seeders: %i, Format: %s)' % (title, size, int(seeders), rightformat))    
                        
                        except Exception, e:
                            logger.error(u"An unknown error occurred in the KAT parser: %s" % e)

        if headphones.WAFFLES:
            provider = "Waffles.fm"
            providerurl = url_fix("https://www.waffles.fm/browse.php")

            bitrate = None
            if headphones.PREFERRED_QUALITY == 3 or losslessOnly:
                format = "FLAC"
                bitrate = "(Lossless)"
                maxsize = 10000000000
            elif headphones.PREFERRED_QUALITY:
                format = "FLAC OR MP3"
                maxsize = 10000000000
            else:
                format = "MP3"
                maxsize = 300000000

            query_items = ['artist:"%s"' % artistterm,
                           'album:"%s"'   % albumterm,
                           'format:(%s)' % format,
                           'size:[0 TO %d]' % maxsize,
                           '-seeders:0'] # cut out dead torrents
            if bitrate:
                query_items.append('bitrate:"%s"' % bitrate)

            params = {
                "uid": headphones.WAFFLES_UID,
                "passkey": headphones.WAFFLES_PASSKEY,
                "rss": "1",
                "c0": "1",
                "s": "seeders", # sort by
                "d": "desc" # direction
            }

            searchURL = "%s?%s&q=%s" % (providerurl, urllib.urlencode(params), urllib.quote(" ".join(query_items)))

            try:
                data = urllib2.urlopen(searchURL, timeout=20).read()
            except urllib2.URLError, e:
                logger.warn('Error fetching data from %s: %s' % (provider, e))
                data = False

            if data:
                
                logger.info(u'Parsing results from <a href="%s">Waffles.fm</a>' % searchURL)
                
                d = feedparser.parse(data)
                if not len(d.entries):
                    logger.info(u"No results found from %s for %s" % (provider, term))
                    pass

                else:
                    for item in d.entries:
                        try:
                            title_match = re.search(r"(.+)\[(.+)\]$", item.title)
                            title = title_match.group(1).strip()
                            details = title_match.group(2).split("-")

                            desc_match = re.search(r"Size: (\d+)<", item.description)
                            size = desc_match.group(1)

                            url = item.link

                            resultlist.append((title, size, url, provider))
                            logger.info('Found %s. Size: %s' % (title, helpers.bytes_to_mb(size)))
                        except Exception, e:
                            logger.error(u"An error occurred while trying to parse the response from Waffles.fm: %s" % e)

        if headphones.WHATCD:
            provider = "What.cd"
            providerurl = "http://what.cd/"

            bitrate = None
            if headphones.PREFERRED_QUALITY == 3 or losslessOnly:
                format_regex = "FLAC"
                maxsize = 10000000000
            elif headphones.PREFERRED_QUALITY:
                format_regex = "(FLAC|MP3)"
                bitrate = headphones.PREFERRED_BITRATE
                maxsize = 10000000000
            else:
                format_regex = "MP3"
                maxsize = 300000000

            try:
                whatcd = whatapi.getWhatcdNetwork(headphones.WHATCD_USERNAME, headphones.WHATCD_PASSWORD)
            except:
                whatcd = None
                logger.warn("What.cd credentials incorrect or site is down.")

            if whatcd:
                whatcd.enableCaching()
                logger.info("Getting artist information for %s..." % artistterm)
                artist = whatcd.getArtist(artistterm)
                artist_id = artist.getArtistId()
            else:
                artist_id = None

            if artist and artist_id: # will be None if artist not found
                logger.info(u"What.cd artist ID: %s" % artist_id)
                artist_releases = artist.getArtistReleases()
                logger.info(u"Found %d releases on %s for %s" % (len(artist_releases), provider, artistterm))
                #Returns a list with all artist's releases in form of dictionary {releasetype, year, name, id}
            else:
                artist_releases = []

            logger.info(u"Loading information about available torrents (this may take a while)")
            logger.info(u"Collecting individual releases...")
            release_torrent_groups = [ whatcd.getTorrentGroup(release['id']) for release in artist_releases if albumterm in release['name'] ]
            logger.info(u"Done gathering torrentgroups.")


            all_children = []
            for group in release_torrent_groups:
                logger.info(u"Getting individual torrents for parent ID %s" % group.getTorrentParentId())
                new_children = group.getTorrentChildren()
                all_children += new_children
                logger.info(u"Found torrent IDs: %s" % ", ".join(new_children))
            # cap at 10 matches, 1 per second to reduce hits on API...don't wanna get in trouble.
            # Might want to turn up number of matches later.
#            max_torrent_info_reads = 10
            info_read_rate = 1

            logger.info(u"Gathering torrent objects for IDs.")
            match_torrents = []
            for i, child_id in enumerate(all_children):
                if i > 0:
                    time.sleep(info_read_rate)
                torrent_object = whatcd.getTorrent(child_id)
                match_torrents.append(torrent_object)
                logger.info(u"Created torrent object for %s" % torrent_object.getTorrentFolderName())


            # filter on format, size, and num seeders
            logger.info(u"Filtering torrents by format, maximum size, and minimum seeders...")
            match_torrents = [ torrent for torrent in match_torrents if re.search(format_regex, torrent.getTorrentDetails(), flags=re.I) ]
            match_torrents = [ torrent for torrent in match_torrents if helpers.mb_to_bytes(torrent.getTorrentSize()) <= maxsize ]
            match_torrents = [ torrent for torrent in match_torrents if int(torrent.getTorrentSeeders()) >= minimumseeders ]
#            match_torrents = [ torrent for torrent in match_torrents
#                               if re.search(format_regex, torrent.getTorrentDetails(), flags=re.I)
#                                and helpers.mb_to_bytes(torrent.getTorrentSize()) <= maxsize
#                                and int(torrent.getTorrentSeeders()) >= minimumseeders ] #hotspot
            logger.info(u"Remaining torrents: %s" % ", ".join([torrent.getTorrentFolderName() for torrent in match_torrents]))

            # sort by times d/l'd
            if not len(match_torrents):
                logger.info(u"No results found from %s for %s after filtering" % (provider, term))
            elif len(match_torrents) > 1:
                logger.info(u"Found %d matching releases from %s for %s - %s after filtering" %
                            (len(match_torrents), provider, artistterm, albumterm))
                logger.info("Sorting torrents by times snatched and preferred bitrate %s..." % bitrate)
                match_torrents.sort(key=lambda x: int(x.getTorrentSnatched()), reverse=True)
                if bitrate:
                    match_torrents.sort(key=lambda x: re.match("mp3", x.getTorrentDetails(), flags=re.I), reverse=True)
                    match_torrents.sort(key=lambda x: str(bitrate) in x.getTorrentFolderName(), reverse=True)
                logger.info(u"New order: %s" % ", ".join([u"%s - %s snatches" % (torrent.getTorrentFolderName(), torrent.getTorrentSnatched())
                                                                        for torrent in match_torrents]))

            pre_sorted_results = True
            for torrent in match_torrents:
                resultlist.append((torrent.getTorrentFolderName(),
                                   helpers.mb_to_bytes(torrent.getTorrentSize()),
                                   providerurl + torrent.getTorrentDownloadURL(),
                                   provider))

        if headphones.ISOHUNT:
            provider = "isoHunt"    
            providerurl = url_fix("http://isohunt.com/js/rss/" + term)
            if headphones.PREFERRED_QUALITY == 3 or losslessOnly:
                categories = "7"        #music
                format = "2"             #flac
                maxsize = 10000000000
            elif headphones.PREFERRED_QUALITY:
                categories = "7"        #music
                format = "10"            #mp3+flac
                maxsize = 10000000000
            else:
                categories = "7"        #music
                format = "8"            #mp3
                maxsize = 300000000        

            params = {   
                        "iht": "2",
                        "sort": "seeds"
                      }
            searchURL = providerurl + "?%s" % urllib.urlencode(params)
            
            try:
                data = urllib2.urlopen(searchURL, timeout=20).read()
            except urllib2.URLError, e:
                logger.warn('Error fetching data from %s: %s' % (provider, e))
                data = False
            
            if data:
            
                logger.info(u'Parsing results from <a href="%s">isoHunt</a>' % searchURL)
                
                d = feedparser.parse(data)
                if not len(d.entries):
                    logger.info(u"No results found from %s for %s" % (provider, term))
                    pass
                
                else:
                    for item in d.entries:
                        try:
                            rightformat = True
                            title = re.sub(r"(?<=  \[)(.+)(?=\])","",item.title)
                            title = title.replace("[]","")
                            sxstart = item.description.find("Seeds: ") + 7
                            seeds = ""
                            while item.description[sxstart:sxstart + 1] != " ":
                                seeds = seeds + item.description[sxstart:sxstart + 1]
                                sxstart = sxstart + 1
                            url = item.links[1]['url']
                            size = int(item.links[1]['length'])
                            try:
                                if format == "2":
                                    request = urllib2.Request(url)
                                    request.add_header('Accept-encoding', 'gzip')
                                    response = urllib2.urlopen(request)
                                    if response.info().get('Content-Encoding') == 'gzip':
                                        buf = StringIO( response.read())
                                        f = gzip.GzipFile(fileobj=buf)
                                        torrent = f.read()
                                    else:
                                        torrent = response.read()
                                    if int(torrent.find(".mp3")) > 0 and int(torrent.find(".flac")) < 1:
                                        rightformat = False
                            except Exception, e:
                                rightformat = False
                            if rightformat == True and size < maxsize and minimumseeders < seeds:
                                resultlist.append((title, size, url, provider))
                                logger.info('Found %s. Size: %s' % (title, helpers.bytes_to_mb(size)))
                            else:
                                logger.info('%s is larger than the maxsize, the wrong format or has too little seeders for this category, skipping. (Size: %i bytes, Seeders: %i, Format: %s)' % (title, size, int(seeds), rightformat))    
                        
                        except Exception, e:
                            logger.error(u"An unknown error occurred in the isoHunt parser: %s" % e)

        if headphones.MININOVA:
            provider = "Mininova"    
            providerurl = url_fix("http://www.mininova.org/rss/" + term + "/5")
            if headphones.PREFERRED_QUALITY == 3 or losslessOnly:
                categories = "7"        #music
                format = "2"             #flac
                maxsize = 10000000000
            elif headphones.PREFERRED_QUALITY:
                categories = "7"        #music
                format = "10"            #mp3+flac
                maxsize = 10000000000
            else:
                categories = "7"        #music
                format = "8"            #mp3
                maxsize = 300000000        

            searchURL = providerurl     
       
            try:
                data = urllib2.urlopen(searchURL, timeout=20).read()
            except urllib2.URLError, e:
                logger.warn('Error fetching data from %s: %s' % (provider, e))
                data = False
            
            if data:
            
                logger.info(u'Parsing results from <a href="%s">Mininova</a>' % searchURL)
                
                d = feedparser.parse(data)
                if not len(d.entries):
                    logger.info(u"No results found from %s for %s" % (provider, term))
                    pass
                
                else:
                    for item in d.entries:
                        try:
                            rightformat = True
                            title = item.title
                            sxstart = item.description.find("Ratio: ") + 7
                            seeds = ""
                            while item.description[sxstart:sxstart + 1] != " ":
                                seeds = seeds + item.description[sxstart:sxstart + 1]
                                sxstart = sxstart + 1
                            url = item.links[1]['url']
                            size = int(item.links[1]['length'])
                            try:
                                if format == "2":
                                    request = urllib2.Request(url)
                                    request.add_header('Accept-encoding', 'gzip')
                                    response = urllib2.urlopen(request)
                                    if response.info().get('Content-Encoding') == 'gzip':
                                        buf = StringIO( response.read())
                                        f = gzip.GzipFile(fileobj=buf)
                                        torrent = f.read()
                                    else:
                                        torrent = response.read()
                                    if int(torrent.find(".mp3")) > 0 and int(torrent.find(".flac")) < 1:
                                        rightformat = False
                            except Exception, e:
                                rightformat = False
                            if rightformat == True and size < maxsize and minimumseeders < seeds:
                                resultlist.append((title, size, url, provider))
                                logger.info('Found %s. Size: %s' % (title, helpers.bytes_to_mb(size)))
                            else:
                                logger.info('%s is larger than the maxsize, the wrong format or has too little seeders for this category, skipping. (Size: %i bytes, Seeders: %i, Format: %s)' % (title, size, int(seeds), rightformat))    
                        
                        except Exception, e:
                            logger.error(u"An unknown error occurred in the Mininova Parser: %s" % e)



        #attempt to verify that this isn't a substring result
        #when looking for "Foo - Foo" we don't want "Foobar"
        #this should be less of an issue when it isn't a self-titled album so we'll only check vs artist
        if len(resultlist):
            resultlist[:] = [result for result in resultlist if verifyresult(result[0], artistterm, term)]
        
        if len(resultlist):    
                       
            if headphones.PREFERRED_QUALITY == 2 and headphones.PREFERRED_BITRATE and not pre_sorted_results:

                logger.debug('Target bitrate: %s kbps' % headphones.PREFERRED_BITRATE)

                tracks = myDB.select('SELECT TrackDuration from tracks WHERE AlbumID=?', [albumid])

                try:
                    albumlength = sum([pair[0] for pair in tracks])

                    targetsize = albumlength/1000 * int(headphones.PREFERRED_BITRATE) * 128
                    logger.info('Target size: %s' % helpers.bytes_to_mb(targetsize))
    
                    newlist = []

                    for result in resultlist:
                        delta = abs(targetsize - result[1])
                        newlist.append((result[0], result[1], result[2], result[3], delta))
        
                    torrentlist = sorted(newlist, key=lambda title: title[4])
                
                except Exception, e:
                    
                    logger.debug('Error: %s' % str(e))
                    logger.info('No track information for %s - %s. Defaulting to highest quality' % (albums[0], albums[1]))
                    
                    torrentlist = sorted(resultlist, key=lambda title: title[1], reverse=True)

            elif pre_sorted_results:

                torrentlist = resultlist
            
            else:
            
                torrentlist = sorted(resultlist, key=lambda title: title[1], reverse=True)
            
            
            if new:
    
                while True:
                    
                    if len(torrentlist):
                    
                        alreadydownloaded = myDB.select('SELECT * from snatched WHERE URL=?', [torrentlist[0][2]])
                        
                        if len(alreadydownloaded):
                            logger.info('%s has already been downloaded. Skipping.' % torrentlist[0][0])
                            torrentlist.pop(0)
                        
                        else:
                            break
                    else:
                        logger.info('No more results found for %s' % term)
                        return

            logger.info(u"Pre-processing result")
            
            (data, bestqual) = preprocesstorrent(torrentlist, pre_sorted_results)
            
            if data and bestqual:
                logger.info(u'Found best result from %s: <a href="%s">%s</a> - %s' % (bestqual[3], bestqual[2], bestqual[0], helpers.bytes_to_mb(bestqual[1])))
                torrent_folder_name = '%s - %s [%s]' % (helpers.latinToAscii(albums[0]).encode('UTF-8').replace('/', '_'), helpers.latinToAscii(albums[1]).encode('UTF-8').replace('/', '_'), year) 
                if headphones.TORRENTBLACKHOLE_DIR == "sendtracker":

                    torrent = classes.TorrentDataSearchResult()
                    torrent.extraInfo.append(data)
                    torrent.name = torrent_folder_name
                    sab.sendTorrent(torrent)

                elif headphones.TORRENTBLACKHOLE_DIR != "":
                
                    # Get torrent name from .torrent, this is usually used by the torrent client as the folder name


                    torrent_name = torrent_folder_name + '.torrent'
                    download_path = os.path.join(headphones.TORRENTBLACKHOLE_DIR, torrent_name)
                    try:
			#Write the torrent file to a path derived from the TORRENTBLACKHOLE_DIR and file name.
			torrent_file = open(download_path, 'wb')
			torrent_file.write(data)
			torrent_file.close()
			#Open the fresh torrent file again so we can extract the proper torrent name
			#Used later in post-processing.
			torrent_file = open(download_path, 'rb')
                        torrent_info = bencode.bdecode(torrent_file.read())
			torrent_file.close()
                        torrent_folder_name = torrent_info['info'].get('name','').decode('utf-8')
                        logger.info('Torrent folder name: %s' % torrent_folder_name)
                    except Exception, e:
                        logger.error('Couldn\'t get name from Torrent file: %s' % e)
                        break
                        
                myDB.action('UPDATE albums SET status = "Snatched" WHERE AlbumID=?', [albums[2]])
                myDB.action('INSERT INTO snatched VALUES( ?, ?, ?, ?, DATETIME("NOW", "localtime"), ?, ?)', [albums[2], bestqual[0], bestqual[1], bestqual[2], "Snatched", torrent_folder_name])

def preprocesstorrent(resultlist, pre_sorted_list=False):
    selresult = ""
    if pre_sorted_list:
        selresult = resultlist[0]
    else:
        for result in resultlist:
            if selresult == "":
                selresult = result
            elif int(selresult[1]) < int(result[1]): # if size is lower than new result replace previous selected result (bigger size = better quality?)
                selresult = result
            
    try:
        request = urllib2.Request(selresult[2])
        request.add_header('Accept-encoding', 'gzip')
        
        if selresult[3] == 'Kick Ass Torrent':
            request.add_header('Referer', 'http://kat.ph/')
            
        response = urllib2.urlopen(request)
        if response.info().get('Content-Encoding') == 'gzip':
            buf = StringIO(response.read())
            f = gzip.GzipFile(fileobj=buf)
            torrent = f.read()
        else:
            torrent = response.read()
    except ExpatError:
        logger.error('Unable to torrent %s' % selresult[2])
        
    return torrent, selresult
