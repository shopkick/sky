# Copyright (c) Shopkick 2017
# See LICENSE for details.
'''
Integration for DCOS.
Responsible for transforming logical configurations into DCOS/Marathon API
objects.
'''
import argparse
import socket
import pprint
import datetime
import time
import json
import difflib
import sys
import copy

import attr
import schema
from schema import Optional as opt
import requests
import colorama
import marathon
import boltons.iterutils

from opensky import plugins
from opensky import schema_builder


# NOTE: this one is harder to lift into site-config
ENVIRONMENTS = ('stage', 'colo1')


@plugins.register_command(
    help='dcos pool management commands',
    requires=('logger', 'service', 'config', 'build_tag', 'site_config'))
def dcos(args, reqs):
    '''
    This plugin implements the two sub-commands
    config and deploy.
    '''
    # TODO: cleaner sub-dispatch
    parser = argparse.ArgumentParser(prog='dcos')
    parser.add_argument('cmd', choices=('config', 'deploy'))
    cmd = parser.parse_args(args[1:2]).cmd
    parser.add_argument(
        '--env', choices=ENVIRONMENTS, help='deploy environment',
        required=True)
    if cmd == 'deploy':
        parser.add_argument('--mode', choices=('full', 'fast', 'canary'),
                            default='full')
    argobj = parser.parse_args(args[1:])
    if argobj.env == 'stage':
        is_stage = True
    elif argobj.env == 'colo1':
        is_stage = False
    dcos_hosts = reqs.site_config['dcos']['environments'][argobj.env]['hosts']
    client = Client(
        reqs.logger, reqs.service, reqs.config,
        stage=is_stage, dcos_hosts=dcos_hosts,
        site_config=reqs.site_config['dcos'])
    if argobj.cmd == 'config':
        # TODO: not print
        pool_configs = client._env_pool_configs(argobj.env)
        pprint.pprint({sid: client.pool2dcos(reqs.build_tag, sid, pool, argobj.env)
                       for sid, pool in pool_configs.items()})
    elif argobj.cmd == 'deploy':
        client.deploy(reqs.build_tag, argobj.env, argobj.mode)


# TODO: better plugin?
@plugins.register_config(
    path=('pools',), optional=True, type='midtier_v1')
def get_pool_config():
    posint = schema.And(int, lambda a: a >=0)
    pool_conf = schema_builder.opt_up({
        opt('instance_size'): schema.And(int, lambda n: 0 < n <= 64),
        opt('instances'): int,
        opt('instance_group'): schema.Or('Backend', 'Common'),
        opt('one_per_host', default=True): bool,
        opt('use_canary', default=True): bool,
        opt('hostname'): str,
        opt('vips'): schema_builder.none_or(schema.Use(_parse_vip)),
        opt('health_check'): {
            opt('grace_period_seconds'): posint,
            opt('interval_seconds'): posint,
            opt('timeout_seconds'): posint,
            opt('max_consecutive_failures'): posint,
            opt('command', default=DEFAULT_HEALTH_CMD): str,
        },
        opt('haproxy_group', default='internal'):
            schema.Or('internal', 'external'),
    })
    return {
        schema.Optional('default'): pool_conf,
        schema.Or(*ENVIRONMENTS): {
            #instance-id e.g. /backends/urlshortener
            str: schema_builder.nullable_dict(pool_conf),
        }
    }


@plugins.register_site_config(name='dcos')
def get_dcos_site_config():
    posint = schema_builder.posint
    return {
        'defaults': {  # TODO: defaults doesn't make sense?
            'deploy_timeout': posint,
            'canary_check_count': posint,
            'canary_check_interval': posint,
            'cpu_slice': posint,
            'mem_slice': posint,
        },
        'environments': {
            schema.Or(*ENVIRONMENTS): {
                'hosts': schema_builder.endpoints
            }
        }

    }


DEFAULT_HEALTH_CMD = (
    "test \\$(curl -sw '%{http_code}' http://${HOST}:${PORT1}/healthz"
    " -o /dev/null) -eq 200")


