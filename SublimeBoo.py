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

import sys
import re
import time
import logging

import sublime
import sublime_plugin

from BooHints import get_server


# HACK: Prevent crashes with broken pipe signals
try:
    import signal
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
except ValueError:
    pass  # Ignore, in Windows we cannot capture SIGPIPE

# Setup logging to use Sublime's console
logger = logging.getLogger('boo')
logger.setLevel(logging.DEBUG)
log_handler = logging.StreamHandler(sys.stdout)
log_handler.setFormatter(logging.Formatter('[%(name)s] %(levelname)s: %(message)s'))
logger.addHandler(log_handler)


# After the given seconds of inactivity the server process will be terminated
SERVER_TIMEOUT = 300

LANGUAGE_REGEX = re.compile("(?<=source\.)[\w+\-#]+")
IMPORT_REGEX = re.compile(r'^import\s+([\w\.]+)?|^from\s+([\w\.]+)?')
NAMED_REGEX = re.compile(r'\b(class|struct|enum|macro|def)\s$')
AS_REGEX = re.compile(r'(\sas|\sof|\[of)\s$')
PARAMS_REGEX = re.compile(r'(def|do)(\s\w+)?\s*\([\w\s,\*]*$')
TYPE_REGEX = re.compile(r'^\s*(class|struct)\s')

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

    ('self', 'self'),
    ('super', 'super'),
    ('System\tnamespace', 'System')
)

IGNORED = (
    'Equals()\tbool',
    'ReferenceEquals()\tbool',
)

# Keeps cached hints for global symbols associated to a view id
_GLOBALS = {}
# Keeps the last messages returned by the parse command associated to a view id
_LINTS = {}
# Keeps a cache of the last result associated to a view id
_RESULT = {}


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


def server(view):
    """ Obtain a server valid for the current view file. If a suitable one was
        already spawned it gets reused, otherwise a new one is created.
    """
    fname = view.file_name()
    cmd = get_setting('bin')
    args = get_setting('args', [])
    rsp = get_setting('rsp')

    return get_server(cmd, args, rsp=rsp, fname=fname)


def get_setting(key, default=None):
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


def get_code(view):
    return view.substr(sublime.Region(0, view.size()))


def query_locals(view, offset=None):
    if not get_setting('locals_complete', True):
        return []

    # Get current cursor position and obtain its row number (1 based)
    if offset is None:
        offset = view.sel()[0].a
    line = view.rowcol(offset)[0] + 1

    resp = server(view).query(
        'locals',
        fname=view.file_name(),
        code=get_code(view),
        line=line
    )
    return convert_hints(resp['hints'])


def query_globals(view):
    resp = server(view).query(
        'globals',
        fname=view.file_name(),
        code=get_code(view)
    )

    hints = []
    for h in resp['hints']:
        name, node, info = (h['name'], h['node'], h.get('info'))

        if name[-5:] == 'Macro':
            lower = name[0:-5].lower()
            hints.append(('{0}\tmacro'.format(lower), lower + ' '))

        if name[-9:] == 'Attribute':
            lower = name[0:-9].lower()
            hints.append(('{0}\tattribute'.format(lower), lower))

        hints.append(convert_hint(h))

    return hints


def query_members(view, offset=None, code=None, line=None):
    # Get current cursor position and obtain its row number (1 based)
    if offset is None:
        offset = view.sel()[0].a
    if line is None:
        line = view.rowcol(offset)[0] + 1

    resp = server(view).query(
        'members',
        fname=view.file_name(),
        code=code or get_code(view),
        offset=offset,
        line=line)

    return convert_hints(resp['hints'])


def convert_hint(hint):
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


def convert_hints(hints):
    return [convert_hint(x) for x in hints]


def normalize_hints(hints):
    # Remove ignored and duplicates (overloads are not shown for autocomplete)
    seen = set()
    hints = [x for x in hints if x[1] not in IGNORED and x[1] not in seen and not seen.add(x[1])]

    # Sort by symbol
    hints.sort(key=lambda x: x[1])

    if not get_setting('defaults_complete'):
        hints = (hints, sublime.INHIBIT_EXPLICIT_COMPLETIONS | sublime.INHIBIT_WORD_COMPLETIONS)

    return hints


def query_async(callback, view, command, delay=0, **kwargs):
    """ Helper to issue commands asynchronously
    """
    def wrapper(result):
        sublime.set_timeout(lambda: callback(result), delay)

    server(view).query_async(wrapper, command, **kwargs)


def refresh_globals(view, delay=0):
    """ Refresh hints for global symbols asynchronously
    """
    def callback(result):
        _GLOBALS[view.id()] = result['hints']

    if get_setting('globals_complete'):
        query_async(callback, view, 'globals', delay=delay, fname=view.file_name(), code=get_code(view))


