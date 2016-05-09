#!/usr/bin/env python
# -*- coding: utf-8 -*-

#
# Copyright (C) 2015 Glencoe Software, Inc. All Rights Reserved.
# Use is subject to license terms supplied in LICENSE.txt
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

"""
   Test of the Tables service with the populate_metadata.py
   and populate_roi.py scripts.
"""

from omero.testlib import ITest
import string
import csv
import os.path
import re

from omero.api import RoiOptions
from omero.grid import ImageColumn
from omero.grid import RoiColumn
from omero.grid import StringColumn
from omero.model import PlateI, WellI, WellSampleI, OriginalFileI
from omero.model import FileAnnotationI, MapAnnotationI, PlateAnnotationLinkI
from omero.model import RoiAnnotationLinkI
from omero.model import RoiI, PointI
from omero.rtypes import rdouble, rint, rstring, unwrap
from omero.sys import ParametersI

from omero.util.populate_metadata import (
    ParsingContext, BulkToMapAnnotationContext, DeleteMapAnnotationContext)
from omero.util.populate_roi import AbstractMeasurementCtx
from omero.util.populate_roi import AbstractPlateAnalysisCtx
from omero.util.populate_roi import MeasurementParsingResult
from omero.util.populate_roi import PlateAnalysisCtxFactory
from omero.constants.namespaces import NSBULKANNOTATIONS
from omero.constants.namespaces import NSMEASUREMENT
from omero.util.temp_files import create_path

from pytest import skip
from pytest import mark


def coord2offset(coord):
    """
    Convert a coordinate of the form AB12 into 0-based row-column indices

    TODO: This should go into a utils file somewhere
    """
    ALPHA = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
    m = re.match('([A-Z]+)([0-9]+)$', coord.upper())
    assert m
    ra, ca = m.groups()
    r = 0
    for a in ra:
        r = r * 26 + ALPHA.find(a) + 1
    c = int(ca)
    return r - 1, c - 1


class BasePopulate(ITest):

    def createCsv(
        self,
        colNames="Well,Well Type,Concentration",
        rowData=("A1,Control,0", "A2,Treatment,10")
    ):

        csvFileName = create_path("test", ".csv")
        csvFile = open(csvFileName, 'w')
        try:
            csvFile.write(colNames)
            csvFile.write("\n")
            csvFile.write("\n".join(rowData))
        finally:
            csvFile.close()
        return str(csvFileName)

    def createDataset(self, names=("A1", "A2")):
        q = self.client.sf.getQueryService()
        up = self.client.sf.getUpdateService()
        ds = self.make_dataset()
        for name in names:
            img = self.importSingleImage(name=name)
            # Name must match exactly. No ".fake"
            img = q.get("Image", img.id.val)
            img.setName(rstring(name))
            img = up.saveAndReturnObject(img)
            self.link(ds, img)
        return ds.proxy()

    def createPlate(self, rowCount, colCount):
        plates = self.import_plates(plate_rows=rowCount,
                                    plate_cols=colCount)
        return plates[0]

    def createPlate1(self, rowCount, colCount):
        uuid = self.ctx.sessionUuid

        def createWell(row, column):
            well = WellI()
            well.row = rint(row)
            well.column = rint(column)
            ws = WellSampleI()
            image = self.new_image(name=uuid)
            ws.image = image
            well.addWellSample(ws)
            return well

        plate = PlateI()
        plate.name = rstring("TestPopulateMetadata%s" % uuid)
        for row in range(rowCount):
            for col in range(colCount):
                well = createWell(row, col)
                plate.addWell(well)
        return self.client.sf.getUpdateService().saveAndReturnObject(plate)


