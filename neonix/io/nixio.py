# Copyright (c) 2014, German Neuroinformatics Node (G-Node)
#                     Achilleas Koutsou <achilleas.k@gmail.com>
#
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted under the terms of the BSD License. See
# LICENSE file in the root of the Project.

from __future__ import absolute_import, print_function

import sys
import time
from datetime import datetime
from collections import Iterable
import itertools
from six import string_types
import warnings

import quantities as pq
import numpy as np

from neo.io.baseio import BaseIO
from neo.core import (Block, Segment, RecordingChannelGroup, AnalogSignal,
                      IrregularlySampledSignal, Epoch, Event, SpikeTrain, Unit)
from neo.io.tools import LazyList

try:
    import nixio
except ImportError:  # pragma: no cover
    raise ImportError("Failed to import NIX (NIXPY not found). "
                      "The NixIO requires the Python bindings for NIX.")


def calculate_timestamp(dt):
    return int(time.mktime(dt.timetuple()))


class NixIO(BaseIO):
    """
    Class for reading and writing NIX files.
    """

    is_readable = False  # for now
    is_writable = True

    supported_objects = [Block, Segment, RecordingChannelGroup,
                         AnalogSignal, IrregularlySampledSignal,
                         Epoch, Event, SpikeTrain, Unit]
    readable_objects = [Block]
    writeable_objects = [Block]

    name = "NIX"
    extensions = ["h5"]
    mode = "file"

    _container_map = {
        "segments": "groups",
        "analogsignals": "data_arrays",
        "irregularlysampledsignals": "data_arrays",
        "events": "multi_tags",
        "epochs": "multi_tags",
        "spiketrains": "multi_tags",
        "recordingchannelgroups": "sources",
        "units": "sources"
    }

    def __init__(self, filename, mode="ro"):
        """
        Initialise IO instance and NIX file.

        :param filename: Full path to the file
        """
        BaseIO.__init__(self, filename)
        self.filename = filename
        if mode == "ro":
            filemode = nixio.FileMode.ReadOnly
        elif mode == "rw":
            filemode = nixio.FileMode.ReadWrite
        elif mode == "ow":
            filemode = nixio.FileMode.Overwrite
        else:
            ValueError("Invalid mode specified '{}'. "
                       "Valid modes: 'ro' (ReadOnly)', 'rw' (ReadWrite), "
                       "'ow' (Overwrite).".format(mode))
        self.nix_file = nixio.File.open(self.filename, filemode)
        self._object_map = dict()
        self._lazy_loaded = list()

    def __del__(self):
        self.nix_file.close()

    def read_all_blocks(self, cascade=True, lazy=False):
        blocks = list()
        for blk in self.nix_file.blocks:
            blocks.append(self.read_block("/" + blk.name, cascade, lazy))
        return blocks

    def read_block(self, path, cascade=True, lazy=False):
        nix_block = self._get_object_at(path)
        neo_block = self._block_to_neo(nix_block)
        if cascade:
            self._read_cascade(nix_block, path, cascade, lazy)
        if lazy:
            self._lazy_loaded.append(path)
        return neo_block

    def read_segment(self, path, cascade=True, lazy=False):
        nix_group = self._get_object_at(path)
        neo_segment = self._group_to_neo(nix_group)
        if cascade:
            self._read_cascade(nix_group, path, cascade, lazy)
        if lazy:
            self._lazy_loaded.append(path)
        return neo_segment

    def read_recordingchannelgroup(self, path, cascade, lazy):
        nix_source = self._get_object_at(path)
        neo_rcg = self._source_rcg_to_neo(nix_source)
        if cascade:
            self._read_cascade(nix_source, path, cascade, lazy)
        if lazy:
            self._lazy_loaded.append(path)
        return neo_rcg

    def read_signal(self, path, lazy=False):
        nix_data_arrays = list()
        parent_group = self._get_object_at("/".join(path.split("/")[:-2]))
        parent_container = parent_group.data_arrays
        signal_group_name = path.split("/")[-1]
        for idx in itertools.count():
            signal_name = "{}.{}".format(signal_group_name, idx)
            if signal_name in parent_container:
                nix_data_arrays.append(parent_container[signal_name])
            else:
                break
        # check metadata segment
        group_section = nix_data_arrays[0].metadata
        for da in nix_data_arrays:
            assert da.metadata == group_section,\
                "DataArray {} is not a member of signal group {}".format(
                    da.name, group_section.name
                )
        neo_signal = self._signal_da_to_neo(nix_data_arrays, lazy)
        if lazy:
            self._lazy_loaded.append(path)
        return neo_signal

    def read_analogsignal(self, path, cascade, lazy=False):
        return self.read_signal(path, lazy)

    def read_irregularlysampledsignal(self, path, cascade, lazy=False):
        return self.read_signal(path, lazy)

    def read_eest(self, path, lazy=False):
        nix_mtag = self._get_object_at(path)
        neo_eest = self._mtag_eest_to_neo(nix_mtag, lazy)
        if lazy:
            self._lazy_loaded.append(path)
        return neo_eest

    def read_epoch(self, path, cascade, lazy=False):
        return self.read_eest(path, lazy)

    def read_event(self, path, cascade, lazy=False):
        return self.read_eest(path, lazy)

    def read_spiketrain(self, path, cascade, lazy=False):
        return self.read_eest(path, lazy)

    def read_unit(self, path, cascade, lazy=False):
        nix_source = self._get_object_at(path)
        neo_unit = self._source_unit_to_neo(nix_source)
        if cascade:
            self._read_cascade(nix_source, path, cascade, lazy)
        if lazy:
            self._lazy_loaded.append(path)
        return neo_unit

    def _block_to_neo(self, nix_block):
        neo_attrs = self._nix_attr_to_neo(nix_block)
        neo_block = Block(**neo_attrs)
        self._object_map[nix_block.id] = neo_block
        return neo_block

    def _group_to_neo(self, nix_group):
        neo_attrs = self._nix_attr_to_neo(nix_group)
        neo_segment = Segment(**neo_attrs)
        self._object_map[nix_group.id] = neo_segment
        return neo_segment

    def _source_rcg_to_neo(self, nix_source):
        neo_attrs = self._nix_attr_to_neo(nix_source)
        rec_channels = list(self._nix_attr_to_neo(c)
                            for c in nix_source.sources
                            if c.type == "neo.recordingchannel")
        neo_attrs["channel_names"] = np.array([c["name"] for c in rec_channels])
        neo_attrs["channel_indexes"] = np.array([c["index"]
                                                 for c in rec_channels])
        if "coordinates" in rec_channels[0]:
            coord_units = rec_channels[0]["coordinates.units"]
            coord_values = list(c["coordinates"] for c in rec_channels)
            neo_attrs["coordinates"] = pq.Quantity(coord_values, coord_units)
        rcg = RecordingChannelGroup(**neo_attrs)
        self._object_map[nix_source.id] = rcg

        nix_units = list(src for src in nix_source.sources
                         if src.type == "neo.unit")
        neo_units = list(self._source_unit_to_neo(nixut)
                         for nixut in nix_units)
        rcg.units.extend(neo_units)

        # referenced signals
        # all_nix_asigs = list(da for da in parent_block.data_arrays
        #                      if da.type == "neo.analogsignal")
        # nix_asigs = self._get_referers(nix_source, all_nix_asigs)
        # neo_asigs = self._get_mapped_objects(nix_asigs)
        # deduplicate by name
        # neo_asigs = list(dict((s.name, s) for s in neo_asigs).values())
        # rcg.analogsignals.extend(neo_asigs)

        # all_nix_isigs = list(da for da in parent_block.data_arrays
        #                      if da.type == "neo.irregularlysampledsignal")
        # nix_isigs = self._get_referers(nix_source, all_nix_isigs)
        # neo_isigs = self._get_mapped_objects(nix_isigs)
        # neo_isigs = list(dict((s.name, s) for s in neo_isigs).values())
        # rcg.irregularlysampledsignals.extend(neo_isigs)
        # rcg.create_many_to_one_relationship()
        return rcg

    def _source_unit_to_neo(self, nix_unit):
        neo_attrs = self._nix_attr_to_neo(nix_unit)
        neo_unit = Unit(**neo_attrs)
        self._object_map[nix_unit.id] = neo_unit

        # referenced spiketrains
        # all_nix_sts = list(mtag for mtag in parent_block.multi_tags
        #                    if mtag.type == "neo.spiketrain")
        # nix_sts = self._get_referers(nix_unit, all_nix_sts)
        # neo_sts = self._get_mapped_objects(nix_sts)
        # neo_unit.spiketrains.extend(neo_sts)
        # neo_unit.create_many_to_one_relationship()
        return neo_unit

    def _signal_da_to_neo(self, nix_da_group, lazy):
        """
        Convert a group of NIX DataArrays to a Neo signal. This method expects
        a list of data arrays that all represent the same, multidimensional
        Neo Signal object.
        This returns either an AnalogSignal or IrregularlySampledSignal.

        :param nix_da_group: a list of NIX DataArray objects
        :return: a Neo Signal object
        """
        nix_da_group = sorted(nix_da_group, key=lambda d: d.name)
        neo_attrs = self._nix_attr_to_neo(nix_da_group[0])
        neo_attrs["name"] = nix_da_group[0].metadata.name
        neo_type = nix_da_group[0].type

        unit = nix_da_group[0].unit
        if lazy:
            signaldata = pq.Quantity(np.empty(0), unit)
        else:
            signaldata = pq.Quantity(np.transpose(nix_da_group), unit)
        timedim = self._get_time_dimension(nix_da_group[0])
        if neo_type == "neo.analogsignal"\
                or isinstance(timedim, nixio.SampledDimension):
            if lazy:
                sampling_period = pq.Quantity(0, timedim.unit)
                t_start = pq.Quantity(0, timedim.unit)
            else:
                sampling_period = pq.Quantity(timedim.sampling_interval,
                                              timedim.unit)
                t_start = pq.Quantity(timedim.offset, timedim.unit)
            neo_signal = AnalogSignal(
                signal=signaldata, sampling_period=sampling_period,
                t_start=t_start, **neo_attrs
            )
        elif neo_type == "neo.irregularlysampledsignal"\
                or isinstance(timedim, nixio.RangeDimension):
            if lazy:
                times = pq.Quantity(np.empty(0), timedim.unit)
            else:
                times = pq.Quantity(timedim.ticks, timedim.unit)
            neo_signal = IrregularlySampledSignal(
                signal=signaldata, times=times, **neo_attrs
            )
        else:
            return None
        for da in nix_da_group:
            self._object_map[da.id] = neo_signal
        return neo_signal

    def _mtag_eest_to_neo(self, nix_mtag, lazy):
        neo_attrs = self._nix_attr_to_neo(nix_mtag)
        neo_type = nix_mtag.type

        time_unit = nix_mtag.positions.unit
        if lazy:
            times = pq.Quantity(np.empty(0), time_unit)
        else:
            times = pq.Quantity(nix_mtag.positions, time_unit)
        if neo_type == "neo.epoch":
            if lazy:
                durations = pq.Quantity(np.empty(0), nix_mtag.extents.unit)
                labels = []
            else:
                durations = pq.Quantity(nix_mtag.extents, nix_mtag.extents.unit)
                labels = nix_mtag.positions.dimensions[0].labels
            eest = Epoch(times=times, durations=durations, labels=labels,
                         **neo_attrs)
        elif neo_type == "neo.event":
            if lazy:
                labels = []
            else:
                labels = nix_mtag.positions.dimensions[0].labels
            eest = Event(times=times, labels=labels, **neo_attrs)
        elif neo_type == "neo.spiketrain":
            eest = SpikeTrain(times=times, **neo_attrs)
            if len(nix_mtag.features):
                wfda = nix_mtag.features[0].data
                wftime = self._get_time_dimension(wfda)
                if lazy:
                    eest.waveforms = pq.Quantity(np.empty(0), wfda.unit)
                    eest.sampling_period = pq.Quantity(0, wftime.unit)
                    eest.left_sweep = 0
                else:
                    eest.waveforms = pq.Quantity(wfda, wfda.unit)
                    eest.sampling_period = pq.Quantity(
                        wftime.sampling_interval, wftime.unit
                    )
                    eest.left_sweep = wfda.metadata["left_sweep"]
        else:
            return None
        self._object_map[nix_mtag.id] = eest
        return eest

    def _read_cascade(self, nix_obj, path, cascade, lazy):
        neo_obj = self._object_map[nix_obj.id]
        for neocontainer in getattr(neo_obj, "_child_containers", []):
            nixcontainer = self._container_map[neocontainer]
            if not hasattr(nix_obj, nixcontainer):
                continue
            neotype = neocontainer[:-1]
            chpaths = list(path + "/" + neocontainer + "/" + c.name
                           for c in getattr(nix_obj, nixcontainer)
                           if c.type == "neo." + neotype)
            if neocontainer in ("analogsignals",
                                "irregularlysampledsignals"):
                chpaths = self._group_signals(chpaths)
            if cascade != "lazy":
                read_obj = getattr(self, "read_" + neotype)
                children = list(read_obj(cp, cascade, lazy)
                                for cp in chpaths)
            else:
                children = LazyList(self, lazy, chpaths)
            setattr(neo_obj, neocontainer, children)

        if isinstance(neo_obj, RecordingChannelGroup):
            # set references to signals
            parent_block_path = "/" + path.split("/")[1]
            parent_block = self._get_object_at(parent_block_path)
            all_nix_asigs = list(da for da in parent_block.data_arrays
                                 if da.type == "neo.analogsignal")
            nix_asigs = self._get_referers(nix_obj, all_nix_asigs)
            neo_asigs = self._get_mapped_objects(nix_asigs)
            # deduplicate by name
            neo_asigs = list(dict((s.name, s) for s in neo_asigs).values())
            neo_obj.analogsignals.extend(neo_asigs)

            all_nix_isigs = list(da for da in parent_block.data_arrays
                                 if da.type == "neo.irregularlysampledsignal")
            nix_isigs = self._get_referers(nix_obj, all_nix_isigs)
            neo_isigs = self._get_mapped_objects(nix_isigs)
            neo_isigs = list(dict((s.name, s) for s in neo_isigs).values())
            neo_obj.irregularlysampledsignals.extend(neo_isigs)
            neo_obj.create_many_to_one_relationship()

        elif isinstance(neo_obj, Unit):
            # set references to spiketrains
            parent_block_path = "/" + path.split("/")[1]
            parent_block = self._get_object_at(parent_block_path)
            all_nix_sts = list(mtag for mtag in parent_block.multi_tags
                               if mtag.type == "neo.spiketrain")
            nix_sts = self._get_referers(nix_obj, all_nix_sts)
            neo_sts = self._get_mapped_objects(nix_sts)
            neo_obj.spiketrains.extend(neo_sts)
            neo_obj.create_many_to_one_relationship()

    def write_block(self, neo_block):
        """
        Convert ``neo_block`` to the NIX equivalent and write it to the file.

        :param neo_block: Neo block to be written
        :return: The new NIX Block
        """
        attr = self._neo_attr_to_nix(neo_block, self.nix_file.blocks)
        nix_block = self.nix_file.create_block(attr["name"], attr["type"])
        nix_block.definition = attr["definition"]
        object_path = "/" + nix_block.name
        self._object_map[id(neo_block)] = nix_block
        self._write_attr_annotations(nix_block, attr, object_path)
        for segment in neo_block.segments:
            self.write_segment(segment, object_path)
        for rcg in neo_block.recordingchannelgroups:
            self.write_recordingchannelgroup(rcg, object_path)
        return nix_block

    def write_all_blocks(self, neo_blocks):
        """
        Convert all ``neo_blocks`` to the NIX equivalent and write them to the
        file.

        :param neo_blocks: List (or iterable) containing Neo blocks
        :return: A list containing the new NIX Blocks
        """
        nix_blocks = list(map(self.write_block, neo_blocks))
        return nix_blocks

    def write_segment(self, segment, parent_path):
        """
        Convert the provided ``segment`` to a NIX Group and write it to the NIX
        file at the location defined by ``parent_path``.

        :param segment: Neo segment to be written
        :param parent_path: Path to the parent of the new segment
        :return: The newly created NIX Group
        """
        parent_block = self._get_object_at(parent_path)
        attr = self._neo_attr_to_nix(segment, parent_block.groups)
        nix_group = parent_block.create_group(attr["name"], attr["type"])
        nix_group.definition = attr["definition"]
        object_path = parent_path + "/segments/" + nix_group.name
        self._object_map[id(segment)] = nix_group
        self._write_attr_annotations(nix_group, attr, object_path)
        for anasig in segment.analogsignals:
            self.write_analogsignal(anasig, object_path)
        for irsig in segment.irregularlysampledsignals:
            self.write_irregularlysampledsignal(irsig, object_path)
        for ep in segment.epochs:
            self.write_epoch(ep, object_path)
        for ev in segment.events:
            self.write_event(ev, object_path)
        for sptr in segment.spiketrains:
            self.write_spiketrain(sptr, object_path)

        return nix_group

    def write_recordingchannelgroup(self, rcg, parent_path):
        """
        Convert the provided ``rcg`` (RecordingChannelGroup) to a NIX Source
        and write it to the NIX file at the location defined by ``parent_path``.

        :param rcg: The Neo RecordingChannelGroup to be written
        :param parent_path: Path to the parent of the new segment
        :return: The newly created NIX Source
        """
        parent_block = self._get_object_at(parent_path)
        attr = self._neo_attr_to_nix(rcg, parent_block.sources)
        nix_source = parent_block.create_source(attr["name"], attr["type"])
        nix_source.definition = attr["definition"]
        object_path = parent_path + "/recordingchannelgroups/" + nix_source.name
        self._object_map[id(rcg)] = nix_source
        self._write_attr_annotations(nix_source, attr, object_path)
        for idx, channel in enumerate(rcg.channel_indexes):
            # create a child source object to represent the individual channel
            if len(rcg.channel_names):
                nix_chan_name = rcg.channel_names[idx]
            else:
                nix_chan_name = "{}.RecordingChannel{}".format(
                    parent_block.name, idx
                )
            nix_chan_type = "neo.recordingchannel"
            nix_chan = nix_source.create_source(nix_chan_name, nix_chan_type)
            nix_chan.definition = nix_source.definition
            chan_obj_path = object_path + "/recordingchannels/" + nix_chan_name
            chan_metadata = self._get_or_init_metadata(nix_chan,
                                                       chan_obj_path)
            chan_metadata.create_property("index", self._to_value(int(channel)))
            if "file_origin" in attr:
                chan_metadata.create_property(
                    "file_origin", self._to_value(attr["file_origin"])
                )

            if hasattr(rcg, "coordinates"):
                chan_coords = rcg.coordinates[idx]
                coord_unit = str(chan_coords[0].dimensionality)
                nix_coord_unit = self._to_value(coord_unit)
                nix_coord_values = tuple(
                    self._to_value(c.rescale(coord_unit).magnitude.item())
                    for c in chan_coords
                )
                chan_metadata.create_property("coordinates",
                                              nix_coord_values)
                chan_metadata.create_property("coordinates.units",
                                              nix_coord_unit)
        for unit in rcg.units:
            self.write_unit(unit, object_path)

        # add signal references
        for nix_asigs in self._get_mapped_objects(rcg.analogsignals):
            # One AnalogSignal maps to list of DataArrays
            for da in nix_asigs:
                da.sources.append(nix_source)
        for nix_isigs in self._get_mapped_objects(rcg.irregularlysampledsignals):
            # One IrregularlySampledSignal maps to list of DataArrays
            for da in nix_isigs:
                da.sources.append(nix_source)

        return nix_source

    def write_analogsignal(self, anasig, parent_path):
        """
        Convert the provided ``anasig`` (AnalogSignal) to a list of NIX
        DataArray objects and write them to the NIX file at the location defined
        by ``parent_path``. All DataArray objects created from the same
        AnalogSignal have their metadata section point to the same object.

        :param anasig: The Neo AnalogSignal to be written
        :param parent_path: Path to the parent of the new segment
        :return: A list containing the newly created NIX DataArrays
        """
        parent_group = self._get_object_at(parent_path)
        block_path = "/" + parent_path.split("/")[1]
        parent_block = self._get_object_at(block_path)
        parent_metadata = self._get_or_init_metadata(parent_group, parent_path)
        attr = self._neo_attr_to_nix(anasig, parent_block.data_arrays)
        anasig_group_segment = parent_metadata.create_section(
            attr["name"], attr["type"]+".metadata"
        )

        if "file_origin" in attr:
            anasig_group_segment.create_property(
                "file_origin", self._to_value(attr["file_origin"])
            )
        if anasig.annotations:
            self._add_annotations(anasig.annotations, anasig_group_segment)

        # common properties
        data_units = self._get_units(anasig)
        # often sampling period is in 1/Hz or 1/kHz - simplifying to s
        time_units = self._get_units(anasig.sampling_period, True)
        # rescale after simplification
        offset = anasig.t_start.rescale(time_units).item()
        sampling_interval = anasig.sampling_period.rescale(time_units).item()

        nix_data_arrays = list()
        for idx, sig in enumerate(anasig.transpose()):
            nix_data_array = parent_block.create_data_array(
                "{}.{}".format(attr["name"], idx),
                attr["type"],
                data=sig.magnitude
            )
            nix_data_array.definition = attr["definition"]
            nix_data_array.unit = data_units

            timedim = nix_data_array.append_sampled_dimension(sampling_interval)
            timedim.unit = time_units
            timedim.label = "time"
            timedim.offset = offset
            chandim = nix_data_array.append_set_dimension()
            parent_group.data_arrays.append(nix_data_array)
            # point metadata to common section
            nix_data_array.metadata = anasig_group_segment
            nix_data_arrays.append(nix_data_array)
        self._object_map[id(anasig)] = nix_data_arrays
        return nix_data_arrays

    def write_irregularlysampledsignal(self, irsig, parent_path):
        """
        Convert the provided ``irsig`` (IrregularlySampledSignal) to a list of
        NIX DataArray objects and write them to the NIX file at the location
        defined by ``parent_path``. All DataArray objects created from the same
        IrregularlySampledSignal have their metadata section point to the same
        object.

        :param irsig: The Neo IrregularlySampledSignal to be written
        :param parent_path: Path to the parent of the new
        :return: The newly created NIX DataArray
        """
        parent_group = self._get_object_at(parent_path)
        block_path = "/" + parent_path.split("/")[1]
        parent_block = self._get_object_at(block_path)
        parent_metadata = self._get_or_init_metadata(parent_group, parent_path)
        attr = self._neo_attr_to_nix(irsig, parent_block.data_arrays)
        irsig_group_segment = parent_metadata.create_section(
            attr["name"], attr["type"]+".metadata"
        )

        if "file_origin" in attr:
            irsig_group_segment.create_property(
                "file_origin", self._to_value(attr["file_origin"])
            )

        if irsig.annotations:
            self._add_annotations(irsig.annotations, irsig_group_segment)

        # common properties
        data_units = self._get_units(irsig)
        time_units = self._get_units(irsig.times)
        times = irsig.times.magnitude.tolist()

        nix_data_arrays = list()
        for idx, sig in enumerate(irsig.transpose()):
            nix_data_array = parent_block.create_data_array(
                "{}.{}".format(attr["name"], idx),
                attr["type"],
                data=sig.magnitude
            )
            nix_data_array.definition = attr["definition"]
            nix_data_array.unit = data_units

            timedim = nix_data_array.append_range_dimension(times)
            timedim.unit = time_units
            timedim.label = "time"
            chandim = nix_data_array.append_set_dimension()
            parent_group.data_arrays.append(nix_data_array)
            # point metadata to common section
            nix_data_array.metadata = irsig_group_segment
            nix_data_arrays.append(nix_data_array)
        self._object_map[id(irsig)] = nix_data_arrays
        return nix_data_arrays

    def write_epoch(self, ep, parent_path):
        """
        Convert the provided ``ep`` (Epoch) to a NIX MultiTag and write it to
        the NIX file at the location defined by ``parent_path``.

        :param ep: The Neo Epoch to be written
        :param parent_path: Path to the parent of the new MultiTag
        :return: The newly created NIX MultiTag
        """
        parent_group = self._get_object_at(parent_path)
        block_path = "/" + parent_path.split("/")[1]
        parent_block = self._get_object_at(block_path)
        attr = self._neo_attr_to_nix(ep, parent_block.multi_tags)

        # times -> positions
        times = ep.times.magnitude
        time_units = self._get_units(ep.times)

        times_da = parent_block.create_data_array(
            attr["name"]+".times", attr["type"]+".times", data=times
        )
        times_da.unit = time_units

        # durations -> extents
        durations = ep.durations.magnitude
        duration_units = self._get_units(ep.durations)

        durations_da = parent_block.create_data_array(
            attr["name"]+".durations",
            attr["type"]+".durations",
            data=durations
        )
        durations_da.unit = duration_units

        # ready to create MTag
        nix_multi_tag = parent_block.create_multi_tag(
            attr["name"], attr["type"], times_da)
        label_dim = nix_multi_tag.positions.append_set_dimension()
        label_dim.labels = ep.labels.tolist()
        nix_multi_tag.extents = durations_da
        parent_group.multi_tags.append(nix_multi_tag)
        nix_multi_tag.definition = attr["definition"]
        object_path = parent_path + "/epochs/" + nix_multi_tag.name
        self._object_map[id(ep)] = nix_multi_tag
        self._write_attr_annotations(nix_multi_tag, attr, object_path)

        nix_multi_tag.references.extend(
            self._get_contained_signals(parent_group)
        )
        return nix_multi_tag

    def write_event(self, ev, parent_path):
        """
        Convert the provided ``ev`` (Event) to a NIX MultiTag and write it to
        the NIX file at the location defined by ``parent_path``.

        :param ev: The Neo Event to be written
        :param parent_path: Path to the parent of the new MultiTag
        :return: The newly created NIX MultiTag
        """
        parent_group = self._get_object_at(parent_path)
        block_path = "/" + parent_path.split("/")[1]
        parent_block = self._get_object_at(block_path)
        attr = self._neo_attr_to_nix(ev, parent_block.multi_tags)

        # times -> positions
        times = ev.times.magnitude
        time_units = self._get_units(ev.times)

        times_da = parent_block.create_data_array(
            attr["name"]+".times", attr["type"]+".times", data=times
        )
        times_da.unit = time_units

        # ready to create MTag
        nix_multi_tag = parent_block.create_multi_tag(
            attr["name"], attr["type"], times_da
        )
        label_dim = nix_multi_tag.positions.append_set_dimension()
        label_dim.labels = ev.labels.tolist()
        parent_group.multi_tags.append(nix_multi_tag)
        nix_multi_tag.definition = attr["definition"]
        object_path = parent_path + "/events/" + nix_multi_tag.name
        self._object_map[id(ev)] = nix_multi_tag
        self._write_attr_annotations(nix_multi_tag, attr, object_path)

        nix_multi_tag.references.extend(
            self._get_contained_signals(parent_group)
        )
        return nix_multi_tag

    def write_spiketrain(self, sptr, parent_path):
        """
        Convert the provided ``sptr`` (SpikeTrain) to a NIX MultiTag and write
        it to the NIX file at the location defined by ``parent_path``.

        :param sptr: The Neo SpikeTrain to be written
        :param parent_path: Path to the parent of the new MultiTag
        :return: The newly created NIX MultiTag
        """
        parent_group = self._get_object_at(parent_path)
        block_path = "/" + parent_path.split("/")[1]
        parent_block = self._get_object_at(block_path)
        attr = self._neo_attr_to_nix(sptr, parent_block.multi_tags)

        # spike times
        time_units = self._get_units(sptr.times)
        times = sptr.times.magnitude
        times_da = parent_block.create_data_array(
            attr["name"]+".times", attr["type"]+".times", data=times
        )
        times_da.unit = time_units

        # ready to create MTag
        nix_multi_tag = parent_block.create_multi_tag(
            attr["name"], attr["type"], times_da
        )
        parent_group.multi_tags.append(nix_multi_tag)

        nix_multi_tag.definition = attr["definition"]
        object_path = parent_path + "/spiketrains/" + nix_multi_tag.name
        self._object_map[id(sptr)] = nix_multi_tag

        mtag_metadata = self._get_or_init_metadata(nix_multi_tag, object_path)
        self._write_attr_annotations(nix_multi_tag, attr, object_path)
        if sptr.t_start:
            t_start = sptr.t_start.rescale(time_units).magnitude.item()
            mtag_metadata.create_property("t_start",
                                          self._to_value(t_start))
        # t_stop is not optional
        t_stop = sptr.t_stop.rescale(time_units).magnitude.item()
        mtag_metadata.create_property("t_stop", self._to_value(t_stop))

        # waveforms
        if sptr.waveforms is not None:
            wf_data = list(wf.magnitude for wf in
                           list(wfgroup for wfgroup in sptr.waveforms))
            waveforms_da = parent_block.create_data_array(
                attr["name"]+".waveforms", "neo.waveforms", data=wf_data
            )
            wf_unit = self._get_units(sptr.waveforms)
            waveforms_da.unit = wf_unit
            nix_multi_tag.create_feature(waveforms_da, nixio.LinkType.Indexed)
            time_units = self._get_units(sptr.sampling_period, True)
            sampling_interval = sptr.sampling_period.rescale(time_units).item()
            wf_spikedim = waveforms_da.append_set_dimension()
            wf_chandim = waveforms_da.append_set_dimension()
            wf_timedim = waveforms_da.append_sampled_dimension(sampling_interval)
            wf_timedim.unit = time_units
            wf_timedim.label = "time"
            wf_path = object_path + "/waveforms/" + waveforms_da.name
            waveforms_da.metadata = self._get_or_init_metadata(waveforms_da,
                                                               wf_path)
            if sptr.left_sweep:
                left_sweep = sptr.left_sweep.rescale(time_units).\
                    magnitude.item()
                waveforms_da.metadata.create_property(
                    "left_sweep", self._to_value(left_sweep)
                )

        return nix_multi_tag

    def write_unit(self, ut, parent_path):
        """
        Convert the provided ``ut`` (Unit) to a NIX Source and write it to the
        NIX file at the parent RCG.

        :param ut: The Neo Unit to be written
        :param parent_path: Path to the parent of the new Source
        :return: The newly created NIX Source
        """
        parent_source = self._get_object_at(parent_path)
        attr = self._neo_attr_to_nix(ut, parent_source.sources)
        nix_source = parent_source.create_source(attr["name"], attr["type"])
        nix_source.definition = attr["definition"]
        # Units are children of the Block
        object_path = parent_path + "/units/" + nix_source.name
        self._object_map[id(ut)] = nix_source
        self._write_attr_annotations(nix_source, attr, object_path)
        # Make contained spike trains refer to parent rcg and new unit
        for nix_st in self._get_mapped_objects(ut.spiketrains):
            nix_st.sources.append(parent_source)
            nix_st.sources.append(nix_source)

        return nix_source

    def _get_or_init_metadata(self, nix_obj, path):
        """
        Creates a metadata Section for the provided NIX object if it doesn't
        have one already. Returns the new or existing metadata section.

        :param nix_obj: The object to which the Section is attached
        :param path: Path to nix_obj
        :return: The metadata section of the provided object
        """
        parent_parts = path.split("/")[:-2]
        parent_path = "/".join(parent_parts)
        if nix_obj.metadata is None:
            if len(parent_parts) == 0:  # nix_obj is root block
                parent_metadata = self.nix_file
            else:
                obj_parent = self._get_object_at(parent_path)
                parent_metadata = self._get_or_init_metadata(obj_parent,
                                                             parent_path)
            nix_obj.metadata = parent_metadata.create_section(
                    nix_obj.name, nix_obj.type+".metadata"
            )
        return nix_obj.metadata

    def _get_object_at(self, path):
        """
        Returns the object at the location defined by the path. ``path`` is a
        '/' delimited string. Each part of the string alternates between an
        object name and a container.

        Example path: /block_1/segments/segment_a/events/event_a1

        :param path: Path string
        :return: The object at the location defined by the path
        """
        parts = path.split("/")
        if parts[0]:
            ValueError("Invalid object path: {}".format(path))
        if len(parts) == 2:  # root block
            return self.nix_file.blocks[parts[1]]
        parent_path = "/".join(parts[:-2])
        parent_obj = self._get_object_at(parent_path)
        parent_container = getattr(parent_obj, self._container_map[parts[-2]])
        return parent_container[parts[-1]]

    def _get_mapped_objects(self, object_list):
        return list(map(self._get_mapped_object, object_list))

    def _get_mapped_object(self, obj):
        try:
            if hasattr(obj, "id"):
                return self._object_map[obj.id]
            else:
                return self._object_map[id(obj)]
        except KeyError:
            raise KeyError("Failed to find mapped object for {}. "
                           "Object not yet converted.".format(obj))

    def _write_attr_annotations(self, nix_object, attr, object_path):
        if "created_at" in attr:
            nix_object.force_created_at(calculate_timestamp(attr["created_at"]))
        if "file_datetime" in attr:
            metadata = self._get_or_init_metadata(nix_object, object_path)
            metadata.create_property(
                "file_datetime", self._to_value(attr["file_datetime"])
            )
        if "file_origin" in attr:
            metadata = self._get_or_init_metadata(nix_object, object_path)
            metadata.create_property(
                "file_origin", self._to_value(attr["file_origin"])
            )
        if "annotations" in attr:
            metadata = self._get_or_init_metadata(nix_object, object_path)
            self._add_annotations(attr["annotations"], metadata)

    @staticmethod
    def _neo_attr_to_nix(neo_obj, container):
        nix_attrs = dict()
        neo_type = type(neo_obj).__name__
        if neo_obj.name:
            nix_basename = neo_obj.name
        else:
            nix_basename = "neo.{}".format(neo_type)
        if neo_type in ["AnalogSignal", "IrregularlySampledSignal"]:
            suffix = ".0"
        else:
            suffix = ""
        if nix_basename+suffix not in container:
            nix_name = nix_basename
        else:
            idx = 1
            nix_name = "{}-{}".format(nix_basename, idx)
            while nix_name+suffix in container:
                idx += 1
                nix_name = "{}-{}".format(nix_basename, idx)
        nix_attrs["name"] = nix_name
        nix_attrs["type"] = "neo.{}".format(neo_type.lower())
        nix_attrs["definition"] = neo_obj.description
        if isinstance(neo_obj, (Block, Segment)):
            if neo_obj.rec_datetime:
                nix_attrs["created_at"] = neo_obj.rec_datetime
            if neo_obj.file_datetime:
                nix_attrs["file_datetime"] = neo_obj.file_datetime
        if neo_obj.file_origin:
            nix_attrs["file_origin"] = neo_obj.file_origin
        if neo_obj.annotations:
            nix_attrs["annotations"] = neo_obj.annotations
        return nix_attrs

    @classmethod
    def _add_annotations(cls, annotations, metadata):
        for k, v in annotations.items():
            v = cls._to_value(v)
            metadata.create_property(k, v)

    @staticmethod
    def _to_value(v):
        """
        Helper function for converting arbitrary variables to types compatible
        with nixio.Value().

        :param v: The value to be converted
        :return: a nixio.Value() object
        """
        if isinstance(v, pq.Quantity):
            # v = nixio.Value((v.magnitude.item(), str(v.dimensionality)))
            warnings.warn("Quantities in annotations are not currently "
                          "supported when writing to NIX.")
            return None
        elif isinstance(v, datetime):
            v = nixio.Value(calculate_timestamp(v))
        elif isinstance(v, string_types):
            v = nixio.Value(v)
        elif isinstance(v, bytes):
            v = nixio.Value(v.decode())
        elif isinstance(v, Iterable):
            vv = list()
            for item in v:
                if isinstance(v, Iterable):
                    warnings.warn("Multidimensional arrays and nested "
                                  "containers are not currently supported "
                                  "when writing to NIX.")
                    return None
                if type(item).__module__ == "numpy":
                    item = nixio.Value(item.item())
                else:
                    item = nixio.Value(item)
                vv.append(item)
            if not len(vv):
                vv = None
            v = vv
        elif type(v).__module__ == "numpy":
            v = nixio.Value(v.item())
        else:
            v = nixio.Value(v)
        return v

    @staticmethod
    def _get_contained_signals(obj):
        return list(
             da for da in obj.data_arrays
             if da.type in ["neo.analogsignal", "neo.irregularlysampledsignal"]
        )

    @staticmethod
    def _get_units(quantity, simplify=False):
        """
        Returns the units of a quantity value or array as a string, or None if
        it is dimensionless.

        :param quantity: Quantity scalar or array
        :param simplify: True/False Simplify units
        :return: Units of the quantity or None if dimensionless
        """
        units = quantity.units.dimensionality
        if simplify:
            units = units.simplified
        units = str(units)
        if units == "dimensionless":
            units = None
        return units

    @staticmethod
    def _nix_attr_to_neo(nix_obj):
        neo_attrs = dict()
        neo_attrs["name"] = nix_obj.name

        neo_attrs["description"] = nix_obj.definition
        if nix_obj.metadata:
            for prop in nix_obj.metadata.props:
                values = prop.values
                if len(values) == 1:
                    neo_attrs[prop.name] = values[0].value
                else:
                    neo_attrs[prop.name] = list(v.value for v in values)

        if isinstance(nix_obj, (nixio.Block, nixio.Group)):
            neo_attrs["rec_datetime"] = datetime.fromtimestamp(
                nix_obj.created_at)
        if "file_datetime" in neo_attrs:
            neo_attrs["file_datetime"] = datetime.fromtimestamp(
                neo_attrs["file_datetime"]
            )
        return neo_attrs

    @staticmethod
    def _group_signals(paths):
        """
        Groups data arrays that were generated by the same Neo Signal object.

        :param paths: A list of paths (strings) of all the signals to be grouped
        :return: A list of paths (strings) of signal groups. The last part of
        each path is the common name of the signals in the group.
        """
        grouppaths = list(".".join(p.split(".")[:-1])
                          for p in paths)
        return list(set(grouppaths))

    @staticmethod
    def _get_referers(nix_obj, obj_list):
        ref_list = list()
        for ref in obj_list:
            if nix_obj.name in list(src.name for src in ref.sources):
                ref_list.append(ref)
        return ref_list

    @staticmethod
    def _get_time_dimension(obj):
        for dim in obj.dimensions:
            if hasattr(dim, "label") and dim.label == "time":
                return dim
        return None

