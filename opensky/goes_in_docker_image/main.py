import os
import os.path
import sys
import time
import socket
import subprocess
import atexit
import json


CONFIG = json.load(open(
    os.path.dirname(os.path.abspath(__file__)) + '/config.json'))
NAME = CONFIG['project_name']
DEPENDS_ON = CONFIG['start_depends_on']


class SubprocessError(SystemExit, Exception):
    """This exception type exists for raising when a subprocess call
    fails. It's catchable, but also if left uncaught, it doesn't yield
    a huge stack trace.
    """
    def __init__(self, code, cmd, cwd, env):
        self.code = code
        self.cmd = cmd
        self.cwd = cwd
        self.env = env

    def __repr__(self):
        cn = self.__class__.__name__
        return ('%s(code=%r, cmd=%r, cwd=%r, env=%r)'
                % (cn, self.code, self.cmd, self.cwd, self.env))

    def __str__(self):
        msg = 'command %r exited with code %r' % (self.cmd, self.code)
        if self.cwd:
            msg += ' (cwd = %r)' % self.cwd
        if self.env:
            msg += ' (env = %r)' % self.env
        return msg


def _make_cleanup(subprocess):
    # capture subprocess into a closure, so that it isn't
    # gc'd before the cleanup gets a chance to run
    @atexit.register
    def cleanup():
        'cleanup any running subprocess instances cleanly'
        subprocess._cleanup()
        for p in subprocess._active:
            os.kill(p.pid, 15)  # SIGTERM
        start = time.time()
        while time.time() - start < 10:  # 10 seconds to shutdown
            time.sleep(0.25)  # quarter second should feel okay
            subprocess._cleanup()
            for p in subprocess._active:
                try:
                    os.kill(p.pid, 0)
                    break  # process was still alive
                except OSError:
                    pass  # good, process is dead
            else:  # no process was alive
                break
        else:  # wait loop ran to completion
            subprocess._cleanup()
            for p in subprocess._active:
                os.kill(p.pid, 9)  # force kill
    return cleanup


cleanup = _make_cleanup(subprocess)


def wait_for(host, port, timeout):
    ips = "({})".format(",".join(sorted(
        set([e[4][0] for e in socket.getaddrinfo(host, None)]))))
    print 'waiting for', host, ips, port, 'to come ready...'
    start = time.time()
    time_left = lambda: '{}/{}'.format(round(time.time() - start, 1), timeout)
    while time.time() - start < timeout:
        sock = socket.socket()
        try:
            sock.connect( (host, port) )
            print '\r', time_left(), 'ready' + ' ' * 50
            break
        except socket.error as e:
            print '\r', time_left(), e,
            time.sleep(0.3)
        finally:
            sock.close()
    else:
        print '\rTIMED OUT AFTER', timeout, 'SECONDS WAITING FOR', host, port
        sys.exit(1)


def run_py(*py_argv, **subprocess_args):
    cmd_args = [sys.executable] + list(py_argv)
    print 'running python script: ', ' '.join(cmd_args)
    try:
        subprocess.check_call(cmd_args, **subprocess_args)
    except KeyboardInterrupt:
        sys.exit(0)
    except subprocess.CalledProcessError as cpe:
        cwd = subprocess_args.get('cwd', os.getcwd())
        env = subprocess_args.get('env', os.environ)
        spe = SubprocessError(cpe.returncode, cpe.cmd, cwd=cwd, env=env)
        print spe
        raise spe


def run_cmd(*argv):
    print 'running command', ' '.join(argv)
    cwd = '/home/app/' + NAME
    try:
        return subprocess.check_call(argv, cwd=cwd)
    except subprocess.CalledProcessError as cpe:
        spe = SubprocessError(cpe.returncode, cpe.cmd, env=os.environ, cwd=cwd)
        print spe
        raise spe


try:  # TODO: is this still needed?
    os.makedirs('/home/app/' + NAME)
except OSError:
    pass