def refresh_lint(view, delay=0):
    """ Refreshes linting information asynchronously
    """
    def process(lints, messages, key, mark='circle'):
        result = []
        for hint in messages[key]:
            line, col = (hint['line'] - 1, hint['column'] - 1)
            point = view.text_point(line, col)
            result.append(sublime.Region(point, point))
            lints[line] = '{0}: {1}'.format(hint['code'], hint['message'])

        view.erase_regions('boo-lint-{0}'.format(key))
        view.add_regions(
            'boo-lint-{0}'.format(key),
            result,
            'boo.{0}'.format(key[:-1]),  # Here we use the singular form
            mark,
            sublime.HIDDEN | sublime.PERSISTENT
        )

    def callback(result):
        view_id = view.id()
        if view_id in _LINTS:
            _LINTS[view_id].clear()
        else:
            _LINTS[view_id] = {}

        process(_LINTS[view_id], result, 'warnings', 'dot')
        process(_LINTS[view_id], result, 'errors', 'circle')

        update_status(view)

    query_async(
        callback,
        view,
        'parse',
        fname=view.file_name(),
        code=get_code(view),
        delay=delay)


def update_status(view):
    """ Updates the status bar with parser hints
    """
    lints = _LINTS.get(view.id(), {})
    ln = view.rowcol(view.sel()[-1].b)[0]
    if ln in lints:
        view.set_status('Boo', lints[ln])
    else:
        view.erase_status('Boo')


def is_supported_language(view):
    if view.is_scratch():
        return False

    caret = view.sel()[0].a
    scope = view.scope_name(caret).strip()
    lang = LANGUAGE_REGEX.search(scope)
    return 'boo' == lang.group(0) if lang else False


class BooEventListener(sublime_plugin.EventListener):

    def on_query_context(self, view, key, operator, operand, match_all):
        """ Resolves context queries for keyboard bindings
        """
        if key == "boo_dot_complete":
            return get_setting('dot_complete', True)
        elif key == "boo_supported_language":
            return is_supported_language(view)
        elif key == "boo_is_code":
            caret = view.sel()[0].a
            scope = view.scope_name(caret).strip()
            return re.search(r'string\.|comment\.', scope) is None

        return False

    def on_load(self, view):
        if not is_supported_language(view):
            return

        # Get hints for globals
        refresh_globals(view)
        refresh_lint(view)

    def on_post_save(self, view):
        if not is_supported_language(view):
            return

        # Get hints for globals
        refresh_globals(view)

        if get_setting('parse_on_save', True):
            refresh_lint(view)

    def on_selection_modified(self, view):
        """ Every time we move the cursor we update the status
        """
        if is_supported_language(view):
            update_status(view)

    def on_query_completions(self, view, prefix, locations):

        def prepare_result(offset, hints):
            hints = normalize_hints(hints)
            _RESULT[view.id()] = (offset, view.substr(view.word(offset)), hints)
            logger.debug('QueryCompletion: %d', (time.time()-start)*1000)
            return hints

        if not is_supported_language(view):
            return

        start = time.time()
        hints = []

        # Find a preceding non-word character in the line
        offset = locations[0]
        if view.substr(offset-1) not in '.':
            offset = view.word(offset).a

        # Try to optimize by comparing with the last execution
        last_offset, last_word, last_result = _RESULT.get(view.id(), (-1, None, None))
        if last_offset == offset and last_word == view.substr(view.word(offset)):
            logger.debug('Reusing last result')
            return last_result

        # TODO: Most of the stuff below could be refactored to use without sublime

        # Obtain the string from the start of the line until the caret
        line = view.substr(sublime.Region(view.line(offset).a, offset))
        logger.debug('Line: "%s"', line)

        # Manage auto completion on import statements
        matches = IMPORT_REGEX.search(line)
        if matches:
            ns = matches.group(1) or matches.group(2)
            ns = ns.rstrip('.') + '.'

            # Auto complete based on members from the detected namespace
            resp = server(view).query(
                'members',
                fname='namespace.boo',
                code=ns,
                offset=len(ns))
            hints = convert_hints(resp['hints'])

            # Since we are modifying imports lets schedule a refresh of the globals
            refresh_globals(view, 500)

            return prepare_result(offset, hints)

        # Check if we need globals, locals or member hints
        ch = view.substr(offset - 1)
        # A preceding dot always triggers member hints
        if ch == '.':
            logger.debug('DOT')
            hints += query_members(view, offset)
        # Type annotations and definitions only hint globals (for inheritance)
        elif AS_REGEX.search(line) or TYPE_REGEX.search(line):
            logger.debug('AS')
            hints += PRIMITIVES
            hints += convert_hints(_GLOBALS.get(view.id(), []))
        # When naming stuff or inside parameters definition disable hints
        elif NAMED_REGEX.search(line) or PARAMS_REGEX.search(line):
            logger.debug('NAMED or PARAMS')
            hints = []
        else:
            logger.debug('ELSE')
            hints += BUILTINS
            hints += convert_hints(_GLOBALS.get(view.id(), []))
            # Without a preceding dot members reports back the ones in the current
            # type (self) and local entities
            hints += query_members(view, offset)

        return prepare_result(offset, hints)
