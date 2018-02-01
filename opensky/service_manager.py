# Copyright (c) Shopkick 2017
# See LICENSE for details.
'''
Class and functions for managing processes / services for local development.
'''
import sys
import os
import urlparse
import threading
import shutil
import difflib
import pipes
import collections
import pkg_resources
import json
import glob

import attr
import colorama
from boltons.fileutils import mkdir_p
from boltons import iterutils

from . import config, docker, log

CUR_PATH = os.path.dirname(os.path.abspath(__file__))

# additional requirements needed by service-manager
# TODO: virtual env for these to avoid mixing with application dependencies?
META_REQUIREMENTS = {
    'pip': [config.PipPkg.parse(_pkg) for _pkg in
        ['ncolony==17.9.0', 'pytest==3.0.6', 'python-cloudfiles==1.7.11']],
    'conda': [config.CondaPkg.parse(_pkg) for _pkg in
        ['anaconda/gcc==4.8.5']],
}


@attr.s
class Service(object):
    project_dir, logger, docker_runner, config, cache, executor, config_path = \
        [attr.ib() for i in range(7)]
    use_docker_machine = attr.ib()
    sky_path = attr.ib()
    site_config = attr.ib()

    def __attrs_post_init__(self):
        # unpack self.config
        self.name = self.config['name']
        self.code_path = self.project_dir + '/' + self.name
        if not os.path.exists(self.code_path):
            raise EnvironmentError('cannot find project code at {0}'.format(self.code_path))
        self.db_name = ''  # empty string is most convenient marker for no DB dependency
        mysql = self.config.get('persistence_deps', {}).get('mysql')
        if mysql:
            self.db_name = mysql['db_name']
        self.cassandra = self.config.get('persistence_deps', {}).get('cassandra')
        if mysql or self.cassandra:
            migrations_path = self.code_path + '/migrations'
            if not os.path.exists(migrations_path):
                raise EnvironmentError('cannot find migrations path at {0}'.format(migrations_path))
        deps = self.config.get('persistence_deps', {})
        self.redis = deps.get('redis')
        self.zookeeper = deps.get('zookeeper', True)  # TODO: revert to default False once sky.yamls are updated
        self.memcached = deps.get('memcached', False)
        self.ports = self.config['ports']
        self.sky_deps_dir = self.project_dir + '/build/sky-deps/'
        mkdir_p(self.sky_deps_dir)
        self.last_setup_path = self.project_dir + '/build/last-setup.sky.yaml'

    def setup(self):
        if self.use_docker_machine:
            uid = 1000  # hope this is right
        else:
            uid = os.stat(self.config_path).st_uid

        depends_on = []
        if self.zookeeper:
            depends_on.append('zookeeper')
        if self.db_name:
            depends_on.append('mysql')
        if self.cassandra:
            depends_on.append('cassandra')

        self.clean()
        self.build(uid=uid)
        self._run_cmd(['SETUP'], depends_on)

        shutil.copy(self.config_path, self.last_setup_path)

    def populate(self):
        self._run_cmd(['POPULATE'], ['mysql'])

    def unit_test_build(self, tag=None):
        '''
        Run unit tests in a build constructed by build(tag).
        This is run outside of docker-compose, so must use
        --entrypoint command line.
        (Intended for use in CI, not local dev)
        '''
        if tag is None:
            tag = self.cache.commit2build_tag()
        self.executor.docker.run(
            tag, '/home/app/main.py', 'TEST',
            entrypoint='/home/app/miniconda2/bin/python').redirect()

    def test(self, pdb_on_error):
        self._check_setup()
        if pdb_on_error:
            cmd = ['TEST_PDB']
        else:
            cmd  = ['TEST']
        self._run_cmd(cmd, [])

    def integration_test(self, pytest_args):
        server_image = self._local_dev_image_service(
            self.name,
            entrypoint=_main_script_entrypoint(['START']))
        server_image_dict = attr.asdict(server_image, recurse=False)
        del server_image_dict['ports']
        server_image = docker.ImageService(
            ports=map(str, self.ports), **server_image_dict)
        self._run_cmd(['INT_TEST'] + pytest_args, self.start_depends_on,
            extra_services=[server_image])

    def start(self):
        self._prepare_start()
        self._run_cmd(['START'], self.start_depends_on)

    def start_debug(self):
        self._check_setup()
        self._run_cmd(['START_DEBUG'], self.start_depends_on)

    def _prepare_start(self):
        'DRY between start() and shell_start()'
        self._check_setup()
        host_log_path = '/private/var/log/services/' + self.name
        with log.sky_log.debug('check {path}', path=host_log_path) as act:
            try:
                mkdir_p(host_log_path)
            except OSError as ose:
                msg = ('failed to find or create {path},'
                       ' create it and try again: {}')
                act.failure(msg, ose)
                raise SystemExit(msg.format(ose, path=host_log_path))

    @property
    def start_depends_on(self):
        depends_on = ['zookeeper', 'memcached']
        if self.db_name:
            depends_on.append('mysql')
        if self.redis:
            depends_on.append('redis')
        if self.cassandra:
            depends_on.append('cassandra')
        return depends_on

    def shell_in(self, root=False):
        '''
        connect stdin / stdout to a new shell running in the main image
        assumes that run_py() has been called in another thread or process
        '''
        raise NotImplemented('need to list runnint containers for names')
        self.docker_runner.interact(
            self._container_name(), '/bin/bash', root)

    def shell_start(self, root=False):
        '''
        Start a bash shell in an environment as if the service was going to run.
        Unlike shell_in(), this is stand-alone.
        '''
        self._prepare_start()
        self._run_entrypoint(
            'shell_start', ['bash'], self.start_depends_on, root=root)

    def repl_start(self):
        '''
        Start a python REPL in the same environment as the server python
        would run.
        '''
        self._prepare_start()
        self._run_cmd(['REPL'], self.start_depends_on)

    def clean(self):
        # TODO: expose command
        # TODO: the "project" argument maybe isn't good
        # TODO: fix/use last-sky.yaml/sky.prev.yaml
        compose_name = self.name
        services = [docker.BUILT_INS[name] for name in self.start_depends_on]
        composition = docker.Dockercompose(compose_name, services)
        self.docker_runner.clean_composition(composition, compose_name)

    def build(self, tag=None, uid=501):
        skydep_paths = self._fetch_skydeps()
        lib_deps = self._collect_lib_deps(skydep_paths)

        yum_pkgs = [
            'bzip2', 'glibc', 'glibc-devel', 'glibc-static', 'git',
            'redhat-lsb-core', 'tar', 'wget'] + lib_deps.get('yum', [])

        # TODO: pull these from the base image spec in the site_config once we loosen up the schema parsing
        # wheelhouse_path = self.site_config['services'].get('default_base_build', {}).get('wheelhouse_path')
        # conda_packages_path = self.site_config['services'].get('default_base_build', {}).get('conda_packages_path')
        wheelhouse_path = '/wheelhouse/'
        conda_pkgs_path = '/conda-packages/'
        conda_pkgs_files = glob.glob(conda_pkgs_path + '*.tar.bz2')
        extra_pypi_urls = self.site_config['pip'].get('extra_pypi_urls', ())
        commands = [
            'useradd -u {} -ms /bin/bash app'.format(uid),
            'yum --setopt=obsoletes=0 install -y ' + ' '.join(["'%s'" % yp for yp in yum_pkgs]),
            'mkdir -p /var/log/services',
            'chmod 777 /var/log/services',
            'yum clean all',
            # chown -R everything takes 5-10 minutes;
            # chown -R just the directories takes 0.05 seconds
            'find /home/app -type d -print0 | xargs -0 chown app',
            # install miniconda, then conda, then pip dependencies
            'su app -lc "/home/app/Miniconda2-latest-Linux-x86_64.sh -b"',
            'su app -c ' + pipes.quote(_fmt_conda_cmd(files=conda_pkgs_files, update_deps=False, offline=True)),
            'su app -c ' + pipes.quote(_fmt_conda_cmd(pkgs=lib_deps['conda'])),
            'su app -c ' + pipes.quote(_fmt_pip_cmd(lib_deps['pip'], [wheelhouse_path], extra_pypi_urls)),
            # clean-up conda
            'su app -c ' + pipes.quote(
                '/home/app/miniconda2/bin/conda clean -ay'),
            # more aggressive cleanup
            'rm -rf /home/app/miniconda2/pkgs /home/app/.cache/pip',
            'mkdir -p /var/log/services/{0}'.format(self.name),
            'chown -R app /var/log/services/{0}'.format(self.name),
            'mkdir -p /var/run/ncolony/messages /var/run/ncolony/config'
        ]

        # commands should be of form "executable --arg arg1 --arg arg2 name"
        startup_proc = ' --arg '.join(_main_script_entrypoint(['RUN_LIVE']))
        startup_proc += ' {}_server'.format(self.name)

        pythonpath = ':'.join(
            ['/home/app/' + sky_name for sky_name in skydep_paths])

        path = ('/home/app/miniconda2/bin/:/usr/local/sbin:/usr/local/bin:'
               '/usr/sbin:/usr/bin:/sbin:/bin')

        commands += [
            '/home/app/miniconda2/bin/python -m ncolony ctl'
            ' --messages /var/run/ncolony/messages'
            ' --config /var/run/ncolony/config add --cmd ' + startup_proc +
            (' --env PYTHONPATH=' + pythonpath if pythonpath else '') +
            ' --env PATH=' + path + ' --extras /home/app/ncolony.json']

        commands += self.config['setup_cmds']

        main = pkg_resources.resource_string(
            'opensky', 'goes_in_docker_image/main.py')
        main += '\n' + pkg_resources.resource_string(
            'opensky', 'goes_in_docker_image/_populate.py')

        sitecustomize_bytes = pkg_resources.resource_string(
            'opensky', 'goes_in_docker_image/debug/sitecustomize.py')

        if os.path.exists(self.config_path):
            sky_yaml_bytes = open(self.config_path, 'rb').read()
        else:
            sky_yaml_bytes = '"sky config not found at %r"' % self.config_path

        small_files = {
            'main.py': main,
            'config.json': json.dumps({
                'project_name': self.name,
                'db_name': self.db_name,
                'host_ports': HOST_PORTS,  # name:endpoint map for dev
                'start_depends_on': self.start_depends_on,
                }),
            'sky.yaml': sky_yaml_bytes,
            'debug/sitecustomize.py': sitecustomize_bytes,
            'ncolony.json': json.dumps({'env_inherit': ['ENV_TYPE']}),
            '.dockerignore': '.git',
        }

        for conf_name, file_name in (('dev', 'development-flags'),
                                     ('beta', 'beta-flags'),
                                     ('stage', 'stage-flags'),
                                     ('colo1', 'production-flags')):
            flags = self.config.get('flags', {}).get(conf_name) or []
            small_files[file_name] = '\n'.join(
                ['--%s' % f for f in flags] + [''])

        # note: top level directories on destination paths
        # will be sliced off by docker COPY instruction
        context_paths = [
            self.code_path + ':app_home_dir/' + self.name,
            self.cache.get_anaconda()]
        int_test_path = self.project_dir + '/integration_test'
        if os.path.exists(int_test_path):
            context_paths.append(int_test_path + ':app_home_dir/integration_test')
        for name, path in skydep_paths.items():
            context_paths.append('{}:app_home_dir/{}'.format(path, name))

        entrypoint = [
            "/home/app/miniconda2/bin/twist", "--log-format", "text",
            "ncolony", "--messages", "/var/run/ncolony/messages",
            "--config", "/var/run/ncolony/config"]

        envvars = [('PATH', '/home/app/miniconda2/bin/:${PATH}')]
        if pythonpath:
            envvars.append(('PYTHONPATH', pythonpath))

        base_build_image = self.site_config['services']['default_base_build']['docker_image']
        self.docker_runner.build_imagespec(
            docker.ImageSpec(
                self.name, base_build_image, context_paths, small_files,
                commands, entrypoint, env=envvars),
            tag=tag or self._cur_image())

    def _run_cmd(self, cmd, depends_on, extra_services=None, root=False):
        '''
        Run main.py inside the image and pass it cmd.
        depends_on is the set of additional background images to run.
        '''
        self._run_entrypoint(cmd[0], _main_script_entrypoint(cmd),
                             depends_on, extra_services, root)

    def _run_entrypoint(self, compose_name_suffix, entrypoint, depends_on,
        extra_services=None, root=False):
        '''
        run the local image created by setup with a given entrypoint
        in the "foreground" of sky so that REPL/PDB/etc work as expected.
        '''
        service = self._local_dev_image_service(
            self.name + '_server',
            # sleep forever, unless you get a SIGTERM
            entrypoint=(
                'python -c "{}"'.format(
                'import signal, time, sys; '
                'signal.signal(15, lambda s, f: sys.exit(0)); '
                '[time.sleep(2**20) for i in xrange(2**20)]')))

        services = [docker.BUILT_INS[name] for name in depends_on]
        services += extra_services or []
        # NOTE: names need to stay boring and consistent
        # so that state that lives in MySQL &etc type
        # containers isn't lost between SETUP and START
        compose_name = self.name  # + '-' + compose_name_suffix
        composition = docker.Dockercompose(compose_name, [service] + services)

        trace = []
        def catch_trace(target, args):
            try:
                return target(*args)
            except:
                # TODO: better way to catch this?
                from . import tb_format
                trace.append(tb_format.format_traceback())
                raise

        compose_thread = threading.Thread(
            target=catch_trace,
            args=(self.docker_runner.run_composition,
                  (composition, compose_name)))
        compose_thread.daemon = True

        def sleep(dur):
            compose_thread.join(dur)
            if not compose_thread.isAlive():
                raise EnvironmentError(
                    'docker-compose thread died:\n' + trace[0])
            return

        container = self._container_name(compose_name)
        try:
            compose_thread.start()
            self._wait_for_container(container, sleep)
            # since the container is just running sleep infinity,
            # it is ready to go
            self.docker_runner.interact(container, entrypoint, root=root)
        finally:
            if self.docker_runner.get_container_id(container):
                self.docker_runner.stop(container)
            if self.docker_runner.get_container_id(container):
                self.docker_runner.kill(container)

    def _wait_for_container(self, container, sleep):
        # TODO: use file creation in volume or something for
        # docker container to declare itself "ready" from inside
        with self.logger.info('wait_for_container', name=container):
            sleep(2.5)
            for i in range(10):
                sleep(2**i)
                container_id = self.docker_runner.get_container_id(container)
                if container_id:
                    break
            else:
                raise EnvironmentError(
                    "container didn't come ready: " + repr(container))

    def _local_dev_image_service(self, name, entrypoint):
        'create a image service set up for local dev image, ports, volumes'
        return docker.ImageService(
            name=name,
            image=self._cur_image(),
            ports=['%s:%s' % (p, p) for p in self.ports],
            volumes=self._local_dev_volumes(),
            entrypoint=entrypoint)

    def _local_dev_volumes(self):
        '''
        Return the set of volumes for local development.
        '''
        cached = {}
        for path in os.listdir(self.sky_deps_dir):
            cached[path] = '{}/{}:/home/app/{}'.format(
                self.sky_deps_dir, path, path)
        for skydep in self.config['library_deps']['sky']:
            if skydep.is_local:
                cached[skydep.name] = '{}:/home/app/{}'.format(
                    skydep.path, skydep.name)
        volumes = (
            self.code_path + ':/home/app/' + self.name,
            '/private/var/log/services:/var/log/services'
            ) + tuple(cached.values())
        if os.path.exists(self.project_dir + '/integration_test'):
            volumes += (
                self.project_dir + '/integration_test:'
                '/home/app/integration_test',)
        return volumes

    def _fetch_skydeps(self):
        '''
        Fetch all of the sky dependencies to local paths.
        '''
        skydep_paths = {}
        for skydep in self.config['library_deps']['sky']:
            if skydep.is_local:
                skydep_paths[skydep.name] = skydep.path
            else:
                skydep_paths[skydep.name] = self.cache.pull_git_to(
                    self.sky_deps_dir + '/' + skydep.name,
                    skydep.repo, skydep.refspec)
        return skydep_paths

    def _collect_lib_deps(self, sky_dep_paths):
        '''
        Fetch and reconcile all of the library dependencies
        (pip and conda dependencies of current service and sky dependencies)
        sky_dep_paths : {name: /path/to/sky.yaml}
        '''
        all_deps = collections.OrderedDict()
        overwrites = []

        def add_deps(dependencies, path):
            def get_deps(dependencies):
                for pkg_type, packages in dependencies.items():
                    if pkg_type == 'sky':
                        continue
                    for pkg in packages:
                        yield pkg, pkg_type
            with self.logger.info('pull_transitive_deps', config=path):
                for pkg, pkg_type in get_deps(dependencies):
                    pkg_name = pkg if isinstance(pkg, basestring) else pkg.pkg
                    if pkg_name in all_deps:
                        # TODO: something smart with overrides
                        overwrites.append((pkg_name, all_deps[pkg_name][2]))
                    all_deps[pkg_name] = (pkg_type, pkg, path)

        add_deps(META_REQUIREMENTS, '<SKY_REQUIREMENTS>')

        for path in sky_dep_paths.values():
            path += '/sky.yaml'
            with self.logger.info('parse_lib_config', path=path):
                lib_conf = config.parse(path)
                add_deps(lib_conf['library_deps'], path)

        add_deps(self.config['library_deps'], self.config_path)

        finalized_dependencies = iterutils.bucketize(
            all_deps.values(), lambda e: e[0])
        if 'sky' in finalized_dependencies:
            del finalized_dependencies['sky']
        finalized_dependencies = {
            key: [e[1] for e in val]
            for key, val in finalized_dependencies.items()
        }

        return finalized_dependencies

    def _cur_image(self):
        '''
        Get the image name (for tag build) for local development.
        '''
        return self.name + ':dev'

    def _container_name(self, compose_name):
        compose_name = compose_name.replace('_', '').replace('-', '').lower()
        return compose_name + '_' + self.name + '_server_1'

    def _check_setup(self):
        if not os.path.exists(self.last_setup_path):
            self.logger.comment('WARNING: setup never run')
            return
        with open(self.last_setup_path) as last_setup,\
             open(self.config_path) as cur_config:
            #
            lines = difflib.unified_diff(
                last_setup.readlines(), cur_config.readlines())
        if not lines:
            return
        colors = {'-': colorama.Fore.RED, '+': colorama.Fore.GREEN}
        colorized = []
        for line in lines:
            if line[0] in colors:
                line = colors[line[0]] + line + colorama.Style.RESET_ALL
            colorized.append(line)
        self.logger.comment('WARNING: sky.yaml modified since last setup')
        sys.stderr.write(''.join(colorized) + '\n')


