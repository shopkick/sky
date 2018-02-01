# Copyright (c) Shopkick 2017
# See LICENSE for details.
'''Plugin for building a new sky executable from an existing sky. Provides the ability to:

  * Add/remove plugins
  * Embed defaults, such as the default site config and proxy,
  * Embed build metadata (date, library versions, etc.)

Example use:

* time python -m opensky self build --default-site-config http://USER@GITLAB_HOSTNAME/REPO --pypier-repo http://USER@GITLAB_HOSTNAME/PYPIER_REPO --default-proxy "socks5h://localhost:8080" --packages sk_confluent --version 17.6.5 --opensky-package ~/work/opensky

(or replace "python -m opensky" with a built artifact)

TODO: could double up and build a sky-tmp artifact and then use the
      build-command output from *that* to build the final artifact, ensuring
      that the bootstrapping works.

TODO: maybe something like reqs.exit_with_message() when an expected
      class of errors can occur (such as with --update below). Could
      also overload returning (exit_code, exit_message) or something.

'''
import os
import sys
import fnmatch
import argparse
import datetime
import pkg_resources

import attr
from boltons import strutils, ecoutils, fileutils

from opensky import plugins


def get_sky_metadata():
    try:
        import sky_metadata
    except ImportError:
        return None
    else:
        return SkyMetadata.from_dict(sky_metadata.__dict__)


@attr.s(frozen=True)
class SkyMetadata(object):
    '''
    This class represents the state of the current build of
    Sky.  (build date, version, command that built etc)
    '''
    timestamp = attr.ib()
    version = attr.ib()
    command = attr.ib(repr=False, default=())
    default_proxy = attr.ib(default=None)
    default_site_config = attr.ib(default=None)
    pypier_repo = attr.ib(default=None)
    opensky_package = attr.ib(default='opensky')
    packages = attr.ib(default=())  # packages that were specified
    all_packages = attr.ib(repr=False, default=None)  # also packages transitively installed by pip
    builder_profile = attr.ib(repr=False, default=attr.Factory(ecoutils.get_profile))

    @classmethod
    def from_dict(cls, in_dict):
        "Builds an instance from a dictionary, ignoring extra fields."
        kwargs = {}
        for fld in attr.fields(cls):
            arg_val = in_dict.get(fld.name)
            if arg_val is not None:
                kwargs[fld.name] = arg_val
        return cls(**kwargs)

    def items(self):
        ret = []
        for fld in attr.fields(self.__class__):
            ret.append((fld.name, getattr(self, fld.name)))
        return ret


@plugins.register_command(
    'self',
    help='meta operations related to sky itself',
    requires=('logger', 'executor', 'sky_metadata', 'sky_path', 'cache'))
def self_plugin(argv, reqs):
    # Note that all sub-plugin requirements have to be lifted
    prs = get_argparser()

    args = prs.parse_args(argv[1:])

    return args.func(args, reqs)


def get_argparser():
    if sys.platform.startswith('linux'):
        plat = 'linux'
    elif sys.platform == 'darwin':
        plat = 'darwin'
    else:
        raise RuntimeError('sky only supports Linux and MacOS (darwin), not %r'
                           % sys.platform)

    prs = argparse.ArgumentParser(prog='self')
    prs.set_defaults(cur_platform=plat)

    subprs = prs.add_subparsers(dest='cmd')

    # http://bugs.python.org/issue9253
    # http://stackoverflow.com/a/18283730/1599393
    subprs.required = True

    build_prs = subprs.add_parser('build', description='build a new sky')
    _add_build_parse_args(build_prs)

    add_arg = build_prs.add_argument
    # TODO: --packages-requirements : allow the passing of a requirements.txt
    add_arg('--update', action="store_true")
    add_arg('--output-dir', type=str, default=None)
    # add_arg('--extra')  # TODO format for extra metadata
    build_prs.set_defaults(func=self_build)

    build_cmd_prs = subprs.add_parser('build-command',
                                      description='print the command used'
                                      ' to build this version of sky')
    build_cmd_prs.add_argument('--raw', action='store_true')
    build_cmd_prs.set_defaults(func=self_build_command)

    version_prs = subprs.add_parser('version',
                                    description='print build version and timestamp')
    version_prs.add_argument('--all', action='store_true')
    version_prs.set_defaults(func=self_version)

    check_prs = subprs.add_parser('check',
                                  description='perform some sanity checks on sky'
                                  ' and the environment')
    check_prs.set_defaults(func=self_check)

    lspkgs_prs = subprs.add_parser('lspackages',
                                   description='dump a full list of packages'
                                   ' contained in the executable')
    lspkgs_prs.set_defaults(func=self_lspkgs)

    shell_prs = subprs.add_parser('shell',
                                  description='start a python REPL with access to sky internals')
    shell_prs.set_defaults(func=self_shell)

    return prs


