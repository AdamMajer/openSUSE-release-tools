import time
from lxml import etree as ET

MAX_FROZEN_AGE = 6.5


class FreezeCommand(object):

    def __init__(self, api):
        self.api = api
        self.projectlinks = []

    def set_links(self):
        url = self.api.makeurl(['source', self.prj, '_meta'])
        f = self.api.retried_GET(url)
        root = ET.parse(f).getroot()
        links = root.findall('link')
        links.reverse()
        self.projectlinks = [link.get('project') for link in links]

    def set_bootstrap_copy(self):
        url = self.api.makeurl(['source', self.prj, '_meta'])

        f = self.api.retried_GET(url)
        oldmeta = ET.parse(f).getroot()

        meta = ET.fromstring(self.prj_meta_for_bootstrap_copy(self.prj))
        meta.find('title').text = oldmeta.find('title').text
        meta.find('description').text = oldmeta.find('description').text
        for person in oldmeta.findall('person'):
            # the xml has a fixed structure
            meta.insert(2, ET.Element('person', role=person.get('role'), userid=person.get('userid')))

        self.api.retried_PUT(url, ET.tostring(meta))

    def create_bootstrap_aggregate(self):
        self.create_bootstrap_aggregate_meta()
        self.create_bootstrap_aggregate_file()

    def bootstrap_packages(self):
        url = self.api.makeurl(['build', '{}:0-Bootstrap'.format(self.api.crings), '_result'])
        f = self.api.retried_GET(url)
        root = ET.parse(f).getroot().find('result')
        res = list()
        for e in root.findall('status'):
            name = e.get('package')
            if name in ['rpmlint-mini-AGGR']:
                continue
            res.append(name)
        return sorted(res)

    def create_bootstrap_aggregate_file(self):
        url = self.api.makeurl(['source', self.prj, 'bootstrap-copy', '_aggregate'])

        root = ET.Element('aggregatelist')
        a = ET.SubElement(root, 'aggregate',
                          {'project': '{}:0-Bootstrap'.format(self.api.crings)})

        for package in self.bootstrap_packages():
            p = ET.SubElement(a, 'package')
            p.text = package

        ET.SubElement(a, 'repository', {'target': 'bootstrap_copy', 'source': 'standard'})
        ET.SubElement(a, 'repository', {'target': 'standard', 'source': 'nothing'})
        ET.SubElement(a, 'repository', {'target': 'images', 'source': 'nothing'})

        self.api.retried_PUT(url, ET.tostring(root))

    def create_bootstrap_aggregate_meta(self):
        url = self.api.makeurl(['source', self.prj, 'bootstrap-copy', '_meta'])

        root = ET.Element('package', {'project': self.prj, 'name': 'bootstrap-copy'})
        ET.SubElement(root, 'title')
        ET.SubElement(root, 'description')
        f = ET.SubElement(root, 'build')
        # this one is to toggle
        ET.SubElement(f, 'disable', {'repository': 'bootstrap_copy'})
        # this one is the global toggle
        ET.SubElement(f, 'disable')

        self.api.retried_PUT(url, ET.tostring(root))

    def build_switch_bootstrap_copy(self, state):
        url = self.api.makeurl(['source', self.prj, 'bootstrap-copy', '_meta'])
        pkgmeta = ET.parse(self.api.retried_GET(url)).getroot()

        for f in pkgmeta.find('build'):
            if f.get('repository', None) == 'bootstrap_copy':
                f.tag = state
                pass
        self.api.retried_PUT(url, ET.tostring(pkgmeta))

    def verify_bootstrap_copy_codes(self, codes):
        url = self.api.makeurl(['build', self.prj, '_result'], {'package': 'bootstrap-copy'})

        root = ET.parse(self.api.retried_GET(url)).getroot()
        for result in root.findall('result'):
            if result.get('repository') == 'bootstrap_copy':
                status = result.find('status')
                if status is None:
                    return False
                if not status.get('code') in codes:
                    return False
        return True

    def perform(self, prj, copy_bootstrap=True):
        self.prj = prj

        if self.api.is_adi_project(prj):
            src_prj = self.api.find_devel_project_from_adi_frozenlinks(self.prj)
            if src_prj is None:
                raise Exception("{} does not have a valid frozenlinks".format(self.prj))
            else:
                self.api.update_adi_frozenlinks(self.prj, src_prj)
            return

        self.set_links()

        self.freeze_prjlinks()

        build_status = self.api.get_flag_in_prj(prj, flag='build')

        # If there is not a bootstrap repository, there is not
        # anything more to do.
        if not self.is_bootstrap():
            return

        if copy_bootstrap:
            self.set_bootstrap_copy()
            self.create_bootstrap_aggregate()
            print("waiting for scheduler to disable...")
            while not self.verify_bootstrap_copy_codes(['disabled']):
                time.sleep(1)
            self.build_switch_bootstrap_copy('enable')
            print("waiting for scheduler to copy...")
            while not self.verify_bootstrap_copy_codes(['finished', 'succeeded']):
                time.sleep(1)
            self.build_switch_bootstrap_copy('disable')

        # Set the original build status for the project
        self.api.build_switch_prj(prj, build_status)

    def prj_meta_for_bootstrap_copy(self, prj):
        root = ET.Element('project', {'name': prj})
        ET.SubElement(root, 'title')
        ET.SubElement(root, 'description')
        links = self.projectlinks or ['{}:1-MinimalX'.format(self.api.crings)]
        for lprj in links:
            ET.SubElement(root, 'link', {'project': lprj})
        f = ET.SubElement(root, 'build')
        # this one stays
        ET.SubElement(f, 'disable', {'repository': 'bootstrap_copy'})
        # this one is the global toggle
        ET.SubElement(f, 'disable')
        f = ET.SubElement(root, 'publish')
        ET.SubElement(f, 'disable')
        ET.SubElement(f, 'enable', {'repository': 'images'})
        f = ET.SubElement(root, 'debuginfo')
        ET.SubElement(f, 'enable')

        r = ET.SubElement(root, 'repository', {'name': 'bootstrap_copy'})
        ET.SubElement(r, 'path', {'project': self.api.cstaging, 'repository': 'standard'})
        for arch in self.api.cstaging_archs:
            a = ET.SubElement(r, 'arch')
            a.text = arch

        r = ET.SubElement(root, 'repository', {'name': 'standard', 'linkedbuild': 'all', 'rebuild': 'direct'})
        ET.SubElement(r, 'path', {'project': prj, 'repository': 'bootstrap_copy'})
        for arch in self.api.cstaging_archs:
            a = ET.SubElement(r, 'arch')
            a.text = arch

        r = ET.SubElement(root, 'repository', {'name': 'images', 'linkedbuild': 'all'})
        ET.SubElement(r, 'path', {'project': prj, 'repository': 'standard'})

        if prj.startswith('SUSE:'):
            a = ET.SubElement(r, 'arch')
            a.text = 'local'
        a = ET.SubElement(r, 'arch')
        a.text = 'x86_64'

        if 'ppc64le' in self.api.cstaging_archs:
            a = ET.SubElement(r, 'arch')
            a.text = 'ppc64le'

        return ET.tostring(root)

    def freeze_prjlinks(self):
        sources = {}
        flink = ET.Element('frozenlinks')

        for lprj in self.projectlinks:
            fl = ET.SubElement(flink, 'frozenlink', {'project': lprj})
            sources = self.receive_sources(lprj, sources, fl)

        url = self.api.makeurl(['source', self.prj, '_project', '_frozenlinks'], {'meta': '1'})
        self.api.retried_PUT(url, ET.tostring(flink))

    def receive_sources(self, prj, sources, flink):
        url = self.api.makeurl(['source', prj], {'view': 'info', 'nofilename': '1'})
        f = self.api.retried_GET(url)
        root = ET.parse(f).getroot()

        for si in root.findall('sourceinfo'):
            package = self.check_one_source(flink, si)
            sources[package] = 1
        return sources

    def check_one_source(self, flink, si):
        package = si.get('package')

        # If the package is an internal one (e.g _product)
        if package.startswith('_'):
            return None

        # Ignore packages with an origing (i.e. with an origin
        # different from the current project)
        if si.find('originproject') is not None:
            return None

        if package in ['rpmlint-mini-AGGR']:
            return package  # we should not freeze aggregates
        ET.SubElement(flink, 'package', {'name': package, 'srcmd5': si.get('srcmd5'), 'vrev': si.get('vrev')})
        return package

    def is_bootstrap(self):
        """Check if there is a bootstrap copy repository."""
        url = self.api.makeurl(['source', self.prj, '_meta'])
        root = ET.parse(self.api.retried_GET(url)).getroot()

        for repo in root.findall('.//repository'):
            if 'bootstrap_copy' == repo.get('name'):
                return True
        return False
