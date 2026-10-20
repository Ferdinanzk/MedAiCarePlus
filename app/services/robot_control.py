"""The robot app's control address (quick fix 9, robot app 0.17.2): the Reachy card's microphone button applies the
switch on the robot at once, instead of at the robot's next heartbeat (up to 10 s).

Every heartbeat of robot app 0.17.2 carries `control: {"url", "key"}`: the app's own web API on the robot's LAN address
toward the server (http://<IPv4>:8042) and a random key the app makes at each start, kept in its memory only. The
server keeps it (reachy_device.status_detail["control"]) only when the url is a private IPv4 address literal on port
8042 with no path, query or user info, and the key is 20-200 URL-safe characters; anything else is dropped. So the
server never calls an address the robot did not prove it serves from a private network. The key never leaves the
server: no status, answer or log line has it (and the account export drops it, api_account._strip_secrets).
"""

import asyncio
import hashlib
import ipaddress
import json
import logging
import re

import requests

log = logging.getLogger(__name__)

MIC_DIRECT_TIMEOUT_SECONDS = 3   # one try; the heartbeat applies the switch within ~10 s anyway
CONTROL_URL = re.compile(r"http://(\d{1,3}(?:\.\d{1,3}){3}):8042")
CONTROL_KEY = re.compile(r"[A-Za-z0-9_-]{20,200}")
PRIVATE_NETWORKS = tuple(ipaddress.IPv4Network(network) for network in ("10.0.0.0/8", "172.16.0.0/12",
                                                                        "192.168.0.0/16"))

_dropped: dict[str, str] = {}   # device id -> a digest of the control last dropped (one WARNING per change)


def check(control) -> tuple[dict | None, str | None]:
    """(the {url, key} to keep, None), or (None, why it is dropped); (None, None) when the heartbeat sent none."""
    if control is None:
        return None, None
    if not isinstance(control, dict):
        return None, "not a {url, key} object"
    url, key = control.get("url"), control.get("key")
    address = CONTROL_URL.fullmatch(url) if isinstance(url, str) else None
    try:
        host = ipaddress.IPv4Address(address.group(1)) if address else None
    except ValueError:   # 256 and above, or a leading zero
        host = None
    if host is None:
        return None, "the address is not http://<IPv4>:8042"
    if not any(host in network for network in PRIVATE_NETWORKS):
        return None, "the address is not on a private network"
    if not isinstance(key, str) or not CONTROL_KEY.fullmatch(key):
        return None, "the key is not 20-200 URL-safe characters"
    return {"url": url, "key": key}, None


def from_heartbeat(device_id: str, u_id: int, control) -> dict | None:
    """The control address a heartbeat carries, to keep in status_detail, or None. A dropped one logs one WARNING per
    change (its reason only: never the address or the key)."""
    kept, reason = check(control)
    if reason is None:
        _dropped.pop(device_id, None)
        return kept
    digest = hashlib.sha256(json.dumps(control, sort_keys=True, default=str).encode()).hexdigest()
    if _dropped.get(device_id) != digest:
        _dropped[device_id] = digest
        log.warning("heartbeat of patient %s: the robot's control address was dropped (%s); the microphone button "
                    "waits for the heartbeat", u_id, reason)
    return None


def _post(url: str, key: str, muted: bool) -> requests.Response:
    with requests.Session() as session:
        session.trust_env = False   # no proxy and no .netrc from the environment: the key goes to the robot alone
        return session.post(f"{url}/api/microphone", json={"muted": muted}, headers={"Authorization": f"Bearer {key}"},
                            timeout=MIC_DIRECT_TIMEOUT_SECONDS, allow_redirects=False)


async def set_microphone(control: dict, muted: bool) -> tuple[int | None, str | None]:
    """POST {url}/api/microphone {"muted"} with the key, one try within MIC_DIRECT_TIMEOUT_SECONDS. (the daemon's
    volume after the change, None) when the robot confirmed the switch (the volume may be None: the robot could not
    read it), else (None, why not). Never raises."""
    try:
        response = await asyncio.wait_for(asyncio.to_thread(_post, control["url"], control["key"], muted),
                                          MIC_DIRECT_TIMEOUT_SECONDS)
    except (asyncio.TimeoutError, requests.Timeout):
        return None, "timeout"
    except requests.ConnectionError:
        return None, "no connection"
    except Exception as error:   # the type only: a request error's text names the address
        log.warning("microphone: the direct call to the robot failed: %s", type(error).__name__)
        return None, "error"
    if response.status_code != 200:
        return None, f"HTTP {response.status_code}"
    try:
        answer = response.json()
    except ValueError:
        return None, "bad answer"
    volume = answer.get("volume") if isinstance(answer, dict) else None
    if not isinstance(answer, dict) or answer.get("muted") is not muted or not (
            volume is None or (isinstance(volume, int) and not isinstance(volume, bool) and 0 <= volume <= 100)):
        return None, "bad answer"
    return volume, None
