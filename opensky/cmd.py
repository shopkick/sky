'''
Uses argparse and gather to dispatch
to the correct opensky plugins and internal functions.
'''
# MAINTENANCE NOTE: this is the entrypoint module
# nobody else should import this module, this module
# may import evertying else
import sys
import pdb
import datetime
import os
from os.path import join as pjoin
import re
import json
import time
import glob
import shutil
import argparse

import attr
import colorama
import ptpython.repl
from boltons import fileutils, timeutils
import seashore
import hyperlink

from . import shell
from . import docker
from . import service_manager
from . import config
from . import typesnap
from . import log
from . import cache
from . import plugins
from . import tb_format
from . import self_plugin
from . import site_config
from . import services_plugin

try:
    from . import version as _version
except ImportError:
    class _version(object):
        version = 'dev'
        revision = 'local'
        revision_name = 'local'
        revision_timestamp = 'now'
        build_timestamp = 'now'


SKY_HOST_DIR = '~/.sky'
DEFAULT_CONFIG_FILENAME = 'sky.yaml'


def _env_flag(name, default=False):
    try:
        val = os.environ[name]
    except KeyError:
        return default
    if val.lower() in ('1', 'true'):
        return True
    elif val.lower() in ('0', 'false'):
        return False
    raise EnvironmentError(
        'env var {!r} set to {!r}, should be 1, true, 0, or false'.format(
            name, val))


# TODO: something a bit more data-driven and high level for
# these environment variable handling
PDB = _env_flag('SKY_PDB')
DUMP_LOG = _env_flag('SKY_DUMP_LOG')
DOCKER_MACHINE_NAME = os.environ.get(
    'SKY_DOCKER_MACHINE_NAME', 'sky')
USE_DOCKER_MACHINE = _env_flag(  # docker-machine everywhere but linux
    'SKY_USE_DOCKER_MACHINE', False)  # sys.platform not in ('linux', 'linux2'))
# disable USE_DOCKER_MACHINE by default until we can solve all
# permissions issues

@plugins.register_command(requires=('service',),
                          help='setup project based on sky.yaml')
def setup(args, reqs):
    reqs.service.setup()


@plugins.register_command(requires=('sky_metadata',),
                          help='output version info based on the current build')
def version(argv, reqs):
    prs = argparse.ArgumentParser(prog='version')
    prs.add_argument('--json', action="store_true",
                     help='print machine readable json version info')
    args = prs.parse_args(argv[1:])

    sky_metadata = reqs.sky_metadata
    version_dict = get_version_dict(sky_metadata)
    if args.json:
        print json.dumps(version_dict)
    else:
        version_dict['opensky_revision'] = version_dict['opensky_revision'][:10]
        print('sky version {version}, built {timestamp} with opensky'
              ' version {opensky_version} ({opensky_revision})'.format(**version_dict))
    return


def get_version_dict(sky_metadata):
    if sky_metadata is None:
        version = 'dev'
        timestamp = datetime.datetime.now().isoformat()
    else:
        version = sky_metadata.version
        timestamp = sky_metadata.timestamp

    ret = {'version': version,
           'timestamp': timestamp,
           'opensky_version': _version.version,
           'opensky_revision': _version.revision,
           'opensky_revision_name': _version.revision_name}
    return ret


@plugins.register_command(name='config',
                          requires=('sky_metadata', 'site_config'),
                          maybe_requires=('config',),
                          help='output version info based on the current build')
def config_cmd(argv, reqs):
    ret = {}
    try:
        config = reqs.build_config()
    except Exception as e:
        config = {'_error': str(e)}
    ret['project'] = config
    ret['site'] = reqs.site_config
    ret['version'] = get_version_dict(reqs.sky_metadata)
    # TODO: 'user' once user gets config
    print json.dumps(ret, indent=2, sort_keys=True, default=_json_dumps_default)
    return


def _json_dumps_default(obj):
    try:
        return attr.asdict(obj)
    except Exception:
        return repr(obj)


@plugins.register_command('start', requires=('service',),
                          help='after setup and populate, start sky application')
