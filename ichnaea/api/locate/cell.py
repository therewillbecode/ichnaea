"""Search implementation using a cell database."""

from collections import defaultdict
import operator

import numpy
from sqlalchemy.orm import load_only

from ichnaea.api.locate.constants import DataSource
from ichnaea.api.locate.result import Position
from ichnaea.api.locate.source import PositionSource
from ichnaea.constants import (
    CELL_MIN_ACCURACY,
    LAC_MIN_ACCURACY,
)
from ichnaea.geocalc import aggregate_position
from ichnaea.models import (
    Cell,
    CellArea,
)


def pick_best_cells(cells, area_model):
    """
    Group cells by area, pick the best cell area. Either
    the one with the most values or the smallest range.
    """
    areas = defaultdict(list)
    for cell in cells:
        areas[area_model.to_hashkey(cell)].append(cell)

    def sort_areas(areas):
        return (len(areas), -min([cell.range for cell in areas]))

    areas = sorted(areas.values(), key=sort_areas, reverse=True)
    return areas[0]


def pick_best_area(areas, area_model):
    """
    Sort areas by size, pick the smallest one.
    """
    areas = sorted(areas, key=operator.attrgetter('range'))
    return areas[0]


def aggregate_cell_position(cells, result_type):
    """
    Given a list of cells from a single cell cluster,
    return the aggregate position of the user inside the cluster.
    """
    circles = numpy.array(
        [(cell.lat, cell.lon, cell.range) for cell in cells],
        dtype=numpy.double)
    lat, lon, accuracy = aggregate_position(circles, CELL_MIN_ACCURACY)
    return result_type(lat=lat, lon=lon, accuracy=accuracy)


def aggregate_area_position(area, result_type):
    """
    Given a single area, return the position of the user inside it.
    """
    accuracy = float(max(area.range, LAC_MIN_ACCURACY))
    return result_type(
        lat=area.lat, lon=area.lon, accuracy=accuracy, fallback='lacf')


def query_database(query, lookups, model, raven_client):
    """
    Given a location query and a list of lookup instances, query the
    database and return a list of model objects.
    """
    hashkeys = [lookup.hashkey() for lookup in lookups]
    if not hashkeys:  # pragma: no cover
        return []

    try:
        load_fields = ('lat', 'lon', 'range')
        model_iter = model.iterkeys(
            query.session,
            hashkeys,
            extra=lambda query: query.options(load_only(*load_fields))
                                     .filter(model.lat.isnot(None))
                                     .filter(model.lon.isnot(None)))

        return list(model_iter)
    except Exception:
        raven_client.captureException()
    return []


class CellPositionMixin(object):
    """
    A CellPositionMixin implements a position search using the cell models.
    """

    cell_model = Cell
    area_model = CellArea
    result_type = Position

    def should_search_cell(self, query, results):
        if not (query.cell or query.cell_area):
            return False
        return True

    def search_cell(self, query):
        result = self.result_type()

        if query.cell:
            cells = query_database(
                query, query.cell, self.cell_model, self.raven_client)
            if cells:
                best_cells = pick_best_cells(cells, self.area_model)
                result = aggregate_cell_position(best_cells, self.result_type)

            if not result.empty():
                return result

        if query.cell_area:
            areas = query_database(
                query, query.cell_area, self.area_model, self.raven_client)
            if areas:
                best_area = pick_best_area(areas, self.area_model)
                result = aggregate_area_position(best_area, self.result_type)

        return result


class CellPositionSource(CellPositionMixin, PositionSource):
    """
    Implements a search using our cell data.

    This source is only used in tests and as a base for the
    OCIDPositionSource.
    """

    fallback_field = None  #:
    source = DataSource.internal

    def should_search(self, query, results):
        return self.should_search_cell(query, results)

    def search(self, query):
        result = self.search_cell(query)
        query.emit_source_stats(self.source, result)
        return result
