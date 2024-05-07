import dateutil.parser
from datetime import datetime

from osc.core import get_request
from osclib.comments import CommentAPI
from osclib.request_finder import RequestFinder


class UnignoreCommand(object):
    MESSAGE = 'Unignored: returned to active backlog.'

    def __init__(self, api):
        self.api = api
        self.comment = CommentAPI(self.api.apiurl)

    def perform(self, requests, cleanup=False):
        """
        Unignore a request by removing from ignore list.
        """

        requests_ignored = self.api.get_ignored_requests()

        if len(requests) == 1 and requests[0] == 'all':
            requests_ignored = {}
        else:
            for request_id in RequestFinder.find_sr(requests, self.api):
                if request_id in requests_ignored.keys():
                    print(f'{request_id}: unignored')
                    del requests_ignored[request_id]
                    self.api.del_ignored_request(request_id)
                    self.comment.add_comment(request_id=str(request_id), comment=self.MESSAGE)

        if cleanup:
            now = datetime.now()
            for request_id in set(requests_ignored):
                request = get_request(self.api.apiurl, str(request_id))
                if request.state.name not in ('new', 'review'):
                    changed = dateutil.parser.parse(request.state.when)
                    diff = now - changed
                    if diff.days > 3:
                        print('Removing {} which was {} {} days ago'
                              .format(request_id, request.state.name, diff.days))
                        self.api.del_ignored_request(request_id)

        return True
