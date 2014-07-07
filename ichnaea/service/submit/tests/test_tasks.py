import base64
from datetime import (
    datetime,
    timedelta,
)
import json
import zlib

import pytz
from sqlalchemy.exc import ProgrammingError
from sqlalchemy import text

from ichnaea.content.models import (
    Score,
    SCORE_TYPE,
)
from ichnaea.heka_logging import RAVEN_ERROR
from ichnaea.models import (
    encode_datetime,
    Cell,
    CellBlacklist,
    CellMeasure,
    from_degrees,
    PERMANENT_BLACKLIST_THRESHOLD,
    RADIO_TYPE,
    Wifi,
    WifiBlacklist,
    WifiMeasure,
)
from ichnaea.service.submit.tasks import (
    insert_cell_measures,
    insert_wifi_measures,
)
from ichnaea.tasks import (
    cell_location_update,
    wifi_location_update,
)
from ichnaea.tests.base import (
    CeleryTestCase,
    PARIS_LAT, PARIS_LON, FRANCE_MCC,
    USA_MCC, ATT_MNC,
)


class TestInsert(CeleryTestCase):

    def test_cell(self):
        session = self.db_master_session
        time = datetime.utcnow().replace(microsecond=0) - timedelta(days=1)
        mcc = FRANCE_MCC

        session.add(Cell(radio=RADIO_TYPE['gsm'], mcc=mcc, mnc=2, lac=3,
                         cid=4, psc=5, new_measures=2,
                         total_measures=5))
        session.add(Score(userid=1, key=SCORE_TYPE['new_cell'], value=7))
        session.flush()

        measure = dict(
            id=0, created=encode_datetime(time),
            lat=from_degrees(PARIS_LAT),
            lon=from_degrees(PARIS_LON),
            time=encode_datetime(time), accuracy=0, altitude=0,
            altitude_accuracy=0, radio=RADIO_TYPE['gsm'],
        )
        entries = [
            # Note that this first entry will be skipped as it does
            # not include (lac, cid) or (psc)
            {"mcc": mcc, "mnc": 2, "signal": -100},

            {"mcc": mcc, "mnc": 2, "lac": 3, "cid": 4, "psc": 5, "asu": 8},
            {"mcc": mcc, "mnc": 2, "lac": 3, "cid": 4, "psc": 5, "asu": 8},
            {"mcc": mcc, "mnc": 2, "lac": 3, "cid": 4, "psc": 5, "asu": 15},
            {"mcc": mcc, "mnc": 2, "lac": 3, "cid": 7, "psc": 5},
        ]
        for e in entries:
            e.update(measure)

        result = insert_cell_measures.delay(entries, userid=1)

        self.assertEqual(result.get(), 4)
        measures = session.query(CellMeasure).all()
        self.assertEqual(len(measures), 4)
        self.assertEqual(set([m.mcc for m in measures]), set([mcc]))
        self.assertEqual(set([m.mnc for m in measures]), set([2]))
        self.assertEqual(set([m.asu for m in measures]), set([-1, 8, 15]))
        self.assertEqual(set([m.psc for m in measures]), set([5]))
        self.assertEqual(set([m.signal for m in measures]), set([0]))

        cells = session.query(Cell).all()
        self.assertEqual(len(cells), 2)
        self.assertEqual(set([c.mcc for c in cells]), set([mcc]))
        self.assertEqual(set([c.mnc for c in cells]), set([2]))
        self.assertEqual(set([c.lac for c in cells]), set([3]))
        self.assertEqual(set([c.cid for c in cells]), set([4, 7]))
        self.assertEqual(set([c.psc for c in cells]), set([5]))
        self.assertEqual(set([c.new_measures for c in cells]), set([1, 5]))
        self.assertEqual(set([c.total_measures for c in cells]), set([1, 8]))

        scores = session.query(Score).all()
        self.assertEqual(len(scores), 1)
        self.assertEqual(scores[0].key, SCORE_TYPE['new_cell'])
        self.assertEqual(scores[0].value, 8)

        # test duplicate execution
        result = insert_cell_measures.delay(entries, userid=1)
        self.assertEqual(result.get(), 4)
        # TODO this task isn't idempotent yet
        measures = session.query(CellMeasure).all()
        self.assertEqual(len(measures), 8)

    def test_insert_invalid_lac(self):
        session = self.db_master_session
        time = datetime.utcnow().replace(microsecond=0) - timedelta(days=1)

        session.add(Cell(radio=RADIO_TYPE['gsm'], mcc=FRANCE_MCC, mnc=2,
                         lac=3, cid=4, new_measures=2, total_measures=5))
        session.add(Score(userid=1, key=SCORE_TYPE['new_cell'], value=7))
        session.flush()

        measure = dict(
            id=0, created=encode_datetime(time),
            lat=from_degrees(PARIS_LAT),
            lon=from_degrees(PARIS_LON),
            time=encode_datetime(time), accuracy=0, altitude=0,
            altitude_accuracy=0, radio=RADIO_TYPE['gsm'])
        entries = [
            {"mcc": FRANCE_MCC, "mnc": 2, "lac": 3147483647, "cid": 2147483647,
             "psc": 5, "asu": 8},
            {"mcc": FRANCE_MCC, "mnc": 2, "lac": -1, "cid": -1,
             "psc": 5, "asu": 8},
        ]
        for e in entries:
            e.update(measure)

        result = insert_cell_measures.delay(entries, userid=1)
        self.assertEqual(result.get(), 2)

        measures = session.query(CellMeasure).all()
        self.assertEqual(len(measures), 2)
        self.assertEqual(set([m.lac for m in measures]), set([-1]))
        self.assertEqual(set([m.cid for m in measures]), set([-1]))

        # Nothing should change in the initially created Cell record
        cells = session.query(Cell).all()
        self.assertEqual(len(cells), 1)
        self.assertEqual(set([c.new_measures for c in cells]), set([2]))
        self.assertEqual(set([c.total_measures for c in cells]), set([5]))

    def test_cell_out_of_range_values(self):
        session = self.db_master_session
        time = datetime.utcnow().replace(microsecond=0) - timedelta(days=1)

        measure = dict(
            id=0, created=encode_datetime(time),
            lat=from_degrees(PARIS_LAT),
            lon=from_degrees(PARIS_LON),
            time=encode_datetime(time), accuracy=0, altitude=0,
            altitude_accuracy=0, radio=RADIO_TYPE['gsm'], mcc=FRANCE_MCC,
            mnc=2, lac=3, cid=4)
        entries = [
            {"asu": 8, "signal": -70, "ta": 32},
            {"asu": -10, "signal": -300, "ta": -10},
            {"asu": 256, "signal": 16, "ta": 128},
        ]
        for e in entries:
            e.update(measure)

        result = insert_cell_measures.delay(entries)
        self.assertEqual(result.get(), 3)

        measures = session.query(CellMeasure).all()
        self.assertEqual(len(measures), 3)
        self.assertEqual(set([m.asu for m in measures]), set([-1, 8]))
        self.assertEqual(set([m.signal for m in measures]), set([0, -70]))
        self.assertEqual(set([m.ta for m in measures]), set([0, 32]))

    def test_wifi(self):
        session = self.db_master_session
        time = datetime.utcnow().replace(microsecond=0) - timedelta(days=1)

        session.add(Wifi(key="ab1234567890"))
        session.add(Score(userid=1, key=SCORE_TYPE['new_wifi'], value=7))
        session.flush()

        measure = dict(
            id=0, created=encode_datetime(time), lat=1, lon=2,
            time=encode_datetime(time), accuracy=0, altitude=0,
            altitude_accuracy=0, radio=-1,
            heading=52.9,
            speed=158.5,
        )
        entries = [
            {"key": "ab1234567890", "channel": 11, "signal": -80},
            {"key": "ab1234567890", "channel": 3, "signal": -90},
            {"key": "ab1234567890", "channel": 3, "signal": -80},
            {"key": "cd3456789012", "channel": 3, "signal": -90},
        ]
        for e in entries:
            e.update(measure)
        result = insert_wifi_measures.delay(entries, userid=1)
        self.assertEqual(result.get(), 4)

        measures = session.query(WifiMeasure).all()
        self.assertEqual(len(measures), 4)
        self.assertEqual(set([m.key for m in measures]), set(["ab1234567890",
                                                              "cd3456789012"]))
        self.assertEqual(set([m.channel for m in measures]), set([3, 11]))
        self.assertEqual(set([m.signal for m in measures]), set([-80, -90]))
        self.assertEqual(set([m.heading or m in measures]), set([52.9]))
        self.assertEqual(set([m.speed or m in measures]), set([158.5]))

        wifis = session.query(Wifi).all()
        self.assertEqual(len(wifis), 2)
        self.assertEqual(set([w.key for w in wifis]), set(["ab1234567890",
                                                           "cd3456789012"]))
        self.assertEqual(set([w.new_measures for w in wifis]), set([1, 3]))
        self.assertEqual(set([w.total_measures for w in wifis]), set([1, 3]))

        scores = session.query(Score).all()
        self.assertEqual(len(scores), 1)
        self.assertEqual(scores[0].key, SCORE_TYPE['new_wifi'])
        self.assertEqual(scores[0].value, 8)

        # test duplicate execution
        result = insert_wifi_measures.delay(entries, userid=1)
        self.assertEqual(result.get(), 4)
        # TODO this task isn't idempotent yet
        measures = session.query(WifiMeasure).all()
        self.assertEqual(len(measures), 8)

    def test_wifi_blacklist(self):
        session = self.db_master_session
        bad_key = "ab1234567890"
        good_key = "cd1234567890"
        black = WifiBlacklist(key=bad_key)
        session.add(black)
        session.flush()
        measure = dict(id=0, lat=1, lon=2)
        entries = [{"key": good_key}, {"key": good_key}, {"key": bad_key}]
        for e in entries:
            e.update(measure)

        result = insert_wifi_measures.delay(entries)
        self.assertEqual(result.get(), 2)

        measures = session.query(WifiMeasure).all()
        self.assertEqual(len(measures), 2)
        self.assertEqual(
            set([m.key for m in measures]), set([good_key]))

        wifis = session.query(Wifi).all()
        self.assertEqual(len(wifis), 1)
        self.assertEqual(set([w.key for w in wifis]), set([good_key]))

    def test_wifi_blacklist_temporary_and_permanent(self):
        session = self.db_master_session

        # This test simulates a wifi that moves once a month, for 2 years.
        # The first 2 * PERMANENT_BLACKLIST_THRESHOLD (12) moves should be
        # temporary, forgotten after a week; after that it should be
        # permanently blacklisted.

        now = datetime.utcnow().replace(tzinfo=pytz.UTC)
        # Station moves between these 4 points, all in the USA:
        points = [
            # NYC
            (from_degrees(40), from_degrees(-74)),
            # SF
            (from_degrees(37), from_degrees(-122)),
            # Seattle
            (from_degrees(47), from_degrees(-122)),
            # Miami
            (from_degrees(25), from_degrees(-80)),
        ]

        N = 4 * PERMANENT_BLACKLIST_THRESHOLD
        for month in range(0, N):
            days_ago = (N - (month + 1)) * 30
            time = now - timedelta(days=days_ago)
            time_enc = encode_datetime(time)

            measure = dict(id=month, key="ab1234567890",
                           time=time_enc,
                           lat=points[month % 4][0],
                           lon=points[month % 4][1])

            # insert_result is num-accepted-measures, override
            # utcnow to set creation date
            insert_result = insert_wifi_measures.delay(
                [measure], utcnow=time_enc)

            # update_result is (num-stations, num-moving-stations)
            update_result = wifi_location_update.delay(min_new=1)

            # Assuming PERMANENT_BLACKLIST_THRESHOLD == 6:
            #
            # 0th insert will create the station
            # 1st insert will create first blacklist entry, delete station
            # 2nd insert will recreate the station at new position
            # 3rd insert will update blacklist, re-delete station
            # 4th insert will recreate the station at new position
            # 5th insert will update blacklist, re-delete station
            # 6th insert will recreate the station at new position
            # ...
            # 11th insert will make blacklisting permanent, re-delete station
            # 12th insert will not recreate station
            # 13th insert will not recreate station
            # ...
            # 23rd insert will not recreate station

            bl = session.query(WifiBlacklist).all()
            if month == 0:
                self.assertEqual(len(bl), 0)
            else:
                self.assertEqual(len(bl), 1)
                # force the blacklist back in time to whenever the
                # measure was supposedly inserted.
                bl = bl[0]
                bl.time = time
                session.add(bl)
                session.commit()

            if month < N / 2:
                # We still haven't exceeded the threshold, so the
                # measurement was admitted.
                self.assertEqual(insert_result.get(), 1)
                self.assertEqual(session.query(WifiMeasure).count(), month + 1)
                if month % 2 == 0:
                    # The station was (re)created.
                    self.assertEqual(update_result.get(), (1, 0))
                    # One wifi record should exist.
                    self.assertEqual(session.query(Wifi).count(), 1)
                else:
                    # The station existed and was seen moving,
                    # thereby activating the blacklist.
                    self.assertEqual(update_result.get(), (1, 1))
                    self.assertEqual(bl.count, ((month + 1) / 2))
                    self.assertEqual(session.query(WifiBlacklist).count(), 1)
                    self.assertEqual(session.query(Wifi).count(), 0)

                    # Try adding one more measurement 1 day later
                    # to be sure it is dropped by the now-active blacklist.
                    next_day = encode_datetime(time + timedelta(days=1))
                    measure['time'] = next_day
                    self.assertEqual(
                        0, insert_wifi_measures.delay([measure],
                                                      utcnow=next_day).get())

            else:
                # Blacklist has exceeded threshold, gone to "permanent" mode,
                # so no measures accepted, no stations seen.
                self.assertEqual(insert_result.get(), 0)
                self.assertEqual(update_result.get(), 0)

    def test_wifi_overflow(self):
        session = self.db_master_session
        key = "001234567890"

        measures = [dict(id=0,
                         key=key,
                         lat=1 + i * 0.0000001,
                         lon=2 + i * 0.0000001) for i in range(3)]

        result = insert_wifi_measures.delay(measures)
        self.assertEqual(result.get(), 3)

        result = insert_wifi_measures.delay(measures, max_measures_per_wifi=3)
        self.assertEqual(result.get(), 0)

        result = insert_wifi_measures.delay(measures, max_measures_per_wifi=10)
        self.assertEqual(result.get(), 3)

        result = insert_wifi_measures.delay(measures, max_measures_per_wifi=3)
        self.assertEqual(result.get(), 0)

        measures = session.query(WifiMeasure).all()
        self.assertEqual(len(measures), 6)

        wifis = session.query(Wifi).all()
        self.assertEqual(len(wifis), 1)
        self.assertEqual(wifis[0].total_measures, 6)

    def test_cell_blacklist(self):
        session = self.db_master_session

        measures = [dict(mcc=FRANCE_MCC, mnc=2, lac=3, cid=i, psc=5,
                         radio=RADIO_TYPE['gsm'],
                         id=0,
                         lat=from_degrees(PARIS_LAT) + i,
                         lon=from_degrees(PARIS_LON) + i) for i in range(1, 4)]

        black = CellBlacklist(
            mcc=FRANCE_MCC, mnc=2, lac=3, cid=1,
            radio=RADIO_TYPE['gsm'],
        )
        session.add(black)
        session.flush()

        result = insert_cell_measures.delay(measures)
        self.assertEqual(result.get(), 2)

        measures = session.query(CellMeasure).all()
        self.assertEqual(len(measures), 2)

        cells = session.query(Cell).all()
        self.assertEqual(len(cells), 2)

    def test_cell_blacklist_temporary_and_permanent(self):
        session = self.db_master_session

        # This test simulates a cell that moves once a month, for 2 years.
        # The first 2 * PERMANENT_BLACKLIST_THRESHOLD (12) moves should be
        # temporary, forgotten after a week; after that it should be
        # permanently blacklisted.

        now = datetime.utcnow().replace(tzinfo=pytz.UTC)
        # Station moves between these 4 points, all in the USA:
        points = [
            # NYC
            (from_degrees(40), from_degrees(-74)),
            # SF
            (from_degrees(37), from_degrees(-122)),
            # Seattle
            (from_degrees(47), from_degrees(-122)),
            # Miami
            (from_degrees(25), from_degrees(-80)),
        ]

        N = 4 * PERMANENT_BLACKLIST_THRESHOLD
        for month in range(0, N):
            days_ago = (N - (month + 1)) * 30
            time = now - timedelta(days=days_ago)
            time_enc = encode_datetime(time)

            measure = dict(id=month, radio=RADIO_TYPE['gsm'],
                           mcc=USA_MCC, mnc=ATT_MNC, lac=456, cid=123,
                           time=time_enc,
                           lat=points[month % 4][0],
                           lon=points[month % 4][1])

            # insert_result is num-accepted-measures, override
            # utcnow to set creation date
            insert_result = insert_cell_measures.delay(
                [measure], utcnow=time_enc)

            # update_result is (num-stations, num-moving-stations)
            update_result = cell_location_update.delay(min_new=1)

            # Assuming PERMANENT_BLACKLIST_THRESHOLD == 6:
            #
            # 0th insert will create the station
            # 1st insert will create first blacklist entry, delete station
            # 2nd insert will recreate the station at new position
            # 3rd insert will update blacklist, re-delete station
            # 4th insert will recreate the station at new position
            # 5th insert will update blacklist, re-delete station
            # 6th insert will recreate the station at new position
            # ...
            # 11th insert will make blacklisting permanent, re-delete station
            # 12th insert will not recreate station
            # 13th insert will not recreate station
            # ...
            # 23rd insert will not recreate station

            bl = session.query(CellBlacklist).all()
            if month == 0:
                self.assertEqual(len(bl), 0)
            else:
                self.assertEqual(len(bl), 1)
                # force the blacklist back in time to whenever the
                # measure was supposedly inserted.
                bl = bl[0]
                bl.time = time
                session.add(bl)
                session.commit()

            if month < N / 2:
                # We still haven't exceeded the threshold, so the
                # measurement was admitted.
                self.assertEqual(insert_result.get(), 1)
                self.assertEqual(session.query(CellMeasure).count(), month + 1)
                if month % 2 == 0:
                    # The station was (re)created.
                    self.assertEqual(update_result.get(), (1, 0))
                    # One cell + one cell-LAC record should exist.
                    self.assertEqual(session.query(Cell).count(), 2)
                else:
                    # The station existed and was seen moving,
                    # thereby activating the blacklist and deleting the cell.
                    self.assertEqual(update_result.get(), (1, 1))
                    self.assertEqual(bl.count, ((month + 1) / 2))
                    self.assertEqual(session.query(CellBlacklist).count(), 1)
                    self.assertEqual(session.query(Cell).count(), 0)

                    # Try adding one more measurement 1 day later
                    # to be sure it is dropped by the now-active blacklist.
                    next_day = encode_datetime(time + timedelta(days=1))
                    measure['time'] = next_day
                    self.assertEqual(
                        0, insert_cell_measures.delay([measure],
                                                      utcnow=next_day).get())

            else:
                # Blacklist has exceeded threshold, gone to "permanent" mode,
                # so no measures accepted, no stations seen.
                self.assertEqual(insert_result.get(), 0)
                self.assertEqual(update_result.get(), 0)

    def test_cell_overflow(self):
        session = self.db_master_session

        measures = [dict(mcc=FRANCE_MCC, mnc=2, lac=3, cid=4, psc=5,
                         radio=RADIO_TYPE['gsm'],
                         id=0,
                         lat=from_degrees(PARIS_LAT) + i,
                         lon=from_degrees(PARIS_LON) + i) for i in range(3)]

        result = insert_cell_measures.delay(measures)
        self.assertEqual(result.get(), 3)

        result = insert_cell_measures.delay(measures, max_measures_per_cell=3)
        self.assertEqual(result.get(), 0)

        result = insert_cell_measures.delay(measures, max_measures_per_cell=10)
        self.assertEqual(result.get(), 3)

        result = insert_cell_measures.delay(measures, max_measures_per_cell=3)
        self.assertEqual(result.get(), 0)

        measures = session.query(CellMeasure).all()
        self.assertEqual(len(measures), 6)

        cells = session.query(Cell).all()
        self.assertEqual(len(cells), 1)
        self.assertEqual(cells[0].total_measures, 6)

    def test_ignore_unhelpful_incomplete_cdma_cells(self):
        # CDMA cell records must have MNC, MCC, LAC and CID filled in
        session = self.db_master_session
        time = datetime.utcnow().replace(microsecond=0) - timedelta(days=1)

        measure = dict(
            id=0, created=encode_datetime(time), lat=from_degrees(PARIS_LAT),
            lon=from_degrees(PARIS_LON), time=encode_datetime(time),
            accuracy=0, altitude=0, altitude_accuracy=0,
            radio=RADIO_TYPE['cdma'],
        )
        entries = [
            # This records is valid
            {"mcc": FRANCE_MCC, "mnc": 2, "lac": 3, "cid": 4},

            # This record should fail as it's missing CID
            {"mcc": FRANCE_MCC, "mnc": 2, "lac": 3},

            # This fails for missing lac
            {"mcc": FRANCE_MCC, "mnc": 2, "cid": 4},

            # Adding a psc doesn't change things
            {"mcc": FRANCE_MCC, "mnc": 2, "psc": 5},
        ]

        for e in entries:
            e.update(measure)
        result = insert_cell_measures.delay(entries, userid=1)

        self.assertEqual(result.get(), 1)
        measures = session.query(CellMeasure).all()
        self.assertEqual(len(measures), 1)
        cells = session.query(Cell).all()
        self.assertEqual(len(cells), 1)

    def test_ignore_unhelpful_incomplete_cells(self):
        # Cell records must have MNC, MCC and at least one of (LAC, CID) or PSC
        # values filled in.
        session = self.db_master_session
        time = datetime.utcnow().replace(microsecond=0) - timedelta(days=1)

        measure = dict(
            id=0, created=encode_datetime(time),
            lat=from_degrees(PARIS_LAT),
            lon=from_degrees(PARIS_LON),
            time=encode_datetime(time), accuracy=0, altitude=0,
            altitude_accuracy=0, radio=RADIO_TYPE['gsm'],
        )
        entries = [
            # These records are valid
            {"mcc": FRANCE_MCC, "mnc": 2, "lac": 3, "cid": 4},
            {"mcc": FRANCE_MCC, "mnc": 2, "lac": 3, "cid": 4, "psc": 5},

            # This record is missing everything
            {},

            # These records fail the mcc check
            {"mnc": 2, "lac": 3, "cid": 4},
            {"mcc": 0, "mnc": 2, "lac": 3, "cid": 4},
            {"mcc": -1, "mnc": 2, "lac": 3, "cid": 4},
            {"mcc": -2, "mnc": 2, "lac": 3, "cid": 4},
            {"mcc": 2000, "mnc": 2, "lac": 3, "cid": 4},

            # These records fail the mnc check
            {"mcc": FRANCE_MCC, "lac": 3, "cid": 4},
            {"mcc": FRANCE_MCC, "mnc": -1, "lac": 3, "cid": 4},
            {"mcc": FRANCE_MCC, "mnc": -2, "lac": 3, "cid": 4},
            {"mcc": FRANCE_MCC, "mnc": 33000, "lac": 3, "cid": 4},

            # These records fail the lac check
            {"mcc": FRANCE_MCC, "mnc": 2, "cid": 4},
            {"mcc": FRANCE_MCC, "mnc": 2, "lac": -1, "cid": 4},
            {"mcc": FRANCE_MCC, "mnc": 2, "lac": -2, "cid": 4},
            {"mcc": FRANCE_MCC, "mnc": 2, "lac": 65536, "cid": 4},

            # These records fail the cid check
            {"mcc": FRANCE_MCC, "mnc": 2, "lac": 3},
            {"mcc": FRANCE_MCC, "mnc": 2, "lac": 3, "cid": -1},
            {"mcc": FRANCE_MCC, "mnc": 2, "lac": 3, "cid": -2},
            {"mcc": FRANCE_MCC, "mnc": 2, "lac": 3, "cid": 2 ** 28},

            # These records fail the (lac or cid) and psc check
            {"mcc": FRANCE_MCC, "mnc": 2},
            {"mcc": FRANCE_MCC, "mnc": 2, "lac": 3},
            {"mcc": FRANCE_MCC, "mnc": 2, "cid": 4},

            # This fails the check for (unknown lac, cid=65535)
            # and subsequently the check for missing psc
            {"mcc": FRANCE_MCC, "mnc": 2, "lac": 0, "cid": 65535},
        ]

        for e in entries:
            e.update(measure)
        result = insert_cell_measures.delay(entries, userid=1)

        self.assertEqual(result.get(), 2)
        measures = session.query(CellMeasure).all()
        self.assertEqual(len(measures), 2)
        cells = session.query(Cell).all()
        self.assertEqual(len(cells), 1)

        entries = [
            # These records are valid
            {"mcc": FRANCE_MCC, "mnc": 2, "psc": 5},
            {"mcc": FRANCE_MCC, "mnc": 2, "lac": 3, "psc": 5},
            {"mcc": FRANCE_MCC, "mnc": 2, "cid": 4, "psc": 5},
        ]
        for e in entries:
            e.update(measure)
        result = insert_cell_measures.delay(entries, userid=1)

        self.assertEqual(result.get(), 3)
        measures = session.query(CellMeasure).all()
        self.assertEqual(len(measures), 5)
        cells = session.query(Cell).all()
        self.assertEqual(len(cells), 1)


