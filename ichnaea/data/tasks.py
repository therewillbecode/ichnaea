from ichnaea.async.app import celery_app
from ichnaea.async.task import BaseTask
from ichnaea.data import area
from ichnaea.data import export
from ichnaea.data.internal import InternalUploader
from ichnaea.data.mapstat import MapStatUpdater
from ichnaea.data import monitor
from ichnaea.data import ocid
from ichnaea.data.report import ReportQueue
from ichnaea.data.score import ScoreUpdater
from ichnaea.data import station
from ichnaea.data.stats import StatCounterUpdater
from ichnaea.models import ApiKey


@celery_app.task(base=BaseTask, bind=True, queue='celery_ocid')
def export_modified_cells(self, hourly=True, _bucket=None):  # pragma: no cover
    # BBB
    ocid.CellExport(self)(hourly=True, _bucket=_bucket)


@celery_app.task(base=BaseTask, bind=True, queue='celery_ocid')
def import_latest_ocid_cells(self, diff=True):  # pragma: no cover
    # BBB
    ocid.ImportExternal(self, update_area_task=update_area)(diff=diff)


@celery_app.task(base=BaseTask, bind=True, queue='celery_monitor')
def monitor_queue_length(self):  # pragma: no cover
    # BBB
    return monitor.QueueSize(self)()


@celery_app.task(base=BaseTask, bind=True, queue='celery_ocid')
def cell_export_diff(self, _bucket=None):
    ocid.CellExport(self)(hourly=True, _bucket=_bucket)


@celery_app.task(base=BaseTask, bind=True, queue='celery_ocid')
def cell_export_full(self, _bucket=None):
    ocid.CellExport(self)(hourly=False, _bucket=_bucket)


@celery_app.task(base=BaseTask, bind=True, queue='celery_ocid')
def cell_import_external(self, diff=True):
    ocid.ImportExternal(self, update_area_task=update_area)(diff=diff)


@celery_app.task(base=BaseTask, bind=True, queue='celery_monitor')
def monitor_api_key_limits(self):
    with self.db_session(commit=False) as session:
        return monitor.ApiKeyLimits(self, session)()


@celery_app.task(base=BaseTask, bind=True, queue='celery_monitor')
def monitor_api_users(self):
    return monitor.ApiUsers(self)()


@celery_app.task(base=BaseTask, bind=True, queue='celery_monitor')
def monitor_ocid_import(self):
    with self.db_session(commit=False) as session:
        return monitor.OcidImport(self, session)()


@celery_app.task(base=BaseTask, bind=True, queue='celery_monitor')
def monitor_queue_size(self):
    return monitor.QueueSize(self)()


@celery_app.task(base=BaseTask, bind=True, queue='celery_export')
def schedule_export_reports(self):
    return export.ExportScheduler(self, None)(export_reports)


@celery_app.task(base=BaseTask, bind=True, queue='celery_export')
def export_reports(self, export_queue_name, queue_key=None):
    export.ReportExporter(
        self, None, export_queue_name, queue_key
    )(export_reports, upload_reports)


@celery_app.task(base=BaseTask, bind=True, queue='celery_incoming')
def insert_reports(self, reports=(),
                   api_key=None, email=None, ip=None, nickname=None):
    with self.redis_pipeline() as pipe:
        with self.db_session() as session:
            api_key = api_key and session.query(ApiKey).get(api_key)
            ReportQueue(
                self, session, pipe,
                api_key=api_key,
                email=email,
                ip=ip,
                nickname=nickname,
            )(reports)


@celery_app.task(base=BaseTask, bind=True, queue='celery_reports')
def queue_reports(self, reports=(),
                  api_key=None, email=None, ip=None, nickname=None):
    with self.redis_pipeline() as pipe:
        export.ExportQueue(
            self, None, pipe,
            api_key=api_key,
            email=email,
            ip=ip,
            nickname=nickname,
        )(reports)


@celery_app.task(base=BaseTask, bind=True, queue='celery_upload')
def upload_reports(self, export_queue_name, data, queue_key=None):
    uploaders = {
        'http': export.GeosubmitUploader,
        'https': export.GeosubmitUploader,
        'internal': InternalUploader,
        's3': export.S3Uploader,
    }
    export_queue = self.app.export_queues[export_queue_name]
    uploader_type = uploaders.get(export_queue.scheme, None)

    if uploader_type is not None:
        uploader_type(self, None, export_queue_name, queue_key)(data)


@celery_app.task(base=BaseTask, bind=True, queue='celery_cell')
def remove_cell(self, cell_keys):
    with self.redis_pipeline() as pipe:
        with self.db_session() as session:
            station.CellRemover(self, session, pipe)(cell_keys)


@celery_app.task(base=BaseTask, bind=True, queue='celery_cell')
def update_cell(self, batch=1000):
    with self.redis_pipeline() as pipe:
        with self.db_session() as session:
            cells, moving = station.CellUpdater(
                self, session, pipe,
                remove_task=remove_cell,
                update_task=update_cell,
            )(batch=batch)
    return (cells, moving)


@celery_app.task(base=BaseTask, bind=True, queue='celery_wifi')
def update_wifi(self, batch=1000):
    with self.redis_pipeline() as pipe:
        with self.db_session() as session:
            station.WifiUpdater(
                self, session, pipe,
                remove_task=None,
                update_task=update_wifi,
            )(batch=batch)


@celery_app.task(base=BaseTask, bind=True, queue='celery_cell')
def scan_areas(self, batch=100):
    return area.CellAreaUpdater(self, None).scan(update_area, batch=batch)


@celery_app.task(base=BaseTask, bind=True, queue='celery_cell')
def update_area(self, area_keys, cell_type='cell'):
    with self.db_session() as session:
        if cell_type == 'ocid':
            updater = area.OCIDCellAreaUpdater(self, session)
        else:
            updater = area.CellAreaUpdater(self, session)
        updater.update(area_keys)


@celery_app.task(base=BaseTask, bind=True)
def update_mapstat(self, batch=1000):
    with self.redis_pipeline() as pipe:
        with self.db_session() as session:
            MapStatUpdater(self, session, pipe)(batch=batch)


@celery_app.task(base=BaseTask, bind=True)
def update_score(self, batch=1000):
    with self.redis_pipeline() as pipe:
        with self.db_session() as session:
            ScoreUpdater(self, session, pipe)(batch=batch)


@celery_app.task(base=BaseTask, bind=True)
def update_statcounter(self, ago=1):
    with self.redis_pipeline() as pipe:
        with self.db_session() as session:
            StatCounterUpdater(self, session, pipe)(ago=ago)
