from blueflood_graphite_finder.blueflood import TenantBluefloodFinder, \
    TenantBluefloodReader, TenantBluefloodLeafNode, \
    BluefloodClient, calc_res, NonNestedDataKey, NestedDataKey

import datetime
import logging.config
import threading
import unittest
from unittest import TestCase

import os
import requests_mock
from blueflood_graphite_finder import auth

logging_file = os.path.join(os.path.dirname(__file__), 'logging.ini')
logging.config.fileConfig(logging_file)

# To run these tests you need to set up the environment vars below
try:
    auth_api_key = os.environ['AUTH_API_KEY']
    auth_user_name = os.environ['AUTH_USER_NAME']
    auth_tenant = os.environ['AUTH_TENANT']
    bf_url = os.environ['BLUEFLOOD_URL']
    print "Authenticating using user_name=" + auth_user_name + \
          ", tenant=" + auth_tenant + ", url=" + bf_url
    auth_config = {
        'blueflood': {
            'authentication_module': 'blueflood_graphite_finder.rax_auth',
            'authentication_class': 'BluefloodAuth',
            'username': auth_user_name,
            'apikey': auth_api_key,
            'urls': [bf_url],
            'tenant': auth_tenant}}
except Exception as e:
    print e
    print "Auth env undefined, not running auth tests"
    auth_config = None

try:
    no_auth_tenant = os.environ['NO_AUTH_TENANT']
    no_bf_url = os.environ['NO_BLUEFLOOD_URL']
    no_auth_config = {'blueflood': {
        'urls': [no_bf_url],
        'tenant': no_auth_tenant}}
except:
    print "NO_AUTH env undefined, not running no_auth tests"
    no_auth_config = None

try:
    from graphite.storage import FindQuery

    print 'using graphite.storage.FindQuery'
except:
    try:
        from graphite_api.storage import FindQuery

        print 'using graphite_api.storage.FindQuery'
    except:
        print 'rolling my own FindQuery'

        class FindQuery(object):
            def __init__(self, pattern, starttime, endtime):
                self.pattern = pattern
                self.startTime = starttime
                self.endTime = endtime


def exc_callback(request, context):
    raise ValueError("Test exceptions")