@attr.s
class Client(object):
    '''
    Primary API is full_deploy(tag, environment).
    '''
    _logger, _service, _config = [attr.ib(repr=False) for i in range(3)]
    dcos_hosts, stage = attr.ib(), attr.ib()
    site_config = attr.ib()

    def __attrs_post_init__(self):
        self._session = requests.Session()
        self._client = marathon.MarathonClient(
            ['http://' + h for h in self.dcos_hosts],
            session=self._session)
        try:
            # TODO: more correct handling of URLs here
            host = self.dcos_hosts[0]
            if ':' in host:  # NOTE: beware IPv6
                host = host.split(':')[0]
            socket.gethostbyname(host)
        except socket.gaierror:
            self._logger.comment(
                self.dcos_hosts[0] +
                ' did not resolve; using localhost:8080 http socks5 proxy')
            self._session.proxies = { 'http': 'socks5h://localhost:8080' }

    def deploy(self, tag, environment, mode='full'):
        '''
        Perform a full deployment, including tests.
        '''
        self._check_env(environment)
        if mode not in ('full', 'canary', 'fast'):
            raise ValueError('mode must be one of full canary or fast')
        pool_configs = self._env_pool_configs(environment)
        pool_dcos = [self.pool2dcos(tag, sid, pool, environment)
                     for sid, pool in pool_configs.items()]
        with self._logger.info('deploy', tag=tag, env=environment, mode=mode) as act:
            try:
                p_dcos = {}  # ensure json.dumps() can't fail
                if mode != 'fast':
                    for p_conf, p_dcos in zip(pool_configs.values(), pool_dcos):
                        if p_conf['use_canary']:
                            self._canary_deploy(p_dcos)
                if mode != 'canary':
                    for p_dcos in pool_dcos:
                        self._fast_deploy(p_dcos)
            except marathon.MarathonHttpError as mhe:
                # grab details of unexpected / unhandled API error
                act['error_details'] = mhe.error_details
                sys.stderr.write(
                    colorama.Fore.RED +
                    json.dumps(p_dcos, sort_keys=True, indent=2) +
                    colorama.Style.RESET_ALL)
                raise

    def _fast_deploy(self, pool_conf):
        '''
        Deploy, skipping canary step.
        '''
        with self._logger.info('deploy', instance=pool_conf['id']):
            app = pool_conf
            try:
                before = self._client.get_app(app['id'])
            except marathon.NotFoundError:
                before = {}
            self._logger.comment('CURRENT CONFIG')
            sys.stderr.write(json.dumps(
                _marathon2dict(before), sort_keys=True, indent=2))
            self._logger.comment('APP INFO TO DCOS')
            sys.stderr.write(json.dumps(app, sort_keys=True, indent=2))
            resp = self._client.update_app(
                app['id'], marathon.MarathonApp.from_json(app))
            after = self._client.get_app(app['id'])
            self._logger.comment('CONFIG CHANGES')
            sys.stderr.write(_marathon_diff(before, after))
            self._wait(resp['deploymentId'])

    def _canary_deploy(self, pool_conf):
        '''
        Perform a test deploy of a canary server to a single
        instance, leave it standing for a few minutes and check
        for errors.
        '''
        # TODO: canary for things other than backends?
        canary_app = copy.deepcopy(pool_conf)
        canary_app['instances'] = 1
        canary_app['id'] += "-canary"
        try:
            with self._logger.info('canary_deployment',
                                   instance=canary_app['id']):
                self._update_and_wait(canary_app)
            app = self._client.get_app(canary_app['id'])
            host, ports = app.tasks[0].host, app.tasks[0].ports
            conf = self.site_config['defaults']
            for i in range(conf['canary_check_count']):
                with self._logger.info('canary_status_check'):
                    time.sleep(conf['canary_check_interval'])
                    # filter down to working port
                    ports = [_check_varz(host, ports)]
        finally:
            self._ensure_deleted(canary_app['id'])

    def _env_pool_configs(self, environment):
        '''
        Get the expanded configuration of all the instances
        for a given environment.
        '''
        pool = copy.deepcopy(self._config['pools']['default'])
        env_conf = self._config['pools'].get(environment)
        if not env_conf:
            raise ValueError(
                'no pools configured for environment'
                ' {!r}'.format(environment))
        return { iid: _r_update(copy.deepcopy(pool), conf)
                 for iid, conf in env_conf.items() }

    def pool2dcos(self, tag, service_id, pool, environment):
        '''
        Convert a pool config to a dcos config suitable
        for submitting to marathon
        '''
        hostname = pool.get('hostname', service_id.rsplit('/', 1)[-1])
        vip_labels = pool['vips']
        constraints = []
        labels = {}
        pool_health_check = pool['health_check']
        health_checks = [{
            "protocol": "COMMAND",
            "command": { "value": pool_health_check['command'] },
            "gracePeriodSeconds": pool_health_check.get(
                'grace_period_seconds', 0),
            "intervalSeconds": pool_health_check.get(
                'interval_seconds', 15),
            "timeoutSeconds": pool_health_check.get(
                'timout_seconds', 10),
            "maxConsecutiveFailures": pool_health_check.get(
                'max_consecutive_failures', 6),
            "ignoreHttp1xx": False
        }]
        if environment != 'stage':
            constraints.append(['Group', 'CLUSTER', pool['instance_group']])
            labels['HAPROXY_GROUP'] = pool['haproxy_group']
        if pool['one_per_host']:
            constraints.append(['hostname', 'UNIQUE'])
        cpu_slice = self.site_config['defaults']['cpu_slice']
        mem_slice = self.site_config['defaults']['mem_slice']
        # 2 - use pool config
        return {
            "id": service_id,
            "cmd": None,
            "instances": pool['instances'],
            "cpus": cpu_slice * pool['instance_size'],
            "mem": mem_slice * pool['instance_size'],
            "container": {
                "type": "DOCKER",
                "docker": {
                    # TODO: construct tag
                    "image": ("GITLAB_HOSTNAME:PORT/REPO/" +
                               self._config['name'] + ":" + tag),
                    "network": "BRIDGE",
                    "portMappings": [
                        {
                            "containerPort": p,
                            "labels": {
                                k: v + ':' + str(p) for k,v in vip_labels.items()
                            }
                        } for p in self._config['ports']
                    ],
                    "parameters": [
                            {
                              "key": "hostname",
                              "value": hostname,
                            }
                    ],
                }
            },
            "constraints": constraints,
            "labels": labels,
            "healthChecks": health_checks,
            "upgradeStrategy": {
                "maximumOverCapacity": 1
            }
        }

    def _update_and_wait(self, app_dict):
        'update the application in DCOS and wait for deployment to complete'
        resp = self._client.update_app(
            app_dict['id'], marathon.MarathonApp.from_json(app_dict))
        self._wait(resp['deploymentId'])

    def _ensure_deleted(self, app_id):
        'ensure the application no longer exists in DCOS'
        try:
            resp = self._client.delete_app(app_id)
        except marathon.NotFoundError:
            pass  # mission already accomplished
        else:
            self._wait(resp['deploymentId'])

    def _wait(self, deployment_id):
        'wait for DCOS deployment to complete'
        start = time.time()
        with self._logger.info('wait_for_dcos'):
            delays = boltons.iterutils.backoff_iter(30, 150, 'repeat')
            deploy_timeout = self.site_config['defaults']['deploy_timeout']
            while time.time() - start < deploy_timeout:
                time.sleep(delays.next())
                try:
                    with self._logger.info('dcos_list_deployments'):
                        deployments = self._client.list_deployments()
                except Exception:
                    continue
                if deployment_id not in [e.id for e in deployments]:
                    break  # deploy complete
            else:  # loop did not break
                raise DeployTimeout(
                    'deploy did not finish within ' + str(deploy_timeout))

    def _check_env(self, environment):
        if self.stage and environment != 'stage':
            raise ValueError('cannot deploy to live with staging client')
        elif not self.stage and environment == 'stage':
            raise ValueError('cannot deploy to stage with live client')