class TestSubmitErrors(CeleryTestCase):
    # this is a standalone class to ensure DB isolation for dropping tables

    def tearDown(self):
        self.setup_tables(self.db_master.engine)
        super(TestSubmitErrors, self).tearDown()

    def test_database_error(self):
        session = self.db_master_session

        stmt = text("drop table wifi;")
        session.execute(stmt)

        entries = [
            {"lat": 10000000, "lon": 20000000,
             "key": "ab:12:34:56:78:90", "channel": 11},
            {"lat": 10000000, "lon": 20000000,
             "key": "ab:12:34:56:78:90", "channel": 3},
            {"lat": 10000000, "lon": 20000000,
             "key": "ab:12:34:56:78:90", "channel": 3},
            {"lat": 10000000, "lon": 20000000,
             "key": "cd:12:34:56:78:90", "channel": 3},
        ]

        try:
            insert_wifi_measures.delay(entries)
        except ProgrammingError:
            pass
        except Exception as exc:
            self.fail("Unexpected exception caught: %s" % repr(exc))

        find_msg = self.find_heka_messages
        messages = find_msg('sentry', RAVEN_ERROR, field_name='msg')
        self.assertEquals(len(messages), 1)

        payload = messages[0].payload
        # duplicate raven.base.RavenClient.decode
        data = json.loads(zlib.decompress(base64.b64decode(payload)))
        sentry_exc = data['sentry.interfaces.Exception']

        self.assertEqual(sentry_exc['module'], ProgrammingError.__module__)
        self.assertEqual(sentry_exc['type'], 'ProgrammingError')
