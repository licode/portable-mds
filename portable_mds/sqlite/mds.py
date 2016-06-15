import os
import sqlite3
import re
from collections import defaultdict
from contextlib import contextmanager
from ..mongoquery.mds import JSONCollection
from ..template.mds import MDSTemplate, MDSROTemplate
from ..template.core import ASCENDING, DESCENDING


LIST_TABLES = "SELECT name FROM sqlite_master WHERE type='table';"
CREATE_TABLE = "CREATE TABLE %s "
INSERT = "INSERT INTO ? VALUES "  # the rest is generated by qmarks func below
SELECT_EVENT_STREAM = "SELECT * FROM %s "


@contextmanager
def cursor(connection):
    """
    a context manager for a sqlite cursor

    Example
    -------
    >>> with cursor(conn) as c:
    ...     c.execute(query)
    """
    c = connection.cursor()
    yield c
    c.close()


def qmarks(num):
    "Generate string like (?, ?, ?)"
    return '(' + '?, ' * (num - 1) + '?)'


class RunStartCollection(JSONCollection):
    def __init__(self, event_col, *args, **kwargs):
        self._event_col = event_col
        super().__init__(*args, **kwargs)

    def insert_one(self, doc):
        self._event_col.new_runstart(doc)
        super().insert_one(doc)


class DescriptorCollection(JSONCollection):
    def __init__(self, event_col, *args, **kwargs):
        self._event_col = event_col
        super().__init__(*args, **kwargs)

    def insert_one(self, doc):
        self._event_col.new_descriptor(doc)
        super().insert_one(doc)


