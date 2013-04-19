# -*- coding: utf-8 -*-
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

    Todo:

        - Clean global state when a view is closed
"""

import sys
import re
import time
import logging

import sublime
import sublime_plugin

from BooHints import get_server, reset_servers, format_method, format_type


# HACK: Prevent crashes with broken pipe signals
try:
    import signal
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
except ValueError:
    pass  # Ignore, in Windows we cannot capture SIGPIPE

# Setup logging to use Sublime's console
logger = logging.getLogger('boo')
logger.setLevel(logging.DEBUG)
# Hack: Check if we are reloading the plugin
if not getattr(logger, '__sublime_initialized', None):
    logger.__sublime_initialized = True
    log_handler = logging.StreamHandler(sys.stdout)
    log_handler.setFormatter(logging.Formatter('[%(name)s] %(levelname)s: %(message)s'))
    logger.addHandler(log_handler)


LANGUAGE_REGEX = re.compile("(?<=source\.)[\w+\-#]+")
IMPORT_REGEX = re.compile(r'^import\s+([\w\.]+)?|^from\s+([\w\.]+)?')
NAMED_REGEX = re.compile(r'\b(class|struct|enum|macro|def)\s$')
AS_REGEX = re.compile(r'(\sas|\sof|\[of)\s$')
PARAMS_REGEX = re.compile(r'(def|do)(\s\w+)?\s*\([\w\s,\*]*$')
TYPE_REGEX = re.compile(r'^\s*(class|struct)\s')


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


def query_members(view, offset=None, code=None, line=None, **kwargs):
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
        line=line,
        **kwargs)

    return convert_hints(resp['hints'])


def convert_hint(hint):
    name, node, type_, info = (hint['name'], hint['node'], hint.get('type'), hint.get('info'))

    if node == 'Method':
        desc = '{0}()\t{1}'.format(name, format_type(type_, True))
        name = name + '($1)$0'
    elif node == 'Namespace':
        desc = '{0}\tnamespace'.format(name)
    elif node == 'Type':
        info = ' '.join(info.split(',')).lower()
        desc = '{0}\t{1}'.format(name, info)
    else:
        desc = '{0}\t{1}'.format(name, format_type(type_, True))

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
    """ Helper to issue commands asynchronously in sublime
    """
    def wrapper(result):
        # We need to route the actual callback via set_timeout
        # since it's the only sublime API which is thread safe
        sublime.set_timeout(lambda: callback(result), delay)

    server(view).query_async(wrapper, command, **kwargs)


def refresh_globals(view, delay=0):
    """ Refresh hints for global symbols asynchronously
    """
    def callback(result):
        _GLOBALS[view.id()] = result['hints']

    if get_setting('globals_complete'):
        query_async(
            callback,
            view,
            'globals',
            delay=delay,
            fname=view.file_name(),
            code=get_code(view))


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
            sublime.HIDDEN  # | sublime.PERSISTENT
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
        delay=delay,
        extra=True)  # Set to False to use a faster parser


def update_status(view):
    """ Updates the status bar with parser hints
    """
    sel = view.sel()
    # Nothing to do if we have multiple selections
    if len(sel) > 1:
        return
    sel = sel[0]
    # Nothing to do if we are selecting text
    if sel.a != sel.b:
        return

    ofs = sel.a

    # Apply linting information
    lints = _LINTS.get(view.id(), {})
    ln = view.rowcol(ofs)[0]
    if ln in lints:
        view.set_status('boo.lint', lints[ln])
    else:
        view.erase_status('boo.lint')

    # TODO: Use syntax identifiers to discard the operation for keywords/strings/comments?

    def callback(resp):
        if not len(resp['hints']):
            view.erase_status('boo.signature')
            return

        hint = resp['hints'][0]
        if hint['node'] == 'Method':
            sign = format_method(hint, u'Ⓜ ({params}): {return}', '{name}: {type}')
            view.set_status('boo.signature', sign)
        elif hint['node'] == 'Namespace':
            view.set_status('boo.signature', u'Ⓝ ' + hint['full'])
        elif hint['node'] == 'Type':
            view.set_status('boo.signature', u'Ⓒ ' + hint['full'])
        else:
            view.set_status('boo.signature', '<' + hint['node'] + ': ' + hint['type'] + '>')

    ch = view.substr(ofs)
    word = view.substr(view.word(ofs)).rstrip('\r\n')
    if ch == '.':
        view.erase_status('boo.signature')
        return
    elif ch in (' ', ',', '(', ')'):
        # Silly algorithm to detect if we are in the middle of a method call
        # If there are more parens open than closed (unbalanced) remove all the
        # ones balanced.
        line = view.substr(sublime.Region(view.line(ofs).a, ofs))
        print 'L "%s"' % line
        unbalanced = 1
        idx = len(line)
        while unbalanced > 0 and idx > 0:
            idx -= 1
            if line[idx] == '(':
                unbalanced -= 1
            elif line[idx] == ')':
                unbalanced += 1

        if unbalanced != 0:
            view.erase_status('boo.signature')
            return

        ofs = ofs - len(line) + idx
        ofs = view.word(ofs).a

    elif word.isalnum() and word not in ('if', 'elif', 'else', 'for', 'while', 'try', 'except', 'ensure', 'def', 'class', 'struct', 'interface', 'continue', 'return', 'yield', 'true', 'false', 'null', 'in', 'of'):
        ofs = view.word(ofs).a

    else:
        view.erase_status('boo.signature')
        return

    # TODO: Cache last result
    row, col = view.rowcol(ofs)
    query_async(
        callback,
        view,
        'entity',
        fname=view.file_name(),
        code=get_code(view),
        line=row + 1,
        column=col + 1,
        extra=True
    )


def is_supported_language(view):
    if view.is_scratch():
        return False

    caret = view.sel()[0].a
    scope = view.scope_name(caret).strip()
    lang = LANGUAGE_REGEX.search(scope)
    return 'boo' == lang.group(0) if lang else False


class BooEventListener(sublime_plugin.EventListener):

    def __init__(self):
        self._initialized = set()

        # Hack: We use the constructor to detect when the plugin reloads
        reset_servers()
        _LINTS.clear()
        _GLOBALS.clear()
        _RESULT.clear()

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

    def on_activated(self, view):
        # On first activation after loading a view refresh caches. If we
        # use load we may stall the editor when it's started with a lot
        # of already opened files
        def initialize():
            if view.id() in self._initialized:
                return
            if view.is_loading():
                sublime.set_timeout(initialize, 100)
                return
            elif is_supported_language(view):
                logger.debug('Initializing view %d', view.id())
                self._initialized.add(view.id())
                refresh_globals(view)
                refresh_lint(view)

        initialize()

    def on_post_save(self, view):
        if not is_supported_language(view):
            return

        # Get hints for globals
        refresh_globals(view)

        if get_setting('parse_on_save', True):
            refresh_lint(view)

    def on_close(self, view):
        """ Clean up caches when closing a view
        """
        if not is_supported_language(view):
            return

        view_id = view.id()
        if view_id in _GLOBALS:
            del _GLOBALS[view_id]
        if view_id in _LINTS:
            del _LINTS[view_id]
        if view_id in _RESULT:
            del _RESULT[view_id]

    def on_selection_modified(self, view):
        """ Every time we move the cursor we update the status
        """
        if is_supported_language(view):
            update_status(view)

    def on_query_completions(self, view, prefix, locations):

        if not is_supported_language(view) or not view.file_name():
            return

        offset = -1
        line = ''
        hints = []

        def prepare_result(offset, hints):
            hints = normalize_hints(hints)
            _RESULT[view.id()] = (offset, line, hints)
            logger.debug('QueryCompletion: %d', (time.time()-start)*1000)
            return hints

        start = time.time()

        # Find a preceding non-word character in the line
        offset = locations[0]
        if view.substr(offset-1) not in '.':
            offset = view.word(offset).a

        # TODO: Most of the stuff below could be refactored to use without sublime

        # Obtain the string from the start of the line until the caret
        line = view.substr(sublime.Region(view.line(offset).a, offset))
        logger.debug('Line: "%s"', line)

        # Try to optimize by comparing with the last execution
        last_offset, last_line, last_result = _RESULT.get(view.id(), (-1, None, None))
        if last_offset == offset and last_line == line:
            logger.debug('Reusing last result')
            return last_result

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
            # TODO: offer completions for public properties?
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
