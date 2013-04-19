import re
from os import path
from glob import glob

from BooHints.server import Server

# Registry of spawned servers
_SERVERS = {}


def get_server(cmd, args, fname=None, rsp=None, cwd=None):
    """ Spawn or retrieve a server suitable for the given arguments
    """
    if rsp is None and fname is not None:
        rsp = locate_rsp(fname)

    if cwd is None:
        if rsp is not None:
            cwd = path.dirname(path.abspath(rsp))
        elif fname is not None:
            cwd = path.dirname(path.abspath(fname))

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


def locate_rsp(fname):
    """ Tries to locate an .rsp file in one of the parent directories
    """
    p = path.dirname(fname)
    while len(p) > 3:
        matches = glob('{0}/*.rsp'.format(p))
        if len(matches):
            return matches[0]

        p = path.dirname(p)

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


__all__ = [
    TYPESMAP,
    get_server,
    locate_rsp,
    format_type,
    format_method
]
