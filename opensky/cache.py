# Copyright (c) Shopkick 2017
# See LICENSE for details.
'''
Engine that actually executes the operation.
'''
import os
import os.path
import sys
import datetime
import shutil

import attr
import requests
import seashore
from boltons import fileutils
from boltons.strutils import bytes2human


# ANACONDA_URL = 'https://repo.continuum.io/archive/Anaconda2-4.2.0-Linux-x86_64.sh'
ANACONDA_URL = 'https://repo.continuum.io/miniconda/Miniconda2-latest-Linux-x86_64.sh'
ANACONDA_INSTALLER = ANACONDA_URL.rpartition('/')[2]


class HTTPClient(object):
    def __init__(self):
        self._session = requests.Session()
        self._proxy_address = 'socks5h://localhost:8080'  # TODO
        self._session.proxies = {'http': self._proxy_address}

    def __getattr__(self, attr_name):
        return getattr(self._session, attr_name)

    def urlretrieve(self, url, dest, print_progress=True):
        resp = self._session.get(url, stream=True)
        total_size, total_chunks = 0, 0
        with fileutils.atomic_save(dest) as f:
            content_iter = resp.iter_content(1024)
            while 1:
                size_str = bytes2human(total_size, 1).rjust(7)
                msg = ('%s downloaded from %s\r' % (size_str, url))
                if print_progress and (total_chunks % 20) == 0:
                    sys.stdout.write(msg)
                    sys.stdout.flush()
                try:
                    chunk = next(content_iter)
                except StopIteration:
                    if print_progress:
                        sys.stdout.write('\n')
                    break
                total_size += len(chunk)
                total_chunks += 1
                f.write(chunk)
        return


@attr.s
class DependencyCache(object):
    '''
    Manages directories and files for caching.
    '''
    executor, logger, cache_dir = attr.ib(), attr.ib(), attr.ib()
    proj_cache_dir = attr.ib()

    def __attrs_post_init__(self):
        self.gitdir = self.cache_dir + '/git'
        self.httpdir = self.cache_dir + '/http'
        self.pipdir = self.cache_dir + '/pip'
        for path in (self.cache_dir, self.gitdir, self.httpdir, self.pipdir):
            fileutils.mkdir_p(path)

        self.proj_gitdir = self.proj_cache_dir + '/git'
        for path in (self.proj_gitdir,):
            fileutils.mkdir_p(path)

    def get_url(self, name, url, perms=0777):  # TODO: expiry
        '''
        Fetch file from passed url, or use cached file.
        Return location on disk.
        '''
        with self.logger.info('fetch_http', name=name, url=url):
            dst = self.cache_dir + '/http/' + name
            if not os.path.exists(dst):
                with self.logger.info('download_anaconda'):  # TODO: better name
                    client = HTTPClient()
                    client.urlretrieve(url, dst)
            os.chmod(dst, perms)
            return dst

    def pull_project_git(self, name, remote, checkout_id='master'):
        return self.pull_git_to(
            self.proj_gitdir + '/'  + name, remote, checkout_id)

    def pull_git_to(self, dest, remote, checkout_id='master'):
        '''
        Pull the working tree of the given remote to the dest path.
        '''
        checkout_id = checkout_id or 'master'
        log_name = dest.rsplit('/', 1)[-1]
        with self.logger.info('git_mirror_{name}', name=log_name):
            global_repo = create_or_update_mirror(
                remote, base_path=self.gitdir,
                executor=self.executor, logger=self.logger)
        # TODO: guts of this goes into GitRepo
        with self.logger.info('git_checkout_{name}', name=log_name):
            # git --git-dir=ip_check/.git --work-tree=ipcheck2/ checkout -f master
            if os.path.exists(dest):
                with self.logger.info('clean', path=dest):
                    shutil.rmtree(dest)
            fileutils.mkdir_p(dest)
            self.executor.command([
                'git', '--git-dir', global_repo, '--work-tree', dest,
                'checkout', '-f', checkout_id
            ]).redirect()
            # TODO: what's the behavior of pulling from a local repo
            # that has local changes?
        return dest

    def pull_git(self, name, remote):
        '''
        pull or clone the passed git repo
        return location of git repo on disk
        '''
        return create_or_update_mirror(
            remote, base_path=self.gitdir,
            executor=self.executor, logger=self.logger)

    def workon_project_git(self, name, remote, branch='master'):
        '''
        For workflows that involve working on a git repo.
        Returns a push(msg) function that pushes changes.
        '''
        dst = self.proj_gitdir + '/' + name
        if os.path.exists(dst):
            with self.logger.info('clean', path=dst):
                shutil.rmtree(dst)
        # TODO: can git push / commit work properly with
        # the split --git-dir / --work-tree thing?
        with self.logger.info('git_clone_{name}', name=name):
            self.executor.git.clone(remote, dst).redirect()
        self.executor.git.checkout(branch).redirect(cwd=dst)
        return GitRepo(dst, self.executor)

    def get_anaconda(self):
        return self.get_url(ANACONDA_INSTALLER, ANACONDA_URL)

    def commit2build_tag(self):
        'return current commit as a docker tag'
        git_ref = self.executor.git.rev_parse('HEAD').batch()[0]
        git_ref = git_ref.strip()[:8]
        # git log --max-count=1 --format=%ct
        unix_ts = int(
            self.executor.git.log(
                max_count=seashore.Eq('1'), format=seashore.Eq('%ct')).batch()[0])
        date = datetime.datetime.utcfromtimestamp(unix_ts).strftime('%y.%m.%d')
        return '_'.join((date, str(unix_ts), git_ref))


@attr.s
class GitRepo(object):
    # TODO: use this as response from all git returning methods of
    # DependencyCache (add attributes as needed)

    # TODO: encapsulate the git-dir / work-tree split nicely here
    path, executor = attr.ib(), attr.ib()

    def push(self, msg, **kw):
        '''
        add, commit with msg, and push
        '''
        dry_run = kw.pop('dry_run', False)
        if kw:
            raise TypeError('unexpected kwargs: %r' % kw.keys())
        opts = []
        if dry_run:
            opts.append('--dry-run')
        git = self.executor.chdir(self.path).git
        git.add('.', *opts).redirect()
        git.commit(*opts, message=msg).redirect()
        git.push(*opts).redirect()


def create_or_update_mirror(remote, base_path, executor, logger):
    escaped_remote = escape_remote(remote)
    dest = os.path.join(base_path, escaped_remote)
    if os.path.isdir(dest):
        with logger.critical('update_git'):
            executor.command([
                'git', 'remote', 'update', '--prune', 'origin'
            ]).redirect(cwd=dest)
    else:
        with logger.critical('clone_git'):
            executor.git.clone(
                remote, escaped_remote, mirror=None).redirect(cwd=base_path)
    return dest


def escape_remote(remote_str):
    """Should create readable filesystem-valid paths for URLs.

    For empty schemes, the resulting slug will default to 'ssh'.
    """
    remote_str = remote_str.lower()
    scheme, _, schemeless = remote_str.rpartition('://')
    if not scheme:
        scheme = 'ssh'
    _, _, userless = schemeless.rpartition('@')
    colonless = userless.replace(':', '/')
    remote_slug = scheme + '+' + colonless.replace('/', '+')
    return remote_slug
