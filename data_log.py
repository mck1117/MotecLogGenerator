import array
import cantools
import csv
import itertools
import math
import numpy as np

class DataLog(object):
    """ Container for storing log data which contains a set of channels with time series data."""
    def __init__(self, name=""):
        self.name = name
        self.channels = {}

        # Any key/value metadata found in the log preamble (e.g. driver, vehicle, venue). Values are
        # strings, or lists of strings for entries with more than one value.
        self.metadata = {}

    def clear(self):
        self.channels = {}
        self.metadata = {}

    def add_channel(self, name, units, data_type, decimals):
        self.channels[name] = Channel(name, units, data_type, decimals)

    def start(self):
        """ Returns the earliest timestamp from all existing channels [s]. """
        t = math.inf
        for name, channel in self.channels.items():
            t = min(t, channel.start())

        if t != math.inf:
            return t
        else:
            return 0.0

    def end(self):
        """ Returns the latest timestamp from all existing channels [s]. """
        end = 0
        for name, channel in self.channels.items():
            end = max(end, channel.end())

        return end

    def duration(self):
        """ Returns the duration of the log [s]. """
        return self.end() - self.start()

    def resample(self, frequency):
        """ Resamples all channels such that all messages occur at a fixed frequency.

        See the resample method of the Channel class for more details.
        """
        start = self.start()
        end = self.end()
        for channel_name in self.channels:
            self.channels[channel_name].resample(start, end, frequency)

    def from_can_log(self, log_lines, can_db):
        """ Creates channels populated with messages from a candump file and can database.

        This will create a channel for each entry in the database that has messages present in the
        log.

        log_lines: Iterable, containing candump log lines (recorded with 'candump' with '-l')
        can_db: cantools.database
        """
        self.clear()

        # Cache all the frame ids in the database for quick lookups
        known_ids = set()
        for msg in can_db.messages:
            known_ids.add(msg.frame_id)

        for line in log_lines:
            stamp, bus, id, data = self.__parse_can_log_line(line)

            if id not in known_ids:
                continue

            db_msg = can_db.get_message_by_frame_id(id)
            msg_decoded = can_db.decode_message(id, data)

            for msg, signal in zip(msg_decoded.items(), db_msg.signals):
                name = msg[0]
                value = msg[1]

                if name not in self.channels:
                    self.add_channel(name, signal.unit, float, 3)

                self.channels[name].append(stamp, value)

    def from_csv_log(self, log_lines):
        """ Creates channels populated with messages from a CSV log file.

        This will create a channel for each column in the CSV file, with the name of that channel
        taken from the CSV header. Any non numeric data will be ignored, and that channel will be
        removed. The first column of data must be time.

        Fields may be separated by commas, tabs, or semicolons, and may be quoted. Rows which do
        not contain a numeric timestamp are skipped. Channels do not need a value in every row, an
        empty field simply means that channel was not updated at that timestamp.

        The log may optionally be preceded by a preamble of "key","value" metadata rows (as
        produced by AiM data loggers), which will be stored in self.metadata. A units row directly
        below the header row is also optional, and will be used to assign units to each channel.
        Otherwise channels are created without any units.

        log_lines: Iterable, containing CSV log lines. This may be an open file, the rows are
        processed as they are read so that large logs do not need to be held in memory.
        """
        self.clear()

        # Peek at the beginning of the file to work out the delimiter, then put those lines back
        lines = iter(log_lines)
        first_lines = list(itertools.islice(lines, 10))
        if not first_lines:
            return

        delimiter = self.__detect_delimiter(first_lines)
        rows = csv.reader(itertools.chain(first_lines, lines), delimiter=delimiter)

        # Everything above the data is read up front so we can work out which rows hold the
        # metadata, header, and units. Blank rows are kept for now as they separate the sections of
        # the file. The data itself is far too large to hold like this, so it is streamed below.
        preamble = []
        first_data_row = None
        for row in rows:
            row = [field.strip() for field in row]

            # The data begins at the first row with a numeric value in the time column
            if len(row) > 1 and self.__is_float(row[0]):
                first_data_row = row
                break

            preamble.append(row)

        # Without a header row, or without any data, there is nothing we can extract
        if not first_data_row or not preamble:
            return

        num_columns = len(first_data_row)

        # The header is the last row above the data, ignoring any blank rows in between. If it is
        # preceded by another row of the same width, without a blank row separating the two, then
        # that first row is the header and the one below it holds the units.
        header_index = len(preamble) - 1
        while header_index >= 0 and not any(preamble[header_index]):
            header_index -= 1

        if header_index < 0:
            return

        units_index = None
        if header_index >= 1 and len(preamble[header_index]) == num_columns \
            and len(preamble[header_index - 1]) == num_columns \
            and any(preamble[header_index - 1]):
            units_index = header_index
            header_index -= 1

        # Everything above the header is "key","value" metadata
        for row in preamble[:header_index]:
            if len(row) >= 2 and row[0]:
                self.metadata[row[0]] = row[1] if len(row) == 2 else row[1:]

        # Get the channel names and units, ignoring the first column as it is assumed to be time
        channel_names = preamble[header_index][1:]
        if units_index is not None:
            channel_units = preamble[units_index][1:]
        else:
            channel_units = []

        # Keep the channel for each column so that rows can be processed without any name lookups.
        # The time column holds no channel, and neither do any columns removed while parsing.
        columns = [None]
        for i, name in enumerate(channel_names):
            if name in self.channels:
                print("WARNING: Found more than one column named %s, ignoring the later one" % name)
                columns.append(None)
                continue

            self.add_channel(name, channel_units[i] if i < len(channel_units) else "", float, 0)
            columns.append(self.channels[name])

        # Go through each row grabbing all the channel values
        for values in itertools.chain([first_data_row], rows):
            # Timestamp is the first element. Skip blank rows and any trailing non data rows
            # (summary lines, etc)
            try:
                t = float(values[0])
            except (ValueError, IndexError):
                continue

            # Grab each remaining channel value. If we fail to read an entry in any column, we will
            # delete that channel entirely.
            invalid_channels = []
            for channel, val_text in zip(columns, values):
                # Channels are not required to have a value in every row, sparsely populated logs
                # only record a value when the channel is updated
                if channel is None or not val_text:
                    continue

                # We'll only parse numeric data
                try:
                    value = float(val_text)
                except ValueError:
                    # Fields holding nothing but whitespace are simply empty
                    if not val_text.strip():
                        continue

                    print("WARNING: Found non numeric values for channel %s, removing channel" % \
                        channel.name)
                    invalid_channels.append(channel)
                    continue

                channel.append(t, value)

                point = val_text.rfind(".")
                if point >= 0:
                    decimals_present = len(val_text) - point - 1
                    if decimals_present > channel.decimals:
                        channel.decimals = decimals_present

            for channel in invalid_channels:
                columns = [None if c is channel else c for c in columns]
                del self.channels[channel.name]

        # Any channel which never received a value holds no data worth converting
        empty_channels = [name for name, channel in self.channels.items() if not len(channel)]
        for name in empty_channels:
            print("WARNING: Found no values for channel %s, removing channel" % name)
            del self.channels[name]

    def from_accessport_log(self, log_lines):
        """ Creates channels populated with messages from a COBB Accessport CSV log file.

        This will create a channel for each column in the CSV file, with the name and units of that
        channel taken from the CSV header. Any non numeric data will be ignored, and that channel
        will be removed.

        log_lines: Iterable, containing CSV log lines
        """

        self.from_csv_log(log_lines)

        # Accessport logs have a column for AP info which is not of any value so we'll delete it
        for key in self.channels.keys():
            if "AP Info" in key:
                del self.channels[key]
                break

        # Update all the channel names and units
        for channel_name, channel in self.channels.items():
            # Channels have the format "Name (Units)"
            print(channel_name)
            name, units = channel_name.split(" (")
            units = units[:-1]

            channel.name = name
            channel.units = units

    @staticmethod
    def __detect_delimiter(log_lines):
        """ Returns the field delimiter used by a delimited text log.

        The delimiter is taken to be whichever candidate appears most often in the given lines from
        the start of the file, defaulting to a comma when none of them are present.
        """
        delimiters = [",", "\t", ";"]
        counts = dict.fromkeys(delimiters, 0)

        for line in log_lines:
            for delimiter in delimiters:
                counts[delimiter] += line.count(delimiter)

        delimiter = max(delimiters, key=lambda d: counts[d])

        return delimiter if counts[delimiter] else ","

    @staticmethod
    def __is_float(text):
        """ Returns True if the text can be parsed as a number. """
        try:
            float(text)
            return True
        except ValueError:
            return False

    @staticmethod
    def __parse_can_log_line(line):
        """ Extracts the timestamp, bus, arbitration id, and data from a single line in a can log file
        recorded with candump -l.
        """
        stamp, bus, msg = line.split()
        stamp = float(stamp[1:-1])
        id, data = msg.split("#")
        id = int(id, 16)
        data = bytearray.fromhex(data)

        return stamp, bus, id, data

    def __str__(self):
        output = "Log: %s, Duration: %f s" % (self.name, (self.end() - self.start()))
        for channel_name, channel_data in self.channels.items():
            output += "\n\t%s" % channel_data
        return output