def _fmt_pip_cmd(pkgs, find_links_urls, extra_index_urls):
    if not pkgs:
        return ":  # no-op pip install"
    cmd = 'PATH=/home/app/miniconda2/bin/:$PATH /home/app/miniconda2/bin/pip install '
    for fl_url in find_links_urls:
        cmd += '--find-links %s ' % fl_url
    for index_url in extra_index_urls:
        trusted_host = urlparse.urlparse(index_url).netloc
        cmd += '--extra-index-url %s --trusted-host %s ' % (index_url, trusted_host)
    cmd += '--no-cache-dir '
    cmd += ' '.join(pkg.raw for pkg in pkgs)
    return cmd

def _fmt_conda_cmd(pkgs=None, files=None, offline=False, update_deps=True, channel_priority=False):
    if not (pkgs or files):
        return ":  # no-op conda install"
    if pkgs and files:
        raise ValueError("expected pkgs or files, not both: %r, %r" % (pkgs, files))
    pkgs = list(pkgs or [])
    files = list(files or [])
    cmd = ('/home/app/miniconda2/bin/conda install'
           ' -y -q --show-channel-urls ')
    if offline:
        cmd += '--offline '
    if not channel_priority:
        cmd += '--no-channel-priority '
    if not update_deps:
        cmd += '--no-update-dependencies '
    channels = ['defaults'] + [pkg.channel for pkg in pkgs]
    if files:
        cmd += ' ' + ' '.join(files)
    if pkgs:
        # autodisables MKL in favor of openblas since it is ~600MB
        cmd += ' '.join(
            ['--channel %s' % chan for chan in iterutils.unique(channels)] +
            ['nomkl'] + [pkg.pkg + '==' + pkg.ver for pkg in pkgs])
    return cmd