def self_shell(args, reqs):
    from ptpython.repl import embed
    embed(__builtins__, locals())


def _add_build_parse_args(parser):
    '''
    Add all of SkyMetadata's attributes as arguments to the
    passed argparser
    '''
    # TODO: ensure no conflicts w/ existing args
    add = parser.add_argument
    for fld in attr.fields(SkyMetadata):
        if fld.name == 'packages':
            add('--packages', nargs='+',
                help='package list to add to the executable (add plugins here)')
        elif fld.name not in ('all_packages',):
            add('--' + fld.name.replace('_', '-'))
    return


def self_lspkgs(args, reqs):
    if not reqs.sky_metadata:
        print
        print('lspackages only works on prebuilt sky executables')
        sys.exit(1)

    for pkg in reqs.sky_metadata.all_packages:
        print(pkg)
    return


def self_check(args, reqs):
    # TODO: expand
    pkg_resources.resource_string(
        'opensky', 'goes_in_docker_image/main.py')
    print 'pkg_resources working'
    reqs.executor.git.version().interactive()
    print '\n'.join(
        reqs.executor.docker.version().batch()[0].splitlines()[:3])
    print 'git and docker working'


def self_build_command(args, reqs):
    if not reqs.sky_metadata:
        print
        print('build-command only works on prebuilt sky executables')
        sys.exit(1)
    build_cmd_list = reqs.sky_metadata.command
    if args.raw:
        print(build_cmd_list)
    else:  # something more human readable and reusable
        #if len(build_cmd_list) > 1 and build_cmd_list[1].endswith('.py'):
        #    # for when sky is built with "python -m opensky"
        #    new_build_cmd = build_cmd_list[2:]
        #else:
        new_build_cmd = build_cmd_list[2:]  # TODO

        if sys.argv[0].endswith('.py') or sys.argv[0].endswith('.pyc'):
            # TODO: this is a very unexpected case
            new_build_cmd = [sys.executable, sys.argv[0]] + new_build_cmd
        else:
            new_build_cmd = [sys.argv[0]] + new_build_cmd
        print(strutils.escape_shell_args(new_build_cmd))


def self_version(args, reqs):
    if not reqs.sky_metadata:
        print
        print('build-command only works on prebuilt sky executables')
        sys.exit(1)

    print('sky v%s (%s)' %
          (reqs.sky_metadata.version, reqs.sky_metadata.timestamp))

    if args.all:
        print
        print('  Dependency versions:')
        for pkg in reqs.sky_metadata.all_packages:
            print('    - ' + pkg.rstrip('.whl'))


