from __future__ import absolute_import
import os
import re
from OpenSSL import SSL
from netlib import http_auth, certutils, tcp
from .. import utils, platform, version
from .primitives import RegularProxyMode, TransparentProxyMode, UpstreamProxyMode, ReverseProxyMode, Socks5ProxyMode

TRANSPARENT_SSL_PORTS = [443, 8443]
CONF_BASENAME = "mitmproxy"
CA_DIR = "~/.mitmproxy"


class HostMatcher(object):
    def __init__(self, patterns=[]):
        self.patterns = list(patterns)
        self.regexes = [re.compile(p, re.IGNORECASE) for p in self.patterns]

    def __call__(self, address):
        address = tcp.Address.wrap(address)
        host = "%s:%s" % (address.host, address.port)
        if any(rex.search(host) for rex in self.regexes):
            return True
        else:
            return False

    def __nonzero__(self):
        return bool(self.patterns)


class ProxyConfig:
    def __init__(
            self,
            host='',
            port=8080,
            server_version=version.NAMEVERSION,
            cadir=CA_DIR,
            clientcerts=None,
            no_upstream_cert=False,
            body_size_limit=None,
            mode=None,
            upstream_server=None,
            http_form_in=None,
            http_form_out=None,
            authenticator=None,
            ignore_hosts=[],
            tcp_hosts=[],
            ciphers_client=None,
            ciphers_server=None,
            certs=[],
            certforward=False,
            ssl_version_client="secure",
            ssl_version_server="secure",
            ssl_ports=TRANSPARENT_SSL_PORTS
    ):
        self.host = host
        self.port = port
        self.server_version = server_version
        self.ciphers_client = ciphers_client
        self.ciphers_server = ciphers_server
        self.clientcerts = clientcerts
        self.no_upstream_cert = no_upstream_cert
        self.body_size_limit = body_size_limit

        if mode == "transparent":
            self.mode = TransparentProxyMode(platform.resolver(), ssl_ports)
        elif mode == "socks5":
            self.mode = Socks5ProxyMode(ssl_ports)
        elif mode == "reverse":
            self.mode = ReverseProxyMode(upstream_server)
        elif mode == "upstream":
            self.mode = UpstreamProxyMode(upstream_server)
        else:
            self.mode = RegularProxyMode()

        # Handle manual overrides of the http forms
        self.mode.http_form_in = http_form_in or self.mode.http_form_in
        self.mode.http_form_out = http_form_out or self.mode.http_form_out

        self.check_ignore = HostMatcher(ignore_hosts)
        self.check_tcp = HostMatcher(tcp_hosts)
        self.authenticator = authenticator
        self.cadir = os.path.expanduser(cadir)
        self.certstore = certutils.CertStore.from_store(self.cadir, CONF_BASENAME)
        for spec, cert in certs:
            self.certstore.add_cert_file(spec, cert)
        self.certforward = certforward
        self.openssl_method_client, self.openssl_options_client = version_to_openssl(ssl_version_client)
        self.openssl_method_server, self.openssl_options_server = version_to_openssl(ssl_version_server)
        self.ssl_ports = ssl_ports


sslversion_choices = ("all", "secure", "SSLv2", "SSLv3", "TLSv1", "TLSv1_1", "TLSv1_2")


def version_to_openssl(version):
    """
    Convert a reasonable SSL version specification into the format OpenSSL expects.
    Don't ask...
    https://bugs.launchpad.net/pyopenssl/+bug/1020632/comments/3
    """
    if version == "all":
        return SSL.SSLv23_METHOD, None
    elif version == "secure":
        # SSLv23_METHOD + NO_SSLv2 + NO_SSLv3 == TLS 1.0+
        # TLSv1_METHOD would be TLS 1.0 only
        return SSL.SSLv23_METHOD, (SSL.OP_NO_SSLv2 | SSL.OP_NO_SSLv3)
    elif version in sslversion_choices:
        return getattr(SSL, "%s_METHOD" % version), None
    else:
        raise ValueError("Invalid SSL version: %s" % version)


