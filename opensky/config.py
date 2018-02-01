'''
Parser for sky.yaml config files
'''
import os.path
import re

import schema
from schema import Optional as opt
from schema import And, Or, Regex, Use
import yaml
import attr
import hyperlink

from . import plugins
from . import schema_builder


def parse(config_path, schema_map=None):
    '''
    Given a path to a yaml config file, attempt to parse
    and validate it into one of the known SKY schemas.
    '''
    if schema_map is None:
        global SCHEMA_MAP
        if SCHEMA_MAP is None:
            SCHEMA_MAP = build_schema_map()
        schema_map = SCHEMA_MAP
    data = yaml.safe_load(open(config_path))
    stype = data['type']
    if stype in schema_map:
        # ignore_extra_keys parameter allows for configuration for a given
        # plugin to be present in the sky.yaml even if that plugin is not
        # currently present
        #   (e.g. a plugin that is only relevant for CI during local dev or vice versa)
        # in the future, maybe a more fine-grained scheme than the ignore_extra_keys
        # feature of schema
        return schema.Schema(schema_map[stype], ignore_extra_keys=True).validate(data)
    raise ValueError('unknown sky type {0!r} (known types are {1!r})'.format(
        stype, schema_map.keys()))


SCHEMA_MAP = None


def build_schema_map():
    '''
    Build a map of all schema types, including gathering
    config plugins.
    '''
    schema_map = {}
    nl = schema_builder.nullable_list
    nd = schema_builder.nullable_dict
    or_n = schema_builder.none_or
    port_number = And(int, lambda n: 0 < n < 2**16)
    schema_map['midtier_v1'] = {
        'name': _ID,
        'type': 'midtier_v1',
        'owners': [str],
        'repo': _REPO,
        'python_vm': _ID,
        'ports': Or(
                    [port_number],
                    [dict(port=port_number, name=str)]),
        opt('flags'): {
            opt('dev'): nl([str]),
            opt('beta'): nl([str]),
            opt('stage'): nl([str]),
            opt('colo1'): nl([str]),
        },
        opt('midtier_deps'): or_n([Regex(r'^' + _REPO_PATTERN + r'$')]),
        opt('persistence_deps'): {
            opt('mysql'): { 'db_name': _ID },
            opt('cassandra', default=False): Or(True, False),
            opt('redis', default=False): Or(True, False),
            opt('zookeeper', default=False): Or(True, False),
            opt('memcached', default=False): Or(True, False),
        },
        'library_deps': schema_builder.default_if_none(
            {
                opt('pip'): nl([Use(PipPkg.parse)]),
                opt('conda'): nl([Use(CondaPkg.parse)]),
                opt('sky'): And(
                    # TODO: "lock in" after path regex matches,
                    # do not do back-tracking and try to match
                    # _REPO_COMMIT_PATTERN instead surface the
                    # error that path does not exist
                    nd({_ID: Or(And(Regex(r'^[\./~]'),
                                 Use(os.path.expanduser),
                                 os.path.isdir),
                             Regex(_REPO_COMMIT_PATTERN))}),
                    Use(parse_skydeps)),
                opt('yum'): nl([str]),
            },
            lambda v: {'pip': [], 'conda': [], 'sky': {}, 'yum': []}),
        opt('setup_cmds', default=()): nl([str, [str]]),
    }

    schema_map['repo_v1'] = {
        'name': _ID,
        'type': 'repo_v1',
        'owners': [str],
        'repo': _REPO,
        'python_vm': _ID,
        'library_deps': or_n(
            {opt('pip'): [Use(PipPkg.parse)],
             opt('conda'): [Use(CondaPkg.parse)]})
    }

    config_plugins = plugins._CONFIG.collect()
    for config_plugin_builder in config_plugins.values():
        validator = config_plugin_builder()
        config_plugin = config_plugin_builder.sky_plugin
        #TODO: better error message on attempt to patch non-existent type
        patch = schema_map[config_plugin.type]
        for seg in config_plugin.path[:-1]:
            if not isinstance(patch[seg], dict):
                raise TypeError(
                    'plugin {!r} expected dict not {!r} along path {!r}'.format(
                        config_plugin_builder, type(patch[seg]), config_plugin.path))
            patch = patch[seg]
        key = config_plugin.path[-1]
        if key in patch:
            raise ValueError(
                'plugin {!r} would override key {!r}'.format(
                config_plugin_builder, key))
        assert key not in patch
        if config_plugin.optional:
            key = opt(key)
        patch[key] = validator

    return schema_map


CUR_DIR = os.path.dirname(os.path.abspath(__file__))


_ID = Regex('^[A-Za-z_][A-Za-z0-9_]+$')
# helps keep quote characters, whitespace, etc from slipping into
# some values by limiting them to valid C idnetifiers


