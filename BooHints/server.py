"""
Implements an interface with the Boo compiler hints server.

TODO: Explore YouCompleteMe to easily create a Vim plugin
TODO: Silly parser for .booproj files if no rsp is found
"""

import os
import subprocess
import threading
import json
import time
import logging
# Work around Python 3 module renames
try:
    import queue
except:
    import Queue as queue

logger = logging.getLogger('boo.server')


class Server(object):
    """ Represents a connection with the hints server, taking care of spawning
        a child process running the compiler in server mode and handling the
        communication with it via standard pipes.
    """

    def __init__(self, bin, args=None, rsp=None, cwd=None, timeout=300):
        try:
            args.insert(0, bin)
            self.args = args
        except:
            self.args = (bin,)

        self.cwd = cwd
        self.rsp = rsp
        self.timeout = timeout
        self.proc = None
        self.results = queue.Queue()
        self.async_queries = queue.Queue()
        self.lock = threading.Lock()
        self._needs_restart = False
        self._invalid = False

        # Setup threads for reading results and errors
        threading.Thread(target=self.thread_async).start()
        threading.Thread(target=self.thread_stdout).start()
        threading.Thread(target=self.thread_stderr).start()

    def start(self):
        self._last_usage = time.time()

        if self._needs_restart:
            self._needs_restart = False
            logger.info('Restarting server...')
            self.stop()
        # Nothing to do if already running
        elif self.is_alive():
            return

        args = list(self.args)
        cwd = self.cwd
        if self.rsp:
            cwd = os.path.dirname(self.rsp)
            # Extract references from rsp
            with open(self.rsp) as fp:
                lines = fp.readlines()
                lines = [ln.strip() for ln in lines]
                args += [ln for ln in lines if ln.startswith('-r')]
                args += [ln.replace('-o', '-r') for ln in lines if ln.startswith('-o')]
                args += [ln for ln in lines if ln.startswith('-ducky')]
            #args.append('@{0}'.format(self.rsp))

        self.proc = subprocess.Popen(
            args,
            cwd=cwd,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
            bufsize=0,
            close_fds=True
        )

        logger.info('Started hint server with PID %s using: %s',
                    self.proc.pid, ' '.join(args))

        # Start monitoring the connection last usage timeout
        self.check_timeout()

    def stop(self):
        if not self.proc:
            return

        if self.is_alive():
            # Try to terminate the compiler gracefully
            try:
                logger.info('Terminating hint server process %s', self.proc.pid)
                self.proc.stdin.write("quit\n".encode('utf-8'))
                self.proc.terminate()
            except IOError:
                pass

        # If still alive try to kill it
        if self.is_alive():
            self.proc.kill()

        self.proc = None

    def check_timeout(self):
        if time.time() - self._last_usage > self.timeout:
            self.stop()
        # Run the check again after a timeout
        elif self.is_alive():
            threading.Timer(self.timeout, self.check_timeout).start()

    def is_alive(self):
        alive = self.proc and self.proc.poll() is None and hasattr(self.proc, 'stdin')
        if not alive:
            self.reset_queue(self.results)
            self.reset_queue(self.async_queries)
        return alive

    def thread_stdout(self):
        """ Thread to consume stdout contents """
        while True:
            if not self.is_alive():
                time.sleep(0.1)
                continue

            line = self.proc.stdout.readline()
            if 0 == len(line):
                continue
            line = line.decode('utf-8')
            line = line.rstrip()
            if line.startswith('#'):
                line = line[1:]
                if line.startswith('!'):
                    self.server_command(line[1:])
                else:
                    logger.debug(line)
            else:
                self.results.put(line)

    def thread_stderr(self):
        """ Thread to consume stderr contents """
        while True:
            if not self.is_alive():
                time.sleep(0.1)
                continue

            line = self.proc.stderr.readline()
            if 0 == len(line):
                continue
            line = line.decode('utf-8')
            line = line.rstrip()
            if line.startswith('#'):
                logger.warning(line[1:])
            else:
                logger.error(line)
                # self.results.put(None)

    def thread_async(self):
        """ Thread to perform async queries """
        while True:
            callback, command, kwargs = self.async_queries.get()
            resp = self.query(command, **kwargs)
            callback(resp)

    def reset_queue(self, queue):
        """ Make sure the queue is empty """
        try:
            while True:
                queue.get_nowait()
        except:
            pass

    def server_command(self, line):
        """ Answers server commands
        """
        if line.startswith('ReferenceModified:'):
            # Force a restart of the server as soon as possible
            self._needs_restart = True
            self.query_async(lambda x: x, 'parse', fname='reload', code='')
        else:
            logger.info('Unsupported server command: %s', line)

    def query(self, command, **kwargs):
        if self._invalid:
            logger.error('Process was flagged as invalid. It ended abnormally.')
            return None

        # Issue the command
        kwargs['command'] = command
        query = json.dumps(
            kwargs,
            check_circular=False,  # Try to make it a bit faster
            separators=(',', ':')  # Make it more compact
        ).encode('utf-8')

        # Uses a lock to sequence the commands to the child process in order to
        # avoid mixed results in the output.
        with self.lock:
            # Make sure we have a server running
            self.start()

            # Reset the response queue
            self.reset_queue(self.results)

            # Send the query and wait for the results
            self.proc.stdin.write(query + '\n'.encode('utf-8'))
            resp = None
            try:
                resp = self.results.get(timeout=3.0)
                if resp is not None:
                    resp = json.loads(resp)
            except queue.Empty as ex:
                logger.error('Timeout waiting for query response')
                if not self.is_alive():
                    self._invalid = True
                    logger.error('Process terminated abnormally. Disabling it.')
            except Exception as ex:
                logger.error(str(ex), exc_info=True)

            return resp

    def query_async(self, callback, command, **kwargs):
        """ Runs a query in a separate thread reporting the result via an
            argument to the supplied callback.
        """
        self.async_queries.put((callback, command, kwargs))
