'''
Functions for building schema.Schema's
'''
import re
import functools

import attr
import schema


def list_or_tuple_of(sub_schema):
    'validates either a list or tuple of sub_schemas'
    return schema.Or((sub_schema,), [sub_schema])


def as_type(sub_schema, typ):
    'after checking sub_schema, pass the result to typ()'
    return schema.And(sub_schema, schema.Use(typ))


as_tuple = functools.partial(as_type, typ=tuple)


def none_or(sub_schema):
    'allow None or sub_schema'
    return schema.Or(None, sub_schema)
# putting None first gives better error messages;
# schema reports the last failure in an Or(), None
# failing to match some structure is trivial / pointless to report
# the reason the structure didn't match 'v' may be useful


def in_range(sub_schema, _min, _max):
    'check that sub_schema is between _min and _max'
    return schema.And(sub_schema, lambda val: _min < val < _max)


def positive(sub_schema):
    'check that sub_schema is >0'
    return schema.And(sub_schema, lambda val: val > 0)


posint = positive(int)


def default_if_none(sub_schema, default_factory):
    'Coerce Nones to a default value.'
    return schema.Or(
        schema.And(None, schema.Use(lambda a: default_factory())),
        sub_schema)


def nullable_list(iterable):
    'convenient for YAML where None is often symantically empty'
    return default_if_none(list(iterable), list)


def nullable_dict(items, **kwargs):
    'convenient for YAML where None is often symantically empty'
    return default_if_none(dict(items, **kwargs), dict)


_UNSET = object()


@attr.s
class AttrSchema(object):
    schema = attr.ib(convert=schema.Schema)
    default = attr.ib(_UNSET)

    def __call__(self, value):
        if value == self.default:
            return value
        return self.schema.validate(value)


def schema_attrib(scheme, default=_UNSET):
    if default is _UNSET:
        return attr.ib(convert=AttrSchema(scheme))
    return attr.ib(convert=AttrSchema(scheme, default), default=default)


def test_schema_attrib():
    @attr.s
    class Child(object):
        nums = schema_attrib([int])

    @attr.s
    class Parent(object):
        children = schema_attrib([schema.Use(lambda d: d if isinstance(d, Child) else Child(**d))])

    p = Parent([Child([1,2,3])])
    assert Parent(**attr.asdict(p)) == p
    
    @attr.s(slots=True)
    class cons(object):
        cell = attr.ib()
        cdr = schema_attrib(lambda v: isinstance(v, cons), default=None)

    cons(1, cons(2))


def _parse_hosts(hosts_string):
    '''
    Parse a host-string and return a list of hosts:
    host-prefix[range],host-prefix[range], ...
    some examples:
    'memcached[009-011]' ->
        ['memcached009', 'memcached010', 'memcached011']
    '10.10.1.[250-252]' ->
        ['10.10.1.250', '10.10.1.251', '10.10.1.252']
    'db001,redis[01-02],10.10.1.2' ->
        ['db001', 'redis01', 'redis02', '10.10.1.2']
    '''
    host_range_regex = r'(?P<host>[\w\.\-]*)(?P<range>\[\d+\-\d+\])?'
    all_hosts = []
    for host_range in hosts_string.split(','):
        match = re.match(host_range_regex, host_range)
        if not match:
            raise ValueError('vip host {} invalid'.format(host_range))
        host, rng = match.groups()
        if rng is None:
            all_hosts.append(host)
            continue
        # range is expected to be something like "001-020"
        rng = rng[1:-1]  # slice off [, and ]
        rng = rng.split('-')
        if len(rng[0]) != len(rng[1]):
            raise ValueError('host {} range invalid {}'.format(host_range, rng))
        width = len(rng[0])
        start = int(rng[0].lstrip('0'))
        stop = int(rng[1].lstrip('0'))
        for i in range(start, stop + 1):
            all_hosts.append(host + "{{:0{}d}}".format(width).format(i))
    return all_hosts


hosts = schema.And(str, schema.Use(_parse_hosts))


def _parse_endpoints(endpoints_str):
    '''
    Similar to hosts-string, except allow :[port-num] suffix.
    '''
    all_endpoints = []
    for endpoints_range in endpoints_str.split(','):
        if ':' in endpoints_range:
            host_range, port = endpoints_range.split(':', 1)
            if not 0 < int(port) < 2**16:
                raise ValueError('port not valid: {}'.format(port))
            for host in _parse_hosts(host_range):
                all_endpoints.append(host + ':' + port)
        else:
            host_range = endpoints_range
            all_endpoints.extend(_parse_hosts(host_range))
    return all_endpoints


endpoints = schema.And(str, schema.Use(_parse_endpoints))


def test_host_parsing():
    assert len(endpoints.validate('a[001-010]')) is 10
    def powerset(items):  # power set without empty set
        from itertools import chain as ch, combinations as co
        return ch.from_iterable(
            co(items, r) for r in range(1, len(items) + 1))
    test_items = ['foo', '[001-010]', ':123']
    for test_str in powerset(test_items):
        endpoints.validate(''.join(test_str))
        endpoints.validate(''.join(test_str) + ',bar')


def opt_up(schema_dict):
    '''
    For a schema dictionary, extract optional defaults upwards recursively across dictionaries.
    The input will be something like:
    {
        schema.Optional('foo'): {
            schema.Optional('bar', default='baz'): str
        }
    }

    And the output will be something like:
    {
        schema.Optional('foo', default={'bar': 'baz'}): {
            schema.Optional('bar', default='baz'): str
        }
    }
    '''
    schema_dict = dict(schema_dict)  # return a copy don't mutate
    for key, val in list(schema_dict.items()):
        if not isinstance(key, schema.Optional):
            continue
        if not isinstance(val, dict):
            continue
        if hasattr(key, "default"):
            continue
        val = opt_up(schema_dict.pop(key))
        default = {}
        for subkey in val.keys():
            if not isinstance(subkey, schema.Optional):
                continue
            if not hasattr(subkey, 'default'):
                continue
            # NOTE: schema.Optional enforces that instances
            # with defaults must have constant keys
            default[subkey.key] = subkey.default
        schema_dict[schema.Optional(key._schema, default=default)] = val
    return schema_dict


def test_opt_up():
    assert schema.Schema(opt_up({
        schema.Optional('foo'): {
                schema.Optional('foo'): {
                    schema.Optional('foo'): {
                            schema.Optional('bar', default='baz'): str,
                        },
                        schema.Optional('bar', default='baz'): str,
                    },
                }
        })).validate({}) == {'foo': {'foo': {'foo': {'bar': 'baz'}, 'bar': 'baz'}}}
