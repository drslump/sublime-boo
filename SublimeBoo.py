"""
    Settings:

        - boo.globals_complete (bool) - set to false to disable completion for top level symbols
        - boo.locals_complete (bool) - set to false to disable completion for locals
        - boo.dot_complete (bool) - set to false to disable automatic completion popup when pressing a dot

    Hack:

        Sublime does not adapt or allows to set a custom width for the autocompletion popup. Use this hack
        at your own risk to make it wider:
        http://www.sublimetext.com/forum/viewtopic.php?f=2&t=10250&sid=06199658f60d16947bf4131ec146e16b#p40640
"""

import os
import glob
import re
import subprocess
import threading
import time
import Queue
import tempfile
import sublime
import sublime_plugin

# After the given seconds of inactivity the server process will be terminated
SERVER_TIMEOUT = 300

LANGUAGE_REGEX = re.compile("(?<=source\.)[\w+\-#]+")
MEMBER_REGEX = re.compile("(([a-zA-Z_]+[0-9_]*)|([\)\]])+)(\.)$")
IMPORT_REGEX = re.compile(r'^import\s+([\w\.]+)(\([\w\s,]+)?')
TERMINATOR = '|||'
TYPESMAP = {
    'Void': 'void',
    'Boolean': 'bool',
    'Int32': 'int',
    'Int64': 'long',
    'System.Object': 'object',
    'System.String': 'string',
    'System.String[]': 'string[]',
    'System.Char[]': 'char[]',
    'System.Array': 'array',
    'Boo.Lang.List`1[System.Object]': 'List[object]',
    'System.Type': 'Type',
}

BUILTINS = (
    ('assert\tmacro', 'assert '),
    ('print\tmacro', 'print '),
    ('trace\tmacro', 'trace '),

    ('len()\tint', 'len(${1:array})$0'),
    ('join()\tstring', 'join(${1:array}, ${2:string})$0'),
    ('range()\tarray', 'range(${1:int})$0'),
)

IGNORED = (
    'Equals()\tbool',
    'ReferenceEquals()\tbool',
)


# HACK: Prevent crashes with broken pipe signals
try:
    import signal
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
except ValueError:
    pass  # Ignore, in Windows we cannot capture SIGPIPE


