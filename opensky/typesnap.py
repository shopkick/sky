'''
Dependency-Injection lite.

The input is key-values of constructors, the output
is key-values of constructed values.

Keys in the arguments to this functions are distributed
to the arguments as values are constructed.

IDIOMS:

To pass a constant value:  keys which are not callable
will be left as-is.

To pass a callable as a value:  simply wrap it in a lambda.

typesnap.snap(thread=threading.Thread, target=callback)
# BAD -- typesnap will attempt to "construct" the target
callback since it is a callable.

typesnap.snap(thread=threading.Thread, target=lambda: callback)
# GOOD -- typesnap sees target as a no argument constructor

To modify arguments:  simply use an in-line lambda.

typesnap.snap(sock=lambda host, port: socket.socket((host, port)))
'''
from __future__ import absolute_import

import inspect
import functools


def snap(builders, targets=None):
    '''
    builders: {str: callables-or-constants}
    targets: [str], items from builders that need to be constructed
    '''
    if targets is None:
        targets = builders.keys()
    return _snap_targets(builders, targets)


def lazy_snap(builders, targets, lazy_targets):
    '''
    builders: {str: callables-or-constants}
    targets: [str], items from builders that need to be constructed
    lazy_targets: [str], items from builders to generate build_{name}
                functions for
    '''
    builder = Builder(builders)
    built = {}
    for name in targets:
        built[name] = builder.build(name)
    for name in lazy_targets:
        build_name = "build_" + name
        if build_name in built:
            raise ValueError('name conflict: ' + build_name)
        built[build_name] = functools.partial(builder.build, name)
    return built


class Builder(object):
    '''
    build_map: {str: callables-or-constants}
    meta_var: if set, the name under which the builder itself is available

    Allow for iterative building with cache.
    '''
    def __init__(self, build_map, meta_var="_builder"):
        self.build_area = dict(build_map)
        if meta_var in self.build_area:
            raise ValueError("name conflict: %r" % meta_var)
        self.build_area[meta_var] = self

    def build(self, target):
        built = snap(self.build_area, targets=[target])
        self.build_area.update(built)
        return self.build_area[target]


class SnapError(ValueError): pass
class CircularDependency(SnapError): pass
class MissingArg(SnapError): pass


def _snap_targets(named, targets):
    inited = {}
    def check(name, sofar):
        if name in inited:
            return
        item = named[name]
        if not callable(item):
            inited[name] = item
            return
        if item in sofar:
            raise CircularDependency(
                'circular dependency: ' + '->'.join(
                    repr(e) for e in (sofar + (item,))))
        args, args_with_defaults = _get_required_and_optional_args(item)
        init_kwargs = {}
        for arg_name in args:
            if arg_name not in named:
                if arg_name in args_with_defaults:
                    continue  # ok to skip, it has a default
                raise MissingArg(
                    'attribute of {0!r} could not be satisfied: {1!r}'.format(
                        item, arg_name))
            check(arg_name, sofar + (item,))
            init_kwargs[arg_name] = inited[arg_name]
        inited[name] = item(**init_kwargs)
    for name in targets:
        check(name, ())
    return inited


def _get_required_and_optional_args(to_call):
    try:
        if isinstance(to_call, type):
            args, _, _, defaults = inspect.getargspec(to_call.__init__)
            args = args[1:]  # slice off self
        else:
            args, _, _, defaults = inspect.getargspec(to_call)
            if inspect.ismethod(to_call):
                args = args[1:]  # slice off self / cls / etc
    except TypeError:  # e.g. built-in or object
        args, defaults = (), None
    if not defaults:
        return args, set()
    return args, set(args[-len(defaults):])
