import sys
import logging
from traceback import format_exc
from prometheus_client import Histogram, Metric

def format_metric(text: str):
    return text.replace('.', '_')


def format_period(text: str):
    return text.split(',', 1)[0]


def try_or_else(op, default):
    try:
        return op()
    except:
        return default

def mapInfoByKeys(point_labels:list, info:Metric, ext_labels:list):
    resp = {}
    for sample in info.samples:
        mapKey = ','.join(map(lambda x:sample.labels.get(x, ''), point_labels))
        mapValue = []
        for el in ext_labels:
            if isinstance(el, dict):
                mapValue.extend(map(lambda x:sample.labels.get(x, ''), el.keys()))
            else:
                mapValue.append(sample.labels.get(el, ''))
        resp[mapKey] = mapValue
    return resp

requestHistogram = Histogram(
    'cloudmonitor_request', 'CloudMonitor request latency', ['namespace', 'limiter'],
    buckets=(.1, .25, .5, .75, 1, 2.5, float('inf'))
)

def _getListens(hosts, ports):
    for h in hosts:
        for p in ports:
            yield (h, int(p))

def createHttpServer(hosts, ports, app):
    from tornado.wsgi import WSGIContainer
    from tornado.httpserver import HTTPServer
    from tornado.ioloop import IOLoop
    from tornado.log import access_log, LogFormatter
    access_handlers = logging.StreamHandler(sys.stdout)
    access_handlers.setFormatter(LogFormatter())
    access_log.addHandler(access_handlers)
    access_log.setLevel(logging.INFO)
    http_server = HTTPServer(WSGIContainer(app))
    listens = list(_getListens(hosts, ports))
    total_listen = len(listens)
    logging.info("Started exporter")
    for address, port in listens:
        logging.info('Listen on %s:%s' % (address, port))
        try:
            http_server.listen(int(port), address=address)
        except Exception as err:
            total_listen -= 1
            logging.warning('Listen on %s:%s Failed. %s' % (address, port, err))
            logging.debug(format_exc())
    if total_listen == 0:
        raise EnvironmentError('Start HttpServer Failed! No Address can be listened!')
    try:
        IOLoop.instance().start()
    except KeyboardInterrupt:
        pass