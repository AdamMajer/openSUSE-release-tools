#!/usr/bin/python3

import argparse
from dateutil.parser import parse as date_parse
from datetime import datetime
import itertools
import logging
import sys
from xml.etree import cElementTree as ET

import osc.conf
import osc.core
from osc.core import get_commitlog
from osc.core import get_request_list

from urllib.error import HTTPError

import subprocess
import time
import yaml
from collections import namedtuple

from osclib.memoize import memoize

logger = logging.getLogger()

makeurl = osc.core.makeurl
http_GET = osc.core.http_GET
http_DELETE = osc.core.http_DELETE
http_PUT = osc.core.http_PUT
http_POST = osc.core.http_POST

class Manager42(object):

    config_defaults = {
        'ignored_packages': [
            '00Meta',
            '00aggregates',
            '000product',
            '000package-groups',
            '000release-packages',
            ],
        'project_preference_order': [],
        'drop_if_vanished_from': [],
        'from_prj': 'openSUSE:Leap:42.3',
        'factory': 'openSUSE:Factory',
        }

    def __init__(self, caching = True, configfh = None):
        self.caching = caching
        self.apiurl = osc.conf.config['apiurl']
        self.config = self._load_config(configfh)
        self.force = False

        self.parse_lookup(self.config.from_prj)
        self.fill_package_meta()
        self.packages = dict()
        self.sle_workarounds = None
        for project in [self.config.from_prj] + self.config.project_preference_order:
            self._fill_package_list(project)

            if project.endswith(':SLE-workarounds'):
                self.sle_workarounds = project

    # FIXME: add to ToolBase and rebase Manager42 on that
    def _load_config(self, handle = None):
        d = self.__class__.config_defaults
        y = yaml.safe_load(handle) if handle is not None else {}
        return namedtuple('BotConfig', sorted(d.keys()))(*[ y.get(p, d[p]) for p in sorted(d.keys()) ])

    def latest_packages(self):
        data = self.cached_GET(makeurl(self.apiurl,
                                       ['project', 'latest_commits',
                                           self.config.from_prj]))
        lc = ET.fromstring(data)
        packages = set()
        for entry in lc.findall('{http://www.w3.org/2005/Atom}entry'):
            title = entry.find('{http://www.w3.org/2005/Atom}title').text
            if title.startswith('In '):
                packages.add(title[3:].split(' ')[0])
        return sorted(packages)

    def all_packages(self):
        return self.packages[self.config.from_prj]

    def parse_lookup(self, project):
        self.lookup_changes = 0
        self.lookup = {}
        try:
            self.lookup = yaml.safe_load(self._load_lookup_file(project))
        except HTTPError as e:
            if e.code != 404:
                raise

    def _load_lookup_file(self, prj):
        return self.cached_GET(makeurl(self.apiurl,
                                       ['source', prj, '00Meta', 'lookup.yml']))

    def _put_lookup_file(self, prj, data):
        return http_PUT(makeurl(self.apiurl,
                                ['source', prj, '00Meta', 'lookup.yml']), data=data)

    def store_lookup(self):
        if self.lookup_changes == 0:
            logger.info('no change to lookup.yml')
            return
        data = yaml.dump(self.lookup, default_flow_style=False, explicit_start=True)
        self._put_lookup_file(self.config.from_prj, data)
        self.lookup_changes = 0

    @memoize()
    def _cached_GET(self, url):
        return self.retried_GET(url).read()

    def cached_GET(self, url):
        if self.caching:
            return self._cached_GET(url)
        return self.retried_GET(url).read()

    def retried_GET(self, url):
        try:
            return http_GET(url)
        except HTTPError as e:
            if 500 <= e.code <= 599:
                logger.warning('Retrying {}'.format(url))
                time.sleep(1)
                return self.retried_GET(url)
            raise e

    def get_source_packages(self, project, expand=False):
        """Return the list of packages in a project."""
        query = {'expand': 1} if expand else {}
        try:
            root = ET.fromstring(
                self.cached_GET(makeurl(self.apiurl,
                                 ['source', project],
                                 query=query)))
            packages = [i.get('name') for i in root.findall('entry')]

        except HTTPError as e:
            if e.code == 404:
                logger.error("{}: {}".format(project, e))
                packages = []
            else:
                raise

        return packages

    def _get_source_package(self, project, package, revision):
        opts = { 'view': 'info' }
        if revision:
            opts['rev'] = revision
        return self.cached_GET(makeurl(self.apiurl,
                                ['source', project, package], opts))

    def crawl(self, packages):
        """Main method of the class that runs the crawler."""

        for package in sorted(packages):
            try:
                self.check_one_package(package)
            except HTTPError as e:
                logger.error("Failed to check {}: {}".format(package, e))
                pass

            # avoid loosing too much work
            if self.lookup_changes > 50:
                self.store_lookup()

            self.sle_workarounds_unneeded_check(package)

        if self.lookup_changes:
            self.store_lookup()

    def sle_workarounds_unneeded_check(self, package):
        # If SLE-workarounds project and package was not sourced from
        # SLE-workarounds, but it does exist in SLE-workarounds.
        if (self.sle_workarounds and not self.sle_workarounds_sourced and
            package in self.packages[self.sle_workarounds]):
            # Determine how recently the package was updated.
            root = ET.fromstringlist(
                get_commitlog(self.apiurl, self.sle_workarounds, package, None, format='xml'))
            updated_last = date_parse(root.find('logentry/date').text)
            age = datetime.now() - updated_last
            if age.total_seconds() < 3600 * 24:
                logger.debug('skip removal of {}/{} since updated within 24 hours'.format(
                    self.sle_workarounds, package))
                return

            requests = get_request_list(self.apiurl, self.sle_workarounds, package, req_type='submit')
            if len(requests):
                logger.debug('existing submit request involving {}/{}'.format(self.sle_workarounds, package))
                return

            self.delete_request(self.sle_workarounds, package,
                                'sourced from {}'.format(self.lookup.get(package)))

    def delete_request(self, project, package, message):
        requests = get_request_list(self.apiurl, project, package, req_type='delete')
        if len(requests):
            logger.debug('existing delete request for {}/{}'.format(project, package))
            return

        logger.info('creating delete request for {}/{}'.format(project, package))
        # No proper API function to perform the same operation.
        message = '"{}"'.format(message)
        print(subprocess.check_output(
            ' '.join(['osc', 'dr', '-m', message, project, package]), shell=True))

    def get_inconsistent(self):
        known = set(self.lookup.keys())
        stale = known - set(self.packages[self.config.from_prj])
        unknown = set(self.packages[self.config.from_prj]) - known

        if (stale):
            logger.info("stale packages: %s", ', '.join(stale))
        if (unknown):
            logger.info("unknown packages: %s", ', '.join(unknown))

        return (stale | unknown)

    def get_package_history(self, project, package, deleted = False):
        try:
            query = {}
            if deleted:
                query['deleted'] = 1
            return self.cached_GET(makeurl(self.apiurl,
                                   ['source', project, package, '_history'], query))
        except HTTPError as e:
            if e.code == 404:
                return None
            raise

    def _is_ignored(self, package):
        if package in self.config.ignored_packages:
            logger.debug("%s in ignore list", package)
            return True
        return False

    def _fill_package_list(self, project):
        if project not in self.packages:
            self.packages[project] = [ p for p in self.get_source_packages(project) if not self._is_ignored(p) ]

    def check_source_in_project(self, project, package, verifymd5, deleted=False):

        self._fill_package_list(project)

        if not deleted and not package in self.packages[project]:
            return None, None

        his = self.get_package_history(project, package, deleted)
        if his is None:
            return None, None

        his = ET.fromstring(his)
        historyrevs = dict()
        revs = list()
        for rev in his.findall('revision'):
            historyrevs[rev.find('srcmd5').text] = rev.get('rev')
            revs.append(rev.find('srcmd5').text)
        revs.reverse()
        for i in range(min(len(revs), 5)): # check last commits
            srcmd5 = revs.pop(0)
            root = self.cached_GET(makeurl(self.apiurl,
                                    ['source', project, package], { 'rev': srcmd5, 'view': 'info'}))
            root = ET.fromstring(root)
            if root.get('verifymd5') == verifymd5:
                return srcmd5, historyrevs[srcmd5]
        return None, None

    # check if we can find the srcmd5 in any of our underlay
    # projects
    def check_one_package(self, package):
        self.sle_workarounds_sourced = False
        lproject = self.lookup.get(package, None)
        if not package in self.packages[self.config.from_prj]:
            if not self._is_ignored(package):
                logger.info("{} vanished".format(package))
            if self.lookup.get(package):
                del self.lookup[package]
                self.lookup_changes += 1
            return

        root = ET.fromstring(self._get_source_package(self.config.from_prj, package, None))
        linked = root.find('linked')
        if not linked is None and linked.get('package') != package:
            lstring = 'subpackage of {}'.format(linked.get('package'))
            if lstring != lproject:
                logger.warning("{} links to {} (was {})".format(package, linked.get('package'), lproject))
                self.lookup[package] = lstring
                self.lookup_changes += 1
            else:
                logger.debug("{} correctly marked as subpackage of {}".format(package, linked.get('package')))
            return

        pm = self.package_metas[package]
        devel = pm.find('devel')
        if devel is not None or (lproject is not None and lproject.startswith('Devel;')):
            develprj = None
            develpkg = None
            if devel is None:
                (dummy, develprj, develpkg) = lproject.split(';')
                logger.warning('{} lacks devel project setting {}/{}'.format(package, develprj, develpkg))
            else:
                develprj = devel.get('project')
                develpkg = devel.get('package')
            srcmd5, rev = self.check_source_in_project(develprj, develpkg,
                                                       root.get('verifymd5'))
            if srcmd5:
                lstring = 'Devel;{};{}'.format(develprj, develpkg)
                if not package in self.lookup or lstring != self.lookup[package]:
                    logger.debug("{} from devel {}/{} (was {})".format(package, develprj, develpkg, lproject))
                    self.lookup[package] = lstring
                    self.lookup_changes += 1
                else:
                    logger.debug("{} lookup from {}/{} is correct".format(package, develprj, develpkg))
                return

        elif lproject and lproject != 'FORK' and not lproject.startswith('subpackage '):
            srcmd5, rev = self.check_source_in_project(lproject, package, root.get('verifymd5'))
            if srcmd5:
                logger.debug("{} lookup from {} is correct".format(package, lproject))
                # if it's from Factory we check if the package can be found elsewhere meanwhile
                if not self.force and lproject != self.config.factory:
                    return
            elif lproject == self.config.factory and not package in self.packages[lproject]:
                his = self.get_package_history(lproject, package, deleted=True)
                if his:
                    logger.debug("{} got dropped from {}".format(package, lproject))

        logger.debug("check where %s came from", package)
        foundit = False
        for project in self.config.project_preference_order:
            srcmd5, rev = self.check_source_in_project(project, package, root.get('verifymd5'))
            if srcmd5:
                if project != lproject:
                    if project.endswith(':SLE-workarounds'):
                        logger.info('{} is from {} but should come from {}'.format(package, project, lproject))
                        self.sle_workarounds_sourced = True
                    else:
                        logger.info('{} -> {} (was {})'.format(package, project, lproject))
                        self.lookup[package] = project
                        self.lookup_changes += 1
                else:
                    logger.debug('{} still coming from {}'.format(package, project))
                foundit = True
                break

        if not foundit:
            if lproject == 'FORK':
                logger.debug("{}: lookup is correctly marked as fork".format(package))
            elif lproject in self.config.drop_if_vanished_from:
                logger.info('{} dropped from {}'.format(package, lproject))
            else:
                logger.info('{} is a fork (was {})'.format(package, lproject))
                self.lookup[package] = 'FORK'
                self.lookup_changes += 1

    def get_link(self, project, package):
        try:
            link = self.cached_GET(makeurl(self.apiurl,
                                    ['source', project, package, '_link']))
        except HTTPError:
            return None
        return ET.fromstring(link)

    def fill_package_meta(self):
        self.package_metas = dict()
        url = makeurl(self.apiurl, ['search', 'package'],
                "match=[@project='%s']" % self.config.from_prj)
        root = ET.fromstring(self.cached_GET(url))
        for p in root.findall('package'):
            name = p.attrib['name']
            self.package_metas[name] = p


