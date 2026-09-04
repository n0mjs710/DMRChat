"""
Kernel routing table lifecycle for the MOTOTRBO link.

Two networks have to be pointed at the radio gateway for the app's traffic to
leave the host at all:

    12.0.0.0/8    private (unicast DM) targets
    225.0.0.0/8   group (room broadcast) targets

Routes are injected at startup and torn down on exit -- including on SIGINT --
so the app never leaves stale entries behind in the kernel table.
"""

import subprocess

from . import protocol

COMMAND_TIMEOUT = 30


def radio_networks():
    """
    The networks that must point at the radio gateway, derived from the
    addressing actually in use rather than hardcoded.

    This matters: private messages are addressed to the PC network (13 by
    default), so a route for 12/8 alone would send every DM out the host's
    default route and off the radio entirely. The radio network is included
    too -- it costs nothing and keeps ping/ARS to the radios themselves
    working over the link.
    """
    octets = {
        protocol.CAI_NETWORK,
        protocol.PRIVATE_TX_PREFIX,
        protocol.GROUP_TX_PREFIX,
        protocol.SOURCE_RX_PREFIX,
    }
    return tuple(f"{octet}.0.0.0/8" for octet in sorted(octets))


RADIO_NETWORKS = radio_networks()

# route(8) says this when the entry we are adding is already present, or when
# the entry we are deleting was never there. Neither is a failure for us.
_BENIGN = ("file exists", "not in table", "no such process", "already in table")


class RouteResult:
    def __init__(self, command, returncode, output, benign=False):
        self.command = command
        self.returncode = returncode
        self.output = output.strip()
        self.benign = benign

    @property
    def ok(self):
        return self.returncode == 0 or self.benign

    def __str__(self):
        state = "ok" if self.returncode == 0 else ("already applied" if self.benign else "FAILED")
        detail = f" -- {self.output}" if self.output and not self.ok else ""
        return f"{' '.join(self.command)} [{state}]{detail}"


class RouteManager:
    """Adds the MOTOTRBO routes, remembers what it added, and removes exactly those."""

    def __init__(self, gateway, networks=None, dry_run=False):
        self.gateway = gateway
        # Resolved at construction, not import, so --dm-prefix is reflected.
        self.networks = tuple(networks if networks is not None else radio_networks())
        self.dry_run = dry_run
        self.installed = []       # networks we successfully routed, for teardown
        self.log = []

    def _run(self, action, network):
        command = ["sudo", "route", "-n", action, "-net", network, self.gateway]

        if self.dry_run:
            result = RouteResult(command, 0, "(dry run, not executed)")
            self.log.append(result)
            return result

        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=COMMAND_TIMEOUT
            )
        except subprocess.TimeoutExpired:
            result = RouteResult(command, 1, "timed out waiting for sudo")
            self.log.append(result)
            return result
        except OSError as error:
            result = RouteResult(command, 1, str(error))
            self.log.append(result)
            return result

        output = (completed.stdout or "") + (completed.stderr or "")
        benign = completed.returncode != 0 and any(
            phrase in output.lower() for phrase in _BENIGN
        )
        result = RouteResult(command, completed.returncode, output, benign)
        self.log.append(result)
        return result

    def install(self):
        """Inject the radio routes. Returns ``(ok, results)``."""
        results = []
        for network in self.networks:
            result = self._run("add", network)
            results.append(result)
            if result.ok:
                self.installed.append(network)
        return all(r.ok for r in results), results

    def teardown(self):
        """
        Remove the routes we installed, in reverse order.

        Safe to call more than once -- the installed list is cleared as we go,
        so a teardown from both the exit path and a signal handler is harmless.
        """
        results = []
        while self.installed:
            network = self.installed.pop()
            results.append(self._run("delete", network))
        return results