def start(argv, reqs):
    prs = argparse.ArgumentParser(prog='start')
    prs.add_argument('--bash', action="store_true",
                     help='start bash prompt in place of app')
    prs.add_argument('--root', action="store_true",
                     help='start bash prompt as root in place of app')
    prs.add_argument('--repl', action="store_true",
                     help="start python repl in place of app")
    prs.add_argument('--debug', action="store_true",
                     help="start app with debug hook enabled")
    args = prs.parse_args(argv[1:])

    if args.bash:
        reqs.service.shell_start(root=False)
    elif args.root:
        reqs.service.shell_start(root=True)
    elif args.repl:
        reqs.service.repl_start()
    elif args.debug:
        reqs.service.start_debug()
    else:
        reqs.service.start()
    return


@plugins.register_command(requires=('service',), help='run unit tests')
def test(argv, reqs):
    prs = argparse.ArgumentParser(prog='test')
    prs.add_argument('--pdb', action="store_true",
                     help='pdb prompt on failures and exceptions')
    args = prs.parse_args(argv[1:])
    reqs.service.test(pdb_on_error=args.pdb)


@plugins.register_command(help='run integration tests', requires=('service',))
def int_test(args, reqs):
    try:
        index = args.index('--')
    except ValueError:
        cmd_args, sub_args = args, []
    else:
        cmd_args, sub_args = args[:index], args[index + 1:]
    reqs.service.integration_test(sub_args)


@plugins.register_command(requires=('global_sky_path', 'executor'),
                          help='start a bash prompt on the last step of crashed docker build')
def bash_broken_build(argv, reqs):
    # TODO: nest under better top-level command (e.g., "sky util" or "sky contrib")
    image_id = None
    last_run = None
    pattern = reqs.global_sky_path + '/workspace/*/log.txt'
    paths = glob.glob(pattern)
    for path in sorted(paths, reverse=True):  # search from newest to oldest
        with open(path) as f:
            for line in f:
                match = re.match('^ ---> ([a-z0-9]{12})$', line)
                if match:
                    image_id = match.groups()[0]
                if re.match('^Step [0-9]+/[0-9]+ : .*$', line):
                    last_run = line
    if not image_id:
        print 'could not find any image ids in files matching pattern %r' % pattern
        return
    if last_run:
        print last_run
        print '\n'
    reqs.executor.docker.run(
        image_id, interactive=None, tty=None,
        entrypoint='bash').interactive()


@plugins.register_command(requires=('service',),
                          help='after setup, fill database')
def populate(argv, reqs):
    reqs.service.populate()


def _build_namespace(name, _dict):
    cls = attr.make_class(name + 'Namespace', _dict.keys())
    return cls(**_dict)


@plugins.register_command(help='make a quick automated commit to trigger ci',
                          requires=('executor',))
def trigger_ci(argv, reqs):
    # TODO: move to a utils
    status = reqs.executor.git.status().batch()[0]
    modified = re.findall('^\tmodified:   .*$', status, re.MULTILINE)
    if modified:
        reqs.executor.git.stash().redirect()
    try:
        reqs.executor.git.commit(
            allow_empty=None, message="trigger ci").redirect()
        # TODO: push may fail because of un-pulled changes
        reqs.executor.git.push().redirect()
    finally:
        if modified:
            reqs.executor.command(
                ['git', 'stash', 'pop']).redirect()


@attr.s
class GitlabCI(object):
    executor = attr.ib()
    image_name, build_token, registry = attr.ib(), attr.ib(), attr.ib()

    @classmethod
    def from_env(cls, executor):
        try:
            # for info on these variables, see https://gitlab.com/help/ci/variables/README.md
            image_name, build_token, registry = [os.environ[k] for k in
                ("CI_REGISTRY_IMAGE", "CI_BUILD_TOKEN", "CI_REGISTRY")]
        except KeyError as ke:
            raise EnvironmentError(
                'required environment variable {0} not present '
                '(is build being run in a non-CI environment?)'.format(ke.message), ke.message)
        return cls(executor, image_name, build_token, registry)

    def docker_login(self):
        self.executor.docker.login(
            self.registry, username='gitlab-ci-token',
            password=self.build_token).redirect()


