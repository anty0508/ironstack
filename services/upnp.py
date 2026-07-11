"""Automatic router port-opening via UPnP-IGD (pure Python, no external deps).

Home PCs sit behind a router and can't receive inbound internet connections
unless the router forwards the port. Rather than make the user configure port
forwarding by hand, this asks the router to open the port automatically over
UPnP. It's best-effort: many routers support UPnP out of the box, but if UPnP is
disabled the mapping fails and the user must port-forward manually (or use a
tunnel/relay).

Kept dependency-free (socket + urllib + regex) so it survives PyInstaller with no
extra hooks.
"""

import re
import socket
import urllib.request
from urllib.parse import urljoin

from logsetup import get_logger

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
# Search targets to try, most specific first.
SSDP_TARGETS = [
    "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
    "upnp:rootdevice",
]


def _discover_igd(timeout=3):
    """SSDP M-SEARCH for the Internet Gateway Device; return its description URL."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(timeout)
    try:
        for st in SSDP_TARGETS:
            msg = ("\r\n".join([
                "M-SEARCH * HTTP/1.1",
                f"HOST: {SSDP_ADDR}:{SSDP_PORT}",
                'MAN: "ssdp:discover"',
                "MX: 2",
                f"ST: {st}",
                "", "",
            ])).encode()
            try:
                sock.sendto(msg, (SSDP_ADDR, SSDP_PORT))
            except OSError:
                continue
            # Collect a few replies for this target.
            try:
                while True:
                    data, _ = sock.recvfrom(65507)
                    text = data.decode("utf-8", "ignore")
                    m = re.search(r"^LOCATION:\s*(\S+)", text, re.IGNORECASE | re.MULTILINE)
                    if m:
                        return m.group(1).strip()
            except socket.timeout:
                continue
    finally:
        sock.close()
    return None


def _find_wan_service(desc_url):
    """From the IGD description XML, find the WAN connection service and return
    (absolute_control_url, service_type)."""
    try:
        with urllib.request.urlopen(desc_url, timeout=5) as resp:
            xml = resp.read().decode("utf-8", "ignore")
    except Exception:
        get_logger().debug("upnp: could not fetch description %s", desc_url, exc_info=True)
        return None, None

    for block in re.findall(r"<service>(.*?)</service>", xml, re.DOTALL):
        st = re.search(r"<serviceType>(.*?)</serviceType>", block, re.DOTALL)
        cu = re.search(r"<controlURL>(.*?)</controlURL>", block, re.DOTALL)
        if not (st and cu):
            continue
        service_type = st.group(1).strip()
        if "WANIPConnection" in service_type or "WANPPPConnection" in service_type:
            control_url = urljoin(desc_url, cu.group(1).strip())
            return control_url, service_type
    return None, None


def _soap(control_url, service_type, action, args_xml):
    body = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        f'<u:{action} xmlns:u="{service_type}">{args_xml}</u:{action}>'
        '</s:Body></s:Envelope>'
    ).encode("utf-8")
    req = urllib.request.Request(
        control_url, data=body,
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": f'"{service_type}#{action}"',
        },
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.read().decode("utf-8", "ignore")


def add_port_mapping(port, internal_ip, description="IronStack", protocol="TCP"):
    """Ask the router to forward external `port` -> `internal_ip:port` for the
    given protocol ("TCP" or "UDP").

    Returns (external_ip, None) on success, or (None, error_message) on failure.
    """
    desc_url = _discover_igd()
    if not desc_url:
        return None, "no UPnP router found (UPnP may be disabled on the router)"

    control_url, service_type = _find_wan_service(desc_url)
    if not control_url:
        return None, "router has no UPnP WAN service"

    args = (
        "<NewRemoteHost></NewRemoteHost>"
        f"<NewExternalPort>{int(port)}</NewExternalPort>"
        f"<NewProtocol>{protocol}</NewProtocol>"
        f"<NewInternalPort>{int(port)}</NewInternalPort>"
        f"<NewInternalClient>{internal_ip}</NewInternalClient>"
        "<NewEnabled>1</NewEnabled>"
        f"<NewPortMappingDescription>{description}</NewPortMappingDescription>"
        "<NewLeaseDuration>0</NewLeaseDuration>"
    )
    try:
        _soap(control_url, service_type, "AddPortMapping", args)
    except Exception as exc:
        get_logger().debug("upnp: AddPortMapping failed", exc_info=True)
        return None, f"router rejected the port mapping ({exc})"

    # Best-effort: fetch the external IP so we can tell the user where to connect.
    external_ip = None
    try:
        resp = _soap(control_url, service_type, "GetExternalIPAddress", "")
        m = re.search(r"<NewExternalIPAddress>(.*?)</NewExternalIPAddress>", resp)
        if m:
            external_ip = m.group(1).strip()
    except Exception:
        pass

    return external_ip, None


def delete_port_mapping(port, protocol="TCP"):
    """Remove a mapping previously added with add_port_mapping (best-effort)."""
    desc_url = _discover_igd()
    if not desc_url:
        return
    control_url, service_type = _find_wan_service(desc_url)
    if not control_url:
        return
    args = (
        "<NewRemoteHost></NewRemoteHost>"
        f"<NewExternalPort>{int(port)}</NewExternalPort>"
        f"<NewProtocol>{protocol}</NewProtocol>"
    )
    try:
        _soap(control_url, service_type, "DeletePortMapping", args)
    except Exception:
        get_logger().debug("upnp: DeletePortMapping failed", exc_info=True)
