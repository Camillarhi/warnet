#!/usr/bin/env python3

import json
import os
import subprocess
import time
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
            self.cleanup_kubectl_created_services()
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
        """Set up Kubernetes Service access to the Circuit Breaker API"""
        # Create a properly labeled service using kubectl expose
        service_name = f"{pod_name}-svc"

        self.log.info(f"Creating service {service_name} for pod {pod_name}")
        try:
            subprocess.run(
                [
                    "kubectl",
                    "expose",
                    "pod",
                    pod_name,
                    "--name",
                    service_name,
                    "--port",
                    str(self.cb_port),
                    "--target-port",
                    str(self.cb_port),
                ],
                check=True,
            )
        except subprocess.CalledProcessError as e:
            self.log.error(f"Failed to create service: {e.stderr}")
            raise

        time.sleep(51) # Wait for the service to be created

        service_url = f"http://{service_name}:{self.cb_port}/api"
        self.service_to_cleanup = service_name
        self.log.info(f"Service URL: {service_url}")

        self.log.info(f"Successfully created service at {service_url}")
        return service_url

    def cb_api_request(self, base_url, method, endpoint, data=None):
        """Make API requests using kubectl run curl-test approach"""
        try:
            # Build the curl command
            curl_cmd = [
                "kubectl",
                "exec",
                self.cb_node,
                "--",
                "curl",
                "-sS",
                "-X",
                method.upper(),
                f"{base_url}{endpoint}",
            ]

            if data:
                curl_cmd.extend(["-d", json.dumps(data), "-H", "Content-Type: application/json"])

            # Run the command and capture output
            result = subprocess.run(curl_cmd, check=True, capture_output=True, text=True)

            output = result.stdout.strip()
            if 'pod "curl-test" deleted' in output:
                output = output.replace('pod "curl-test" deleted', "").strip()

            return json.loads(output)
        except subprocess.CalledProcessError as e:
            self.log.error(f"API request failed: {e.stderr}")
            raise
        except json.JSONDecodeError:
            self.log.error(f"Invalid JSON response: {result.stdout}")
            raise

    def cleanup_kubectl_created_services(self):
        """Clean up any created resources"""
        if hasattr(self, "service_to_cleanup") and self.service_to_cleanup:
            self.log.info(f"Deleting service {self.service_to_cleanup}")
            subprocess.run(
                ["kubectl", "delete", "svc", self.service_to_cleanup],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


if __name__ == "__main__":
    test = LNBasicTest()
    test.run_test()