cmd = sys.argv[1]
finished = True
if cmd == 'SETUP':
    if 'mysql' in DEPENDS_ON:
        import sqlalchemy as sa
        DBNAME = CONFIG['db_name']
        print "connecting to mysql with user root"
        wait_for('mysql', 3306, 30)
        engine = sa.create_engine('mysql://root@mysql')
        conn = engine.connect()
        conn.execute('SELECT 1')
        print "SELECT 1 works"
        conn.execute('commit')
        print "creating database", DBNAME
        conn.execute('CREATE DATABASE IF NOT EXISTS ' + DBNAME)
        conn.close()
        print "running migration scripts"
        db_url = "mysql://root@mysql/" + DBNAME
        # mark database as under version control
        run_py("migrations/manage.py", "version_control", db_url,
               cwd="/home/app/" + NAME)
        # upgrade to latest version
        run_py("migrations/manage.py", "upgrade", db_url,
               cwd="/home/app/" + NAME)
    else:
        print "no database dependency, skipping mysql"
    if 'cassandra' in DEPENDS_ON:
        import cdeploy.migrator
        wait_for('cassandra', 9042, 30)
        print "running cassandra migrations"
        argv_bak = sys.argv  # temporarily change sys.argv
        sys.argv = [  # simulate a command-line call
            'cdeploy',
            '/home/app/{}/migrations/cassandra'.format(NAME)]
        cdeploy.migrator.main()
        sys.argv = argv_bak
    print "setting up zookeeper"
    wait_for('zookeeper', 2181, 30)

    from kazoo.client import KazooClient

    SERVICE_ENDPOINTS = CONFIG['host_ports']
    print 'connecting to zookeeper'
    zk = KazooClient(hosts='zookeeper:2181')
    zk.start()
    for service, paths in SERVICE_ENDPOINTS.items():
        for path in paths:
            full_path = '/services/cluster_local/' + service + '/' + path
            if not zk.exists(full_path):
                zk.create(full_path, makepath=True)
    zk.stop()
    print 'setup complete'
elif cmd == 'START':
    if 'mysql' in DEPENDS_ON:
        wait_for('mysql', 3306, 30)
    wait_for('zookeeper', 2181, 30)
    if 'cassandra' in DEPENDS_ON:
        wait_for('cassandra', 9042, 30)
    run_py('-m', NAME, '--nodaemon',
           '--flagfile=/home/app/development-flags')
elif cmd == 'START_DEBUG':
    # TODO: argparse/dedupe
    if 'mysql' in DEPENDS_ON:
        wait_for('mysql', 3306, 30)
    wait_for('zookeeper', 2181, 30)
    if 'cassandra' in DEPENDS_ON:
        wait_for('cassandra', 9042, 30)
    env = dict(os.environ)
    py_path = env.get('PYTHONPATH', '')
    py_path = '/home/app/debug:' + py_path if py_path else '/home/app/debug'
    env['PYTHONPATH'] = py_path
    run_py('-m', NAME, '--nodaemon',
           '--flagfile=/home/app/development-flags',
           cwd='/home/app', env=env)
elif cmd == 'TEST':
    run_py('-m', 'pytest', '/home/app/' + NAME)
elif cmd == 'TEST_PDB':
    run_py('-m', 'pytest', '/home/app/' + NAME, '-s', '--pdb')
elif cmd == 'RUN_LIVE':
    env_type = os.environ.get('ENV_TYPE') or 'prod'
    if env_type == 'stage':
        flagfile = '/home/app/stage-flags'
    elif env_type == 'beta':
        flagfile = '/home/app/beta-flags'
    elif env_type == 'prod':
        flagfile = '/home/app/production-flags'
    else:
        raise EnvironmentError('unknown ENV_TYPE: ' + repr(env_type))
    run_py('-m', NAME, '--nodaemon',
           '--flagfile=' + flagfile)
elif cmd == 'INT_TEST':
    endpoint = CONFIG['host_ports'][NAME][0].split(':')
    # TODO: better parsing of sub-args
    wait_for(endpoint[0], int(endpoint[1]), 60)
    run_py(*(
        ['-m', 'pytest'] + sys.argv[2:] +
        ['/home/app/integration_test/']))
elif cmd == 'REPL':
    run_py()
elif cmd == 'POPULATE':
    finished = False
else:
    raise EnvironmentError('unrecognized command:' + cmd)
if finished:
    cleanup()
    raise SystemExit