def self_build(args, reqs):
    # note that ^ this is parsed args, not argv
    import virtualenv

    cur_info = _args2sky_metadata(args, reqs)
    build_dir = args.output_dir or (reqs.sky_path + '/self-build/')
    build_venv = build_dir + 'build-env'
    wheelhouse = build_dir + 'wheelhouse'

    fileutils.mkdir_p(build_dir)
    virtualenv.create_environment(build_venv)
    in_virtualenv = reqs.executor.in_virtualenv(
        build_venv).patch_env(ALL_PROXY='')

    def build_wheels(*pkgs):
        in_virtualenv.pip.wheel(
            *pkgs, wheel_dir=wheelhouse, **wheel_kwargs).interactive()

    wheel_kwargs = {}
    if cur_info.pypier_repo:
        pypier_dir = reqs.cache.pull_project_git('pypier', cur_info.pypier_repo)
        wheel_kwargs['find_links'] = pypier_dir + '/packages/index.html'

    # build all dependencies + opensky itself
    build_wheels(cur_info.opensky_package, *cur_info.packages)
    all_packages = fnmatch.filter(os.listdir(wheelhouse), '*.whl')
    # create metadata package
    cur_info = attr.evolve(
        cur_info,
        # TODO: sys.executable sucks inside a pex
        command=[sys.executable] + sys.argv,
        all_packages=all_packages)
    sky_metadata_pkg_path = generate_package(cur_info, build_dir)
    # build meta-data package
    build_wheels(sky_metadata_pkg_path)

    top_level_pkgs = ['opensky', 'sky_metadata'] + list(cur_info.packages)

    in_virtualenv.pip.install('pex').batch()

    artifact_path = build_dir + '/sky-' + cur_info.version
    in_virtualenv.command(
        ['pex', '--python-shebang=/usr/bin/env python2.7',
         '--repo', wheelhouse, '--no-index', '--pre',
         '--output-file=' + artifact_path,
         '--disable-cache', '--entry-point', 'opensky'] + top_level_pkgs
        ).redirect()
    sky_size = os.path.getsize(artifact_path)
    sky_human_size = strutils.bytes2human(sky_size, ndigits=2)
    message = ('sky %s (%s) executable successfully saved to: %s'
               % (cur_info.version, sky_human_size, artifact_path))

    return (0, message)


def _args2sky_metadata(args, reqs):
    # TODO: externalize these exits more nicely
    if args.update:
        if not reqs.sky_metadata:
            print
            print('build-command only works on prebuilt sky executables')
            sys.exit(1)
        defaults = attr.asdict(reqs.sky_metadata)
    else:
        if args.version is None:
            raise ValueError('specify --version')
            sys.exit(1)
        defaults = dict(
                timestamp=datetime.datetime.now().isoformat())
    md = dict(defaults)
    md.update([(k, v) for k, v in args.__dict__.items() if v is not None])
    return SkyMetadata.from_dict(md)


'''Note that within a PEX, sys.executable is pretty much useless. We
have to use programmatic interfaces to commands like pip. Even pip
tries to subprocess for wheel operations, meaning that we have to go
around and use wheel programmatically as well.

pip and pex are possible to interact with programmatically. wheel is
so involved that we're forced back to doing virtualenv, and of course
using virtualenv programmatically.

'''


def generate_package(metadata, path):
    '''
    generate a package at path which will freeze out the
    current state of this object;
    intended to be used in combination with new_build:

    old_or_default_meta.new_build().generate_package()

    returns the directory that can be passed to pip
    '''
    path = os.path.join(path, 'sky_metadata')
    fileutils.mkdir_p(path)
    _setup_bytes = _SKY_METADATA_SETUP_TMPL.format(version=metadata.version,
                                                   docstring=_SKY_METADATA_DOC)
    with open(path + '/setup.py', 'wb') as f:
        f.write(_setup_bytes)

    _metadata_bytes = _get_metadata_module_bytes(metadata, _SKY_METADATA_DOC)
    with open(path + '/sky_metadata.py', 'wb') as f:
        f.write(_metadata_bytes)
    return path


def _get_metadata_module_bytes(metadata_obj, docstring):
    lines = ['"""' + docstring + '"""', '', '']
    for k, v in metadata_obj.items():
        lines.append('%s = %r' % (k, v))
    return '\n'.join(lines)


_SKY_METADATA_DOC = """A generated package for distributing metadata used by
non-development versions of sky. Includes versions, timestamps, and
default URLs for a particular site configuration.

Do not modify this directly. Check out the sky "self" subcommands.
"""


_SKY_METADATA_SETUP_TMPL = '''\
"""
{docstring}
"""

from setuptools import setup

setup(
    name="sky_metadata",
    version={version!r},
    author="Sky Devs",
    description="Generated package for sky executables, don't touch.",
    long_description=__doc__,
    py_modules=['sky_metadata'],
    platforms='any')
'''
