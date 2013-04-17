"""
Implements an interface with the Boo compiler hints server.
"""

import os
import subprocess
import threading
import json
import Queue
import time
import logging

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
        self.queue = Queue.Queue()
        self.lock = threading.Lock()

    def start(self):
        self._last_usage = time.time()

        # Nothing to do if already running
        if self.is_alive():
            return

        args = list(self.args)
        cwd = self.cwd
        if self.rsp:
            args.append('@{0}'.format(self.rsp))
            cwd = os.path.dirname(self.rsp)

        args.append('-hints-server')

        self.proc = subprocess.Popen(
            args,
            cwd=cwd,
            #shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE
        )

        # Setup threads for reading results and errors
        threading.Thread(target=self.thread_stdout).start()
        threading.Thread(target=self.thread_stderr).start()

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
                self.proc.stdin.write("quit\n")
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
            t = threading.Timer(self.timeout, self.check_timeout)
            t.start()

    def is_alive(self):
        return self.proc and self.proc.poll() is None

    def thread_stdout(self):
        """ Thread to consume stdout contents """
        try:
            while self.is_alive():
                line = self.proc.stdout.readline()
                if 0 == len(line):
                    break
                line = line.rstrip()
                if line[0] == '#':
                    logger.debug(line[1:])
                else:
                    self.queue.put(line)
        finally:
            self.stop()

    def thread_stderr(self):
        """ Thread to consume stderr contents """
        try:
            while self.is_alive():
                line = self.proc.stderr.readline()
                if 0 == len(line):
                    break
                line = line.rstrip()
                if line[0] == '#':
                    logger.debug(line[1:])
                else:
                    logger.error(line)
        finally:
            self.stop()

    def reset_queue(self):
        """ Make sure the queue is empty """
        try:
            while True:
                self.queue.get_nowait()
        except:
            pass

    def query(self, command, **kwargs):
        kwargs['command'] = command

        logger.debug('Querying request for %s', command)

        # Issue the command
        query = json.dumps(
            kwargs,
            check_circular=False,  # Try to make it a bit faster
            separators=(',', ':')  # Make it more compact
        )
        #logger.debug('Query: %s', query)

        # Use a lock to sequence the commands to the child process in order to
        # avoid mixed results in the output.
        with self.lock:
            # Make sure we have a server running
            self.start()

            logger.debug('Querying %s to server %s', command, self.proc.pid)

            # Reset the response queue
            self.reset_queue()

            self.proc.stdin.write("%s\n" % query)

            # Wait for the results
            resp = None
            try:
                resp = self.queue.get(timeout=5.0)
                #logger.debug('Response: %s', resp)
                resp = json.loads(resp)
            except Exception as ex:
                logger.error(str(ex), exc_info=True)
            finally:
                self.reset_queue()

            logger.debug('Querying %s finished', command)

            return resp

    def query_async(self, callback, command, **kwargs):
        """ Runs a query in a separate thread reporting the result via an
            argument to the supplied callback.
        """
        def target():
            result = self.query(command, **kwargs)
            callback(result)

        t = threading.Thread(target=target)
        t.start()