_REPO_PATTERN = ( # from http://stackoverflow.com/a/22312124
    r'((git|ssh|http(s)?)|(git@[\w\.]+))(:(//)?)([\w\.@\:/\-~]+)(\.git)(/)?')
_REPO_COMMIT_PATTERN = r'^' + _REPO_PATTERN + r'#[^#]+$'
_REPO = Regex(r'^' + _REPO_PATTERN + r'$')


@attr.s(frozen=True)
class GitRemoteRef(object):
    '''
    Represents a git remote; has all of the information
    necessary to clone + checkout a git repo.
    Primarily constructed by an input string of the form
    [git-url]#[git-ref]

    A "git-url" is defined as any valid input to git clone.
    A "git-ref" is defined as any valid input to git checkout.

    If there is something that git clone or git checkout accepts
    which this class does not parse, that is a bug in this class.
    '''
    scheme, host, userinfo, path, ref = [attr.ib() for i in range(5)]

    @classmethod
    def from_text(cls, text):
        if isinstance(text, str):
            text = text.decode('utf-8')
        if not re.match(_REPO_PATTERN, text.rsplit('#', 1)[0]):
            raise ValueError('not a valid git url: {!r}'.format(text))
        if text.startswith('http://') or text.startswith('https://'):
            git_url = hyperlink.URL.from_text(text)
            path = git_url.path
            ref = git_url.fragment
            scheme = git_url.scheme
        else:
            _git_url, git_path = text.rsplit(':', 1)
            git_url = hyperlink.URL.from_text(_git_url)
            if not git_url.scheme:  # implicit ssh
                git_url = hyperlink.URL.from_text(u'ssh://' + _git_url)
                scheme = u''
            else:
                scheme = git_url.scheme
            git_path = hyperlink.URL.from_text(git_path)
            path = git_path.path
            ref = git_path.fragment
        host, userinfo = git_url.host, git_url.userinfo
        return cls(
            scheme=scheme, host=git_url.host,
            userinfo=git_url.userinfo, path=path, ref=ref)

    @property
    def url(self):
        're-assemble the git-url suitable for passing to git-clone'
        if self.scheme in ('http', 'https'):
            return hyperlink.URL(
                scheme=self.scheme, host=self.host, userinfo=self.userinfo,
                path=self.path).to_text()
        base = hyperlink.URL(scheme=self.scheme, host=self.host,
            userinfo=self.userinfo).to_text()
        if base.startswith('//'):  # if scheme is ''
            base = base[2:]
        return base + u':' + u'/'.join(self.path)

    def to_text(self):
        if self.ref:
            return self.url + u'#' + self.ref
        return self.url


@attr.s(repr=False)
class _Rebuild(object):
    raw = attr.ib()
    _attrs = attr.ib(repr=False)

    @classmethod
    def parse(cls, spec):
        match = re.match(cls.pattern, spec)
        if not match:
            raise ValueError(cls.error)
        return cls(raw=spec, attrs=match.groupdict(None))

    def __getattr__(self, name):
        try:
            return self._attrs[name]
        except KeyError:
            raise AttributeError(
                '{0!r} object has no attribute {1!r}'.format(self.__class__, name), name)

    def __repr__(self):
        return "{0}({1!r})".format(self.__class__.__name__, self.raw)


class CondaPkg(_Rebuild):
    pattern = (
        '^(?P<channel>[A-Za-z0-9_\-]+)/(?P<pkg>[A-Za-z0-9_\-\.]+)==(?P<ver>\d+(\.\d+)*)$')
    error = "not a conda package of form '{channel}/{pkg}=={ver}'"


class PipPkg(_Rebuild):
    pattern = (
        '(^(?P<pkg>[A-Za-z0-9_\-]+)==(?P<ver>\d+(\.\w+)*)$)|'
        '(git\+https://[^#]+#egg=(?P<egg>[A-Za-z0-9_\-]+))')
    error = "not a pip package of form '{pkg}=={ver}' or 'git+https://.../#egg={pkg}'"


def parse_skydeps(_dict):
    all_deps = []
    for name, loc in _dict.items():
        if loc.startswith(('.', '/')):
            dep = LocalSkyDep(name, loc)
        else:
            repo, _, refspec = loc.partition('#')
            dep = SkyDep(name, repo, refspec)
        all_deps.append(dep)
    return all_deps


@attr.s(frozen=True)
class SkyDep(object):
    '''
    name = logical name, also destination path within docker image
    repo = git repo (local path or remote addr to clone from)
    refspec = git branch/tag/commit to checkout to
    '''
    is_local = False
    name, repo, refspec = attr.ib(), attr.ib(), attr.ib()


@attr.s(frozen=True)
class LocalSkyDep(object):
    is_local = True
    name, path = attr.ib(), attr.ib()
