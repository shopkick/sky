# Copyright (c) Shopkick 2017
# See LICENSE for details.
'''
Classes and functions for operating docker.
'''
import sys
import shutil
import os.path
import os
import json
import time
import argparse

from boltons import fileutils, iterutils
import yaml
import attr
import schema
import seashore
from schema_builder import schema_attrib, as_tuple, list_or_tuple_of
import plugins

CENTOS = 'centos:7.3.1611'


@plugins.register_command(
    name='docker',
    help='docker helper commands',
    requires=('docker_daemon_fixer',),
    maybe_requires=('service', 'cache'))
def docker_cmd_plugin(args, reqs):
    # TODO: cleaner sub-dispatch
    parser = argparse.ArgumentParser(prog='docker')
    parser.add_argument(
        'cmd', choices=('gc', 'shell', 'commit2tag', 'clock_sync'))
    cmd = parser.parse_args(args[1:2]).cmd
    if cmd == 'gc':
        reqs.docker_daemon_fixer.clean()
    elif cmd == 'shell':
        parser.add_argument('--root', action='store_true', default=False)
        root = parser.parse_args(args[1:]).root
        reqs.build_service().shell_in(root)
    elif cmd == 'commit2tag':
        print reqs.build_cache().commit2build_tag()
    elif cmd == 'clock_sync':
        reqs.docker_daemon_fixer.clock_sync()


DOCKER_FOR_MAC_HOST_BRIDGE_IP = '192.168.65.1'