@plugins.register_command(
    name='build-image',
    help='build a docker image from a sky.yaml or Dockerfile',
    requires=('executor', 'docker_runner', 'logger'),
    maybe_requires=('gitlab_ci', 'build_tag', 'config', 'service'))
def build_image(args, reqs):
    # TODO: separate argument parser from function, have a
    # "plain python function" layer for these plugins
    parser = argparse.ArgumentParser(prog='build-image')
    parser.add_argument('--dockerfile',
        help='path to Dockerfile; do not generate from sky.yaml'),
    parser.add_argument('--push-gitlab', help='push to gitlab',
                        action='store_true')
    parser.add_argument('--save-local', action='store_true',
                        help='keep local copy after push')
    parser.add_argument('--dont-overwrite', action='store_true',
                        help='dont delete the untagged orphan image if one is created')
    argobj = parser.parse_args(args[1:])
    if argobj.push_gitlab:
        gitlab_ci = reqs.build_gitlab_ci()
        # login early so CI will fail-fast before docker build
        # instead of after
        gitlab_ci.docker_login()
        conf_repo_name = reqs.build_config()['repo']
        if not _is_image_of_remote(remote=conf_repo_name,
                                   image=gitlab_ci.image_name):
            raise ConfigError(
                "$CI_REGISTRY_IMAGE ({}) does not match sky.yaml repo ({})".format(
                    gitlab_ci.image_name, conf_repo_name))
        build_tag = '{}:{}'.format(
            gitlab_ci.image_name, reqs.build_build_tag())
    else:
        build_tag = reqs.build_config()['name']
    old_image_id = reqs.docker_runner.get_image_id(build_tag)
    if argobj.dockerfile:
        reqs.executor.docker.build(
            argobj.dockerfile, tag=build_tag).redirect()
    else:
        reqs.build_service().build(build_tag)
    new_image_id = reqs.docker_runner.get_image_id(build_tag)
    if argobj.push_gitlab:
        reqs.executor.docker.push(build_tag).interactive()
        if not argobj.save_local:
            reqs.executor.docker.rmi(build_tag, force=None).redirect()
    if reqs.docker_runner.get_image_id(build_tag):
        reqs.logger.comment('left docker image name {}, id {}'.format(
            build_tag, new_image_id))
    if old_image_id and new_image_id != old_image_id:
        if argobj.dont_overwrite:
            reqs.logger.comment(
                'created orphan image: old name {}, id {}'.format(
                    build_tag, old_image_id))
        else:
            reqs.executor.docker.rmi(old_image_id).redirect()


def _is_image_of_remote(image, remote):
    '''
    given a remote of the form http(s)://host(:port)/org/project.git,
    and an image name of the form host(:port)/org/project(/sub1/sub2)
    checks that the org and project name match between the two

    does not check the basic format of "remote" or "image"
    '''
    if not isinstance(remote, unicode):
        remote = remote.decode('utf-8')
    url = hyperlink.URL.from_text(remote)
    rorg, rproj = url.path[0], url.path[1][:-len(".git")]
    if not isinstance(image, unicode):
        image = image.decode('utf-8')
    url = hyperlink.URL.from_text(u"http://" + image)
    iorg, iproj = url.path[0], url.path[1]
    # docker lower-cases everything
    return (rorg.lower(), rproj.lower()) == (iorg, iproj)


@plugins.register_command(
    name='test-image',
    help='run unit self-tests of a sky docker image',
    requires=('executor', 'build_tag', 'config', 'service', 'gitlab_ci'),
    )
def test_image(args, reqs):
    # TODO: run unit tests locally as well (currently, this only works with CI)
    parser = argparse.ArgumentParser(prog='test-image')
    argobj = parser.parse_args(args[1:])
    # doesn't take any arguments, but we want consistent errors
    build_tag = '{}:{}'.format(reqs.gitlab_ci.image_name, reqs.build_tag)
    reqs.gitlab_ci.docker_login()
    reqs.service.unit_test_build(build_tag)