class TestPopulateMetadata(BasePopulate):

    def setup_method(self, method):
        self.plate_csv = self.createCsv()
        self.dataset_csv = self.createCsv(
            colNames="Image Name,Type,Concentration",
        )
        self.rowCount = 1
        self.colCount = 2
        self.plate = None
        self.dataset = None
        self.images = []

    def get_plate(self):
        if not self.plate:
            self.plate = self.createPlate(self.rowCount, self.colCount)
        return self.plate

    def get_dataset(self):
        if not self.dataset:
            self.dataset = self.createDataset()
            self.images = self.get_dataset_images()
        return self.dataset

    def get_dataset_images(self):
        if not self.dataset:
            return []
        query = """select i from Image i
            left outer join fetch i.datasetLinks links
            left outer join fetch links.parent d
            where d.id=%s""" % self.dataset.id.val
        qs = self.client.sf.getQueryService()
        return qs.findAllByQuery(query, None)

    def get_plate_annotations(self):
        query = """select p from Plate p
            left outer join fetch p.annotationLinks links
            left outer join fetch links.child
            where p.id=%s""" % self.plate.id.val
        qs = self.client.sf.getQueryService()
        plate = qs.findByQuery(query, None)
        anns = plate.linkedAnnotationList()
        return anns

    def get_dataset_annotations(self):
        query = """select d from Dataset d
            left outer join fetch d.annotationLinks links
            left outer join fetch links.child
            where d.id=%s""" % self.dataset.id.val
        qs = self.client.sf.getQueryService()
        ds = qs.findByQuery(query, None)
        anns = ds.linkedAnnotationList()
        return anns

    def get_image_annotations(self):
        if not self.images:
            return []
        params = ParametersI()
        params.addIds([x.id for x in self.images])
        query = """select a from Image i
            left outer join i.annotationLinks links
            left outer join links.child as a
            where i.id in (:ids) and a <> null"""
        qs = self.client.sf.getQueryService()
        return qs.findAllByQuery(query, params)

    def get_well_annotations(self):
        if not self.plate:
            return []
        query = """
            SELECT wal.child,wal.parent.id,wal.parent.row,wal.parent.column
            FROM WellAnnotationLink wal
            WHERE wal.parent.plate.id=%d""" % self.plate.id.val
        qs = self.client.sf.getQueryService()
        was = unwrap(qs.projection(query, None))
        return was

    METADATA_TESTS = (
        ("plate", 4, 2),
        ("dataset", 4, 2)
    )
    METADATA_IDS = [x[0] for x in METADATA_TESTS]

    @mark.parametrize("data", METADATA_TESTS, ids=METADATA_IDS)
    def testPopulateMetadata(self, data):
        """
        We should really test each of the parsing contexts in separate tests
        but in practice each one uses data created by the others, so for
        now just run them all together
        """
        try:
            import yaml
            print yaml, "found"
        except Exception:
            skip("PyYAML not installed.")
        type, count, anns = data
        self._test_parsing_context(type, count)
        self._test_bulk_to_map_annotation_context(type, anns)
        self._test_delete_map_annotation_context(type, anns)

    def _test_parsing_context(self, type, count):
        """
            Create a small csv file, use populate_metadata.py to parse and
            attach to Plate. Then query to check table has expected content.
        """

        target = getattr(self, "get_%s" % type)()
        csv = getattr(self, "%s_csv" % type)
        ctx = ParsingContext(self.client, target, file=csv)
        ctx.parse()
        ctx.write_to_omero()

        # Get file annotations
        anns = getattr(self, "get_%s_annotations" % type)()
        # Only expect a single annotation which is a 'bulk annotation'
        assert len(anns) == 1
        tableFileAnn = anns[0]
        assert unwrap(tableFileAnn.getNs()) == NSBULKANNOTATIONS
        fileid = tableFileAnn.file.id.val

        # Open table to check contents
        r = self.client.sf.sharedResources()
        t = r.openTable(OriginalFileI(fileid), None)
        cols = t.getHeaders()
        rows = t.getNumberOfRows()
        assert rows == self.rowCount * self.colCount
        for hit in range(rows):
            rowValues = [col.values[0] for col in t.read(range(len(cols)),
                                                         hit, hit+1).columns]
            assert len(rowValues) == count
            # Unsure where the lower-casing is happening
            if "A1" in rowValues or "a1" in rowValues:
                assert "Control" in rowValues
            elif "A2" in rowValues or "a2" in rowValues:
                assert "Treatment" in rowValues
            else:
                assert False, \
                    "Row does not contain 'a1' or 'a2': %s" % rowValues

    def _test_bulk_to_map_annotation_context(self, type, ann_count):
        # self._testPopulateMetadataPlate()
        assert len(self.get_well_annotations()) == 0
        assert len(self.get_image_annotations()) == 0

        cfg = os.path.join(
            os.path.dirname(__file__), 'bulk_to_map_annotation_context.yml')

        target = getattr(self, "get_%s" % type)()
        anns = getattr(self, "get_%s_annotations" % type)()
        fileid = anns[0].file.id.val
        ctx = BulkToMapAnnotationContext(
            self.client, target, fileid=fileid, cfg=cfg)
        ctx.parse()
        assert len(self.get_well_annotations()) == 0
        assert len(self.get_image_annotations()) == 0

        ctx.write_to_omero()
        was = self.get_well_annotations()
        ias = self.get_image_annotations()

        if len(was) == ann_count:
            assert len(ias) == 0
            oas = was
        elif len(ias) == ann_count:
            assert len(was) == 0
            oas = ias
        else:
            assert False, "W:%s & I:%s" % (len(was), len(ias))

        for ma, wid, wr, wc in was:
            assert isinstance(ma, MapAnnotationI)
            assert unwrap(ma.getNs()) == NSBULKANNOTATIONS
            mv = ma.getMapValueAsMap()
            assert mv['Well'] == str(wid)
            assert coord2offset(mv['Well Name']) == (wr, wc)
            if (wr, wc) == (0, 0):
                assert mv['Well Type'] == 'Control'
                assert mv['Concentration'] == '0'
            else:
                assert mv['Well Type'] == 'Treatment'
                assert mv['Concentration'] == '10'

    def _test_delete_map_annotation_context(self, type, ann_count):
        # self._test_bulk_to_map_annotation_context()
        assert len(self.get_well_annotations()) == 2 or \
               len(self.get_image_annotations()) == 2

        target = getattr(self, type)
        ctx = DeleteMapAnnotationContext(self.client, target)
        ctx.parse()
        assert len(self.get_well_annotations()) == 2 or \
               len(self.get_image_annotations()) == 2

        ctx.write_to_omero()
        assert len(self.get_well_annotations()) == 0 and \
               len(self.get_image_annotations()) == 0