@attr.s
class Runner(object):
    '''
    Stateful runner for docker tasks.  This is meant to be 1:1 with sky.yaml files,
    and actually calls out to docker on the command line.
    '''
    executor, shell, logger, docker_path = (attr.ib() for i in range(4))
    use_docker_machine = attr.ib(default=True)
    sudo = attr.ib(default=None)

    def __attrs_post_init__(self):
        if not os.path.exists(self.docker_path):
            os.mkdir(self.docker_path)  # create one level of directory
        self.compose_path = self.docker_path + '/compositions/'
        self.image_path = self.docker_path + '/images/'
        if not os.path.exists(self.compose_path):
            os.mkdir(self.compose_path)
        if not os.path.exists(self.image_path):
            os.mkdir(self.image_path)
        # docker-client has a bug with socks5h protocol
        self.executor = self.executor.patch_env(ALL_PROXY='')

    def run_composition(self, composition, project):
        '''
        Run an instance of Dockercompose, halting if any container exits.
        *composition* - a Dockercompose instance
        *project* - the docker-compose namespace in which to run
           (e.g. networks, volumes "see" each other within a namespace)
        '''
        self._docker_compose_cmd(composition, project,
                                 ['up', '--abort-on-container-exit'])
        return

    def _docker_compose_cmd(self, composition, project, args):
        work_dir = self.compose_path + composition.namespace
        if os.path.exists(work_dir):
            self._clean_dir(work_dir)
        else:
            fileutils.mkdir_p(work_dir)
        compose_path = work_dir + '/docker-compose.yml'
        with open(compose_path, 'w') as compose_file:
            yaml.dump(composition.to_data(), compose_file)
        self.shell.log_file(compose_path)

        if not self.use_docker_machine:
            cmd_args = ['docker-compose'] + list(args)
            return self.executor.chdir(work_dir).command(cmd_args).redirect()

        # docker-compose in docker
        def _fmt_vols(paths):
            return sum(
                [['--volume', '{0}:{0}'.format(p)] for p in paths], [])

        dcd_args = (['docker', 'run']
                    + _fmt_vols((work_dir, '/var/run/docker.sock'))
                    + ['--env', 'COMPOSE_PROJECT_NAME=' + project,
                       '--workdir', work_dir, '--interactive', '--rm']
                    + [DOCKER_COMPOSE_IMAGE] + list(args))
        return self.executor.command(dcd_args)

    def build_imagespec(self, image_spec, tag='latest', user='app'):
        small_files = dict(image_spec.small_files)
        assert 'install_cmds.json' not in small_files  # reserved
        # make nice readable json array file
        small_files['install_cmds.json'] = '[\n{}\n]'.format(
            ',\n'.join([json.dumps(cmd) for cmd in image_spec.commands]))
        install_script = '\n'.join([
            'import subprocess, json, sys, time',
            't1 = time.time()',
            'for cmd in json.load(open("/home/{user}/install_cmds.json")):',
            '    sys.stderr.write(cmd + "\\n")',
            '    t2 = time.time()',
            '    subprocess.check_call(cmd, shell=True, cwd="/home/{user}/")',
            '    sys.stderr.write("{{}}, {{:g}}, {{:g}}\\n".format(',
            '        cmd, time.time() - t1, time.time() - t2))',
            ]).format(user=user)
        assert 'install.py' not in small_files  # reserved
        small_files['install.py'] = install_script
        copy_paths = []

        context_paths = list(image_spec.context_paths)
        for sf in small_files.keys():
            sf_parent = sf.split('/')[0]
            if sf_parent == sf:
                copy_paths.append(sf)
                continue
            if not sf_parent:
                raise NotImplementedError(
                    'only relative path small files supported, not: %r' % sf)

        for cp in context_paths:
            if ':' in cp:  # internal path specified
                copy_paths.append(
                    cp.rsplit(':', 1)[1].split('/', 1)[0])
            else:
                copy_paths.append(os.path.basename(cp))
        copy_paths = sorted(set(copy_paths))
        dockerfile_template = '\n'.join([
            'FROM {base}',
            'ENTRYPOINT {entrypoint}',
            'COPY {copy_paths} /home/{user}/',
            'RUN /usr/bin/python /home/{user}/install.py',
            'USER {user}',
            'WORKDIR /home/{user}'
            ])
        dockerfile = dockerfile_template.format(
            base=image_spec.base, copy_paths=' '.join(copy_paths),
            user=user, entrypoint=json.dumps(image_spec.entrypoint))
        dockerfile += '\n' + '\n'.join([
            'ENV {} {}'.format(name, val) for name, val in image_spec.env])

        return self.build_dockerfile(
            image_spec.name, tag=tag, dockerfile=dockerfile,
            context_paths=context_paths, small_files=small_files)

    def build_dockerfile(self, name, tag, dockerfile, context_paths=(),
                         small_files={}, ignore_patterns=('.git', '*.pyc')):
        '''
        Build a dockerfile, name is the image name.
        *name* the logical name (must be unique per SKY env)
        *tag* the tag to build with
        *dockerfile* the contents of the dockerfile
        *context_paths* paths to additional files / directories to be
          copied into the build directory (included in docker context)
        *small_files* dict of {path: file contents} to be added to context
        small_files may be recursive, with child dicts representing child
        directories
        '''
        work_dir = self.setup_docker_context(
            name, dockerfile, context_paths, small_files, ignore_patterns)
        self.executor.docker.build(work_dir, tag=tag).redirect()

    def setup_docker_context(self, name, dockerfile, context_paths,
                             small_files, ignore_patterns=('.git', '*.pyc')):
        '''
        setup a docker context on disk ready to build the given dockerfile
        returns the path to the new directory, which is now ready to
        be passed to docker build / docker-compose
        '''
        work_dir = self.image_path + name
        self._clean_dir(work_dir)
        with open(work_dir + '/Dockerfile', 'w') as f:
            f.write(dockerfile)
        self.shell.log_file(work_dir + '/Dockerfile')
        ignore = shutil.ignore_patterns(*ignore_patterns)
        with self.logger.info('setup_docker_context', name=name, paths=context_paths):
            for path in context_paths:
                if ':' in path:
                    path, target = path.split(':', 1)
                else:
                    target = os.path.basename(path)
                target = work_dir + '/' + target
                if not os.path.exists(path):
                    raise ValueError('path {} does not exist'.format(path))
                if os.path.isdir(path):
                    shutil.copytree(path, target, symlinks=True, ignore=ignore)
                else:
                    shutil.copy2(path, target)
        # recursively walk into directory, creating directories and files

        def walk_paths(tree, handler):
            def enter_adapter(path, key, value):
                if path or key is not None:  # path=() key=None is root
                    handler(path + (key,), value)
                return iterutils.default_enter(path, key, value)
            return iterutils.remap(tree, enter_adapter)

        def handle_small_file(path, value):
            rel_path = '/'.join(path)
            if '/' in rel_path:
                target = work_dir + '/app_home_dir/' + rel_path
            else:
                target = work_dir + '/' + rel_path
            if isinstance(value, basestring):
                target_dir = os.path.dirname(target)
                if target_dir and not os.path.exists(target_dir):
                    fileutils.mkdir_p(target_dir)
                open(target, 'w').write(value)

        # TODO: fails on recursive dicts (unhashable type dict)
        walk_paths(small_files, handle_small_file)
        return work_dir

    def interact(self, container, cmd, root=False, batch=False):
        '''
        run *cmd* in *container*; *root* controls whether to run as
        containers default user or root; *batch* controls whether to
        interact with terminal input (batch=True means there is
        no human involved, so don't expect interaction just dump output)
        '''
        args = {}
        if root:
            args['user'] = 'root'
        if not batch and sys.stdin.isatty() and not sys.stdin.closed:
            args['interactive'] = args['tty'] = None
        if isinstance(cmd, list):
            cmd = ' '.join(cmd)
        self.executor.docker.exec_(
            container, '/bin/bash', '-c', cmd, **args).interactive()

    def get_container_id(self, name):
        'get the container id of a running instance, or None if no instance is running'
        return self.executor.docker.ps(
            quiet=None, filter="name=" + name).batch()[0].strip() or None

    def get_port_container_id(self, port):
        filter_str = "publish=%s" % port
        _cmd = self.executor.docker.ps(quiet=None, filter=filter_str)
        res = _cmd.batch()  # NOTE: docker version 17.06+ ok
        return res[0].strip() or None

    def get_image_id(self, tag):
        'get the image id if one exists with the given tag'
        return self.executor.docker.images(tag, quiet=None).batch()[0].strip() or None


    def kill(self, container):
        self.executor.docker.kill(container).redirect()
        self.executor.docker.rm(container).redirect()

    def stop(self, container):
        self.executor.docker.stop(container).redirect()

    def _clean_dir(self, path):
        with self.logger.info('clean_dir', path=path):
            if os.path.exists(path):
                shutil.rmtree(path)
            os.mkdir(path)

    def clean_composition(self, composition, project):  # TODO: flags
        self._docker_compose_cmd(composition, project, ['down', '--remove-orphans'])
        return


