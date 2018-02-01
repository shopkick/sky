# Copyright (c) Shopkick 2017
# See LICENSE for details.
import seashore
import attr

from opensky import docker


def test_in_ensured_docker_machine():
    @attr.s
    class TestShell(object):
        machine = attr.ib(default=attr.Factory(DockerMachine))
        env = attr.ib(default=attr.Factory(dict))
        def _do_cmd(self, cmd):
            if cmd == ['env', 'dump']:
                return '\n'.join([k + '=' + v for k,v in self.env.items()]), ''
            if cmd[0] == 'docker-machine':
                return getattr(self.machine, cmd[1]).__call__(*cmd[2:]) or '', ''
            raise ValueError('unhandled call: ' + repr(cmd))
        interactive = batch = _do_cmd
        def clone(self): return TestShell(self.machine, dict(self.env))
        def setenv(self, key, val): self.env[key] = val

    shell = TestShell()
    exec_ = seashore.Executor(shell, commands=['env'])
    docker.in_ensured_docker_machine(exec_, 'test')
    shell.machine.stop('test')
    exec_ = docker.in_ensured_docker_machine(exec_, 'test')
    assert 'DOCKER_MACHINE_NAME=test' in exec_.env.dump().batch()[0]


@attr.s
class DockerMachine(object):
    'test docker machine'
    vms = attr.ib(init=False, default=attr.Factory(dict))
    ips = attr.ib(init=False, default=attr.Factory(dict))
    next_ip = attr.ib(init=False, default=0)

    def status(self, name):
        try:
            return self.vms[name]
        except KeyError:
            raise seashore.ProcessError(1, '', 'Host does not exist: ' + name)

    def _set_running(self, name):
        self.vms[name] = 'Running\n'
        self.next_ip += 1
        self.ips[name] = '192.168.{}.{}'.format(
            self.next_ip / 256, self.next_ip % 256)

    def start(self, name):
        assert self.vms[name] in ('Saved\n', 'Stopped\n')
        self._set_running(name)

    def create(self, name):
        self._set_running(name)

    def stop(self, name):
        assert self.vms[name] == 'Running\n'
        self.vms[name] = 'Stopped\n'

    def env(self, tag, val, name):
        # TODO: plausible value
        assert tag == '--shell'
        assert val == 'cmd'
        return MACHINE_ENV

    def ip(self, name):
        assert self.vms[name] == 'Running\n'
        return self.ips[name]


MACHINE_ENV = '''\
SET DOCKER_TLS_VERIFY=1
SET DOCKER_HOST=tcp://192.168.99.100:2376
SET DOCKER_CERT_PATH=/Users/kurtrose/.docker/machine/machines/test
SET DOCKER_MACHINE_NAME=test
REM Run this command to configure your shell: 
REM     @FOR /f "tokens=*" %i IN ('docker-machine env --shell cmd test') DO @%i
'''
