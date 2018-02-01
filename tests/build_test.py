import os
import binascii

import attr
import seashore
from boltons import iterutils

import opensky.cmd


def test_build_image(tmpdir):
    under_test = opensky.cmd.build_image

    def fresh_reqs():
        injectables = _build_test_injectables(tmpdir)
        return opensky.cmd._get_plugin_reqs(
            under_test.sky_plugin, injectables)

    reqs = fresh_reqs()  # refresh reqs from scratch

    def test_run(args):
        under_test(args, reqs)

    test_run(['build-image'])
    tmpdir.ensure('manual-docker/Dockerfile').write('')
    dockerfile = str(tmpdir.ensure_dir('manual-docker'))
    os.environ['CI_REGISTRY_IMAGE'] = 'company-gitlab.com:1234/org/test_app'
    os.environ['CI_BUILD_TOKEN'] = 'password'
    os.environ['CI_REGISTRY'] = 'somehost:1234'
    test_run(['build-image', '--dockerfile', dockerfile])
    test_run(['build-image', '--push-gitlab'])
    test_run(['build-image', '--push-gitlab', '--save-local'])
    test_run(['build-image', '--dockerfile', dockerfile, '--push-gitlab'])
    test_run(['build-image', '--dockerfile', dockerfile, '--push-gitlab',
              '--save-local'])
    try:
        os.environ['CI_REGISTRY_IMAGE'] = 'company-gitlab.com:1234/org/other_app'
        reqs = fresh_reqs()  # refresh to pick up new CI_REGISTRY_IMAGE
        test_run(['build-image', '--push-gitlab'])
    except opensky.cmd.ConfigError:
        pass
    else:
        assert False, "allowed mis-matched repo and CI_REGISTRY_IMAGE"


def test_test_image(tmpdir):
    os.environ['CI_REGISTRY_IMAGE'] = 'company-gitlab.com:1234/org/test_app'
    os.environ['CI_BUILD_TOKEN'] = 'password'
    os.environ['CI_REGISTRY'] = 'somehost:1234'
    injectables = _build_test_injectables(tmpdir)
    reqs = opensky.cmd._get_plugin_reqs(
        opensky.cmd.test_image.sky_plugin, injectables)
    opensky.cmd.test_image(['test-image'], reqs)



def _build_test_injectables(tmpdir):
    test_app = 'test_app'

    def mktempd(name):
        return _flatten_path(tmpdir.ensure(name, dir=True))

    project_dir = mktempd('project_dir')
    code = tmpdir.ensure('project_dir/' + test_app + '/__main__.py')
    code.write('# auto-generated empty project code')

    injectables = opensky.cmd.get_injectables()
    injectables.update(dict(
        project_dir=project_dir,
        global_sky_path=mktempd('global-sky-path'),
        cache=FakeCache(tmpdir),
        executor=seashore.Executor(FakeShell()),
        config={
            'name': test_app,
            'repo': 'http://company-gitlab.com/org/test_app.git',
            'ports': [1],
            'library_deps': {'sky': []},
            'setup_cmds': [],
        },
        site_config={
            'pip': {},
            'services': {'default_base_build': {'docker_image': "shopkick/dockerbase-build:17.09.27_1506471265_83a63535"}}
        },
        ))
    return injectables


@attr.s
class FakeCache(object):
    tmpdir = attr.ib()
    http_downloaded = attr.ib(default=attr.Factory(dict))
    project_git = attr.ib(default=attr.Factory(dict))

    def get_url(self, name, url):
        if name not in self.http_downloaded:
            self.http_downloaded[name] = self.tmpdir.ensure(
                'fake_cache/http/' + opensky.cache.escape_remote(url))
        return _flatten_path(self.http_downloaded[name])

    def pull_project_git(self, name, remote):
        if name not in self.project_git:
            self.project_git[name] = self.tmpdir.ensure(
                'fake_cache/' + opensky.cache.escape_remote(remote))
        return _flatten_path(self.project_git[name])

    def commit2build_tag(self):
        return '17.04.14_1492203572_4d413fd4'

    def get_anaconda(self):
        return self.get_url(
            opensky.cache.ANACONDA_INSTALLER,
            opensky.cache.ANACONDA_URL)


@attr.s
class FakeDocker(object):
    local_images = attr.ib(default=attr.Factory(dict))

    def build(self, args):
        if args[0] == '--tag':
            args.pop(0)
            tag = args.pop(0)
        else:
            tag = binascii.hexlify(os.urandom(4))
        assert os.path.exists(args[0] +'/Dockerfile')
        self.local_images[tag] = args[0]
        return tag

    def run(self, args):
        if args != ['--entrypoint', '/home/app/miniconda2/bin/python',
                    'company-gitlab.com:1234/org/test_app:17.04.14_1492203572_4d413fd4',
                    '/home/app/main.py', 'TEST']:
            raise ValueError(args)
        self.local_images[args[2]] = args[2]
        return 'test-image-output'

    def login(self, args):
        # TODO more robust argument testing
        if set(args) != set(['--password', 'password', '--username',
                             'gitlab-ci-token', 'somehost:1234']):
            raise ValueError(args)
        return 'test-login-output'

    def images(self, args):
        valid_args = [
            ['--quiet', 'test_app'],
            ['--quiet', 'company-gitlab.com:1234/org/test_app:17.04.14_1492203572_4d413fd4']]
        if args not in valid_args:
            raise ValueError(args)
        return '1234-test-image-id'

    def push(self, args):
        if args != ['company-gitlab.com:1234/org/test_app:17.04.14_1492203572_4d413fd4']:
            raise ValueError(args)
        return 'test-push-result'

    def rmi(self, args):
        valid_args = [
            ['--force', 'company-gitlab.com:1234/org/test_app:17.04.14_1492203572_4d413fd4']
        ]
        if args not in valid_args:
            raise ValueError(args)
        return 'test-rmi-result'


@attr.s
class FakeShell(object):
    env = attr.ib(default=attr.Factory(dict))
    cwd = attr.ib(default=os.getcwd())
    docker = attr.ib(default=attr.Factory(FakeDocker))
    def _do_cmd(self, cmd, **kw):
        if cmd == ['env', 'dump']:
            return '\n'.join([k + '=' + v for k,v in self.env.items()]), ''
        if cmd[0] == 'docker':
            return getattr(self.docker, cmd[1])(cmd[2:]), ''
        raise ValueError('unhandled call: ' + repr(cmd) + repr(kw))
    interactive = redirect = batch = _do_cmd
    def clone(self): return attr.evolve(self, env=self.env.copy())
    def chdir(self, dir): self.cwd = dir
    def setenv(self, key, val): self.env[key] = val


def _flatten_path(pathobj):
    return pathobj.dirname + '/' + pathobj.basename