@attr.s
class DaemonFixer(object):
    '''
    This class provides utilities to fix common ways that a docker
    daemon's internal state may get messed up.  (These all require
    manual intervention but less copy-pasta at the command line.)
    '''
    executor, logger = attr.ib(), attr.ib()

    def clean(self):
        containers = self.executor.docker.ps(
            quiet=None, filter="status=exited").batch()[0].split()
        self.executor.docker.rm(*containers).redirect()
        print('removed containers: ' + ','.join(containers))
        images = self.executor.docker.images(
            quiet=None, filter="dangling=true", no_trunc=None).batch()[0].split()
        self.executor.docker.rmi(*images).redirect()
        print('removed images: ' + ','.join(images))

    def clock_sync(self):
        '''
        Sync the docker daemon clock up to current clock.
        '''
        with self.logger.info('sync_clock'):
            cmd = 'date +%s -s @' + str(int(time.time()))
            self.executor.docker.run(
                CENTOS, cmd, rm=None, privileged=None
            ).interactive()


_EMPTY = attr.make_class('Empty', [])()  # represents a value that should be dropped from the YAML
_DEPENDS_ON_ALL = '*'  # special value for depends_on for connecting to everything else
# (mainly for jupyter notebook or similar debugging tool)
SK_CENTRAL_REMOTE = 'USER@GITLAB_HOSTNAME:REPO'

_ENV_SCHEMA = schema.Or(
    [schema.Regex('[_a-zA-Z][_a-zA-Z0-9]*(=[^=]+)?')],
    {schema.Regex('[_a-zA-Z][_a-zA-Z0-9]*'): schema.Or(str, None)})

_PORT_SCHEMA = schema.Regex(r'\d+(\:\d+)?')

'''
Environment variables in docker-compose:
https://docs.docker.com/compose/compose-file/#/environment

environment:
  RACK_ENV: development
  SHOW: 'true'
  SESSION_SECRET:

environment:
  - RACK_ENV=development
  - SHOW=true
  - SESSION_SECRET
'''

# represents a directory, which references by name (str)
# files (str) and sub-directories
_DIR_SCHEMA = {}
_DIR_SCHEMA[str] = schema.Or(str, _DIR_SCHEMA)

@attr.s(frozen=True)
class ImageSpec(object):
    '''
    Represents a cookie-cutter docker image spec which can be built.
    '''
    name = schema_attrib(str)  # logical name
    base = schema_attrib(str)  # base image to build from
    context_paths = schema_attrib(as_tuple(list_or_tuple_of(str)))
    small_files = schema_attrib(_DIR_SCHEMA)
    commands = schema_attrib([str, [str]])
    entrypoint = schema_attrib([str])
    env = schema_attrib([(str, str)], default=None)