class QueryServer(object):

    def __init__(self, bin, args=None, rsp=None):
        try:
            args.insert(0, bin)
            self.args = args
        except:
            self.args = (bin,)

        self.rsp = rsp
        self.proc = None
        # Create a temporary file to hold the buffer contents
        self.tmpfile = tempfile.NamedTemporaryFile(delete=True)
        self.queue = Queue.Queue()

    def start(self):
        self.last_usage = time.time()

        if self.proc:
            return

        cwd = None
        args = self.args[:]
        if self.rsp:
            args.append('@{1}'.format(self.rsp))
            cwd = os.path.dirname(self.rsp)

        args.append('-hints-server')

        self.proc = subprocess.Popen(
            args,
            cwd=cwd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE
        )

        # Setup threads for reading results and errors
        t = threading.Thread(target=self.thread_stdout)
        t.start()
        t = threading.Thread(target=self.thread_stderr)
        t.start()

        print '[Boo] Started hint server with PID %s using: %s' % (self.proc.pid, cmd)

        self.check_last_usage()

    def stop(self):
        if not self.proc:
            return

        # Notify any in flight query that it should end
        self.queue.put(TERMINATOR)

        # Try to terminate the compiler gracefully
        try:
            self.proc.stdin.write("quit\n")
            self.proc.terminate()
        except IOError:
            pass

        # If still alive try to kill it
        if self.proc.poll() == None:
            print '[Boo] Killing hint server process %s...' % self.proc.pid
            self.proc.kill()

        self.proc = None

    def check_last_usage(self):
        if time.time() - self.last_usage > SERVER_TIMEOUT:
            self.stop()
        elif self.proc:
            # Check if we should stop the server after a timeout
            sublime.set_timeout(self.check_last_usage, SERVER_TIMEOUT * 1000)

    def thread_stdout(self):
        try:
            while True:
                if self.proc is None or self.proc.poll() is not None:
                    break
                line = self.proc.stdout.readline()
                if line:
                    line = line.strip()
                    #print 'STDOUT: %s' % line
                    if len(line) and line[0] != '#':
                        self.queue.put(line)
        finally:
            self.stop()

    def thread_stderr(self):
        try:
            while True:
                if self.proc is None or self.proc.poll() is not None:
                    break
                line = self.proc.stderr.readline()
                if line:
                    print '[Boo] ERROR: %s' % line.strip()
        finally:
            self.stop()

    def query(self, command, content=None):

        self.start()

        if content:
            command = command.format(fname=self.tmpfile.name)
            self.tmpfile.seek(0)
            self.tmpfile.truncate()
            self.tmpfile.write(content)
            # Make sure every byte is written before querying the server
            self.tmpfile.flush()
            os.fsync(self.tmpfile.fileno())

        # Make sure the queue is empty
        try:
            while True:
                self.queue.get_nowait()
        except:
            pass

        # Issue the command
        print '[Boo] Command: %s' % command
        self.proc.stdin.write("%s\n" % command)

        # Wait for the results
        hints = []
        while True:
            try:
                read = self.queue.get(timeout=3.0)
                if read == TERMINATOR:
                    break

                hints.append(read)
            except Exception as ex:
                print '[Boo] Error: %s' % ex
                break

        return hints

    def locals(self, content, line):
        names = self.query('locals {fname}@%d' % line, content)
        # We only get a list of names for locals (no type information)
        return [('{0}\tlocal'.format(name), name) for name in names]

    def globals(self, content):
        items = self.query('globals {fname}', content)

        hints = []
        for item in items:
            symbol, type_, desc = item.split('|')

            if symbol[-5:] == 'Macro':
                lower = symbol[0:-5].lower()
                hints.append(('{0}\tmacro'.format(lower), lower + ' '))

            if symbol[-9:] == 'Attribute':
                lower = symbol[0:-9].lower()
                hints.append(('{0}\tattribute'.format(lower), lower))

            desc = '{0}\t{1}'.format(symbol, type_)
            hints.append((desc, symbol))

        return hints

    def members(self, content, offset):
        items = self.query('members {fname}@%d' % offset, content)

        hints = []
        for item in items:
            # Remove overloads count
            item = re.sub(r'\s?\(\d+ overloads\)', '', item)

            symbol, type_, desc = item.split('|')
            if '(' in desc:
                args = []
                matches = re.finditer(r'[\(,]([^\),]+)', desc)
                for idx, match in enumerate(matches):
                    param = match.group(1).strip()
                    param = TYPESMAP.get(param, param)
                    args.append('${' + str(idx + 1) + ':' + param + '}')
                symbol = symbol + '(' + ', '.join(args) + ')$0'

                # The autocomplete popup is really small, remove method args
                desc = re.sub(r'\([^\)]*\)', '()', desc)

                desc = desc.split(' ')
                rettype = desc.pop(0)

                rettype = TYPESMAP.get(rettype, rettype)

                desc = ' '.join(desc)
                desc = "{0}\t{1}".format(desc, rettype)
            elif type_ == 'Method':
                desc = "{0}()\t{1}".format(symbol, type_)
            else:
                desc = "{0}\t{1}".format(symbol, type_)

            hints.append((desc, symbol))

        return hints

    def overloads(self, content, line, method):
        items = self.query('overloads %s {fname}@%d' % (method, line), content)
        print items
        return []

    def parse(self, content):
        items = self.query('parse {fname}', content)

        hints = []
        for item in items:
            parts = item.split('|')
            parts[1] = int(parts[1])
            parts[2] = int(parts[2])
            hints.append(parts)

        return hints


class BooDotComplete(sublime_plugin.TextCommand):
    """
    Command triggered when the dot key is pressed to show the autocomplete popup
    """
    def run(self, edit):
        for region in self.view.sel():
            self.view.insert(edit, region.end(), ".")

        caret = self.view.sel()[0].begin()
        line = self.view.substr(sublime.Region(self.view.word(caret - 1).a, caret))
        if MEMBER_REGEX.search(line) != None:
            self.view.run_command("hide_auto_complete")
            sublime.set_timeout(self.delayed_complete, 1)

    def delayed_complete(self):
        self.view.run_command("auto_complete")


