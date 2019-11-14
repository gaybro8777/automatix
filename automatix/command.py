import os
import re
import subprocess

from shlex import quote

from .constants import CONSTANTS
from .logger import LOG

ENCODING = os.getenv('ENCODING', 'utf-8')
IMPORT_PATH = '.'
SSH_CMD = 'ssh {hostname} sudo '
REMOTE_TMP_DIR = 'automatix_tmp'


class Command:
    def __init__(self, pipeline_cmd: dict, index: int, systems: dict, variables: dict, imports: []):
        self.pipeline_cmd = pipeline_cmd
        self.index = index
        self.systems = systems
        self.vars = variables
        self.imports = imports
        for key, value in pipeline_cmd.items():
            self.orig_key = key
            self.assignment, self.assignment_var, self.key = get_assignment_var(key=key)
            if isinstance(value, dict):
                # We need this workaround because the yaml lib returns a dictionary instead of a string,
                # if there is nothing but a variable in the command. Alternative is to use quotes in the config yaml.
                self.value = f'{{{next(iter(value))}}}'
            else:
                self.value = value
            break

    def get_type(self):
        if self.key == 'local':
            return 'local'
        if self.key == 'manual':
            return 'manual'
        if self.key == 'python':
            return 'python'
        if re.search('remote@', self.key):
            return 'remote'
        raise UnknownCommandException(f'Command type {self.key} is not known.')

    def get_system(self):
        if self.get_type() == 'remote':
            return self.systems[re.search('remote@(.*)', self.key).group(1)]
        return 'localhost'

    def get_resolved_value(self):
        variables = self.vars.copy()
        for key, value in CONSTANTS.items():
            variables[f'const_{key}'] = value
        return self.value.format(**variables)

    def execute(self, interactive: bool = False, force: bool = False):
        LOG.notice(f'\n({self.index}) [{self.orig_key}]: {self.get_resolved_value()}')
        return_code = 0

        if self.get_type() == 'manual' or interactive:
            answer = input(f'Proceed? (p: proceed (default), s: skip, a: abort)')
            if answer == 's':
                return
            if answer == 'a':
                raise AbortException(str(1))

        if self.get_type() == 'local':
            return_code = self._local_action()
        if self.get_type() == 'python':
            return_code = self._python_action()
        if self.get_type() == 'remote':
            return_code = self._remote_action()

        if return_code != 0:
            LOG.error(f'Command ({self.index}) failed with return code {return_code}.')
            if force:
                return
            err_answer = input('What should I do? (p: proceed (default), r: retry, a: abort)')
            if err_answer == 'r':
                return self.execute(interactive)
            if err_answer == 'a':
                raise AbortException(str(return_code))

    def _local_action(self) -> int:
        cmd = self._build_command(path=IMPORT_PATH)
        try:
            return self._run_local_command(cmd=cmd)
        except KeyboardInterrupt:
            LOG.info('Abort command by user key stroke. Exit code is set to 130.')
            return 130

    def _python_action(self) -> int:
        cmd = self.get_resolved_value()
        # for key, value in self.systems.items():
        #    exec(f'{key}_node = BW_REPO.get_node(value)')
        try:
            LOG.debug(f'Run python command: {cmd}')
            if self.assignment_var:
                exec(f'self.vars[self.assignment_var] = {cmd}')
                LOG.info(f'Variable {self.assignment_var} = {self.vars[self.assignment_var]}')
            else:
                exec(cmd)
            return 0
        except KeyboardInterrupt:
            LOG.info('Abort command by user key stroke. Exit code is set to 130.')
            return 130
        except Exception as exc:
            LOG.error(exc)
            return 1

    def _remote_action(self) -> int:
        # node = BW_REPO.get_node(self.get_system())
        hostname = self.get_system()
        ssh_cmd = SSH_CMD.format(hostname=hostname)
        remote_cmd = self.get_resolved_value()
        prefix = ''
        if self.imports:
            prefix = f'tar -C {IMPORT_PATH} -cf - ' + ' '.join(self.imports) + ' | '
            remote_cmd = f'mkdir {REMOTE_TMP_DIR};' \
                f' tar -C {REMOTE_TMP_DIR} -xf -;' \
                f' {self._build_command(path=REMOTE_TMP_DIR)}'

        cmd = f'{prefix}{ssh_cmd}{quote("bash -c " + quote(remote_cmd))}'

        try:
            exitcode = self._run_local_command(cmd=cmd)
        except KeyboardInterrupt:
            LOG.info('Abort command by user key stroke. Exit code is set to 130.')
            exitcode = 130

            ps_pids = get_remote_pids(hostname=hostname, cmd=self.get_resolved_value())
            while ps_pids:
                LOG.notice('Remote command seems still to be running! Found PIDs: {}'.format(','.join(ps_pids)))
                answer = input(
                    'What should I do? '
                    '(i: send SIGINT (default), t: send SIGTERM, k: send SIGKILL, p: do nothing and proceed) ')

                if answer == 'p':
                    break
                elif answer == 't':
                    signal = 'TERM'
                elif answer == 'k':
                    signal = 'KILL'
                else:
                    signal = 'INT'
                for pid in ps_pids:
                    kill_cmd = f'{ssh_cmd} kill -{signal} {pid}'
                    LOG.info(f'Kill {pid} on {hostname}')
                    subprocess.run(kill_cmd, shell=True)

                ps_pids = get_remote_pids(hostname=hostname, cmd=self.get_resolved_value())
            LOG.info('Keystroke interrupt handled.\n')

        if self.imports:
            cleanup_cmd = f'{ssh_cmd} rm -r {REMOTE_TMP_DIR}'
            LOG.debug(f'Executing: {cleanup_cmd}')
            proc = subprocess.run(cleanup_cmd, shell=True, executable='/bin/bash')
            if proc.returncode != 0:
                LOG.warning(f'Failed to remove {REMOTE_TMP_DIR}, exitcode: {proc.returncode}')

        return exitcode

    def _build_command(self, path: str) -> str:
        if not self.imports:
            return self.get_resolved_value()
        return f'. {path}/' + f'; . {path}/'.join(self.imports) + '; ' + self.get_resolved_value()

    def _run_local_command(self, cmd: str) -> int:
        LOG.debug(f'Executing: {cmd}')
        if self.assignment:
            proc = subprocess.run(cmd, shell=True, executable='/bin/bash', stdout=subprocess.PIPE)
            output = proc.stdout.decode(ENCODING)
            self.vars[self.assignment_var] = output
            LOG.info(f'Variable {self.assignment_var} = {output}')
        else:
            proc = subprocess.run(cmd, shell=True, executable='/bin/bash')
        return proc.returncode


def get_remote_pids(hostname, cmd) -> []:
    ps_cmd = f"ps axu | grep {quote(cmd)} | grep -v 'grep' | awk '{{print $2}}'"
    cmd = f'ssh {hostname} {quote(ps_cmd)} 2>&1'
    pids = subprocess.check_output(cmd, shell=True, executable='/bin/bash').decode(ENCODING).split()

    return pids


def get_assignment_var(key) -> (bool, str, str):
    if not re.search('=', key):
        return False, '', key
    return (True,) + re.search('(.*)=(.*)', key).group(1, 2)


class AbortException(Exception):
    pass


class UnknownCommandException(Exception):
    pass