class Channel(object):
    """ Represents a singe channel of data containing a time series of values.

    The time series is held as a pair of arrays rather than a list of objects, as logs can easily
    contain tens of millions of messages.
    """
    def __init__(self, name, units, data_type, decimals):
        self.name = str(name)
        self.units = str(units)
        self.data_type = data_type
        self.decimals = decimals
        self.timestamps = array.array("d")
        self.values = array.array("d")

    def append(self, timestamp, value):
        """ Adds a single message to the end of the time series. """
        self.timestamps.append(timestamp)
        self.values.append(value)

    def __len__(self):
        return len(self.values)

    def start(self):
        if self.timestamps:
            return self.timestamps[0]
        else:
            return 0

    def end(self):
        if self.timestamps:
            return self.timestamps[-1]
        else:
            return 0

    def avg_frequency(self):
        """ Computes the average frequency from the samples based on the duration of the channel
        and the number of messages"""
        if len(self) >= 2:
            dt = self.end() - self.start()
            return len(self) / dt
        else:
            return 0

    def resample(self, start_time, end_time, frequency):
        """ Resamples the data such that all messages occur at a fixed frequency.

        If multiple messages fall within the time interval between messages for the new frequency,
        the latest message will be used. When no existing messages fall within the time interval
        the most recent value will be retained. If no existing message is present within the first
        new time interval, then the first message will be initialized at 0.
        """
        if not self.values:
            return

        # Determine how many messages this channel should have,
        num_msgs = math.floor(frequency * (end_time - start_time))
        dt_step = 1.0 / frequency

        # Each new sample takes the value of the latest existing message which falls within its
        # time window, holding the previous value when there is no such message. Searching for all
        # of those messages at once keeps this to a handful of passes over the data.
        if num_msgs < 1:
            return

        timestamps = np.frombuffer(self.timestamps, dtype=np.float64)
        values = np.frombuffer(self.values, dtype=np.float64)

        # The new sample times are accumulated one step at a time, so that a sample which lands
        # exactly on the edge of a time window falls on the same side of it every time
        steps = np.full(num_msgs, dt_step)
        steps[0] = start_time
        new_timestamps = np.cumsum(steps)

        indices = np.searchsorted(timestamps, new_timestamps + 0.5 * dt_step) - 1

        # Samples before the first message have no value yet, they are initialized at 0
        new_values = np.where(indices >= 0, values[indices.clip(0)], 0.0)

        self.timestamps = array.array("d", new_timestamps)
        self.values = array.array("d", new_values)

    def __str__(self):
        return "Channel: %s, Units: %s, Decimals: %d, Messages: %d, Frequency: %.2f Hz" % \
        (self.name, self.units, self.decimals, len(self), self.avg_frequency())
