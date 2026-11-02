"""CI/M7 offline guard: OS sockets may connect only to exact loopback or Unix."""
import ipaddress
import socket

_connect=socket.socket.connect
_connect_ex=socket.socket.connect_ex

def _allowed(sock,address):
    if sock.family==socket.AF_UNIX: return
    try: allowed=ipaddress.ip_address(address[0]).is_loopback
    except ValueError: allowed=address[0]=='localhost'
    if not allowed: raise RuntimeError('Offline verification blocked an external network connection')

def connect(self,address):
    _allowed(self,address)
    return _connect(self,address)

def connect_ex(self,address):
    _allowed(self,address)
    return _connect_ex(self,address)

socket.socket.connect=connect
socket.socket.connect_ex=connect_ex