class BooEventListener(sublime_plugin.EventListener):

    def __init__(self):
        self.file_mapping = {}
        self.last_offset = -1
        self.last_result = None
        self.globals = []
        self.lints = {}

    def on_query_context(self, view, key, operator, operand, match_all):
        if key == "boo_dot_complete":
            return self.get_setting('dot_complete', True)
        elif key == "boo_supported_language":
            return self.is_supported_language(view)
        elif key == "boo_is_code":
            caret = view.sel()[0].a
            scope = view.scope_name(caret).strip()
            return re.search(r'string\.|comment\.', scope) == None

        return False

    def on_load(self, view):
        if not self.is_supported_language(view):
            return

        # Get hints for globals
        self.update_globals(view)

    def on_post_save(self, view):
        if not self.is_supported_language(view):
            return

        # Get hints for globals
        self.update_globals(view)

        if self.get_setting('parse_on_save', True):
            sublime.set_timeout(lambda: self.query_parse(view), 10)

    def on_selection_modified(self, view):
        if not self.is_supported_language(view):
            return
        self.update_status(view)

    def update_status(self, view):
        ln = view.rowcol(view.sel()[-1].b)[0]
        if ln in self.lints:
            view.set_status('Boo', self.lints[ln])
        else:
            view.erase_status('Boo')

    def update_globals(self, view):
        def query(view):
            server = self.get_server(view)
            self.globals = server.globals(self.get_contents(view))

        if self.get_setting('globals_complete', True):
            sublime.set_timeout(lambda: query(view), 300)

    def on_query_completions(self, view, prefix, locations):
        hints = []

        if not self.is_supported_language(view):
            return hints

        # Find previous dot (or non-word character)
        offset = locations[0]
        if view.substr(offset - 1) not in ('.',):
            offset = view.word(offset - 1).a

        if offset == self.last_offset:
            print '[Boo] Reusing last result'
            return self.last_result
        self.last_offset = offset

        # Detect imports
        line = view.substr(view.line(offset))
        matches = IMPORT_REGEX.search(line)
        if matches:
            ns = matches.group(1)
            if matches.group(2):
                ns = ns + '.'

            server = self.get_server(view)
            hints = server.members(ns, len(ns))

            # Since we are modifying the imports lets schedule an update for the globals
            self.update_globals(view)

            return self.normalize_hints(hints)

        ch = view.substr(offset - 1)
        # If no dot is found include globals and locals
        if ch != '.':
            hints += BUILTINS
            if self.get_setting('globals_complete', True):
                hints += self.globals
            if self.get_setting('locals_complete', True):
                hints += self.query_locals(view)
            hints += self.query_members(view, offset)
        else:
            hints += self.query_members(view, offset)

        return self.normalize_hints(hints)

    def normalize_hints(self, hints, flags=sublime.INHIBIT_WORD_COMPLETIONS | sublime.INHIBIT_EXPLICIT_COMPLETIONS):
        # Remove ignored entries
        hints = filter(lambda tup: tup[0] not in IGNORED, hints)

        # Sort by symbol
        hints.sort(key=lambda tup: tup[1])

        self.last_result = (hints, flags)
        return self.last_result

    def query_members(self, view, offset=None):
        if offset is None:
            offset = view.sel()[0].a

        server = self.get_server(view)
        hints = server.members(self.get_contents(view), offset)
        return hints

    def query_locals(self, view):
        # Get current cursor position and obtain its row number (1 based)
        offset = view.sel()[0].a
        line = view.rowcol(offset)[0] + 1

        server = self.get_server(view)
        hints = server.locals(self.get_contents(view), line)
        return hints

    def query_parse(self, view):
        server = self.get_server(view)
        hints = server.parse(self.get_contents(view))

        self.lints.clear()
        errors = []
        warnings = []
        for hint in hints:
            line, col = (hint[1] - 1, hint[2] - 1)
            a = b = view.text_point(line, col)
            if hint[0][0:3] == 'BCE':
                errors.append(sublime.Region(a, b))
            else:
                warnings.append(sublime.Region(a, b))

            self.lints[line] = '{0}: {1}'.format(hint[0], hint[3])

        view.erase_regions('boo-errors')
        view.erase_regions('boo-warnings')

        if len(errors):
            view.add_regions("boo-errors", errors, "boo.error", "circle", sublime.HIDDEN | sublime.PERSISTENT)
        if len(warnings):
            view.add_regions("boo-warnings", warnings, "boo.warning", "dot", sublime.HIDDEN | sublime.PERSISTENT)

        self.update_status(view)

    def get_contents(self, view):
        return view.substr(sublime.Region(0, view.size()))

    def get_server(self, view):
        fname = view.file_name()
        if fname not in self.file_mapping:
            cmd = self.get_setting('bin')
            args = self.get_setting('args')
            rsp = self.get_setting('rsp', self.find_rsp_file(fname))
            self.file_mapping[fname] = QueryServer(cmd, args, rsp)

        return self.file_mapping[fname]

    def find_rsp_file(self, fname):
        path = os.path.dirname(fname)
        while len(path) > 3:
            matches = glob.glob('{0}/*.rsp'.format(path))
            if len(matches):
                return matches[0]

            path = os.path.dirname(path)

        return None

    def get_setting(self, key, default=None):
        """ Search for the setting in Sublime using the "boo." prefix. If
            not found it will use the plugin settings file without the prefix
            to find a valid key. If still not found the default is returned.
        """
        settings = sublime.active_window().active_view().settings()
        if settings.has('{0}.{1}'.format('boo', key)):
            return settings.get(key)

        settings = sublime.load_settings('Boo.sublime-settings')
        return settings.get(key, default)

    def is_supported_language(self, view):
        if view.is_scratch():
            return False

        caret = view.sel()[0].a
        scope = view.scope_name(caret).strip()
        lang = LANGUAGE_REGEX.search(scope)
        return 'boo' == lang.group(0) if lang else False