@attr.s(frozen=True)
class ImageService(object):
    '''
    A service which runs from a pre-built image.
    '''
    name = schema_attrib(str)  # logical name for service (unique per docker-compose)
    image = schema_attrib(str)  # image to use
    environment = schema_attrib(_ENV_SCHEMA, default=_EMPTY)  # environment vars
    depends_on = schema_attrib([str], default=_EMPTY)  # logical names of other services
    volumes = schema_attrib(as_tuple(list_or_tuple_of(str)), default=_EMPTY) #  TODO: regexp
    entrypoint = schema_attrib(schema.Or(str, [str]), default=_EMPTY)
    ports = schema_attrib([_PORT_SCHEMA], default=_EMPTY)
    privileged = schema_attrib(bool, default=_EMPTY)


@attr.s
class Dockercompose(object):
    '''
    Represents a docker compose that can be run.
    '''
    namespace = schema_attrib(str)
    services = schema_attrib([ImageService])

    def to_data(self):
        '''
        Returns the current Dockercompose in a form suitable for YAML serialization.
        '''
        data = {}  # collections.OrderedDict()  # wtf yaml
        data['version'] = '2'
        data['services'] = {
            service.name: attr.asdict(
                service, filter=lambda a, v: v != _EMPTY and a.name != 'name')
            for service in self.services
        }
        # resolve _DEPENDS_ON_ALL
        for name, serv in data['services'].items():
            if _DEPENDS_ON_ALL in serv.get('depends_on', ()):
                other_services = [n for n in data['services'].keys() if n != name]
                if other_services:
                    serv['depends_on'] = other_services
                else:
                    del serv['depends_on']
        all_volumes = sum(
            [service.volumes for service in self.services if service.volumes != _EMPTY], ())
        v_names = [v.partition(':')[0] for v in all_volumes]
        data['volumes'] = {}
        for name in v_names:
            if '/' not in name:  # named volume, not shared with host
                data['volumes'][name] = None
            elif name.startswith('.'):  # relative volume
                pass  # TODO: create directory
            elif name.startswith('/'):  # host path -> volume
                local_path = name
                if not os.path.exists(local_path):
                     raise EnvironmentError(
                        'local path {} does not exist'.format(name))
        if not data['volumes']:
            del data['volumes']
        return data


def in_ensured_docker_machine(executor, name):
    '''
    returns an executor in an ensured docker machine
    '''
    machine = executor.docker_machine
    try:
        out, _ = machine.status(name).batch()
        if out.strip().lower() in ('saved', 'stopped'):
            machine.start(name).interactive()
    except seashore.ProcessError as pe:
        # working around seashore bug with pe.error
        # TODO: fix this once seashore had a release
        # with bugfix
        _, out, err = pe._args
        if err.startswith('Host does not exist: '):
            machine.create(name).interactive()
        else:
            raise
    no_proxy = machine.ip(name).batch()[0].strip()
    if 'no_proxy' in os.environ:
        no_proxy = os.environ['no_proxy'] + ',' + no_proxy
    return executor.in_docker_machine(name).patch_env(no_proxy=no_proxy)


DIR = os.path.dirname(os.path.abspath(__file__))


DOCKER_COMPOSE_IMAGE = (
    'GITLAB_HOSTNAME:PORT'
    'open-source/docker-compose-in-docker:'
    '17.06.20_1497918403_c3d71d17')

BUILT_INS = {
    'mysql': ImageService(
        name='mysql', environment={'MYSQL_ALLOW_EMPTY_PASSWORD': 'true'},
        image='mysql:5.7.17'),
        # volumes=('mysql:/var/lib/mysql:Z',)),
    'redis': ImageService(name='redis', image='redis:3.2.6-alpine'),
    'zookeeper': ImageService(name='zookeeper', image='zookeeper:3.4.9',
                              volumes=('zk_data_dir:/data:Z', 'zk_data_log:/datalog:Z')),
    'memcached': ImageService(name='memcached', image='memcached:1.4.34-alpine'),
    'cassandra': ImageService(name='cassandra', image='cassandra:3.10',
                              volumes=('cassandra_data:/var/lib/cassandra:Z',)),
}