def _main_script_entrypoint(cmd):
    'create an entrypoint that will run the *cmd* command of main script'
    return ['/home/app/miniconda2/bin/python', '/home/app/main.py'] + cmd


def _ensure(pkg, cls):
    if isinstance(pkg, cls):
        return pkg
    return cls.parse(pkg)


#TODO: where should these really come from?
HOST_PORTS = {
  'partners':          ['partners:9022'],
  'userstore':         ['userstore:9000'],
  'locations':         ['locations:9002'],
  'catalog':           ['catalog:9042'],
  'zones':             ['zones:9050'],
  'flawless':          ['flawless:8998'],
  'memcached':         ['memcached'],
  'storm_kestrel':     ['storm_kestrel:22133'],
  'secret':            ['secret:9030'],
  'developers':        ['developers:9040'],
  'appconfig':         ['appconfig:9032'],
  'personalization':   ['personalization:9044'],
  'redis':             ['1/masters/redis:6379'],
  'symmetric_redis':   ['masters/redis:6379', 'all/redis:6379'],
  'apnsrelay':         ['apnsrelay:9036'],
  'urlshortener':      ['urlshortener:9038'],
  'elasticsearch':     ['elasticsearch:9200'],
  'authorization':     ['authorization:9046'],
  'recipes':           ['recipes:9210']
}