def get_injectables():
    def find_config_path(project_dir):
        config_path = project_dir + '/sky.local.yaml'
        if os.path.exists(config_path):
            log.sky_log.comment('using local project sky config: %r' % config_path)
        else:
            config_path = project_dir + '/sky.yaml'
            log.sky_log.comment('using project sky config: %r' % config_path)
        return config_path

    def setup_sky_path(global_sky_path):
        'set up the working directory for the current command'
        workdir = global_sky_path + '/workspace'
        ensure_path(workdir)
        num_to_keep = 50
        index_dirs = {}
        for fn in os.listdir(workdir):
            if not os.path.isdir(workdir):
                continue
            try:
                index_dirs[int(fn[:4].lstrip('0'))] = fn
            except (ValueError, TypeError):
                continue
        last = max(index_dirs.keys() + [0])
        for index in index_dirs:  # delete excess
            if index < last - num_to_keep:
                shutil.rmtree(workdir + '/' + index_dirs[index])
        cmd_info = []
        for seg in sys.argv[1:]:
            if seg.startswith('--'):
                break
            cmd_info.append(seg)
        try:
            project_dir = find_project_dir(os.getcwd())
        except (ConfigNotFound, OSError):
            project_dir = None
        if project_dir:
            project_slug = os.path.split(project_dir)[1].replace('.', '_')
        else:
            project_slug = '.'
        sky_path = '{}/{:04d}-{}-{}'.format(
            workdir, (last + 1) % 10000, project_slug, '+'.join(cmd_info))
        ensure_path(sky_path)
        log.sky_log.comment('working directory ' + sky_path)
        return sky_path

    def setup_executor(shell):
        executor = seashore.Executor(
            shell, commands=['docker_compose', 'python'])
        if sys.platform == 'darwin':
            executor = executor.patch_env(
                ALL_PROXY='socks5h://localhost:8080')
        if USE_DOCKER_MACHINE:
            executor = docker.in_ensured_docker_machine(
                executor, DOCKER_MACHINE_NAME)
        else:
            if 'DOCKER_MACHINE_NAME' in os.environ:
                print ('WARNING, SKY_USE_DOCKER_MACHINE=0'
                       ' but, shell is configured to use docker-machine'
                       + repr(os.environ['DOCKER_MACHINE_NAME']))
        return executor

    # TODO: normalize paths/dirs variable names
    # NOTE: "unnecessary" lambda is there to insulate args
    injectables = dict(
        executor=setup_executor,
        shell=shell.Shell,
        seashore_shell=lambda: seashore.Shell(),
        service=service_manager.Service,
        docker_runner=docker.Runner,
        docker_daemon_fixer=docker.DaemonFixer,
        cache=cache.DependencyCache,
        build_tag=lambda cache: cache.commit2build_tag(),
        gitlab_ci=GitlabCI.from_env,
        logger=log.build_file_enabled_logger,
        # NOTE: this path interacts with cache.rotate_logs
        log_file=lambda sky_path: open(sky_path + '/log.txt', 'ab'),
        config=config.parse,
        config_path=find_config_path,
        schema_map=config.build_schema_map,
        cache_dir=lambda global_sky_path: ensure_path(global_sky_path + '/cache'),
        proj_cache_dir=lambda sky_path: ensure_path(sky_path + '/cache'),
        project_dir=lambda: find_project_dir(os.getcwd()),
        docker_path=lambda sky_path: ensure_path(sky_path + '/docker'),
        sky_path=setup_sky_path,
        global_sky_path=lambda: ensure_path(SKY_HOST_DIR),
        sky_metadata=self_plugin.get_sky_metadata,
        site_config_url=get_site_config_url,
        site_config=site_config.get_site_config,
        use_docker_machine=USE_DOCKER_MACHINE,
    )
    return injectables


