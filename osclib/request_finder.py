try:
    from urllib.parse import quote
    from urllib.error import HTTPError
except ImportError:
    #python 2.x
    from urllib2 import HTTPError, quote

from xml.etree import cElementTree as ET

from osc import oscerr
from osc.core import makeurl
from osc.core import http_GET


def _is_int(x):
    return isinstance(x, int) or x.isdigit()


class RequestFinder(object):

    def __init__(self, api):
        """
        Store the list of submit request together with the source project

        Example:
             {
                 212454: {
                     'project': 'openSUSE:Factory',
                 },
                 223870: {
                     'project': 'openSUSE:Factory',
                     'staging': 'openSUSE:Factory:Staging:A',
                 }
             }
        """
        self.api = api
        self.srs = {}

    def load_request(self, request_id):
        if not _is_int(request_id):
            return None

        url = makeurl(self.api.apiurl, ['request', str(request_id)])
        try:
            f = http_GET(url)
        except HTTPError:
            return None

        root = ET.parse(f).getroot()

        if root.get('id', None) != str(request_id):
            return None

        return root

    def find_request_id(self, request_id):
        """
        Look up the request by ID to verify if it is correct
        :param request_id: ID of the added request
        """

        root = self.load_request(request_id)
        if not root:
            return False

        project = root.find('action').find('target').get('project')
        if (project != self.api.project and not project.startswith(self.api.cstaging)):
            msg = 'Request {} is not for {}, but for {}'
            msg = msg.format(request_id, self.api.project, project)
            raise oscerr.WrongArgs(msg)
        self.srs[int(request_id)] = {'project': project}

        return True

    def find_request_package(self, package):
        """
        Look up the package by its name and return the SR#
        :param package: name of the package
        """

        query = 'types=submit,delete&states=new,review&project={}&view=collection&package={}'
        query = query.format(self.api.project, quote(package))
        url = makeurl(self.api.apiurl, ['request'], query)
        f = http_GET(url)

        root = ET.parse(f).getroot()

        requests = []
        for sr in root.findall('request'):
            # Check the target matches - OBS query is case insensitive, but OBS is not
            rq_target = sr.find('action').find('target')
            if package != rq_target.get('package') or self.api.project != rq_target.get('project'):
                continue

            request = sr.get('id')
            state = sr.find('state').get('name')

            self.srs[int(request)] = {'project': self.api.project, 'state': state}
            requests.append(request)

        if len(requests) > 1:
            msg = 'There are multiple requests for package "{}": {}'
            msg = msg.format(package, ', '.join(requests))
            raise oscerr.WrongArgs(msg)

        request = int(requests[0]) if requests else None
        return request

    def find_request_project(self, source_project, newcand):
        """
        Look up the source project by its name and return the SR#(s)
        :param source_project: name of the source project
        :param newcand: the review state of staging-group must be new
        """

        query = 'types=submit,delete&states=new,review&project={}&view=collection'.format(self.api.project)
        url = makeurl(self.api.apiurl, ['request'], query)
        f = http_GET(url)
        root = ET.parse(f).getroot()

        ret = None
        for sr in root.findall('request'):
            # ensure staging tool don't picks the processed request again
            if newcand:
                staging_group_states = [review.get('state') for review in sr.findall('review') if review.get('by_group') == self.api.cstaging_group]
                if 'new' not in staging_group_states:
                    continue
            for act in sr.findall('action'):
                src = act.find('source')
                if src is not None and src.get('project') == source_project:
                    request = int(sr.attrib['id'])
                    state = sr.find('state').get('name')
                    self.srs[request] = {'project': self.api.project, 'state': state}
                    ret = True

        return ret

    def find(self, pkgs, newcand):
        """
        Search for all various mutations and return list of SR#s
        :param pkgs: mesh of argumets to search for
        :param newcand: the review state of staging-group must be new

        This function is only called for its side effect.
        """
        for p in pkgs:
            if _is_int(p) and self.find_request_id(p):
                continue
            if self.find_request_package(p):
                continue
            if self.find_request_project(p, newcand):
                continue
            raise oscerr.WrongArgs('No SR# found for: {}'.format(p))

    def find_via_stagingapi(self, pkgs):
        """
        Search for all various mutations and return list of SR#s. Use
        and instance of StagingAPI to direct the search, this makes
        sure that the SR# are inside a staging project.
        :param pkgs: mesh of argumets to search for

        This function is only called for its side effect.
        """

        for p in pkgs:
            found = False
            for staging in self.api.get_staging_projects():
                if _is_int(p) and self.api.get_package_for_request_id(staging, p):
                    self.srs[int(p)] = {'staging': staging}
                    found = True
                    break
                else:
                    rq = self.api.get_request_id_for_package(staging, p)
                    if rq:
                        self.srs[rq] = {'staging': staging}
                        found = True
                        break
            if not found:
                raise oscerr.WrongArgs('No SR# found for: {}'.format(p))

    @classmethod
    def find_sr(cls, pkgs, api, newcand=False):
        """
        Search for all various mutations and return list of SR#s
        :param pkgs: mesh of argumets to search for
        :param api: StagingAPI instance
        :param newcand: the review state of staging-group must be new
        """
        finder = cls(api)
        finder.find(pkgs, newcand)
        return finder.srs

    @classmethod
    def find_staged_sr(cls, pkgs, api):
        """
        Search for all various mutations and return a single SR#s.
        :param pkgs: mesh of argumets to search for (SR#|package name)
        :param api: StagingAPI instance
        """
        finder = cls(api)
        finder.find_via_stagingapi(pkgs)
        return finder.srs
