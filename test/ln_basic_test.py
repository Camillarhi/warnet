#!/usr/bin/env python3

import json
import os
import random
import subprocess
import time
import requests
from pathlib import Path
from time import sleep

from test_base import TestBase

from warnet.process import stream_command


class LNBasicTest(TestBase):
    def __init__(self):
        super().__init__()
        self.network_dir = Path(os.path.dirname(__file__)) / "data" / "ln"
        self.scen_dir = Path(os.path.dirname(__file__)).parent / "resources" / "scenarios"
        self.lns = [
            "tank-0000-ln",
            "tank-0001-ln",
            "tank-0002-ln",
            "tank-0003-ln",
            "tank-0004-ln",
            "tank-0005-ln",
        ]

        self.cb_port = 9235
        self.cb_node = "tank-0003-ln"
        self.port_forward = None

    def run_test(self):
        try:
            # Wait for all nodes to wake up. ln_init will start automatically
            self.setup_network()

            # Test circuit breaker API
            self.test_circuit_breaker_api()

            # Send a payment across channels opened automatically by ln_init
            self.pay_invoice(sender="tank-0005-ln", recipient="tank-0003-ln")

            # Manually open two more channels between first three nodes
            # and send a payment using warnet RPC
            self.manual_open_channels()
            self.wait_for_gossip_sync(self.lns[:3], 2 + 2)
            self.pay_invoice(sender="tank-0000-ln", recipient="tank-0002-ln")

        finally:
            self.cleanup()

    def setup_network(self):
        self.log.info("Setting up network")
        stream_command(f"warnet deploy {self.network_dir}")

    def fund_wallets(self):
        outputs = ""
        for lnd in self.lns:
            addr = json.loads(self.warnet(f"ln rpc {lnd} newaddress p2wkh"))["address"]
            outputs += f',"{addr}":10'
        # trim first comma
        outputs = outputs[1:]

        self.warnet("bitcoin rpc tank-0000 sendmany '' '{" + outputs + "}'")
        self.warnet("bitcoin rpc tank-0000 -generate 1")

    def wait_for_two_txs(self):
        self.wait_for_predicate(
            lambda: json.loads(self.warnet("bitcoin rpc tank-0000 getmempoolinfo"))["size"] == 2
        )

    def manual_open_channels(self):
        # 0 -> 1 -> 2
        pk1 = self.warnet("ln pubkey tank-0001-ln")
        pk2 = self.warnet("ln pubkey tank-0002-ln")

        host1 = ""
        host2 = ""

        while not host1 or not host2:
            if not host1:
                host1 = self.warnet("ln host tank-0001-ln")
            if not host2:
                host2 = self.warnet("ln host tank-0002-ln")
            sleep(1)

        print(
            self.warnet(
                f"ln rpc tank-0000-ln openchannel --node_key {pk1} --local_amt 100000 --connect {host1}"
            )
        )
        print(
            self.warnet(
                f"ln rpc tank-0001-ln openchannel --node_key {pk2} --local_amt 100000 --connect {host2}"
            )
        )

        self.wait_for_two_txs()

        self.warnet("bitcoin rpc tank-0000 -generate 10")

    def wait_for_gossip_sync(self, nodes, expected):
        while len(nodes) > 0:
            for node in nodes:
                chs = json.loads(self.warnet(f"ln rpc {node} describegraph"))["edges"]
                if len(chs) >= expected:
                    nodes.remove(node)
            sleep(1)

    def pay_invoice(self, sender: str, recipient: str):
        init_balance = int(json.loads(self.warnet(f"ln rpc {recipient} channelbalance"))["balance"])
        inv = json.loads(self.warnet(f"ln rpc {recipient} addinvoice --amt 1000"))
        print(inv)
        print(self.warnet(f"ln rpc {sender} payinvoice -f {inv['payment_request']}"))

        def wait_for_success():
            return (
                int(json.loads(self.warnet(f"ln rpc {recipient} channelbalance"))["balance"])
                == init_balance + 1000
            )

        self.wait_for_predicate(wait_for_success)

    def scenario_open_channels(self):
        # 2 -> 3
        # connecting all six ln nodes in the graph
        scenario_file = self.scen_dir / "test_scenarios" / "ln_init.py"
        self.log.info(f"Running scenario from: {scenario_file}")
        self.warnet(f"run {scenario_file} --source_dir={self.scen_dir} --debug")

    def test_circuit_breaker_api(self):
        self.log.info("Testing Circuit Breaker API")

        # Set up port forwarding to the circuit breaker
        cb_url = self.setup_api_access(self.cb_node)

        self.log.info(f"Testing Circuit Breaker API at {cb_url}")

        # Test /info endpoint
        info = self.cb_api_request(cb_url, "get", "/info")
        assert "version" in info, "Circuit breaker info missing version"

        # Test /limits endpoint
        limits = self.cb_api_request(cb_url, "get", "/limits")
        assert isinstance(limits, dict), "Limits should be a dictionary"

        self.log.info("✅ Circuit Breaker API tests passed")

    def setup_api_access(self, pod_name):
        """Set up local port forwarding to the circuit breaker"""
        port = random.randint(10000, 20000)
        self.log.info(f"Setting up port-forward {port}:9235 to {pod_name}")

        self.port_forward = subprocess.Popen(
            ["kubectl", "port-forward", pod_name, f"{port}:{self.cb_port}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # prevent subprocess from holding up execution
            start_new_session=True,
        )

        cb_url = f"http://localhost:{port}/api"
        if not self.wait_for_port_forward_ready(cb_url):
            self.port_forward.terminate()
            raise Exception("Port-forward failed to become ready")

        return cb_url

    def wait_for_port_forward_ready(self, cb_url, timeout=60):
        """Wait until we can successfully connect to the API"""
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                response = requests.get(f"{cb_url}/info", timeout=2)
                if response.status_code == 200:
                    return True
            except requests.exceptions.RequestException:
                time.sleep(1)
        return False

    def cb_api_request(self, base_url, method, endpoint, data=None):
        url = f"{base_url}{endpoint}"
        try:
            if method == "get":
                response = requests.get(url)
            elif method == "post":
                response = requests.post(url, json=data)
            else:
                raise ValueError(f"Unsupported method: {method}")

            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            self.log.error(f"Circuit Breaker API request failed: {e}")
            raise


if __name__ == "__main__":
    test = LNBasicTest()
    test.run_test()
