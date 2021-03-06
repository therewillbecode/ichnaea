from datetime import timedelta

from ichnaea.api.locate.result import ResultList
from ichnaea.api.locate.tests.base import BaseSourceTest
from ichnaea.api.locate.wifi import WifiPositionSource
from ichnaea.constants import (
    PERMANENT_BLOCKLIST_THRESHOLD,
    WIFI_MIN_ACCURACY,
)
from ichnaea.tests.factories import WifiShardFactory
from ichnaea import util


class TestWifi(BaseSourceTest):

    TestSource = WifiPositionSource

    def test_wifi(self):
        wifi = WifiShardFactory(radius=200)
        wifi2 = WifiShardFactory(
            lat=wifi.lat, lon=wifi.lon + 0.00001, radius=300,
            block_count=1, block_last=None)
        self.session.flush()

        query = self.model_query(wifis=[wifi, wifi2])
        result = self.source.search(query)
        self.check_model_result(
            result, wifi,
            lon=wifi.lon + 0.000005, accuracy=WIFI_MIN_ACCURACY)

    def test_wifi_no_position(self):
        wifi = WifiShardFactory()
        wifi2 = WifiShardFactory(lat=wifi.lat, lon=wifi.lon)
        wifi3 = WifiShardFactory(lat=None, lon=wifi.lon, radius=None)
        self.session.flush()

        query = self.model_query(wifis=[wifi, wifi2, wifi3])
        result = self.source.search(query)
        self.check_model_result(result, wifi)

    def test_wifi_temp_blocked(self):
        today = util.utcnow().date()
        yesterday = today - timedelta(days=1)
        wifi = WifiShardFactory(radius=200)
        wifi2 = WifiShardFactory(
            lat=wifi.lat, lon=wifi.lon + 0.00001, radius=300,
            block_count=1, block_last=yesterday)
        self.session.flush()

        query = self.model_query(wifis=[wifi, wifi2])
        result = self.source.search(query)
        self.check_model_result(result, None)

    def test_wifi_permanent_blocked(self):
        wifi = WifiShardFactory(radius=200)
        wifi2 = WifiShardFactory(
            lat=wifi.lat, lon=wifi.lon + 0.00001, radius=300,
            block_count=PERMANENT_BLOCKLIST_THRESHOLD, block_last=None)
        self.session.flush()

        query = self.model_query(wifis=[wifi, wifi2])
        result = self.source.search(query)
        self.check_model_result(result, None)

    def test_check_empty(self):
        query = self.model_query()
        result = self.source.result_type()
        self.assertFalse(self.source.should_search(query, ResultList(result)))

    def test_empty(self):
        query = self.model_query()
        with self.db_call_checker() as check_db_calls:
            result = self.source.search(query)
            self.check_model_result(result, None)
            check_db_calls(rw=0, ro=0)

    def test_few_candidates(self):
        wifis = WifiShardFactory.create_batch(2)
        self.session.flush()

        query = self.model_query(wifis=[wifis[0]])
        result = self.source.search(query)
        self.check_model_result(result, None)

    def test_few_matches(self):
        wifis = WifiShardFactory.create_batch(3)
        wifis[0].lat = None
        self.session.flush()

        query = self.model_query(wifis=wifis[:2])
        result = self.source.search(query)
        self.check_model_result(result, None)

    def test_arithmetic_similarity(self):
        wifi = WifiShardFactory(mac='00000000001f')
        wifi2 = WifiShardFactory(mac='000000000020')
        self.session.flush()

        query = self.model_query(wifis=[wifi, wifi2])
        result = self.source.search(query)
        self.check_model_result(result, None)

    def test_hamming_distance_similarity(self):
        wifi = WifiShardFactory(mac='000000000058')
        wifi2 = WifiShardFactory(mac='00000000005c')
        self.session.flush()

        query = self.model_query(wifis=[wifi, wifi2])
        result = self.source.search(query)
        self.check_model_result(result, None)

    def test_similar_many_clusters(self):
        wifi11 = WifiShardFactory(mac='00000000001f')
        wifi12 = WifiShardFactory(mac='000000000020',
                                  lat=wifi11.lat, lon=wifi11.lon)
        wifi21 = WifiShardFactory(mac='000000000058',
                                  lat=wifi11.lat + 0.00004,
                                  lon=wifi11.lon + 0.00004)
        wifi22 = WifiShardFactory(mac='00000000005c',
                                  lat=wifi21.lat, lon=wifi21.lon)
        self.session.flush()

        query = self.model_query(wifis=[wifi11, wifi12, wifi21, wifi22])
        result = self.source.search(query)
        self.check_model_result(
            result, wifi11,
            lat=wifi11.lat + 0.00002, lon=wifi11.lon + 0.00002)

    def test_similar_many_found_clusters(self):
        wifi = WifiShardFactory(mac='00000000001f')
        wifi2 = WifiShardFactory(mac='000000000024',
                                 lat=wifi.lat + 0.00004,
                                 lon=wifi.lon + 0.00004)
        other_wifi = [
            WifiShardFactory.build(mac='000000000020'),
            WifiShardFactory.build(mac='000000000021'),
            WifiShardFactory.build(mac='000000000022'),
            WifiShardFactory.build(mac='000000000023'),
        ]
        self.session.flush()

        query = self.model_query(wifis=[wifi, wifi2] + other_wifi)
        result = self.source.search(query)
        self.check_model_result(
            result, wifi,
            lat=wifi.lat + 0.00002, lon=wifi.lon + 0.00002)

    def test_ignore_outlier(self):
        wifi = WifiShardFactory()
        wifis = WifiShardFactory.create_batch(3, lat=wifi.lat, lon=wifi.lon)
        wifis[0].lat = wifi.lat + 0.0001
        wifis[1].lat = wifi.lat + 0.0002
        wifis[2].lat = wifi.lat + 1.0
        self.session.flush()

        query = self.model_query(wifis=[wifi] + wifis)
        result = self.source.search(query)
        self.check_model_result(
            result, wifi, lat=wifi.lat + 0.0001)

    def test_cluster_size_over_better_signal(self):
        wifi11 = WifiShardFactory()
        wifi12 = WifiShardFactory(lat=wifi11.lat + 0.0002, lon=wifi11.lon)
        wifi21 = WifiShardFactory(lat=wifi11.lat + 1.0, lon=wifi11.lon + 1.0)
        wifi22 = WifiShardFactory(lat=wifi21.lat + 0.0002, lon=wifi21.lon)
        self.session.flush()

        query = self.model_query(wifis=[wifi11, wifi12, wifi21, wifi22])
        query.wifi[0].signal = -100
        query.wifi[1].signal = -80
        query.wifi[2].signal = -100
        query.wifi[3].signal = -54
        result = self.source.search(query)
        self.check_model_result(
            result, wifi21, lat=wifi21.lat + 0.0001)

    def test_larger_cluster_over_signal(self):
        wifi = WifiShardFactory()
        wifis = WifiShardFactory.create_batch(
            3, lat=wifi.lat, lon=wifi.lon)
        wifis2 = WifiShardFactory.create_batch(
            3, lat=wifi.lat + 1.0, lon=wifi.lon)
        self.session.flush()

        query = self.model_query(wifis=[wifi] + wifis + wifis2)
        for entry in query.wifi[:-3]:
            entry.signal = -80
        for entry in query.wifi[-3:]:
            entry.signal = -70
        result = self.source.search(query)
        self.check_model_result(result, wifi)

    def test_top_five_in_noisy_cluster(self):
        # all these should wind up in the same cluster since
        # clustering threshold is 500m and the 10 wifis are
        # spaced in increments of (+1m, +1.2m)
        wifi = WifiShardFactory.build()
        wifis = []
        for i in range(0, 10):
            wifis.append(WifiShardFactory(lat=wifi.lat + i * 0.00001,
                                          lon=wifi.lon + i * 0.000012))

        self.session.flush()

        query = self.model_query(wifis=wifis)
        for i, entry in enumerate(query.wifi):
            entry.signal = -70 - i
        result = self.source.search(query)
        self.check_model_result(
            result, wifi,
            lat=wifi.lat + 0.00002,
            lon=wifi.lon + 0.000024)

    def test_wifi_not_closeby(self):
        wifi = WifiShardFactory()
        wifis = [
            WifiShardFactory(lat=wifi.lat + 0.00001, lon=wifi.lon),
            WifiShardFactory(lat=wifi.lat + 1.0, lon=wifi.lon),
            WifiShardFactory(lat=wifi.lat + 1.00001, lon=wifi.lon),
        ]
        self.session.flush()

        query = self.model_query(wifis=[wifi, wifis[1]])
        result = self.source.search(query)
        self.check_model_result(result, None)