def _check_varz(host, ports, session=requests):
    '''
    host, port, and optional session (e.g. for proxy configuration)
    returns port which successfully communicated
    '''
    network_fails = []
    varz = None
    for port in ports:
        try:
            varz = session.get(
                'http://{}:{}/varz'.format(host, port), timeout=30).json()
            break
        except Exception as e:
            network_fails.append(e)
    if varz is None:
        raise VarzNetworkErrors(network_fails)
    keys = [k for k in varz if k.startswith(('tservice__', 'pylons__')) and k.endswith('errors')]
    errors = [k for k in keys if varz[k]['count_1m'] > 0]
    if errors:
        raise VarzErrors(errors)
    return port


class DeployTimeout(Exception):
    'deploy took too long'


class VarzErrors(Exception):
    'varz fetch found problems'


class VarzNetworkErrors(VarzErrors):
    'unable to fetch varz'


def _r_update(a, b):
    'dict.update(), but recurse when both values are dicts'
    for k, v in b.items():
        if v.__class__ is dict and a.get(k).__class__ is dict:
            a[k] = _r_update(a[k], v)
        else:
            a[k] = b[k]
    return a


def _marathon2dict(val):
    'convert a marathon API object to json-compatible dict'
    def enter(path, key, value):
        try:
            value = value.json_repr(minimal=True)
        except AttributeError:
            pass
        return boltons.iterutils.default_enter(path, key, value)
    def visit(path, key, value):
        if isinstance(value, datetime.datetime):
            value = value.isoformat()
        return key, value
    return boltons.iterutils.remap(val, enter=enter, visit=visit)


def _marathon_diff(src, dst):
    '''
    Get a terminal-colorized, git style diff of the changes.
    '''
    src_json = _marathon2dict(src)
    dst_json = _marathon2dict(dst)
    def json_str(d):
        return json.dumps(d, sort_keys=True, indent=2).split('\n')
    lines = difflib.unified_diff(
        json_str(src_json), json_str(dst_json), 'before', 'after')
    colors = {'-': colorama.Fore.RED, '+': colorama.Fore.GREEN}
    colorized = []
    for line in lines:
        if line[0] in colors:
            line = colors[line[0]] + line + colorama.Style.RESET_ALL
        colorized.append(line)
    return '\n'.join(colorized)


def _parse_vip(vip_string):
    '''
    Parse a VIP-String of the form:

    host-prefix[range],host-prefix[range], ...

    some examples:
    vip-backend[001-016]
    10.19.199.[250-252]
    10.19.195.[246-249],10.19.195.[232-243]
    vip1,vip2,vip3
    '''
    all_vips = schema_builder.hosts.validate(vip_string)
    vip_dict = {}
    for n, vip in enumerate(all_vips):
        try:
            ip_list = socket.gethostbyname_ex(vip)[2]  # safe for host or ip
        except socket.error as se:  # TODO: better way to handle this?
            ip_list = [repr((vip, se))]  # mark "failed" hostname resolution
        else:
            if len(ip_list) != 1:
                ip_list = [repr(EnvironmentError(
                    'vip host {} resolved to multiple ips {}'.format(
                        vip, ip_list)))]
        vip_dict["VIP_" + str(n)] = ip_list[0]
    return vip_dict