def get_site_config_url(sky_metadata):
    if sky_metadata:
        return sky_metadata.default_site_config
    elif 'SKY_SITE_CONFIG' in os.environ:
        return os.environ['SKY_SITE_CONFIG']
    raise EnvironmentError('missing sky site config, set the SKY_SITE_CONFIG'
                           ' environment variable to a valid URL')


def ensure_path(path):
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        with log.sky_log.debug('ensure_path', path=path):
            fileutils.mkdir_p(path)
    return path


class ConfigError(ValueError):
    pass


class ConfigNotFound(ConfigError):
    pass


def find_project_dir(start_dir, filename=DEFAULT_CONFIG_FILENAME):
    prev_dir = None
    cur_dir = os.path.abspath(start_dir)
    while prev_dir != cur_dir:
        if os.path.isfile(pjoin(cur_dir, filename)):
            break
        prev_dir = cur_dir
        cur_dir = os.path.dirname(cur_dir)
    else:
        raise ConfigNotFound('expected current or parent directories to'
                             ' contain %s, not found in: %s' %
                             (filename, start_dir))
    return cur_dir


def _get_plugin_reqs(plugin, injectables=None):
    if injectables is None:  # allow overriding for testability
        injectables = get_injectables()
        injectables.update(**plugin.overrides or {})
    built = typesnap.lazy_snap(
        injectables, plugin.requires or (),
        plugin.maybe_requires or ())
    return attr.make_class('Requirements', sorted(built))(**built)


def _default_exc_fmt(e_type, e_val, e_tb):
    msg = str(e_val)
    if msg:
        return '%s: %s' % (e_type.__name__, e_val)
    else:
        return e_type.__name__

# A mapping of exceptions which should not have stack traces emitted,
# and what error code they should exit with, as well as a formatting
# function for the message to print instead.
EXC_EXIT_MAP = {shell.ShellSubprocessError: (1, _default_exc_fmt),
                ConfigError: (3, _default_exc_fmt),
                ConfigNotFound: (3, _default_exc_fmt),
                services_plugin.ServiceNotFound: (3, _default_exc_fmt),
                services_plugin.InvalidPort: (3, _default_exc_fmt),
                KeyboardInterrupt: (130, _default_exc_fmt)}


def _post_init_log():
    sky_metadata = self_plugin.get_sky_metadata()
    version_dict = get_version_dict(sky_metadata)

    log.sky_log.debug('command_initialization',
                      executable=sys.executable,
                      argv=list(sys.argv),
                      cwd=os.getcwd(),
                      environ=dict(os.environ),
                      version=version_dict).success()
    return


def format_help(cmd_map):

    class SubcommandArgumentParser(argparse.ArgumentParser):
        def __init__(self, *args, **kw):
            kw['formatter_class'] = SubcommandHelpFormatter
            argparse.ArgumentParser.__init__(self, *args, **kw)
            self._positionals.title = 'Commands'
            self._optionals.title = 'Options'
            self.usage = '%(prog)s [OPTIONS] COMMAND'

    class SubcommandHelpFormatter(argparse.HelpFormatter):
        def add_arguments(self, actions):
            if not actions or not actions[0].choices:
                super(SubcommandHelpFormatter, self).add_arguments(actions)
                return
            new_actions = [argparse.Action((), dest=k, help=v.description)
                           for k, v in sorted(actions[0].choices.items(), key=lambda i: i[0])]
            super(SubcommandHelpFormatter, self).add_arguments(new_actions)

    prs = SubcommandArgumentParser(description="sky is your gateway to service development")

    subprs = prs.add_subparsers(dest='subcmd')
    for cmd_name, func in cmd_map.items():
        cmd_prs = subprs.add_parser(cmd_name, description=func.sky_plugin.help)
        cmd_prs.set_defaults(func=func)

    return prs.format_help()


