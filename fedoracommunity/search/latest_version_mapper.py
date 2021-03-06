import sys
import os

import xappy
import koji
import rpm

from distmappings import tags, tags_to_name_map
from utils import filter_search_string

import time

try:
    import json
except ImportError:
    import simplejson as json

class Mapper(object):
    """
    Indexes package to versions in a xapian db:

    package-name
        payload: {name: package-name,
                  product-name: {version: package-version,
                                 build-id: koji-build-id},
                  product-name: {version: package-version,
                                 build-id: koji-build-id}
                 }

    e.g.

    dbus
        payload: {'name': 'dbus',
                  'Rawhide': {'version': '1.4.10-3.fc17',
                              'build-id': 273333},
                  'Fedora 16': {'version': '1.4.10-3.fc16',
                                'build-id': 259964},
                  ...
                 }

    Last run timestamp is indexed by the _last_run_ key.
    """
    def __init__(self, dbpath, koji_url='http://koji.fedoraproject.org/kojihub'):
        self.dbpath = dbpath
        self.create_index()

        self.koji_client = koji.ClientSession('http://koji.fedoraproject.org/kojihub')
        self.koji_client.opts['anon_retry'] = True
        self.koji_client.opts['offline_retry'] = True
        self.updated_packages = {}
        self.new_packages = {}
        self.found_packages = {}
        self.sconn_needs_reload = False


    def create_index(self):
        self.iconn = xappy.IndexerConnection(self.dbpath)
        self.sconn = xappy.SearchConnection(self.dbpath)

        # keys are filtered package names or "_last_run_"
        self.iconn.add_field_action('key', xappy.FieldActions.INDEX_EXACT)

    def search(self, key):
        if self.sconn_needs_reload:
            self.sconn.reopen()
        q = self.sconn.query_parse('key:%s' % filter_search_string(key)) 
        results = self.sconn.search(q, 0, 1)

        return results

    def get_timestamp_doc(self):
        results = self.search('_last_run_')
        if not results:
            return None
        return results[0]

    def get_current_timestamp(self):
        doc = self.get_timestamp_doc()
        if not doc:
            return None
        time_stamp = doc._doc.get_data()
        return time_stamp

    def update_timestamp(self, timestamp):
        doc = self.get_timestamp_doc()
        if doc:
            doc._doc.set_data(str(timestamp))
            self.iconn.replace(doc)
            self.iconn.flush()
        else:
            doc = xappy.UnprocessedDocument()
            doc.fields.append(xappy.Field('key', '_last_run_'))
            processed_doc = self.iconn.process(doc, False)
            processed_doc._doc.set_data(str(timestamp))
            # preempt xappy's processing of data
            processed_doc._data = None
            self.iconn.add(processed_doc)
            self.iconn.flush()

    def init_db(self, *args):
        """
        loop through all packages and get the latest builds for koji tags
        listed in distmappings
        """
        self.new_timestamp = time.time() - 60
        print "Calculating timestamp minus 1 minute to account for any skew between the servers (%s)" % time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.new_timestamp))

        print "Initializing Index"
        package_list = self.koji_client.listPackages()
        i = 0
        for pkg in package_list:
            i += 1
            pkg_name = pkg['package_name']
            print "%d: Processing package %s" % (i, pkg_name)
            name_len = len(pkg_name)

            doc = xappy.UnprocessedDocument()
            filtered_name = filter_search_string(pkg_name)
            doc.fields.append(xappy.Field('key', filtered_name))

            latest_builds = {'name': pkg_name}
            for t in tags:
                tag = t['tag']
                if t['name'] in latest_builds:
                    # short circuit optimization
                    continue

                builds = self.koji_client.getLatestBuilds(tag, package=pkg_name)
                if builds:
                    build = None
                    for b in builds:
                        # only get builds which completed
                        if b['state'] == koji.BUILD_STATES['COMPLETE']:
                            build = b
                            break

                    if build:
                        data = {'version': build['version'],
                                'release': build['release'],
                                'build_id': build['build_id']}

                        if build.get('epoch', None) != None:
                            data['epoch'] = str(build['epoch'])
                            version_display = "%s:%s.%s" % (data['epoch'], data['version'], data['release'])
                        else:
                            version_display = "%s.%s" % (data['version'], data['release'])

                        latest_builds[t['name']] = data
                        print "    %s: %s" % (t['name'], version_display)

            if len(latest_builds) < 2:
                # don't process doc if there is no real data
                # most likely this is an outdated package
                continue

            processed_doc = self.iconn.process(doc, False)
            processed_doc._doc.set_data(json.dumps(latest_builds))
            # preempt xappy's processing of data
            processed_doc._data = None
            self.iconn.add(processed_doc)

        print "Finished updating timestamp"
        self.update_timestamp(self.new_timestamp)

    def update_db(self, timestamp=None):
        """ ask koji for any changes after we last ran the mapper
            if a timestamp is provided in ISO format ('YYYY-MM-DD HH:MI:SS')
            use that instead
        """

        try:
            timestamp = float(timestamp)
        except (ValueError, TypeError):
            pass

        if not timestamp:
            timestamp = self.get_current_timestamp()
            try:
                timestamp = float(timestamp)
            except (ValueError, TypeError):
                pass

            if not timestamp:
                print "Error: you need to specify a time to update from in ISO format ('YYYY-MM-DD HH:MI:SS') or run init"
                exit(-1)

        self.new_timestamp = time.time() - 60
        print "Calculating timestamp minus 1 minute to account for any skew between the servers (%s)" % time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.new_timestamp))


        opts = {'completedAfter': timestamp,
                'method': 'tagBuild',
                'decode': True}

        if isinstance(timestamp, float):
            display_timestamp = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(timestamp))
        else:
            display_timestamp = timestamp
        print "Getting Task List since %s" % display_timestamp
        task_list = self.koji_client.listTasks(opts=opts)
        print "Updating Index"
        for task in task_list:
            parent_id = task['parent']
            if parent_id:
                builds = self.koji_client.listBuilds(taskID=parent_id)

                if len(builds) < 1:
                    continue

                build = builds[0]

                pkg_tags = self.koji_client.listTags(build['build_id'])
                dist_name = None
                for t in pkg_tags:
                    dist_name = tags_to_name_map.get(t['name'], None)
                    if dist_name:
                        break

                if not dist_name:
                    continue

                pkg_doc = None
                if build['name'] in self.found_packages:
                    pkg_doc = self.found_packages[build['name']]
                else:
                    results = self.search(build['name'])

                    if results:
                        pkg_doc = results[0]

                build_epoch = build.get('epoch', None)
                if build_epoch is not None:
                    build_epoch = str(build_epoch)

                if not pkg_doc:
                    # TODO create new document
                    print "ran into new package %s" % build['name']
                    self.new_packages[build['name']] = True
                    doc = xappy.UnprocessedDocument()
                    filtered_name = filter_search_string(build['name'])
                    doc.fields.append(xappy.Field('key', filtered_name))

                    latest_builds = {'name': build['name']}
                    data = {}
                    if build_epoch is not None:
                        data['epoch'] = build_epoch
                    data['version'] = build['version']
                    data['release'] = build['release']
                    data['build_id'] = build['build_id']
                    latest_builds[dist_name] = data

                    processed_doc = self.iconn.process(doc, False)
                    processed_doc._doc.set_data(json.dumps(latest_builds))
                    # preempt xappy's processing of data
                    processed_doc._data = None
                    self.iconn.add(processed_doc)
                    self.sconn_needs_reload = True
                    self.iconn.flush()
                else:
                    latest_builds = json.loads(pkg_doc._doc.get_data())
                    data = latest_builds.get(dist_name, {'version': '0',
                                                         'release': '0',
                                                         'build_id': 0})
                    data_epoch = None
                    do_update = False
                    if 'release' not in data:
                        # do the update because we have old data
                        do_update = True
                    else:
                        data_epoch = data.get('epoch', None)
                        if data_epoch is not None:
                            data_epoch = str(data_epoch)

                        if rpm.labelCompare(
                            (build_epoch, build['version'], build['release']),
                            (data_epoch, data['version'], data['release'])) == 1:
                            do_update = True

                    if do_update:
                        self.updated_packages[build['name']] = True
                        build_vr = ''
                        if build_epoch is not None:
                            build_vr = "%s:%s.%s" % (build_epoch, build['version'], build['release'])
                        else:
                            build_vr = "%s.%s" % (build['version'], build['release'])

                        data_vr = ''
                        if data_epoch is not None:
                            data_vr = "%s:%s.%s" % (data_epoch, data['version'], data.get('release',''))
                        else:
                            data_vr = "%s.%s" % (data['version'], data.get('release', ''))

                        print "Updating package %s in dist %s to version %s (from %s)" % (
                                build['name'], dist_name, build_vr, data_vr)

                        if build_epoch is not None:
                            data['epoch'] = build_epoch
                        data['version'] = build['version']
                        data['release'] = build['release']
                        data['build_id'] = build['build_id']
                        latest_builds[dist_name] = data

                        pkg_doc._doc.set_data(json.dumps(latest_builds))
                        # preempt xappy's processing of data
                        pkg_doc._data = None
                        self.iconn.replace(pkg_doc)
                        self.sconn_needs_reload = True
                        self.found_packages[build['name']] = pkg_doc
                        self.iconn.flush()

        updated_count = len(self.updated_packages)
        new_count = len(self.new_packages)
        print "Updated: %d packages" % updated_count
        print "  Added: %d packages" % new_count
        print "========================="
        print "  Total: %s" % (updated_count + new_count)

        self.update_timestamp(self.new_timestamp)

    def cleanup(self):
        self.iconn.close()
        self.sconn.close()

def run(cache_path, action=None, timestamp=None, koji_url=None):

    versionmap_path = os.path.join(cache_path, 'versionmap')
    if action is None:
        if os.path.isdir(versionmap_path):
            # we assume we need to update because the path exists
            action = Mapper.update_db
        else:
            # otherwise we assume we need to create the db
            action = Mapper.init_db
    elif action == 'init':
        action = Mapper.init_db
    elif action == 'update':
        action = Mapper.update_db
    else:
        print "Unknown action %s" % action
        exit(-1)

    mapper = Mapper(versionmap_path, koji_url=koji_url)
    action(mapper, timestamp)
    mapper.cleanup()

if __name__ == '__main__':
    run(cache_path=os.getcwd())