def process_proxy_options(parser, options):
    body_size_limit = utils.parse_size(options.body_size_limit)

    c = 0
    mode, upstream_server = None, None
    if options.transparent_proxy:
        c += 1
        if not platform.resolver:
            return parser.error("Transparent mode not supported on this platform.")
        mode = "transparent"
    if options.socks_proxy:
        c += 1
        mode = "socks5"
    if options.reverse_proxy:
        c += 1
        mode = "reverse"
        upstream_server = options.reverse_proxy
    if options.upstream_proxy:
        c += 1
        mode = "upstream"
        upstream_server = options.upstream_proxy
    if c > 1:
        return parser.error("Transparent, SOCKS5, reverse and upstream proxy mode "
                            "are mutually exclusive.")

    if options.clientcerts:
        options.clientcerts = os.path.expanduser(options.clientcerts)
        if not os.path.exists(options.clientcerts) or not os.path.isdir(options.clientcerts):
            return parser.error(
                "Client certificate directory does not exist or is not a directory: %s" % options.clientcerts
            )

    if (options.auth_nonanonymous or options.auth_singleuser or options.auth_htpasswd):
        if options.auth_singleuser:
            if len(options.auth_singleuser.split(':')) != 2:
                return parser.error("Invalid single-user specification. Please use the format username:password")
            username, password = options.auth_singleuser.split(':')
            password_manager = http_auth.PassManSingleUser(username, password)
        elif options.auth_nonanonymous:
            password_manager = http_auth.PassManNonAnon()
        elif options.auth_htpasswd:
            try:
                password_manager = http_auth.PassManHtpasswd(options.auth_htpasswd)
            except ValueError, v:
                return parser.error(v.message)
        authenticator = http_auth.BasicProxyAuth(password_manager, "mitmproxy")
    else:
        authenticator = http_auth.NullProxyAuth(None)

    certs = []
    for i in options.certs:
        parts = i.split("=", 1)
        if len(parts) == 1:
            parts = ["*", parts[0]]
        parts[1] = os.path.expanduser(parts[1])
        if not os.path.exists(parts[1]):
            parser.error("Certificate file does not exist: %s" % parts[1])
        certs.append(parts)

    ssl_ports = options.ssl_ports
    if options.ssl_ports != TRANSPARENT_SSL_PORTS:
        # arparse appends to default value by default, strip that off.
        # see http://bugs.python.org/issue16399
        ssl_ports = ssl_ports[len(TRANSPARENT_SSL_PORTS):]

    return ProxyConfig(
        host=options.addr,
        port=options.port,
        cadir=options.cadir,
        clientcerts=options.clientcerts,
        no_upstream_cert=options.no_upstream_cert,
        body_size_limit=body_size_limit,
        mode=mode,
        upstream_server=upstream_server,
        http_form_in=options.http_form_in,
        http_form_out=options.http_form_out,
        ignore_hosts=options.ignore_hosts,
        tcp_hosts=options.tcp_hosts,
        authenticator=authenticator,
        ciphers_client=options.ciphers_client,
        ciphers_server=options.ciphers_server,
        certs=certs,
        certforward=options.certforward,
        ssl_version_client=options.ssl_version_client,
        ssl_version_server=options.ssl_version_server,
        ssl_ports=ssl_ports
    )


def ssl_option_group(parser):
    group = parser.add_argument_group("SSL")
    group.add_argument(
        "--cert", dest='certs', default=[], type=str,
        metavar="SPEC", action="append",
        help='Add an SSL certificate. SPEC is of the form "[domain=]path". '
             'The domain may include a wildcard, and is equal to "*" if not specified. '
             'The file at path is a certificate in PEM format. If a private key is included in the PEM, '
             'it is used, else the default key in the conf dir is used. '
             'The PEM file should contain the full certificate chain, with the leaf certificate as the first entry. '
             'Can be passed multiple times.'
    )
    group.add_argument(
        "--cert-forward", action="store_true",
        dest="certforward", default=False,
        help="Simply forward SSL certificates from upstream."
    )
    group.add_argument(
        "--ciphers-client", action="store",
        type=str, dest="ciphers_client", default=None,
        help="Set supported ciphers for client connections. (OpenSSL Syntax)"
    )
    group.add_argument(
        "--ciphers-server", action="store",
        type=str, dest="ciphers_server", default=None,
        help="Set supported ciphers for server connections. (OpenSSL Syntax)"
    )
    group.add_argument(
        "--client-certs", action="store",
        type=str, dest="clientcerts", default=None,
        help="Client certificate directory."
    )
    group.add_argument(
        "--no-upstream-cert", default=False,
        action="store_true", dest="no_upstream_cert",
        help="Don't connect to upstream server to look up certificate details."
    )
    group.add_argument(
        "--ssl-port", action="append", type=int, dest="ssl_ports", default=list(TRANSPARENT_SSL_PORTS),
        metavar="PORT",
        help="Can be passed multiple times. Specify destination ports which are assumed to be SSL. "
             "Defaults to %s." % str(TRANSPARENT_SSL_PORTS)
    )
    group.add_argument(
        "--ssl-version-client", dest="ssl_version_client",
        default="secure", action="store",
        choices=sslversion_choices,
        help="Set supported SSL/TLS version for client connections. "
             "SSLv2, SSLv3 and 'all' are INSECURE. Defaults to secure."
    )
    group.add_argument(
        "--ssl-version-server", dest="ssl_version_server",
        default="secure", action="store",
        choices=sslversion_choices,
        help="Set supported SSL/TLS version for server connections. "
             "SSLv2, SSLv3 and 'all' are INSECURE. Defaults to secure."
    )
