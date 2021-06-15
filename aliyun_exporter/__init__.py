import argparse

import yaml
import logging
import signal
import sys
import time

from prometheus_client.core import REGISTRY

from .collector import AliyunCollector, CollectorConfig
from .web import create_app
from .utils import createHttpServer

def shutdown():
    logging.info('Shutting down, see you next time!')
    sys.exit(1)

def signal_handler():
    shutdown()

def main():
    signal.signal(signal.SIGTERM, signal_handler)
    logging.getLogger().setLevel(logging.INFO)

    parser = argparse.ArgumentParser(description="Aliyun CloudMonitor exporter for Prometheus.")
    parser.add_argument('-c', '--config-file', default='aliyun-exporter.yml',
                       help='path to configuration file.')
    parser.add_argument('-H', '--host', default=[], action='append',
                        help='exporter exposed host(default: "")')
    parser.add_argument('-p', '--port', default=[], action='append',
                        help='exporter exposed port(default: 9525)')
    parser.add_argument('-d', '--debug', default=False, action='store_true',
                        help='run exporter in debug mode')
    args = parser.parse_args()

    with open(args.config_file, 'r') as config_file:
        cfg = yaml.load(config_file, Loader=yaml.FullLoader)
    collector_config = CollectorConfig(**cfg)

    collector = AliyunCollector(collector_config)
    REGISTRY.register(collector)

    app = create_app(collector_config)

    if not args.host:
        hosts = ['']
    else:
        hosts = args.host
    if not args.port:
        ports = [9525]
    else:
        ports = args.port

    try:
        createHttpServer(hosts, ports, app, args.debug)
    except KeyboardInterrupt:
        pass

