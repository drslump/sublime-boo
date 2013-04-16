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


__all__ = [
    get_server,
    locate_rsp
]
