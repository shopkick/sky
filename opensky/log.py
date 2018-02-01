# -*- coding: utf-8 -*-

import os
import sys

from lithoxyl import (Logger,
                      StreamEmitter,
                      SensibleSink,
                      SensibleFilter,
                      SensibleFormatter)

from lithoxyl.sinks import DevDebugSink

fmt = ('{status_char}+{import_delta_s:.3f}'
       ' - {duration_s:>8.3f}s'
       ' - {parent_depth_indent}{event_message}')

begin_fmt = ('{status_char}+{import_delta_s:.3f}'
             ' -------------'
             ' {parent_depth_indent}{event_message}')

comment_fmt = ('{status_char} - {iso_begin} - {event_message}')


sky_log = Logger('sky')
sky_log.log_file_path = None


def _on_import_log_setup():
    # called at the bottom of the module
    stderr_fmtr = CompactFormatter()
    stderr_emtr = StreamEmitter('stderr', sep='')
    stderr_filter = SensibleFilter(begin='info',
                                   success='info',
                                   failure='info',
                                   exception='debug')
    stderr_sink = SensibleSink(formatter=stderr_fmtr,
                               emitter=stderr_emtr,
                               filters=[stderr_filter])
    sky_log.add_sink(stderr_sink)
    sky_log.add_sink(DevDebugSink(post_mortem=bool(os.getenv('SKY_DEBUG'))))
    sky_log.debug('sky_log_initialization').success()
    return


def build_file_enabled_logger(log_file):
    # TODO: make idempotent
    file_emtr = StreamEmitter(log_file)
    file_fmtr = SensibleFormatter(fmt,
                                  comment=comment_fmt,
                                  begin=begin_fmt)
    file_filter = SensibleFilter(begin='debug',
                                 success='debug',
                                 failure='debug',
                                 exception='debug')
    file_sink = SensibleSink(formatter=file_fmtr,
                             emitter=file_emtr,
                             filters=[file_filter])
    sky_log.add_sink(file_sink)

    log_file_path = getattr(log_file, 'name', None)
    sky_log.info('sky_log_file_initialization', path='file://' + log_file_path).success()
    sky_log.log_file_path = log_file_path

    return sky_log


class CompactFormatter(object):
    'overwrites begin logs with end logs; a singleton since it messes with stderr'
    def __init__(self):
        self.sensible = SensibleFormatter(fmt, begin=begin_fmt, comment=comment_fmt)
        self.on_blank_line = True
        self.last_print_was_begin = False
        self.last_begin_action_id = None
        self.expecting_write = False
        self.stderr_wrapper = FileWriteInterceptor(
            sys.stderr, self._stderr_write)
        self.stdout_wrapper = FileWriteInterceptor(
            sys.stdout, self._stdout_write)
        self.stderr = sys.stderr
        self.stdout = sys.stdout
        sys.stderr = self.stderr_wrapper
        sys.stdout = self.stdout_wrapper

    def on_begin(self, begin_event):
        line = self.sensible.on_begin(begin_event)
        if not self.on_blank_line:
            line = '\n' + line
        self.last_print_was_begin = True
        self.last_begin_action_id = begin_event.action_id
        self.on_blank_line = False
        self.expecting_write = True
        return line

    def on_comment(self, comment_event):
        line = self.sensible.on_comment(comment_event)
        if not self.on_blank_line:
            line = '\n' + line
        self.last_print_was_begin = True
        self.last_begin_action_id = comment_event.action_id
        self.on_blank_line = False
        self.expecting_write = True
        return line


    def on_end(self, end_event):
        line = self.sensible.on_end(end_event)
        if not self.on_blank_line:
            if (self.last_print_was_begin and
                    end_event.action_id == self.last_begin_action_id):
                line = '\r' + line
            else:
                line = '\n' + line
        line = line + '\n'
        self.last_print_was_begin = False
        self.on_blank_line = True
        self.expecting_write = True
        return line

    def _stderr_write(self, msg):
        if self.expecting_write:
            self.expecting_write = False
        else:
            self._flush()
        self.stderr.write(msg)

    def _stdout_write(self, msg):
        self._flush()
        self.stdout.write(msg)

    def _flush(self):
        'reset output, give up on \\r fanciness'
        if not self.on_blank_line:
            self.stderr.write('\n')
            self.on_blank_line = True
        self.last_print_was_begin = False


class FileWriteInterceptor(object):
    'wrap a file'
    def __init__(self, file, on_write):
        self.file = file
        self.on_write = on_write

    def write(self, msg):
        self.on_write(msg)

    def __getattr__(self, name):
        return getattr(self.file, name)


_on_import_log_setup()
