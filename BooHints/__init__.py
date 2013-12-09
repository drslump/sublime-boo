import re
from os import path
from glob import glob

from .server import Server


# Registry of spawned servers
_SERVERS = {}

IMPORT_RE = re.compile(r'^import\s+([\w\.]+)?|^from\s+([\w\.]+)?')
CONTINUATION_RE = re.compile(r'[\\,][\s\r\n]*$')


def get_server(cmd, args, fname=None, rsp=None, cwd=None):
    """ Spawn or retrieve a server suitable for the given arguments
    """
    dirname = path.dirname(path.abspath(fname))
    if rsp is not None:
        rsp = locate_rsp(dirname, rsp)

    if cwd is None:
        if rsp is not None:
            cwd = path.dirname(path.abspath(rsp))
        elif fname is not None:
            cwd = path.dirname(path.abspath(fname))

    if isinstance(cmd, list) or isinstance(cmd, tuple):
        args = cmd[1:] + args
        cmd = cmd[0]

    key = (cmd, tuple(args), cwd, rsp)
    if key not in _SERVERS:
        _SERVERS[key] = Server(cmd, args, rsp=rsp, cwd=cwd)

    return _SERVERS[key]


def reset_servers():
    """ Closes all tracked servers.
    """
    for server in _SERVERS.values():
        server.stop()
    _SERVERS.clear()


def locate_rsp(dirname, pattern):
    """ Tries to locate an .rsp file in one of the parent directories
    """
    while len(dirname) > 3:
        matches = glob('{0}/{1}'.format(dirname, pattern))
        if len(matches):
            # Get the first one sorting without file extension
            return sorted(matches, key=lambda x: x[:-4].lower())[0]
        dirname = path.dirname(dirname)

    return None


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
    'System.Type': 'Type',
    'Boo.Lang.List': 'List',
}

_RE_GENERIC_TYPE = re.compile(r'^([^\[]+)\[of ([^\]]+)\]$')


def format_type(name, short=False):
    if not name or not len(name):
        return ''

    if name in TYPESMAP:
        return TYPESMAP[name]
    elif name.startswith('System.Nullable[of '):
        name = name[len('System.Nullable[of '):-1]
        return format_type(name, short) + '?'

    # Handle generics
    if name[-1] == ']':
        match = _RE_GENERIC_TYPE.search(name)
        if match:
            return '{0}[{1}]'.format(
                format_type(match.group(1), short),
                format_type(match.group(2), short))

    if short:
        return name.split('.')[-1]
    return name


def format_method(hint, format='{name}({params}): {return}', format_param='{name}: {fulltype}'):
    data = {
        'name': hint['name'],
        'full': hint['full'],
        'return': format_type(hint.get('type', '*unknown*'), True),
        'fullreturn': format_type(hint.get('type', '*unknown*'), False),

    }

    params = []
    for param in hint.get('params', []):
        n, t = (x.strip() for x in param.split(':'))
        params.append(format_param.format(
            name=n,
            type=format_type(t, True),
            fulltype=format_type(t, False)
        ))

    data['count'] = len(params)
    data['params'] = ', '.join(params)

    return format.format(**data)


def find_open_paren(code, open='([{', close=')]}'):
    """ Looks backwards to check if we are inside some kind of parens. A number
        of assumptions are made:
            - Multiple lines are supported if they end with `,` or `\`
            - Strings and comments must not contain parens
    """
    ofs = len(code)
    unbalanced = 1
    while unbalanced > 0 and ofs > 0:
        ofs -= 1
        char = code[ofs]
        if char in close:
            unbalanced += 1
        elif char in open:
            unbalanced -= 1
        elif char == '\n':
            # Check if the preceding line ends with a continuation character
            if not CONTINUATION_RE.search(code[max(0, ofs - 10):ofs]):
                break

    if unbalanced != 0:
        return None

    return ofs


def get_import_namespace(code):
    """ If the code ends with an import statement returns the target namespace
        being imported
    """
    # Handle explicit symbol imports with the form `import System(IO, Diagnost`
    ofs = find_open_paren(code, '(', ')')
    if ofs is None:
        ofs = len(code)

    # Extract the line where the offset is
    while ofs > 0 and code[ofs - 1] != '\n':
        ofs -= 1
    line = code[ofs:]

    matches = IMPORT_RE.search(line)
    if not matches:
        return None

    ns = matches.group(1) or matches.group(2)
    if not ns:
        return ''

    return ns.rstrip('.')


__all__ = [
    TYPESMAP,
    get_server,
    locate_rsp,
    format_type,
    format_method,
    find_open_paren,
    get_import_namespace,
]