class MockMeasurementCtx(AbstractMeasurementCtx):

    def well_name_to_number(self, well):
        m = re.match("(?P<COL>[a-z]+)(?P<ROW>\d+)",
                     well, re.IGNORECASE)
        if not m:
            raise Exception("Bad well: %s" % well)

        col = m.group("COL").upper()
        row = m.group("ROW")

        row_num = int(row) - 1
        col_num = 0
        for c in col:
            i = string.ascii_uppercase.find(c)
            col_num += i + 1

        # wellnumber_from_colrow
        numcols = self.analysis_ctx.numcols
        return (col_num * numcols) + row_num

    def parse(self):
        provider = self.original_file_provider
        data = provider.get_original_file_data(self.original_file)
        try:
            rows = list(csv.reader(data, delimiter=","))
        finally:
            data.close()

        columns = [
            ImageColumn("Image", "", list()),
            RoiColumn("ROI", "", list()),
            StringColumn("Type", "", 12, list()),
        ]

        for row in rows[1:]:
            wellnumber = self.well_name_to_number(row[0])
            image = self.analysis_ctx.\
                image_from_wellnumber(wellnumber)
            #  TODO: what to do with the field?!
            #  field = int(row[1])
            #  image = images[field]
            roi = RoiI()
            shape = PointI()
            shape.x = rdouble(float(row[2]))
            shape.y = rdouble(float(row[3]))
            shape.textValue = rstring(row[4])
            roi.addShape(shape)
            roi.image = image.proxy()
            rid = self.update_service\
                .saveAndReturnIds([roi])[0]

            columns[0].values.append(image.id.val)
            columns[1].values.append(rid)
            columns[2].values.append(row[4])

        return MeasurementParsingResult([columns])

    def get_name(self, *args, **kwargs):
        # Strip .csv
        return self.original_file.name.val[:-4]

    def parse_and_populate_roi(self, columns):
        # Remove from interface
        # Using this as a place to set file annotation
        self.file_annotation =\
            self.update_service.saveAndReturnObject(
                self.file_annotation)
        rois = columns[1].values
        for roi in rois:
            link = RoiAnnotationLinkI()
            link.parent = RoiI(roi, False)
            link.child = self.file_annotation.proxy()
            self.update_service\
                .saveObject(link)

    def populate(self, columns):
        self.update_table(columns)