@log.sky_log.wrap(level='debug')
def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if not argv or argv[0] == '-h':
        argv = ['--help']
    cmd = argv[0]
    if cmd == '--help':
        print colorama.Fore.LIGHTWHITE_EX, colorama.Back.LIGHTBLUE_EX
        print BILLBOARD
        print colorama.Style.RESET_ALL
    start = time.time()
    had_error = False
    reqs = None
    cmd_map = plugins._COMMANDS.collect()
    try:
        try:
            if cmd in cmd_map:
                plugin = cmd_map[cmd].sky_plugin
                reqs = _get_plugin_reqs(plugin)
                _post_init_log()  # this is here for post-typesnap log config
                func_ret = plugin.func(argv, reqs)
            else:
                print format_help(cmd_map)
                if cmd == '--help':
                    func_ret = 0
                else:
                    func_ret = 2, 'unrecognized command: %s' % cmd
        except Exception as e:
            had_error = True
            raise
        else:
            exit_code, exit_msg = _func_ret2exit_info(func_ret)
        finally:
            if reqs and getattr(reqs, 'logger', None):
                log_path = reqs.logger.log_file_path
                if DUMP_LOG:
                    print '\nlog.txt contents...\n'
                    print open(log_path).read()
                elif had_error:
                    print "last 20 lines of logs:"
                    print ''.join(map(_clip, open(log_path).readlines()[-20:]))
                    print 'for more details, right-click + open --> ',
                    print 'file://' + log_path
    except tuple(EXC_EXIT_MAP.keys()) as e:
        e_type, e_val, e_tb = sys.exc_info()
        try:
            types = e.__class__.mro()
        except AttributeError:
            types = [e_type]
        for t in types:
            try:
                exit_code, exit_msg_fmtr = EXC_EXIT_MAP[t]
            except (KeyError, TypeError):
                exit_code, exit_msg_fmtr = (1, _default_exc_fmt)
        exit_msg = exit_msg_fmtr(e_type, e_val, e_tb)
    except Exception as e:
        tb_str = tb_format.format_traceback()
        if not PDB:
            tb_str += " (set SKY_PDB=1 in your shell and rerun to debug)"
        print tb_str
        if PDB:
            pdb.post_mortem()
        exit_code, exit_msg = getattr(e, "returncode", 1), ''

    if exit_msg:
        print exit_msg

    return exit_code


def _clip(line, width=120):
    if len(line) < width + 1:
        return line
    clip_len = width - 20
    return (line[:clip_len] +
        '... ({} bytes snipped)\n'.format(len(line) - clip_len))


def _func_ret2exit_info(func_ret):
    """Converts the value of a subcommand return from inside the main()
    function to a 2-tuple (exit_code, exit_msg).
    """
    if func_ret is None:
        exit_code, exit_msg = 0, ''
    elif isinstance(func_ret, int):
        exit_code, exit_msg = func_ret, ''
    else:
        try:
            exit_code, exit_msg = func_ret
        except (TypeError, ValueError):
            print('Warning: main subcommand returned invalid type'
                  ' , expected None, int, or 2-tuple of'
                  ' (exit_code, exit_msg), not: %r' % func_ret)
            exit_code, exit_msg = 0, ''
    return exit_code, exit_msg


BILLBOARD = r'''
                                                                                  .
       _____ _             _____                                          _     \ _ /
      / ____| |           / ____|                                        | |  -= (_) =-
     | (___ | | ___   _  | |     ___  _ __ ___  _ __ ___   __ _ _ __   __| |    /   \
      \___ \| |/ / | | | | |    / _ \| '_ ` _ \| '_ ` _ \ / _` | '_ \ / _` |    __'  _
      ____) |   <| |_| | | |___| (_) | | | | | | | | | | | (_| | | | | (_| |  _(  )_( )_
     |_____/|_|\_\\__, |  \_____\___/|_| |_| |_|_| |_| |_|\__,_|_| |_|\__,_| (_   _    _)
   __   _          __/ |                                                       (_) (__)
 _(  )_( )_       |___/             __   _           ____       _
(_   _    _)                      _(  )_( )_       |__\_\_o,___/ \
  (_) (__)                       (_   _    _)      ([___\_\_____-\'---</opensky/<
                                   (_) (__)        | o'
'''[1:-1]


if __name__ == "__main__":
    sys.exit(main())