def main(args):
    # Configure OSC
    osc.conf.get_config(override_apiurl=args.apiurl)
    osc.conf.config['debug'] = args.debug

    uc = Manager42(caching = args.cache_requests, configfh = args.config )
    given_packages = set(args.packages)
    if args.all:
        given_packages = set(uc.all_packages())
    elif not given_packages:
        given_packages = set(uc.latest_packages())

    if args.check_inconsistent:
        given_packages |= uc.get_inconsistent()

    if args.force:
        uc.force = True
    uc.crawl(given_packages)

if __name__ == '__main__':
    description = 'maintain 00Meta/lookup.yml'
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('-A', '--apiurl', metavar='URL', help='API URL')
    parser.add_argument('-d', '--debug', action='store_true',
                        help='print info useful for debuging')
    parser.add_argument('-a', '--all', action='store_true',
                        help='check all packages')
    parser.add_argument('-c', '--config', dest='config', metavar='FILE',
                        type=argparse.FileType('r'), required = True,
                        help='read config file FILE')
    parser.add_argument('-n', '--dry', action='store_true',
                        help='dry run, no POST, PUT, DELETE')
    parser.add_argument('--force', action='store_true',
                        help='don\'t take previous lookup information into consideration')
    parser.add_argument('--cache-requests', action='store_true', default=False,
                        help='cache GET requests. Not recommended for daily use.')
    parser.add_argument('--check-inconsistent', action='store_true', default=False,
                        help='also check insonsistent lookup entries')
    parser.add_argument("packages", nargs='*', help="packages to check")

    args = parser.parse_args()

    # Set logging configuration
    logging.basicConfig(level=logging.DEBUG if args.debug
                        else logging.INFO,
                        format='%(asctime)s - %(module)s:%(lineno)d - %(levelname)s - %(message)s')

    if args.dry:
        def dryrun(t, *args, **kwargs):
            return lambda *args, **kwargs: logger.debug("dryrun %s %s %s", t, args, str(kwargs)[:200])

        http_POST = dryrun('POST')
        http_PUT = dryrun('PUT')
        http_DELETE = dryrun('DELETE')

    sys.exit(main(args))