class MockPlateAnalysisCtx(AbstractPlateAnalysisCtx):

    def __init__(self, images, original_files,
                 original_file_image_map,
                 plate_id, service_factory):

        super(MockPlateAnalysisCtx, self).__init__(
            images, original_files, original_file_image_map,
            plate_id, service_factory
        )
        for original_file in original_files:
            name = original_file.name.val
            if name.endswith("csv"):
                self.measurements[len(self.measurements)] = \
                    original_file

    def is_this_type(klass, original_files):
        for original_file in original_files:
            name = unwrap(original_file.name)
            if name.endswith(".csv"):
                return True
    is_this_type = classmethod(is_this_type)

    def get_measurement_count(self):
        return len(self.measurements)

    def get_measurement_ctx(self, index):
        sf = self.service_factory
        provider = self.DEFAULT_ORIGINAL_FILE_PROVIDER(sf)
        return MockMeasurementCtx(
            self, sf, provider,
            self.measurements[index], None)

    def get_result_file_count(self, index):
        return 1


class TestPopulateRois(BasePopulate):

    def testPopulateRoisPlate(self):
        """
            Create a small csv file, use populate_roi.py to parse and
            attach to Plate. Then query to check table has expected content.
        """

        csvName = self.createCsv(
            colNames="Well,Field,X,Y,Type",
            rowData=("A1,0,15,15,Test",))

        rowCount = 1
        colCount = 1
        plate = self.createPlate(rowCount, colCount)

        # As opposed to the ParsingContext, here we are expected
        # to link the file ourselves
        ofile = self.client.upload(csvName).proxy()
        ann = FileAnnotationI()
        ann.file = ofile
        link = PlateAnnotationLinkI()
        link.parent = plate.proxy()
        link.child = ann
        link = self.client.sf.getUpdateService()\
            .saveAndReturnObject(link)
        # End linking

        factory = PlateAnalysisCtxFactory(self.client.sf)
        factory.implementations = (MockPlateAnalysisCtx,)
        ctx = factory.get_analysis_ctx(plate.id.val)
        assert 1 == ctx.get_measurement_count()
        meas = ctx.get_measurement_ctx(0)
        meas.parse_and_populate()

        # Get file annotations
        query = """select p from Plate p
            left outer join fetch p.annotationLinks links
            left outer join fetch links.child as ann
            left outer join fetch ann.file as file
            where p.id=%s""" % plate.id.val
        qs = self.client.sf.getQueryService()
        plate = qs.findByQuery(query, None)
        anns = plate.linkedAnnotationList()
        # Only expect a single annotation which is a 'bulk annotation'
        # the other is the original CSV
        assert len(anns) == 2
        files = dict(
            [(a.ns.val, a.file.id.val) for a in anns if a.ns])
        fileid = files[NSMEASUREMENT]

        # Open table to check contents
        r = self.client.sf.sharedResources()
        t = r.openTable(OriginalFileI(fileid), None)
        cols = t.getHeaders()
        rows = t.getNumberOfRows()
        assert rows == 1

        data = t.read(range(len(cols)), 0, 1)
        imag = data.columns[0].values[0]
        rois = self.client.sf.getRoiService()
        anns = rois.getRoiMeasurements(imag, RoiOptions())
        assert anns
