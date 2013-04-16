"""
    Settings:

        - boo.defaults_complete (boo) - set to false to disable default completion from the file
        - boo.globals_complete (bool) - set to false to disable completion for top level symbols
        - boo.locals_complete (bool) - set to false to disable completion for locals
        - boo.dot_complete (bool) - set to false to disable automatic completion popup when pressing a dot
        - boo.parse_on_save (bool) - set to false to disable automatic parsing of the file

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
import json
import Queue
import tempfile
import sublime
import sublime_plugin


# HACK: Prevent crashes with broken pipe signals
try:
    import signal
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
except ValueError:
    pass  # Ignore, in Windows we cannot capture SIGPIPE


# After the given seconds of inactivity the server process will be terminated
SERVER_TIMEOUT = 300

LANGUAGE_REGEX = re.compile("(?<=source\.)[\w+\-#]+")
IMPORT_REGEX = re.compile(r'^import\s+([\w\.]+)?|^from\s+([\w\.]+)?')
NAMED_REGEX = re.compile(r'\b(class|struct|enum|macro|def)\s$')
AS_REGEX = re.compile(r'(\sas|\sof|\[of)\s$')
PARAMS_REGEX = re.compile(r'(def|do)(\s\w+)?\s*\([\w\s,\*]*$')
TYPE_REGEX = re.compile(r'^\s*(class|struct)\s')
MEMBER_REGEX = re.compile(r'[\w\)\]]\.$')

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
    'BooJs.Lang.Globals.Object': 'object',
}

PRIMITIVES = (
    ('void\tprimitive', 'void'),
    ('object\tprimitive', 'object'),
    ('bool\tprimitive', 'bool'),
    ('int\tprimitive', 'int'),
    ('double\tprimitive', 'double'),
    ('string\tprimitive', 'string'),

    ('System\tnamespace', 'System')
)

BUILTINS = (
    ('assert\tmacro', 'assert '),
    ('print\tmacro', 'print '),
    ('trace\tmacro', 'trace '),

    ('len()\tint', 'len(${1:array})$0'),
    ('join()\tstring', 'join(${1:array}, ${2:string})$0'),
    ('range()\tarray', 'range(${1:int})$0'),

    ('System\tnamespace', 'System')
)

IGNORED = (
    'Equals()\tbool',
    'ReferenceEquals()\tbool',
)


def maptype(t):
    if not t:
        return ''

    if t in TYPESMAP:
        return TYPESMAP[t]

    # Process special types
    if t.startswith('System.Nullable[of '):
        t = t[len('System.Nullable[of '):-1]
        return TYPESMAP.get(t, t) + '?'
    if t.startswith('Boo.Lang.List[of '):
        t = t[len('Boo.Lang.List[of '):-1]
        return 'List[' + TYPESMAP.get(t, t) + ']'

    return t


class QueryServer(object):

    def __init__(self, bin, args=None, rsp=None):
        try:
            args.insert(0, bin)
            self.args = args
        except:
            self.args = (bin,)

        self.rsp = rsp
        self.proc = None
        self.queue = Queue.Queue()

    def start(self):
        self.last_usage = time.time()

        if self.proc:
            return

        cwd = None
        args = list(self.args)
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
        t = threading.Thread(target=self.thread_stdout)
        t.start()
        t = threading.Thread(target=self.thread_stderr)
        t.start()

        print '[Boo] Started hint server with PID %s using: %s' % (self.proc.pid, ' '.join(args))

        self.check_last_usage()

    def stop(self):
        if not self.proc:
            return

        # Try to terminate the compiler gracefully
        try:
            self.proc.stdin.write("quit\n")
            self.proc.terminate()
        except IOError:
            pass

        # If still alive try to kill it
        if self.proc.poll() is None:
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
                    if line[0] == '#':
                        print '[Boo] DEBUG %s' % line[1:].strip()
                    else:
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
                    if line[0] == '#':
                        print '[Boo] DEBUG %s' % line[1:].strip()
                    else:
                        self._empty_queue()
                        print '[Boo] ERROR: %s' % line.strip()
        finally:
            self.stop()

    def _empty_queue(self):
        """ Make sure the queue is empty """
        try:
            while True:
                self.queue.get_nowait()
        except:
            pass

    def query(self, command, fname=None, code=None, **kwargs):
        kwargs['command'] = command
        kwargs['fname'] = fname
        kwargs['code'] = code

        command = json.dumps(kwargs)

        # Make sure we have a server running
        self.start()

        # Reset the response queue
        self._empty_queue()

        # Issue the command
        #print '[Boo] Command: %s' % command
        self.proc.stdin.write("%s\n" % command)

        # Wait for the results
        resp = None
        try:
            resp = self.queue.get(timeout=3.0)
            #print '[Boo] Response: %s' % resp
            resp = json.loads(resp)
        except Exception as ex:
            self._empty_queue()
            print '[Boo] Error: %s' % ex

        return resp

    def hint(self, hint):
        name, node, info = (hint['name'], hint['node'], hint.get('info'))

        if node == 'Method':
            ret = info.split('): ')[-1]
            desc = '{0}()\t{1}'.format(name, maptype(ret))
            name = name + '($1)$0'
        elif node == 'Namespace':
            desc = '{0}\tnamespace'.format(name)
        elif node == 'Type':
            info = ' '.join(info.split(',')).lower()
            desc = '{0}\t{1}'.format(name, info)
        else:
            desc = '{0}\t{1}'.format(name, maptype(info))

        return (desc, name)

    def locals(self, fname, code, line):
        resp = self.query('locals', fname=fname, code=code, line=line)
        hints = [self.hint(hint) for hint in resp['hints']]
        return ((a.replace("\t", " <local>\t"), b) for a, b in hints)

    def globals(self, fname, code):
        resp = self.query('globals', fname=fname, code=code)

        hints = []
        for hint in resp['hints']:
            name, node, info = (hint['name'], hint['node'], hint.get('info'))

            if name[-5:] == 'Macro':
                lower = name[0:-5].lower()
                hints.append(('{0}\tmacro'.format(lower), lower + ' '))

            if name[-9:] == 'Attribute':
                lower = name[0:-9].lower()
                hints.append(('{0}\tattribute'.format(lower), lower))

            hints.append(self.hint(hint))

        return hints

    def members(self, fname, code, offset, line=None):
        resp = self.query('members', fname, code, offset=offset, line=line)

        hints = []
        for hint in resp['hints']:
            hints.append(self.hint(hint))

        return hints

    def parse(self, fname, code):
        resp = self.query('parse', fname=fname, code=code)
        return resp['errors'] + resp['warnings']


class BooDotComplete(sublime_plugin.TextCommand):
    """
    Command triggered when the dot key is pressed to show the autocomplete popup
    """
    def run(self, edit):
        for region in self.view.sel():
            self.view.insert(edit, region.end(), ".")

        caret = self.view.sel()[0].begin()
        line = self.view.substr(sublime.Region(self.view.word(caret - 1).a, caret))
        if MEMBER_REGEX.search(line) is not None:
            self.view.run_command("hide_auto_complete")
            sublime.set_timeout(self.delayed_complete, 1)

    def delayed_complete(self):
        self.view.run_command("auto_complete")


class BooQuickPanelComplete(sublime_plugin.WindowCommand):

    def run(self):
        # TODO: Pre filter the results based on the already written characters

        self.hints = [
            ['Entity', 'Boo.Ide.CompletionProposal.Entity'],
            ['Name', 'Boo.Ide.CompletionProposal.Name'],
            ['EntityType', 'Boo.Ide.CompletionProposal.EntityType'],
            ['Description', 'Boo.Ide.CompletionProposal.Description'],
            ['Equals(obja as object, count as int, messages as (string), errors as List[of string], extra as Hash, node as Boo.Lang.Compiler.Ast.Reference) as bool',
             '(obja: object, count: int, messages: (string), errors: List[of string], extra: Hash, node: Boo.Lang.Compiler.Ast.Reference) as bool'],
            ['GetHashCode(): int',
             '(foo: string, bar: object, node: Boo.Lang.Compiler.Ast.MethodInvocationExpression): int'],
            ['GetType(foo, bar, baz, node): System.Type',
             '(foo: string, bar: object, baz: double, node: Boo.Lang.Compiler.Ast.MethodInvocationExpression): System.Type'],
            ['ToString()', 'System.String ToString()']
        ]

        # Ignore if there are no hints to show
        if not self.hints:
            return

        flags = 0  # sublime.MONOSPACE_FONT
        selected = 0
        self.window.show_quick_panel(self.hints, self.on_select, flags, selected)

    def on_select(self, idx):
        if idx < 0:
            return

        hint = self.hints[idx]

        view = self.window.active_view()
        edit = view.begin_edit()
        for s in view.sel():
            view.insert(edit, s.a, hint[0])
        view.end_edit(edit)


class BooGotoDeclaration(sublime_plugin.TextCommand):

    def run(self, edit):
        # Get the position at the start of the symbol
        caret = self.view.sel()[0].begin()
        caret = self.view.word(caret).a
        row, col = self.view.rowcol(caret)

        print 'TODO BooGoToDeclaration: %d:%d' % (row + 1, col + 1)


class BooFindUsages(sublime_plugin.TextCommand):
    """
    TODO: Implement Find Usages by sending the server a list of files, it will
          then visit them looking for the desired entity.
    """
    def run(self, edit):
        pass


class BooEventListener(sublime_plugin.EventListener):
    """
    TODO: Refactor the query commands into a separate object so we can use them
          from Window or edit commands easily.

    TODO: Use threading to improve editor responsiveness.
          Blocking operations must execute in its own thread, to comunicate back with
          sublime (the view) we must use a function via settimeout (the only API thread safe)
          http://tekonomist.wordpress.com/2010/06/04/latex-plugin-update-threading-for-fun-and-profit/
    """

    def __init__(self):
        self.servers = {}

        # TODO: This state should be targeted to a given fname/view!
        self.last_offset = -1
        self.last_result = None
        self.globals = []
        self.lints = {}

        # Cache for .rsp look up
        self._fname2rsp = {}

    def on_query_context(self, view, key, operator, operand, match_all):
        """ Resolves context queries for keyboard shortcuts definitions
        """
        if key == "boo_dot_complete":
            return self.get_setting('dot_complete', True)
        elif key == "boo_supported_language":
            return self.is_supported_language(view)
        elif key == "boo_is_code":
            caret = view.sel()[0].a
            scope = view.scope_name(caret).strip()
            return re.search(r'string\.|comment\.', scope) is None

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
        """ Updates the status bar with parser hints
        """
        ln = view.rowcol(view.sel()[-1].b)[0]
        if ln in self.lints:
            view.set_status('Boo', self.lints[ln])
        else:
            view.erase_status('Boo')

    def update_globals(self, view):
        """ Updates the hints for global symbols
        """
        def query(view):
            server = self.get_server(view)
            self.globals = server.globals(view.file_name(), self.get_contents(view))

        # Run the command asynchronously to avoid blocking the editor
        if self.get_setting('globals_complete', True):
            sublime.set_timeout(lambda: query(view), 300)

    def on_query_completions(self, view, prefix, locations):
        start = time.time()
        hints = []

        if not self.is_supported_language(view):
            return self.normalize_hints(hints)

        # Find a preceding non-word character in the line
        offset = locations[0]
        if view.substr(offset-1) not in '.':
            offset = view.word(offset).a

        # Try to optimize by comparing with the last execution
        if offset == self.last_offset:
            print '[Boo] Reusing last result'
            return self.last_result

        # Reset last offset to the current one
        self.last_offset = offset

        # Obtain the string from the start of the line until the caret
        line = view.substr(sublime.Region(view.line(offset).a, offset))
        print 'LINE', line

        # Manage auto completion on import statements
        matches = IMPORT_REGEX.search(line)
        if matches:
            ns = matches.group(1) or matches.group(2)
            ns = ns.rstrip('.') + '.'

            # Auto complete based on members from the detected namespace
            server = self.get_server(view)
            hints = server.members('NS', ns, len(ns))

            # Since we are modifying imports lets schedule a refresh of the globals
            self.update_globals(view)

            self.last_result = self.normalize_hints(hints)
            return self.last_result

        # Check if we need globals, locals or member hints
        ch = view.substr(offset - 1)
        # A preceding dot always trigger member hints
        if ch == '.':
            print 'DOT'
            hints += self.query_members(view, offset)
        # Type annotations and definitions only hint globals (for inheritance)
        elif AS_REGEX.search(line) or TYPE_REGEX.search(line):
            print 'AS'
            hints += PRIMITIVES
            hints += self.globals
        # When naming stuff or inside parameters definition disable hints
        elif NAMED_REGEX.search(line) or PARAMS_REGEX.search(line):
            print 'NAMED or PARAMS'
            hints = []
        else:
            print 'ELSE'
            hints += BUILTINS
            hints += self.globals
            #hints += self.query_locals(view, offset)
            # Without a preceding dot it reports back the ones in the current type (self)
            hints += self.query_members(view, offset, line=view.rowcol(offset)[0] + 1)

        self.last_result = self.normalize_hints(hints)

        print 'QueryCompletion: %d' % ((time.time()-start)*1000)
        return self.last_result

    def normalize_hints(self, hints, flags=sublime.INHIBIT_WORD_COMPLETIONS | sublime.INHIBIT_EXPLICIT_COMPLETIONS):
        # Remove ignored and duplicates (overloads are not shown for autocomplete)
        seen = set()
        hints = [x for x in hints if x[1] not in IGNORED and x[1] not in seen and not seen.add(x[1])]

        # Sort by symbol
        hints.sort(key=lambda x: x[1])

        if not self.get_setting('defaults_complete'):
            hints = (hints, flags)

        return hints

    def query_members(self, view, offset=None, line=None):
        if offset is None:
            offset = view.sel()[0].a

        server = self.get_server(view)
        hints = server.members(view.file_name(), self.get_contents(view), offset, line)
        return hints

    def query_locals(self, view, offset=None):
        if not self.get_setting('locals_complete', True):
            return []

        # Get current cursor position and obtain its row number (1 based)
        if offset is None:
            offset = view.sel()[0].a
        line = view.rowcol(offset)[0] + 1

        server = self.get_server(view)
        hints = server.locals(view.file_name(), self.get_contents(view), line)
        return hints

    def query_parse(self, view):
        server = self.get_server(view)
        hints = server.parse(view.file_name(), self.get_contents(view))

        self.lints.clear()
        errors = []
        warnings = []
        for hint in hints:
            line, col = (hint['line'] - 1, hint['column'] - 1)
            a = b = view.text_point(line, col)
            if hint['code'][0:3] == 'BCE':
                errors.append(sublime.Region(a, b))
            else:
                warnings.append(sublime.Region(a, b))

            self.lints[line] = '{0}: {1}'.format(hint['code'], hint['message'])

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
        """ Obtain a server valid for the current view file. If a suitable one was
            already spawned it gets reused, otherwise a new one is created.
        """
        cmd = self.get_setting('bin')
        args = self.get_setting('args', [])
        rsp = self.get_setting('rsp', self.find_rsp_file(view.file_name()))

        key = (cmd, tuple(args), rsp)
        if key not in self.servers:
            self.servers[key] = QueryServer(cmd, args, rsp)

        return self.servers[key]

    def find_rsp_file(self, fname):
        if fname in self._fname2rsp:
            return self._fname2rsp[fname]

        self._fname2rsp[fname] = None
        path = os.path.dirname(fname)
        while len(path) > 3:
            matches = glob.glob('{0}/*.rsp'.format(path))
            if len(matches):
                self._fname2rsp[fname] = matches[0]
                break

            path = os.path.dirname(path)

        return self._fname2rsp[fname]

    def get_setting(self, key, default=None):
        """ Search for the setting in Sublime using the "boo." prefix. If
            not found it will use the plugin settings file without the prefix
            to find a valid key. If still not found the default is returned.
        """
        settings = sublime.active_window().active_view().settings()
        prefixed = '{0}.{1}'.format('boo', key)
        if settings.has(prefixed):
            return settings.get(prefixed)

        settings = sublime.load_settings('Boo.sublime-settings')
        return settings.get(key, default)

    def is_supported_language(self, view):
        if view.is_scratch():
            return False

        caret = view.sel()[0].a
        scope = view.scope_name(caret).strip()
        lang = LANGUAGE_REGEX.search(scope)
        return 'boo' == lang.group(0) if lang else False
