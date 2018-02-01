import sys
import inspect
import traceback
import re


def format_traceback():
    etype, value, tb = sys.exc_info()
    items = traceback.format_exception(etype, value, tb)
    ritems = items[::-1]
    returns = [ritems.pop()]
    while tb:  # put args next to function line
        segments = ritems.pop().split('\n')
        segments[0] += _format_frame_args(tb.tb_frame)
        returns.append('\n'.join(segments))
        tb = tb.tb_next
    returns.extend(ritems[::-1])
    return ''.join(returns)

def _format_frame_args(frame):
    if inspect.isgenerator(frame.f_locals.get('.0')):
        return repr(frame.f_locals['.0'])
    maxlen = 40
    def _format_value(value):
        s = re.sub('<(.+?) at .*?>', lambda m: '<%s>' % m.group(1), repr(value))
        if len(s) > maxlen:
            s = s[:maxlen/2] + '...' + s[-maxlen/2:]
        return '=' + s
    return inspect.formatargvalues(
        *inspect.getargvalues(frame), formatvalue=_format_value)
