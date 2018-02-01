# Copyright (c) Shopkick 2017
# See LICENSE for details.
'''
PyPI Extension Repository commands

Python Package Index in a repo.
'''
import os
import os.path
import sys
import json
import shutil
import argparse
import datetime
import getpass
import socket

import ashes
from boltons import fileutils
import seashore

from opensky import plugins
from opensky.cmd import find_project_dir

PIP_CMDS = ('install', 'download', 'list', 'search')
# TODO: uninstall, unpublish (when a package is obsolete), and maybe
#       gc (compact the git repo a bit)


@plugins.register_command(
    help='pypi in a repo interaction commands',
    requires=('logger', 'cache', 'executor', 'site_config'))
def pypier(args, reqs):
    '''
    only supports pure-python repos for now
    '''
    parser = argparse.ArgumentParser(prog='pypier')
    parser.add_argument('cmd', choices=('config', 'publish', 'pip-index') + PIP_CMDS)
    cmd = parser.parse_args(args[1:2]).cmd
    cache = reqs.cache
    executor = reqs.executor
    site_config = reqs.site_config
    pypier_repo = site_config['pypier']['repo']
    pypier_repo_ro = site_config['pypier']['repo_ro']
    if cmd == 'config':
        print
        print 'PyPIER repos:'
        print '  ', pypier_repo_ro, '(fetch)'
        print '  ', pypier_repo, '(publish)'
    elif cmd == 'publish':
        parser.add_argument('--dry-run', action='store_true')
        arg_vals = parser.parse_args(args[1:])
        setup_dir = find_project_dir(os.getcwd(), 'setup.py')
        pypier_read_write = cache.workon_project_git(
            'pypier', pypier_repo)
        executor.python('setup.py', 'sdist').redirect(cwd=setup_dir)
        # TODO manylinux wheels?  OSX wheels?
        version = executor.python(
            'setup.py', version=None).batch()[0].strip()
        output = [fn for fn in os.listdir(setup_dir + '/dist/')
                  if version in fn]
        name = output[0].split('-', 1)[0]
        # typical artifact: foo-ver.tar.gz
        dst = pypier_read_write.path + '/packages/' + name + '/'
        fileutils.mkdir_p(dst)
        # TODO: instead of just looking for anything in the dist
        # directory, query setup for the version and check for that.
        for result in output:
            if os.path.exists(os.path.join(dst, result)):
                raise EnvironmentError(
                    "{} has already been published".format(result))
        for result in output:
            shutil.copy(setup_dir + '/dist/' + result, dst)
        with fileutils.atomic_save(os.path.join(dst, 'pkg_info.json')) as f:
            pkg_info = get_pkg_info(executor, setup_dir)
            pkg_info_json = json.dumps(pkg_info, indent=2, sort_keys=True)
            f.write(pkg_info_json + '\n')
        update_index(pypier_read_write.path)
        source_metadata = get_source_metadata(executor, setup_dir)
        commit_msg = 'PyPIER publish: {}\n\n{}\n'.format(
            ', '.join(output),
            json.dumps(source_metadata, indent=2, sort_keys=True))
        pypier_read_write.push(commit_msg, dry_run=arg_vals.dry_run)
    elif cmd == 'pip-index':
        pypier_read_only = cache.pull_project_git(
            'pypier', pypier_repo_ro)
        link_path = pypier_read_only + '/packages/index.html'
        print link_path  # NOTE: this print command is the primary purpose
    elif cmd in PIP_CMDS:
        pypier_read_only = cache.pull_project_git('pypier', pypier_repo_ro)
        link_path = pypier_read_only + '/packages/index.html'
        #env = dict(os.environ)
        #env['PIP_FIND_LINKS'] = ' '.join(
        #    [link_path] + env.get('PIP_FIND_LINKS', '').split())
        # TODO: figure out clean way to extend env
        # TODO: remove ALL_PROXY='' once urllib3 + requests
        #       do a release and don't pre-emptively die
        #       on socks5h:// proxy
        executor.patch_env(PIP_FIND_LINKS=link_path, ALL_PROXY='').command(
            ['python', '-m', 'pip'] + args[1:]).redirect(
                stdout=sys.stdout, stderr=sys.stderr)
    else:
        # argparse should catch this above
        raise ValueError('unrecognized sub-command %r' % cmd)


@plugins.register_site_config('pypier')
def pypier_site_config():
    return {
        'repo': str,
        'repo_ro': str
    }


