import datetime
import numpy as np
import struct
from data_log import DataLog, Channel
from ldparser.ldparser import ldVehicle, ldVenue, ldEvent, ldHead, ldChan, ldData

# All text in a .ld file is stored in fixed size single byte strings, so any other characters must
# be substituted or dropped, and the text truncated, before it is written. These are the plain text
# equivalents for symbols commonly found in log units.
TEXT_REPLACEMENTS = {
    "°C": "C",
    "°F": "F",
    "°": "deg",
    "µ": "u",
    "Ω": "ohm",
}

def sanitize_text(text, max_length):
    """ Converts text to a plain ASCII string that will fit in a fixed size .ld text field.

    max_length: int, size of the field in bytes, one byte is reserved for the null terminator
    """
    for symbol, replacement in TEXT_REPLACEMENTS.items():
        text = text.replace(symbol, replacement)

    text = text.encode("ascii", "ignore").decode("ascii")

    return text[:max_length - 1]

class MotecLog(object):
    """ Handles generating a MoTeC .ld file from log data.

    This utilizes the ldparser library for packing all the meta data and channel data into the
    correct binary format. Some functionality and information (e.g. pointer constants below) was
    missing from the ldparser library, so this class servers as a wrapper to fill in the gaps.

    This operates on containers from the data_log library.
    """
    # Pointers to locations in the file where data sections should be written. These have been
    # determined from inspecting some MoTeC .ld files, and were consistent across all files.
    VEHICLE_PTR = 1762
    VENUE_PTR = 5078
    EVENT_PTR = 8180
    HEADER_PTR = 11336

    CHANNEL_HEADER_SIZE = struct.calcsize(ldChan.fmt)

    # Sizes of the text fields in the .ld format [bytes]
    NAME_SIZE = 64
    COMMENT_SIZE = 1024
    VEHICLE_TYPE_SIZE = 32
    CHANNEL_NAME_SIZE = 32
    CHANNEL_UNITS_SIZE = 12

    # MoTeC i2 will load a log with empty driver, vehicle, venue, or session fields, and will show
    # the channel list from it, but none of the channel data is usable. Any of these left empty are
    # filled in with a placeholder so the log is always usable. The remaining fields (event name,
    # vehicle type, and the comments) may be left empty without any problems.
    DEFAULT_DRIVER = "Unknown"
    DEFAULT_VEHICLE_ID = "Unknown"
    DEFAULT_VENUE_NAME = "Unknown"
    DEFAULT_EVENT_SESSION = "Session"

    def __init__(self):
        self.driver = ""
        self.vehicle_id = ""
        self.vehicle_weight = 0
        self.vehicle_type = ""
        self.vehicle_comment = ""
        self.venue_name = ""
        self.event_name = ""
        self.event_session = ""
        self.long_comment = ""
        self.short_comment = ""
        self.datetime = datetime.datetime.now()

        # File components from ldparser
        self.ld_header = None
        self.ld_channels = []

    def initialize(self):
        """ Initializes all the meta data for the motec log.

        This must be called before adding any channel data.
        """
        # Fill in placeholders for the fields i2 needs in order to show any channel data
        driver = sanitize_text(self.driver, self.NAME_SIZE) or self.DEFAULT_DRIVER
        vehicle_id = sanitize_text(self.vehicle_id, self.NAME_SIZE) or self.DEFAULT_VEHICLE_ID
        vehicle_type = sanitize_text(self.vehicle_type, self.VEHICLE_TYPE_SIZE)
        vehicle_comment = sanitize_text(self.vehicle_comment, self.VEHICLE_TYPE_SIZE)
        venue_name = sanitize_text(self.venue_name, self.NAME_SIZE) or self.DEFAULT_VENUE_NAME
        event_name = sanitize_text(self.event_name, self.NAME_SIZE)
        event_session = sanitize_text(self.event_session, self.NAME_SIZE) \
            or self.DEFAULT_EVENT_SESSION
        long_comment = sanitize_text(self.long_comment, self.COMMENT_SIZE)
        short_comment = sanitize_text(self.short_comment, self.NAME_SIZE)

        ld_vehicle = ldVehicle(vehicle_id, self.vehicle_weight, vehicle_type, vehicle_comment)
        ld_venue = ldVenue(venue_name, self.VEHICLE_PTR, ld_vehicle)
        ld_event = ldEvent(event_name, event_session, long_comment, self.VENUE_PTR, ld_venue)

        self.ld_header = ldHead(self.HEADER_PTR, self.HEADER_PTR, self.EVENT_PTR, ld_event, \
            driver, vehicle_id, venue_name, self.datetime, short_comment, event_name, \
            event_session)

    def add_channel(self, log_channel):
        """ Adds a single channel of data to the motec log.

        log_channel: data_log.Channel
        """
        # Advance the header data pointer
        self.ld_header.data_ptr += self.CHANNEL_HEADER_SIZE

        # Advance the data pointers of all previous channels
        for ld_channel in self.ld_channels:
            ld_channel.data_ptr += self.CHANNEL_HEADER_SIZE

        # Determine our file pointers
        if self.ld_channels:
            meta_ptr = self.ld_channels[-1].next_meta_ptr
            prev_meta_ptr = self.ld_channels[-1].meta_ptr
            data_ptr = self.ld_channels[-1].data_ptr + self.ld_channels[-1]._data.nbytes
        else:
            # First channel needs the previous pointer zero'd out
            meta_ptr = self.HEADER_PTR
            prev_meta_ptr = 0
            data_ptr = self.ld_header.data_ptr
        next_meta_ptr = meta_ptr + self.CHANNEL_HEADER_SIZE

        # Channel specs
        data_len = len(log_channel)
        data_type = np.float32 if log_channel.data_type is float else np.int32
        freq = int(log_channel.avg_frequency())
        shift = 0
        multiplier = 1
        scale = 1

        # Decimal places must be hard coded to zero, the ldparser library doesn't properly
        # handle non zero values, consequently all channels will have zero decimal places
        # decimals = log_channel.decimals
        decimals = 0

        name = sanitize_text(log_channel.name, self.CHANNEL_NAME_SIZE)
        units = sanitize_text(log_channel.units, self.CHANNEL_UNITS_SIZE)

        ld_channel = ldChan(None, meta_ptr, prev_meta_ptr, next_meta_ptr, data_ptr, data_len, \
            data_type, freq, shift, multiplier, scale, decimals, name, "", units)

        # Add in the channel data, converted in a single pass
        ld_channel._data = np.frombuffer(log_channel.values, dtype=np.float64).astype(data_type)

        # Add the ld channel and advance the file pointers
        self.ld_channels.append(ld_channel)

    def add_all_channels(self, data_log):
        """ Adds all channels from a DataLog to the motec log.

        data_log: data_log.DataLog
        """
        for channel_name, channel in data_log.channels.items():
            self.add_channel(channel)

    def write(self, filename):
        """ Writes the motec log data to disc. """
        # Check for the presence of any channels, since the ldData write() method doesn't
        # gracefully handle zero channels
        if self.ld_channels:
            ld_data = ldData(self.ld_header, self.ld_channels)

            # Need to zero out the final channel pointer
            ld_data.channs[-1].next_meta_ptr = 0

            ld_data.write(filename)
        else:
            with open(filename, "wb") as f:
                self.ld_header.write(f, 0)
