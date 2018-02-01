'''
Provides API for executing commands against the shell.
'''
import os
import os.path

import attr
from seashore import ProcessError


class ShellSubprocessError(Exception):
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



@attr.s
class Shell(object):
    '''
    Wraps seashore execution.

    Modifies behavior:
      * failed execution = exit process
    '''
    _seashore_shell, _logger, _log_file = attr.ib(), attr.ib(), attr.ib()

    def batch(self, command, cwd=None):
        '''
        Call a sub-process like a remote procedure call:
        stdin closed, return stdout and stderr
        '''
        return self._call(command, self._seashore_shell.batch, cwd,
            will_print=False)

    def interactive(self, command, cwd=None):
        '''
        Call a sub-process in the foreground:
        stdin + stdout work.
        '''
        return self._call(command, self._seashore_shell.interactive, cwd)

    def redirect(self, command, stdout=None, stderr=None,
                 env=None, cwd=None):
        '''
        Call a sub-process in the background:
        stdin is closed, stdout and stderr go to logfile.
        '''
        out = stdout or self._log_file
        err = stderr or self._log_file
        def callthru(command, cwd):
            return self._seashore_shell.redirect(
                command, outfp=out, errfp=err, cwd=cwd)
        return self._call(command, callthru, cwd, will_print=False)

    def clone(self):
        return self.__class__(
            self._seashore_shell.clone(), self._logger, self._log_file)

    def setenv(self, key, value):
        return self._seashore_shell.setenv(key, value)

    def getenv(self, key):
        return self._seashore_shell.getenv(key)

    def chdir(self, path):
        return self._seashore_shell.chdir(path)

    def _call(self, command, callthru, cwd, will_print=True):
        if isinstance(command, basestring):
            log_name = command
        else:
            log_name = ' '.join(command)
        with self._logger.info(log_name) as act:
            if will_print:
                print('')  # force logging to newline
            self._log_file.write("### {} \n".format(repr(command)))
            self._log_file.flush()
            try:
                return callthru(command, cwd=cwd)
            except ProcessError as pe:
                act['return_code'] = pe.returncode
                act.failure(
                    "command {action_name} exited with status {return_code}"
                    " (output: {!r}, error: {!r}) {data_map_repr}",
                    getattr(pe, "output", ""),
                    getattr(pe, "error", ""))
                # TODO: env
                raise ShellSubprocessError(pe.returncode, command, cwd=cwd, env=None)
        assert False  # should never get here

    def log_file(self, path):
        if os.path.exists(path):
            lines = list(open(path))
            self._log_file.write('### file {0} ({1} lines):\n'.format(path, len(lines)))
            self._log_file.write(''.join(lines))
            self._log_file.write('### end file {0}\n'.format(path))