def get_pkg_info(executor, setup_dir):
    ret = {}
    fields = ['name', 'version', 'fullname', 'contact', 'contact-email',
              'url', 'license', 'description', 'provides', 'requires']
    executor = executor.patch_env(TERM='xterm').chdir(setup_dir)
    for field in fields:
        value = executor.python('setup.py', **{field: None}).batch()[0]
        ret[field.replace('-', '_')] = value.strip()
    ret['info_updated'] = datetime.datetime.utcnow().isoformat()
    return ret


def get_source_metadata(executor, source_path):
    """TODO: other candidate info: active virtualenv, whether there are
    modified files (might need to be an error)
    """
    executor = executor.chdir(source_path)
    git_rev_timestamp = executor.git.rev_list(
        'HEAD', format=seashore.Eq('format:%ai'), max_count='1').batch()[0]
    git_rev_timestamp = git_rev_timestamp.strip().splitlines()[-1]
    return {
        'git_rev': executor.git.rev_parse('HEAD').batch()[0].strip(),
        'git_rev_name': executor.git.rev_parse(
            'HEAD', abbrev_ref=None).batch()[0].strip(),
        'git_origin_url': executor.git.config(
            get='remote.origin.url').batch()[0].strip(),
        'git_rev_timestamp': git_rev_timestamp,
        'user': _try_user(),
        'hostname': socket.gethostname(),
    }


def _try_user():
    try:  # TODO: exception type?
        return getpass.getuser()
    except Exception:
        try:
            return os.getuid()
        except Exception:
            return ''


def update_index(pypier_path):
    new_index_bytes, new_readme_bytes = generate_index(pypier_path)
    index_path = pypier_path + '/packages/index.html'
    readme_path = pypier_path + '/packages/README.md'
    with fileutils.atomic_save(index_path) as f:
        f.write(new_index_bytes)
    with fileutils.atomic_save(readme_path) as f:
        f.write(new_readme_bytes)
    return


def generate_index(pypier_path):
    packages = []

    pkgs_path = pypier_path + '/packages'

    for pkg_name in os.listdir(pkgs_path):
        cur_pkg_path = os.path.join(pkgs_path, pkg_name)
        if not os.path.isdir(cur_pkg_path):
            continue
        cur_pkg = {'name': pkg_name}
        pkg_info_path = os.path.join(cur_pkg_path, 'pkg_info.json')
        pkg_info = json.load(open(pkg_info_path))
        if pkg_info['name'] != pkg_name:
            print 'warning: package name/info mismatch for %r: %r' % (pkg_name, pkg_info_path)
        cur_pkg['info'] = pkg_info
        versions = []
        for release_fn in os.listdir(cur_pkg_path):
            if release_fn.split('-')[0] != pkg_name:
                continue
            # splitext doesn't work because .tar.gz
            # always the second item, even with wheels
            version = _strip_pkg_ext(release_fn).split('-')[1]
            versions.append({'path': os.path.join(pkg_name, release_fn),
                             'version': version})
        # TODO: do better than alphabetical sort
        cur_pkg['versions'] = sorted(versions, key=lambda x: x['version'], reverse=True)
        packages.append(cur_pkg)

    ae = ashes.AshesEnv()
    ae.register_source('pkg_idx', INDEX_TMPL)
    ae.register_source('readme', README_TMPL)

    ctx = {'packages': sorted(packages, key=lambda x: x['name']),
           'gen_date': datetime.datetime.utcnow().isoformat()}
    index = ae.render('pkg_idx', ctx)
    readme = ae.render('readme', ctx)
    return index, readme


def _strip_pkg_ext(pkg_filename):
    # accepts file or path
    return pkg_filename.replace('.tar.gz', '').replace('.zip', '').replace('.whl', '')


_ctx = {'packages': [{'name': '', 'versions': [{'path': '', 'version': ''}]}]}


INDEX_TMPL = """\
<!DOCTYPE html>
<html>
  <head>
    <title>PyPIER Index</title>
  </head>
  <body>
    <h1>PyPIER Index</h1>
    <p>Generated at {gen_date}</p>
    {#packages}
    <h3 id="{name}">{name}</h3>
    <p>Code repo: <a href="{info.url}">{info.url}</a>
    <ul>
    {#versions}<li><a href="{path}">{version}</a></li>{/versions}
    </ul>
    {/packages}
  </body>
</html>
"""


README_TMPL = """\
Published Packages and Versions
===============================

{#packages}
[{name}](name)
--------------
*[View code]({info.url})*

{#versions}
* [{version}](name/version)

{/versions}
{/packages}
"""
