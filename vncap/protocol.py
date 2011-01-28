from os import urandom

from twisted.internet import reactor
from twisted.internet.defer import Deferred, DeferredList
from twisted.internet.endpoints import TCP4ClientEndpoint
from twisted.internet.protocol import Factory
from twisted.protocols.stateful import StatefulProtocol
from twisted.python import log
from twisted.python.failure import Failure

from d3des import generate_response
from rfb import check_password

(
    STATE_VERSION,
    STATE_SECURITY_TYPES,
    STATE_AUTHENTICATION,
    STATE_RESULT,
    STATE_CONNECTED
) = range(5)

class VNCAuthenticator(StatefulProtocol):
    """
    Base class for VNC protocols.

    This protocol isn't interesting on its own; subclass it to make a server
    or client.
    """

    VERSION = "RFB 003.008\n"

    def __init__(self, password):
        self.password = password
        self.authentication_d = Deferred()

    def authenticated(self):
        """
        Switch to proxy mode.
        """

        log.msg("Successfully authenticated %s!" % self)
        self.transport.pauseProducing()
        reactor.callLater(0, self.authentication_d.callback, self)

class VNCServerAuthenticator(VNCAuthenticator):
    """
    Trivial server protocol which can authenticate VNC clients.

    This protocol is lacking lots of things, like support for older VNC
    protocols.
    """

    def connectionMade(self):
        self.transport.write(self.VERSION)

    def getInitialState(self):
        return self.check_version, 12

    def check_version(self, version):
        if version == self.VERSION:
            log.msg("Checked version!")
            self.transport.write("\x02\x01\x02")
            return self.select_security_type, 1
        else:
            log.err("Can't handle VNC version %s" % version)
            self.transport.loseConnection()

    def select_security_type(self, security_type):
        """
        Choose the security type that the client wants.
        """
        log.msg("trace pick_security_types")

        security_type = ord(security_type)

        if security_type == 2:
            # VNC authentication. Issue our challenge.
            self.challenge = urandom(16)
            self.transport.write(self.challenge)

            return self.vnc_authentication_result, 16
        elif security_type == 1:
            # No authentication. Just move to the SecurityResult.
            self.authenticated()
        else:
            log.err("Couldn't agree on an authentication scheme!")
            self.transport.loseConnection()

    def vnc_authentication_result(self, response):
        log.msg("Doing VNC auth, buf %r" % response)

        if check_password(self.challenge, response, self.password):
            self.authenticated()
        else:
            log.err("Failed VNC auth!")
            self.transport.loseConnection()

    def authenticated(self):
        self.transport.write("\x00\x00\x00\x00")
        VNCAuthenticator.authenticated(self)

class VNCClientAuthenticator(VNCAuthenticator):
    """
    Trivial client protocol which can authenticate itself to a VNC server.

    This protocol is lacking lots of things, like support for older VNC
    protocols.
    """

    def __init__(self, *args, **kwargs):
        VNCAuthenticator.__init__(self, *args, **kwargs)
        log.msg("Init'd client")

    def getInitialState(self):
        log.msg("Client initial state")
        return self.check_version, 12

    def check_version(self, version):
        if version == self.VERSION:
            log.msg("Checked version!")
            self.transport.write(self.VERSION)
            return self.count_security_types, 1
        else:
            log.err("Can't handle VNC version %s" % version)
            self.transport.loseConnection()

    def count_security_types(self, data):
        count = ord(data)

        if not count:
            log.err("Server wouldn't give us any security types!")
            self.transport.loseConnection()

        return self.pick_security_type, count

    def pick_security_type(self, data):
        """
        Ascertain whether the server supports any security types we might
        want.
        """

        security_types = set(ord(i) for i in data)
        log.msg("Available authentication methods: %s"
            % ", ".join(hex(i) for i in security_types))

        if 2 in security_types:
            log.msg("Choosing VNC authentication...")
            self.transport.write("\x02")
            return self.vnc_authentication, 16
        elif 1 in security_types:
            log.msg("Choosing no authentication...")
            self.transport.write("\x01")
            return self.security_result, 4
        else:
            log.err("Couldn't agree on an authentication scheme!")
            self.transport.loseConnection()

    def vnc_authentication(self, challenge):
        # Take in 16 bytes, encrypt with 3DES using the password as the key,
        # and send the response.

        response = generate_response(self.password, challenge)
        self.transport.write(response)

        return self.security_result, 4

    def security_result(self, data):
        log.msg("trace authenticated")
        if data == "\x00\x00\x00\x00":
            # Success!
            self.authenticated()
        else:
            log.err("Failed security result!")
            self.transport.loseConnection()

class VNCClientAuthenticatorFactory(Factory):
    protocol = VNCClientAuthenticator

    def __init__(self, password):
        self.password = password

    def buildProtocol(self, addr):
        p = self.protocol(self.password)
        p.factory = self
        return p

def start_proxying(result):
    """
    Callback to start proxies.
    """

    log.msg("Starting proxy")
    client_result, server_result = result
    success, client = client_result
    if not success:
        return Failure("Couldn't connect on client side!")
    success, server = server_result
    if not success:
        return Failure("Couldn't connect on server side!")

    server.dataReceived = client.transport.write
    client.dataReceived = server.transport.write

    server.transport.resumeProducing()
    client.transport.resumeProducing()
    log.msg("Proxying started!")

def prepare_proxy(client, server):
    """
    Set up the deferred proxy callback.
    """

    log.msg("Preparing proxies for client %s and server %s"
        % (client, server))
    dl = DeferredList([client.authentication_d, server.authentication_d])
    dl.addCallback(start_proxying)

def make_server_and_client(host, port, password):
    """
    Make a server protocol and client protocol with matching passwords, and
    glue them together so that they will auto-proxy after authenticating.

    Returns the server protocol. The client protocol is automatically started
    and is not retrieveable.
    """

    server = VNCServerAuthenticator(password)

    endpoint = TCP4ClientEndpoint(reactor, host, port)
    d = endpoint.connect(VNCClientAuthenticatorFactory(password))
    d.addCallback(prepare_proxy, server)

    return server