class EventCollection(object):
    def __init__(self, dirpath):
        self._runstarts = {}
        self._descriptors = {}
        self._dirpath = dirpath
        self.reconnect()

    def reconnect(self):
        for fn in os.listdir(self._dirpath):
            # Cache connections to every sqlite file.
            match = re.match('([a-z-]+)\.sqlite', fn)
            if match is None:
                # skip unrecognized file
                continue
            uid, = match
            fp = os.path.join(self._dirpath, fn)
            conn = sqlite3.connect(fp)
            # Return rows as objects that support getitem.
            conn.row_factory = sqlite3.Row
            self._runstarts[uid] = conn

            # Build a mapping of descriptor uids to run start uids.
            with cursor(self._run_starts[uid]) as c:
                c.execute(LIST_TABLES)
                for descriptor_uid in self._cur.fetchall():
                    self._descriptors[descriptor_uid] = uid

    def new_runstart(self, doc):
        uid = doc['uid']
        fp = os.path.join(self._dirpath, '{}.sqlite'.format(uid))
        conn = sqlite3.connect(fp)
        conn.row_factory = sqlite3.Row
        self._runstarts[uid] = conn

    @classmethod
    def columns(cls, keys):
        sorted_keys = list(sorted(keys))
        safe_keys = [key.replace('-', '_') for key in sorted_keys]
        columns = tuple(['uid', 'seq_num', 'time'] +
                        ['data_' + key for key in safe_keys] +
                        ['timestamps_' + key for key in safe_keys])
        return columns

    def new_descriptor(self, doc):
        uid = doc['uid']
        table_name = 'desc_' + uid.replace('-', '_')
        run_start_uid = doc['run_start']
        columns = self.columns(doc['data_keys'])
        with cursor(self._runstarts[run_start_uid]) as c:
            c.execute(CREATE_TABLE % table_name
                      + '(' + ','.join(columns) + ')')
        self._descriptors[uid] = run_start_uid

    def find(self, query, sort=None):
        if list(query.keys()) != ['descriptor']:
            raise NotImplementedError("Only queries based on descriptor uid "
                                      "are supported.")
        desc_uid = query['descriptor']
        table_name = 'desc_' + desc_uid.replace('-', '_')
        with cursor(self._runstarts[self._descriptors[desc_uid]]) as c:
            c.execute(SELECT_EVENT_STREAM % table_name)
            raw = c.fetchall()
        rows_as_dicts = [dict(row) for row in raw]
        events = []
        for row in rows_as_dicts:
            event = {}
            event['uid'] = row.pop('uid')
            event['seq_num'] = row.pop('seq_num')
            event['time'] = row.pop('time')
            event['data'] = {}
            event['timestamps'] = {}
            for k, v in row.items():
                if k.startswith('data_'):
                    new_key = k[len('data_'):]
                    event['data'][new_key] = v
                else:
                    new_key = k[len('timestamps_'):]
                    event['timestamps'][new_key] = v
            events.append(event)
        return (ev for ev in events)

    def find_one(self, query):
        # not used on event_col
        raise NotImplementedError()

    def insert_one(self, doc):
        desc_uid = doc['descriptor']
        table_name = 'desc_' + desc_uid.replace('-', '_')
        # Get list of keys and list of values in consistent order.
        keys = []
        values = []
        for k, v in doc.items():
            keys.append(k)
            values.append(v)
        keys = ['uid', 'seq_num', 'time'] + keys
        values = tuple([doc['uid']], [doc['seq_num']] + [doc['time']]
                        + doc['data'] + doc['timestamps'])
        with cursor(self._runstarts[self._descriptors[desc_uid]]) as c:
            c.execute(("INSERT INTO %s (%s)" % (table_name, ','.join(keys)))
                       + qmarks(len(values)), values)

    def insert(self, docs):
        values = defaultdict(list)
        ordered_keys = {}
        columns = {}
        for doc in docs:
            # Stash an arbitrary but consistent order for the keys.
            uid = doc['descriptor']
            if uid not in ordered_keys:
                ordered_keys[uid] = sorted(doc['data'])
                columns[uid] = self.columns(doc['data'])

            value = tuple([doc['uid']] + [doc['seq_num']] + [doc['time']] + 
                          [doc['data'][k] for k in ordered_keys[uid]] +
                          [doc['timestamps'][k] for k in ordered_keys[uid]])
            values[uid].append(value)
        for desc_uid in values:
            table_name = 'desc_' + desc_uid.replace('-', '_')
            cols = columns[desc_uid]
            with cursor(self._runstarts[self._descriptors[desc_uid]]) as c:
                c.executemany(("INSERT INTO %s (%s) VALUES " %
                               (table_name, ','.join(cols)))
                              + qmarks(len(cols)), values[desc_uid])


class _CollectionMixin(object):
    def __init__(self, *args, **kwargs):
        self._config = None
        super().__init__(*args, **kwargs)
        self.__event_col = None
        self.__descriptor_col = None
        self.__runstart_col = None
        self.__runstop_col = None

    @property
    def config(self):
        return self._config

    @config.setter
    def config(self, val):
        self._config = val
        self.__event_col = None
        self.__descriptor_col = None
        self.__runstart_col = None
        self.__runstop_col = None

    @property
    def _runstart_col(self):
        if self.__runstart_col is None:
            fp = os.path.join(self.config['directory'], 'run_starts.json')
            self.__runstart_col = RunStartCollection(self._event_col, fp)
        return self.__runstart_col

    @property
    def _runstop_col(self):
        if self.__runstop_col is None:
            fp = os.path.join(self.config['directory'], 'run_stops.json')
            self.__runstop_col = JSONCollection(fp)
        return self.__runstop_col

    @property
    def _descriptor_col(self):
        self._event_col
        if self.__descriptor_col is None:
            fp = os.path.join(self.config['directory'],
                              'event_descriptors.json')
            self.__descriptor_col = DescriptorCollection(self._event_col, fp)
        return self.__descriptor_col

    @property
    def _event_col(self):
        if self.__event_col is None:
            self.__event_col = EventCollection(self.config['directory'])
        return self.__event_col


class MDSRO(_CollectionMixin, MDSROTemplate):
    pass


class MDS(_CollectionMixin, MDSTemplate):
    pass