class BluefloodTests(TestCase):
    # The "requests_mock" mocking framework we use is not thread-safe.
    # This mock inserts a lock to fix that
    fm_lock = threading.Lock()
    orig_find_metrics_with_enum_values = \
        TenantBluefloodFinder.find_metrics_with_enum_values

    def mock_find_metrics_with_enum_values(s, query):
        with BluefloodTests.fm_lock:
            return BluefloodTests.orig_find_metrics_with_enum_values(s, query)

    TenantBluefloodFinder.find_metrics_with_enum_values = \
        mock_find_metrics_with_enum_values

    def setUp(self):
        if not (auth_config or no_auth_config):
            self.fail("Failing: Environment variables not set")
        self.alias_key = '_avg'
        config = {'blueflood': {
            'urls': ["http://dummy.com"],
            'tenant': 'dummyTenant',
            'submetric_aliases': {self.alias_key: 'average',
                                  "_enum": 'enum'}}}
        self.finder = TenantBluefloodFinder(config)
        self.metric1 = "a.b.c"
        self.metric2 = "e.f.g"
        self.metric3 = "x.y.z"
        self.reader = TenantBluefloodReader(self.metric1, self.finder.tenant,
                                            self.finder.bf_query_endpoint,
                                            self.finder.enable_submetrics,
                                            self.finder.submetric_aliases,
                                            None)
        metric_with_enum1 = self.metric3 + '.' + 'v1'
        metric_with_enum2 = self.metric3 + '.' + 'v2'
        self.enum_reader1 = TenantBluefloodReader(
            metric_with_enum1,
            self.finder.tenant,
            self.finder.bf_query_endpoint,
            self.finder.enable_submetrics,
            self.finder.submetric_aliases,
            "v1")
        self.enum_reader2 = TenantBluefloodReader(
            metric_with_enum2,
            self.finder.tenant,
            self.finder.bf_query_endpoint,
            self.finder.enable_submetrics,
            self.finder.submetric_aliases,
            "v2")
        self.node1 = TenantBluefloodLeafNode(self.metric1, self.reader)
        self.node2 = TenantBluefloodLeafNode(self.metric2, self.reader)
        self.node3 = TenantBluefloodLeafNode(metric_with_enum1,
                                             self.enum_reader1)
        self.node4 = TenantBluefloodLeafNode(metric_with_enum2,
                                             self.enum_reader2)
        self.bfc = BluefloodClient(self.finder.bf_query_endpoint,
                                   self.finder.tenant,
                                   self.finder.enable_submetrics,
                                   self.finder.submetric_aliases, False)
        auth.set_auth(None)

    def run_find(self, finder):
        nodes = list(finder.find_nodes(FindQuery('*', 0, 100)))
        self.assertTrue(len(nodes) > 0)

    def setup_UTC_mock(self):
        # setup a mock that forces expiration
        self.orig_get_current_UTC = type(auth.auth).get_current_UTC
        self.orig_do_auth = type(auth.auth).do_auth
        this = self
        self.authCount = 0

        def mock_get_current_UTC(self):
            return auth.auth.expiration_UTC + datetime.timedelta(days=1)

        def mock_do_auth(self):
            this.authCount += 1
            this.orig_do_auth(self)

        type(auth.auth).get_current_UTC = mock_get_current_UTC
        type(auth.auth).do_auth = mock_do_auth

    def unset_UTC_mock(self):
        type(auth.auth).get_current_UTC = self.orig_get_current_UTC
        type(auth.auth).do_auth = self.orig_do_auth

    @unittest.skipIf(os.getenv("TRAVIS") == 'true',
                     "Don't run auth tests from Travis")
    def test_finder(self):
        if no_auth_config:
            print "\nRunning NO_AUTH tests"
            finder = TenantBluefloodFinder(no_auth_config)
            self.run_find(finder)

        if auth_config:
            print "\nRunning AUTH tests"
            finder = TenantBluefloodFinder(auth_config)
            self.run_find(finder)

            # force re-auth
            auth.auth.token = ""
            self.run_find(finder)

            # test expired UTC
            self.setup_UTC_mock()
            self.run_find(finder)
            self.unset_UTC_mock()
            self.assertTrue(self.authCount == 1)

    def test_gen_groups(self):
        # one time through without submetrics
        self.bfc.enable_submetrics = False

        # only 1 metric per group even though room for more
        self.bfc.maxmetrics_per_req = 1
        self.bfc.maxlen_per_req = 20
        groups = self.bfc.gen_groups([self.node1, self.node2])
        self.assertSequenceEqual(groups, [['a.b.c'], ['e.f.g']])

        # check that enum values get reduced to single metric name
        self.bfc.maxmetrics_per_req = 1
        self.bfc.maxlen_per_req = 20
        groups = self.bfc.gen_groups([self.node3, self.node4])
        self.assertSequenceEqual(groups, [['x.y.z']])

        # allow 2 metrics per group
        self.bfc.maxmetrics_per_req = 2
        groups = self.bfc.gen_groups([self.node1, self.node2])
        self.assertSequenceEqual(groups, [['a.b.c', 'e.f.g']])

        # now only room for 1 per group
        self.bfc.maxlen_per_req = 12
        groups = self.bfc.gen_groups([self.node1, self.node2])
        self.assertSequenceEqual(groups, [['a.b.c'], ['e.f.g']])

        # no room for metric in a group
        self.bfc.maxlen_per_req = 11
        with self.assertRaises(IndexError):
            groups = self.bfc.gen_groups([self.node1, self.node2])

        # now with submetrics
        self.bfc.enable_submetrics = True

        # only 1 metric per group even though room for more
        self.bfc.maxmetrics_per_req = 1
        self.bfc.maxlen_per_req = 15
        groups = self.bfc.gen_groups([self.node1, self.node2])
        groups[0].sort()
        self.assertSetEqual(set(tuple(map(tuple, groups))),
                            set([('a.b',), ('e.f',)]))

        # allow 2 metrics per group
        self.bfc.maxmetrics_per_req = 2
        groups = self.bfc.gen_groups([self.node1, self.node2])
        groups[0].sort()

        self.assertSetEqual(set(tuple(map(tuple, groups))),
                            set([('a.b', 'e.f',)]))

        # now only room for 1 per group
        self.bfc.maxlen_per_req = 10
        groups = self.bfc.gen_groups([self.node1, self.node2])
        groups[0].sort()
        self.assertSetEqual(set(tuple(map(tuple, groups))),
                            set([('a.b',), ('e.f',)]))

        # no room for metric in a group
        self.bfc.maxlen_per_req = 9
        with self.assertRaises(IndexError):
            groups = self.bfc.gen_groups([self.node1, self.node2])

    def make_data(self, start, step):
        def step_correction(value, step):
            return value * (step/60)
        # should be 0th element in response
        first_timestamp = start * 1000
        # should be skipped because it overlaps first_timestamp + 1000*step
        second_timestamp = first_timestamp + (1000 * step - 1)
        # should be 4th element
        third_timestamp = first_timestamp + (5000 * step - 1)
        # should be 7th element
        fourth_timestamp = first_timestamp + (7000 * step + 1)

        metric1 = self.metric1
        metric2 = self.metric2
        if self.bfc.enable_submetrics:
            submetric = '.' + self.alias_key
            metric1 += submetric
            metric2 += submetric
        node1 = TenantBluefloodLeafNode(metric1, self.reader)
        node2 = TenantBluefloodLeafNode(metric2, self.reader)
        return ([node1, node2],
                [{u'data': [
                    {u'timestamp': third_timestamp,
                     u'average': step_correction(4449.97, step),
                     u'numPoints': 1},
                    {u'timestamp': fourth_timestamp,
                     u'average': step_correction(14449.97, step),
                     u'numPoints': 1}],
                    u'metric': self.metric1, u'type': u'number',
                    u'unit': u'unknown'},
                    {u'data': [
                        {u'timestamp': first_timestamp, u'average':
                         step_correction(6421.18, step),
                         u'numPoints': 1},
                        {u'timestamp': second_timestamp, u'average':
                         step_correction(26421.18, step),
                         u'numPoints': 1}],
                        u'metric': self.metric2, u'type': u'number',
                        u'unit': u'unknown'}])

    def make_enum_data(self, start, step):
        # should be 0th element in response
        first_timestamp = start * 1000
        # should be skipped because it overlaps first_timestamp + 1000*step
        # second_timestamp = first_timestamp + (1000 * step - 1)
        # should be 4th element
        third_timestamp = first_timestamp + (5000 * step - 1)
        # should be 7th element
        fourth_timestamp = first_timestamp + (7000 * step + 1)

        return ([self.node3, self.node4],
                [{u'data': [
                    {u'timestamp': third_timestamp,
                     u'average': 4449.97,
                     u'numPoints': 1},
                    {u'timestamp': fourth_timestamp,
                     u'average': 14449.97,
                     u'numPoints': 1}],
                    u'metric': self.metric1, u'type': u'number',
                    u'unit': u'unknown'},
                    {u'data': [
                        {u'timestamp': third_timestamp,
                         u'enum_values': {u'v1': 13, u'v2': 7},
                         u'numPoints': 20},
                        {u'timestamp': fourth_timestamp,
                         u'enum_values': {u'v1': 11, u'v2': 3},
                         u'numPoints': 14}],
                        u'metric': self.metric3, u'type': u'number',
                        u'unit': u'unknown'}])

    def test_gen_dict(self):
        step = 3000
        start = 1426120000
        end = 1426147000
        nodes, responses = self.make_data(start, step)
        dictionary = self.bfc.gen_dict(nodes, responses, start, end, step)
        self.assertDictEqual(dictionary,
                             {nodes[1].path: [6421.18, None, None, None, None,
                                              None, None, None, None],
                              nodes[0].path: [None, None, None, None, 4449.97,
                                              7783.303333333333,
                                              11116.636666666667, 14449.97,
                                              None]})

        # check that it handles unfound metric correctly
        nodes[1].path += '.dummy'
        dictionary = self.bfc.gen_dict(nodes, responses, start, end, step)
        self.assertDictEqual(dictionary,
                             {nodes[0].path: [None, None, None, None, 4449.97,
                                              7783.303333333333,
                                              11116.636666666667, 14449.97,
                                              None]})

        # check enums
        nodes, responses = self.make_enum_data(start, step)
        dictionary = self.bfc.gen_dict(nodes, responses, start, end, step)
        self.assertDictEqual(dictionary,
                             {nodes[1].path: [None, None, None, None, 7, 5, 4,
                                              3, None],
                              nodes[0].path: [None, None, None, None, 13, 12,
                                              11, 11, None]})

        # now with submetrics
        self.bfc.enable_submetrics = True
        nodes, responses = self.make_data(start, step)
        dictionary = self.bfc.gen_dict(nodes, responses, start, end, step)
        self.assertDictEqual(dictionary,
                             {nodes[1].path: [6421.18, None, None, None, None,
                                              None, None, None, None],
                              nodes[0].path: [None, None, None, None, 4449.97,
                                              7783.303333333333,
                                              11116.636666666667, 14449.97,
                                              None]})

        # check enums with submetrics
        nodes, responses = self.make_enum_data(start, step)
        dictionary = self.bfc.gen_dict(nodes, responses, start, end, step)
        self.assertDictEqual(dictionary,
                             {nodes[1].path: [None, None, None, None, 7, 5, 4,
                                              3, None],
                              nodes[0].path: [None, None, None, None, 13, 12,
                                              11, 11, None]})

    def test_gen_responses(self):
        step = 3000
        start = 1426120000
        end = 1426147000
        groups1 = [[self.metric1, self.metric2]]
        payload = self.bfc.gen_payload(start, end, 'FULL')
        endpoint = self.bfc.get_multi_endpoint(self.finder.bf_query_endpoint,
                                               self.finder.tenant)
        # test 401 error
        with requests_mock.mock() as m:
            m.post(endpoint, json={}, status_code=401)
            responses = self.bfc.gen_responses(groups1, payload)
            self.assertSequenceEqual(responses, [])

        # test single group
        _, responses = self.make_data(start, step)
        with requests_mock.mock() as m:
            m.post(endpoint, json={'metrics': responses}, status_code=200)
            new_responses = self.bfc.gen_responses(groups1, payload)
            self.assertSequenceEqual(responses, new_responses)

        # test multiple groups
        groups2 = [[self.metric1], [self.metric2]]
        with requests_mock.mock() as m:
            global json_data
            json_data = [{'metrics': responses[:1]},
                         {'metrics': responses[1:]}]

            def json_callback(request, context):
                global json_data
                response = json_data[0]
                json_data = json_data[1:]
                return response

            m.post(endpoint, json=json_callback, status_code=200)
            new_responses = self.bfc.gen_responses(groups2, payload)
            self.assertSequenceEqual(responses, new_responses)

    def test_find_nodes(self):
        endpoint = self.finder.find_nodes_endpoint(
            self.finder.bf_query_endpoint, self.finder.tenant)
        endpoint_old = self.finder.find_metrics_endpoint(
            self.finder.bf_query_endpoint, self.finder.tenant)

        # one time through without submetrics
        self.finder.enable_submetrics = False
        with requests_mock.mock() as m:
            # test 401 errors
            query = FindQuery("*", 1, 2)
            m.get(endpoint, json=[], status_code=401)
            metrics = self.finder.find_nodes(query)
            self.assertTrue(list(metrics) == [])

        with requests_mock.mock() as m:
            query = FindQuery("*", 1, 2)
            m.get(endpoint, json=exc_callback, status_code=401)
            with self.assertRaises(ValueError):
                list(self.finder.find_nodes(query))

        def get_start(x):
            return lambda y: '.'.join(y.split('.')[:x])

        def get_path(x):
            return x.path

        def query_test(query_pattern, fg_data, search_results):
            # query_pattern is the pattern to search for
            # fg_data -> data returned by the "mainthread" call to find metrics
            # search_results are the expected results
            def json_callback(request, context):
                print("json thread callback" + threading.current_thread().name)
                return fg_data

            qlen = len(query_pattern.split("."))
            with requests_mock.mock() as m:
                query = FindQuery(query_pattern, 1, 2)
                m.get(endpoint, json=json_callback, status_code=200)
                metrics = self.finder.find_nodes(query)
                self.assertSetEqual(set(map(get_path, list(metrics))),
                                    set(map(get_start(qlen), search_results)))

        def query_test_with_submetrics(query_pattern, fg_data, search_results,
                                       bg_data=[]):
            # query_pattern is the pattern to search for
            # fg_data -> data returned by the "mainthread" call to find metrics
            # search_results are the expected results
            # bg_data - non submetric calls do 2 calls to find_metrics, (one
            # in a background thread,)
            #   bg_data simulates what gets returnd by the background thread
            def json_callback(request, context):
                print("json thread callback" + threading.current_thread().name)
                return fg_data

            qlen = len(query_pattern.split("."))
            with requests_mock.mock() as m:
                query = FindQuery(query_pattern, 1, 2)
                m.get(endpoint_old, json=json_callback, status_code=200)
                metrics = self.finder.find_nodes(query)
                self.assertSetEqual(set(map(get_path, list(metrics))),
                                    set(map(get_start(qlen), search_results)))

            enum_vals = ['v1', 'v2']

            query_test("*",
                       [{'a': False},
                        {'b': False}],
                       ['a', 'b'])

            query_test("a.*",
                       [{'a.b': False}],
                       ['a.b'])

            query_test("a.b.*",
                       [{'a.b.c': True}],
                       [self.metric1])

            query_test("a.b.c",
                       [{'a.b.c': True}],
                       [self.metric1])

            query_test("a.b.c.*",
                       [{'a.b.c.d': False},
                        {'a.b.c.v1': True},
                        {'a.b.c.v2': True}],
                       [self.metric1 + '.' + v
                        for v in enum_vals] + [self.metric1 + '.d'])

            query_test("a.b.*.v*",
                       [{'a.b.c.v': False},
                        {'a.b.c.v1': True},
                        {'a.b.c.v2': True}],
                       [self.metric1 + '.' + v
                        for v in enum_vals] + [self.metric1 + '.v.e'])

            query_test("a.b.*.v*.*",
                       [{'a.b.c.v.e': False}],
                       [self.metric1 + '.v.e.f'])

            # now again, with submetrics
            self.finder.enable_submetrics = True
            query_test_with_submetrics(
                       "*",
                       [{u'metric': self.metric1, u'unit': u'percent'},
                        {u'metric': self.metric2, u'unit': u'percent'}],
                       [self.metric1, self.metric2])

            query_test_with_submetrics(
                       "a.*",
                       [{u'metric': self.metric1, u'unit': u'percent'}],
                       [self.metric1])

            query_test_with_submetrics(
                       "a.*",
                       [{u'metric': self.metric1, u'unit': u'percent',
                         u'enum_values': enum_vals}],
                       [self.metric1])

            query_test_with_submetrics(
                       "a.b.*",
                       [{u'metric': self.metric1, u'unit': u'percent'},
                        {u'metric': 'a.bb.c', u'unit': u'percent'}],
                       [self.metric1])

            query_test_with_submetrics(
                       "a.b.c",
                       [{u'metric': self.metric1, u'unit': u'percent'}],
                       [self.metric1])

            query_test_with_submetrics(
                       "a.b.c.*",
                       [{u'metric': self.metric1, u'unit': u'percent'},
                        {u'metric': (self.metric1 + 'd'),
                         u'unit': u'percent'}],
                       [self.metric1 + '.' + k
                        for k in self.finder.submetric_aliases])

            query_test_with_submetrics(
                       "a.b.c.*",
                       [{u'metric': self.metric1, u'unit': u'percent',
                         u'enum_values': enum_vals},
                        {u'metric': (self.metric1 + 'd'),
                         u'unit': u'percent'}],
                       [self.metric1 + '.' + k
                        for k in self.finder.submetric_aliases])

            query_test_with_submetrics(
                       "a.b.c._avg",
                       [{u'metric': self.metric1, u'unit': u'percent'}],
                       [self.metric1 + '.' + self.alias_key])

            query_test_with_submetrics(
                       "a.b.c._avg",
                       [{u'metric': self.metric1, u'unit': u'percent',
                         u'enum_values': enum_vals}],
                       [self.metric1 + '.' + self.alias_key])

            query_test_with_submetrics(
                       "a.b.c.v1._enum",
                       [{u'metric': self.metric1, u'unit': u'percent',
                         u'enum_values': enum_vals}],
                       [self.metric1 + '.v1'])

            query_test_with_submetrics(
                       "a.b.c.*._enum",
                       [{u'metric': self.metric1, u'unit': u'percent',
                         u'enum_values': enum_vals}],
                       [self.metric1 + '.' + v for v in enum_vals])

            query_test_with_submetrics(
                       "a.b.*.*._enum",
                       [{u'metric': self.metric1, u'unit': u'percent',
                         u'enum_values': enum_vals}],
                       [self.metric1 + '.' + v for v in enum_vals])

            query_test_with_submetrics(
                       "a.b.c.v*._enum",
                       [{u'metric': self.metric1, u'unit': u'percent',
                         u'enum_values': enum_vals}],
                       [self.metric1 + '.' + v for v in enum_vals])

    def test_fetch(self):
        step = 3000
        start = 1426120000
        end = 1426147000
        endpoint = self.bfc.get_multi_endpoint(self.finder.bf_query_endpoint,
                                               self.finder.tenant)
        nodes, responses = self.make_data(start, step)
        with requests_mock.mock() as m:
            m.post(endpoint, json={'metrics': responses}, status_code=200)
            time_info, dictionary = self.finder.fetch_multi(nodes, start, end)
            self.assertSequenceEqual(time_info, (1426120000, 1426147300, 300))
            self.assertDictEqual(dictionary,
                                 {'e.f.g':
                                  [64211.8, 86434.02222222222,
                                   108656.24444444444, 130878.46666666666,
                                   153100.6888888889, 175322.9111111111,
                                   197545.13333333333, 219767.35555555555,
                                   241989.57777777777, 264211.8,
                                   None, None, None, None, None, None,
                                   None, None, None, None, None, None,
                                   None, None, None, None, None, None,
                                   None, None, None, None, None, None,
                                   None, None, None, None, None, None,
                                   None, None, None, None, None, None,
                                   None, None, None, None, None, None,
                                   None, None, None, None, None, None,
                                   None, None, None, None, None, None,
                                   None, None, None, None, None, None,
                                   None, None, None, None, None, None,
                                   None, None, None, None, None, None,
                                   None, None, None, None, None, None,
                                   None, None, None],
                                  'a.b.c':
                                  [None, None, None, None, None, None,
                                   None, None, None, None, None, None,
                                   None, None, None, None, None, None,
                                   None, None, None, None, None, None,
                                   None, None, None, None, None, None,
                                   None, None, None, None, None, None,
                                   None, None, None, None, None, None,
                                   None, None, None, None, None, None,
                                   None, 44499.7, 49261.60476190476,
                                   54023.509523809524, 58785.41428571429,
                                   63547.31904761905, 68309.22380952381,
                                   73071.12857142858, 77833.03333333334,
                                   82594.9380952381, 87356.84285714287,
                                   92118.74761904763, 96880.6523809524,
                                   101642.55714285716, 106404.46190476192,
                                   111166.36666666668, 115928.27142857145,
                                   120690.17619047621, 125452.08095238097,
                                   130213.98571428574, 134975.89047619049,
                                   139737.79523809525, 144499.7, None,
                                   None, None, None, None, None, None,
                                   None, None, None, None, None, None,
                                   None, None, None, None, None, None,
                                   None]})

        with requests_mock.mock() as m:
            m.post(endpoint, json=exc_callback, status_code=200)
            with self.assertRaises(ValueError):
                time_info, dictionary = self.finder.fetch_multi(nodes, start,
                                                                end)

    def test_calc_res(self):
        start = 0
        # 1 minute more than 18 weeks:
        stop1 = (60 * 60 * 24 * 7 * 18) + 60
        stop2 = stop1 - 1
        self.assertEqual(calc_res(start, stop1), 'MIN1440')
        self.assertEqual(calc_res(start, stop2), 'MIN240')

    def test_process_path(self):
        b = BluefloodClient("host", "tenant", False, None, False)
        step = 100
        big_step = step * 1000
        val_step = 12
        first_time = 1385074800000
        first_val = 48
        second_time = first_time + big_step
        second_val = first_val + val_step
        third_time = second_time + big_step
        third_val = second_val + val_step
        data_key = NonNestedDataKey(u'average')
        values = [{u'timestamp': first_time, u'average': first_val,
                   u'numPoints': 97},
                  {u'timestamp': second_time, u'average': second_val,
                   u'numPoints': 3},
                  {u'timestamp': third_time, u'average': third_val,
                   u'numPoints': 3}]

        enum_values = [
            {u'timestamp': first_time, u'enum_values': {u'v1': first_val},
             u'numPoints': 97},
            {u'timestamp': second_time, u'enum_values': {u'v1': second_val},
             u'numPoints': 3},
            {u'timestamp': third_time, u'enum_values': {u'v1': third_val},
             u'numPoints': 3}]
        enum_data_key = NestedDataKey('enum_values', 'v1')

        start_time = first_time / 1000
        end_time = third_time / 1000 + 1

        # test that start and end time exactly match the datapoints
        ret = b.process_path(values, start_time, end_time, step, data_key)
        self.assertSequenceEqual(ret, (first_val, second_val, third_val))

        ret = b.process_path(enum_values, start_time, end_time, step,
                             enum_data_key)
        self.assertSequenceEqual(ret, (first_val, second_val, third_val))

        ret = b.process_path(values, start_time, end_time, step, data_key)
        self.assertSequenceEqual(ret, (first_val, second_val, third_val))

        ret = b.process_path(enum_values, start_time, end_time, step,
                             enum_data_key)
        self.assertSequenceEqual(ret, (first_val, second_val, third_val))

        # test end time past end of data
        end_time += 2 * step
        ret = b.process_path(values, start_time, end_time, step, data_key)
        self.assertSequenceEqual(ret, (
            first_val, second_val, third_val, None, None))

        ret = b.process_path(enum_values, start_time, end_time, step,
                             enum_data_key)
        self.assertSequenceEqual(ret, (
            first_val, second_val, third_val, None, None))

        # test start time before beginning of data
        end_time -= 2 * step
        start_time -= 2 * step
        ret = b.process_path(values, start_time, end_time, step, data_key)
        self.assertSequenceEqual(ret, (
            None, None, first_val, second_val, third_val))

        ret = b.process_path(enum_values, start_time, end_time, step,
                             enum_data_key)
        self.assertSequenceEqual(ret, (
            None, None, first_val, second_val, third_val))

        # test end time before beginning of data
        end_time -= 3 * step
        start_time -= 3 * step
        ret = b.process_path(values, start_time, end_time, step, data_key)
        self.assertSequenceEqual(ret, (None, None, None, None, None))

        ret = b.process_path(enum_values, start_time, end_time, step,
                             enum_data_key)
        self.assertSequenceEqual(ret, (None, None, None, None, None))

        # test start and end outside of data and interpolation in the middle
        second_time = first_time + (3 * big_step)
        third_time = second_time + (3 * big_step)
        start_time = first_time - (2 * big_step)
        start_time /= 1000
        end_time = third_time + (2 * big_step)
        end_time = (end_time / 1000) + 1
        values = [{u'timestamp': first_time, u'average': first_val,
                   u'numPoints': 97},
                  {u'timestamp': second_time, u'average': second_val,
                   u'numPoints': 3},
                  {u'timestamp': third_time, u'average': third_val,
                   u'numPoints': 3}]

        enum_values = [
            {u'timestamp': first_time, u'enum_values': {u'v1': first_val},
             u'numPoints': 97},
            {u'timestamp': second_time, u'enum_values': {u'v1': second_val},
             u'numPoints': 3},
            {u'timestamp': third_time, u'enum_values': {u'v1': third_val},
             u'numPoints': 3}]

        ret = b.process_path(values, start_time, end_time, step, data_key)
        self.assertSequenceEqual(ret, (
            None, None, first_val, first_val + 4, first_val + 8, second_val,
            second_val + 4, second_val + 8, third_val, None, None))

        ret = b.process_path(enum_values, start_time, end_time, step,
                             enum_data_key)
        self.assertSequenceEqual(ret, (
            None, None, first_val, first_val + 4, first_val + 8, second_val,
            second_val + 4, second_val + 8, third_val, None, None))

    def test_process_path_with_statsd(self):
        b = BluefloodClient("host", "tenant", False, None, True)
        step = 100
        big_step = step * 1000
        val_step = 12
        first_time = 1385074800000
        first_val = 48
        second_val = first_val + val_step
        third_val = second_val + val_step
        data_key = NonNestedDataKey(u'average')

        # test start and end outside of data and no interpolation in the middle
        second_time = first_time + (3 * big_step)
        third_time = second_time + (3 * big_step)
        start_time = first_time - (2 * big_step)
        start_time /= 1000
        end_time = third_time + (2 * big_step)
        end_time = (end_time / 1000) + 1
        values = [{u'timestamp': first_time, u'average': first_val,
                   u'numPoints': 97},
                  {u'timestamp': second_time, u'average': second_val,
                   u'numPoints': 3},
                  {u'timestamp': third_time, u'average': third_val,
                   u'numPoints': 3}]
        ret = b.process_path(values, start_time, end_time, step, data_key)
        self.assertSequenceEqual(ret, (
            None, None, first_val, None, None, second_val,
            None, None, third_val, None, None))


if __name__ == '__main__':
    unittest.main()
