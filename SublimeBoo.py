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
        - alt+space quick panel:
            - between parens offer options for target method
            - at end of word boundary: auto complete symbol

    Future:
        - Implement a "navigation" quick panel:
            - Keep history of jumps and show a "Go back to..."
            - Show all reported lints in the file
            - Show namespaces to browse them
        - Prompt panel:
            - insert import ?
        - Sticky info panel that keep getting updated with the current entity
        - Help command that shows the plugin help
        - Command to show declaration in panel instead of jumping
        - ST3 has a view.show_popup_menu(items, onselect) API
        - Add support for "literate boo" .litboo / .boo.md
"""
import sys
import re
import time
import logging

import sublime
import sublime_plugin

from .BooHints import get_server, reset_servers, format_type, format_method, find_open_paren

# Try to reload dependencies (useful while developing the plugin)
from imp import reload
mod_prefix = '.'.join(__name__.split('.')[:-1])
for mod in ('BooHints', 'BooHints.server'):
    reload(sys.modules[mod_prefix + '.' + mod])

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
IMPORT_REGEX = re.compile(r'^(import|from)\s')


IGNORED = (
    'Equals()\tbool',
    'ReferenceEquals()\tbool',
)

# Views initialized
_INITIALIZED = set()
# Keeps cached hints for builtin symbols associated to a view id
_BUILTINS = {}
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

    try:
        return get_server(cmd, args, rsp=rsp, fname=fname)
    except FileNotFoundError as ex:
        logger.error('Error spawning server: %s', ex)


def get_setting(key, default=None):
    """ Search for the setting in Sublime using the "boo." prefix. If
        not found it will use the plugin settings file without the prefix
        to find a valid key. If still not found the default is returned.
    """
    # Obtain settings from user preferences and/or project
    settings = sublime.active_window().active_view().settings()

    # Prefixed like `boo.rsp`
    prefixed = '{0}.{1}'.format('boo', key)
    if settings.has(prefixed):
        return settings.get(prefixed)

    # Inside a section named boo
    if key in settings.get('boo', {}):
        return settings.get('boo').get(key)

    # Query a custom settings file
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

    return convert_hints(resp['hints']) if resp else []


def query_complete(view, offset=None, code=None, line=None, skip_globals=True, **kwargs):
    # Get current cursor position and obtain its row number (1 based)
    if offset is None:
        offset = view.sel()[0].a
    if line is None:
        line = view.rowcol(offset)[0] + 1
    if code is None:
        code = get_code(view)

    resp = server(view).query(
        'complete',
        fname=view.file_name(),
        code=code or get_code(view),
        offset=offset,
        line=line,
        params=(skip_globals,),
        **kwargs)

    if not resp:
        return 'error', []

    return resp['scope'], resp['hints']


def symbol_for(hint):
    SYMBOL_MAP = {
        'Method': u'ƒ',
        'Namespace': u'η',
        'Property': u'ρ',
        'Event': u'ɘ',
        'Field': u'ʇ',
        'Local': u'ʟ',
        'Parameter': u'ʟ',
        'Ambiguous': u'⸮',
        'Type': u'τ',
        'Type.class': u'ϲ',
        'Type.interface': u'ɪ',
        'Type.struct': u'ƨ',
        'Type.enum': u'ǝ',
    }

    if hint['node'] == 'Type':
        flags = set(x.strip() for x in hint['info'].split(','))
        flags = flags & set(('class', 'interface', 'struct', 'enum'))
        if len(flags):
            return SYMBOL_MAP.get('Type.' + flags.pop())

    return SYMBOL_MAP.get(hint['node'], '?')


def convert_hint(hint):
    name, node, type_, info = (hint['name'], hint['node'], hint.get('type'), hint.get('info'))

    if node == 'Namespace':
        desc = name
    elif node == 'Type':
        if name.endswith('Macro'):
            name = name[:-5].lower() + ' '
            desc = '{0}\t{1}'.format(name, 'macro')
            print(name, desc)
        else:
            flags = set(x.strip() for x in info.split(','))
            flags = flags - set(('class', 'interface', 'struct', 'event', 'value'))
            desc = '{0}\t{1}'.format(name, ' '.join(flags))
    # TODO: Is this still being used?
    elif node == 'Macro':
        desc = '{0}\t{1}'.format(name, 'macro')
        name = name + ' '
    elif node == 'Ambiguous':
        desc = '{0} (x{1})\t{2}'.format(name, info, format_type(type_, True))
    else:
        desc = '{0}\t{1}'.format(name, format_type(type_, True))

    symbol = symbol_for(hint)
    if symbol:
        desc = u'{0} {1}'.format(symbol, desc)

    return (desc, name)


def convert_hints(hints):
    return [convert_hint(x) for x in hints]


def normalize_hints(hints):
    # Remove duplicates
    seen = set()
    hints = [x for x in hints if x[1] not in seen and not seen.add(x[1])]

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
        if result:
            sublime.set_timeout(lambda: callback(result), delay)

    server(view).query_async(wrapper, command, **kwargs)


def refresh_builtins(view, delay=0):
    """ Refresh hints for builtin symbols asynchronously
    """
    def callback(result):
        _BUILTINS[view.id()] = result['hints']

    query_async(
        callback,
        view,
        'builtins',
        delay=delay,
        fname=view.file_name(),
        code=''
    )


def refresh_globals(view, delay=0):
    """ Refresh hints for global symbols asynchronously
    """
    def callback(result):
        _GLOBALS[view.id()] = result['hints']

    if not get_setting('globals_complete'):
        return

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

    query_async(
        callback,
        view,
        'parse',
        fname=view.file_name(),
        code=get_code(view),
        delay=delay,
        extra=True)  # Set to False to use a faster parser


# TODO: This should be run at a fixed interval to avoid computing the offset
#       each time we move the cursor.
STATUS_CACHE = {
    'view': -1,
    'offset': -1,
}


def update_status():
    """ Update the status bar at a fixed interval
    """
    view = sublime.active_window().active_view()
    if view and view.id() in _INITIALIZED:
        if view.id() != STATUS_CACHE['view']:
            view.erase_status('boo.lint')
            view.erase_status('boo.sign')
            STATUS_CACHE['offset'] = -1
        STATUS_CACHE['view'] = view.id()

        # Only process if we are not selecting text
        sel = view.sel()
        if len(sel) == 1 and sel[0].a == sel[0].b:
            render_status(view, sel[0].a)

    sublime.set_timeout(update_status, 700)


def render_status(view, ofs):
    """ Updates the status bar with parser hints
    """
    # Apply linting information
    lints = _LINTS.get(view.id(), {})
    row, col = view.rowcol(ofs)
    if row in lints:
        view.set_status('boo.lint', lints[row])
    else:
        view.erase_status('boo.lint')

    # Use syntax scopes to quickly discard looking for a signature
    if view.score_selector(ofs, 'comment, string, constant, keyword') > 0:
        view.erase_status('boo.sign')
        return

    # Find the entity under the cursor
    if view.substr(ofs) in (' ', ',', '(', ')'):
        # Try to find the entity in a call or slicing expression
        ofs = find_open_paren(view.substr(sublime.Region(0, ofs)), open='([', close=')]')
        if not ofs:
            view.erase_status('boo.sign')
            return

    # Get the start position of the entity
    ofs = view.word(ofs).a
    if not view.substr(view.word(ofs)).isalnum():
        view.erase_status('boo.sign')
        return

    # If we are at the same point just exit
    if ofs == STATUS_CACHE['offset']:
        return

    STATUS_CACHE['offset'] = ofs

    def callback(resp):
        if not len(resp['hints']):
            view.erase_status('boo.sign')
            return

        hint = resp['hints'][0]
        if hint['node'] == 'Method':
            sign = format_method(hint, symbol_for(hint) + ' ({params}): {return}', '{name}: {type}')
            view.set_status('boo.sign', sign)
        elif hint['node'] in ('Namespace', 'Type'):
            view.set_status('boo.sign', '{0} {1}'.format(symbol_for(hint), hint['full']))
        else:
            view.set_status('boo.sign', '{0} {1}'.format(symbol_for(hint), hint.get('type')))

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
    if view.is_scratch() or not view.file_name():
        return False

    caret = view.sel()[0].a
    scope = view.scope_name(caret).strip()
    lang = LANGUAGE_REGEX.search(scope)
    return 'boo' == lang.group(0) if lang else False


class BooEventListener(sublime_plugin.EventListener):

    def __init__(self):
        # Hack: We use the constructor to detect when the plugin reloads
        reset_servers()
        _INITIALIZED.clear()
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
        # used on_load we may stall the editor when it's started with a
        # lot of files
        def initialize():
            if view.id() in _INITIALIZED:
                return
            if view.is_loading():
                sublime.set_timeout(initialize, 100)
                return
            elif is_supported_language(view):
                logger.debug('Initializing view %d', view.id())
                _INITIALIZED.add(view.id())

                refresh_builtins(view)
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
        if view_id in _BUILTINS:
            del _BUILTINS[view_id]
        if view_id in _LINTS:
            del _LINTS[view_id]
        if view_id in _RESULT:
            del _RESULT[view_id]

    def on_query_completions(self, view, prefix, locations):

        if not is_supported_language(view) or not view.file_name():
            return

        start = time.time()

        vid = view.id()
        offset = -1
        line = ''
        hints = []

        def prepare_result(offset, hints):
            hints = normalize_hints(hints)
            _RESULT[vid] = (offset, line, hints)
            logger.debug('QueryCompletion: %d', (time.time()-start)*1000)
            return hints

        # Find a preceding non-ident character in the line
        offset = locations[0]
        if view.substr(offset-1).isalnum() or view.substr(offset-1) == '_':
            offset = view.word(offset).a

        # Obtain the string from the start of the line until the caret
        line = view.substr(sublime.Region(view.line(offset).a, offset))
        #logger.debug('Line: "%s"', line)

        # Try to optimize by comparing with the last execution
        last_offset, last_line, last_result = _RESULT.get(vid, (-1, None, None))
        if last_offset == offset and last_line == line:
            logger.debug('Reusing last result')
            return last_result

        scope, hints = query_complete(view, offset)
        if scope == 'name':
            hints = []
        elif scope == 'import':
            # Schedule a refresh globals
            refresh_globals(view, 2000)
        elif scope == 'type':
            # Filter out everything but types in globals
            items = _BUILTINS.get(vid, []) + _GLOBALS.get(vid, [])
            hints += (h for h in items if h['node'] in ('Type', 'Namespace'))
        elif scope == 'members':
            pass
        elif scope == 'complete':
            # Include builtins and globals
            logger.info('Including builtins')
            hints += _BUILTINS.get(vid, []) + _GLOBALS.get(vid, [])
        else:
            logger.info('Unknown scope <%s>', scope)

        hints = convert_hints(hints)
        return prepare_result(offset, hints)


# Initialize the status updater
update_status()
