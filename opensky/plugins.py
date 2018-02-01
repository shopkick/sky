# Copyright (c) Shopkick 2017
# See LICENSE for details.
import attr
import gather


def register_config(path, type, optional=True):
    '''
    Decorate a function which returns something compatible with schema.Schema

    (If you don't want to use the schema library, return a python object
    with a validate() function that returns the validated output.)
    '''
    def deco(func):
        func.sky_plugin = ConfigPlugin(func, path, type, optional)
        _CONFIG.register(path)(func)
        return func
    return deco


ConfigPlugin = attr.make_class(
    'ConfigPlugin', ['make', 'path', 'type', 'optional'])


def register_command(
    name=None, help=None, requires=None, maybe_requires=None,
    overrides=None):
    '''
    Register an extension command with the given name
    and help string.
    '''
    if isinstance(requires, basestring):
        requires = (requires,)  # just handle a super common typo
    if isinstance(maybe_requires, basestring):
        maybe_requires = (maybe_requires,)
    def deco(func):
        func.sky_plugin = CommandPlugin(
            func, name, help, requires, maybe_requires, overrides)
        _COMMANDS.register(name or func.__name__)(func)
        return func
    return deco



CommandPlugin = attr.make_class(
    'CommandPlugin',
    ['func', 'name', 'help', 'requires', 'maybe_requires', 'overrides'])


def register_site_config(name):
    '''
    Add a new sub-section of site config under the given name.

    This should be used to decorate a function that returns a validator
    for all the data under the given name in site-config.
    '''
    def deco(func):
        func.sky_plugin = SiteConfigPlugin(name)
        _SITE_CONFIG.register(name)(func)
        return func
    return deco


SiteConfigPlugin = attr.make_class(
    'SiteConfigPlugin', ['name'])

_COMMANDS = gather.Collector('Commands', depth=2)
_CONFIG = gather.Collector('Config', depth=2)
_SITE_CONFIG = gather.Collector('SiteConfig', depth=2)
