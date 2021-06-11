
from prometheus_client import Histogram

def format_metric(text: str):
    return text.replace('.', '_')


def format_period(text: str):
    return text.split(',', 1)[0]


def try_or_else(op, default):
    try:
        return op()
    except:
        return default

requestHistogram = Histogram(
    'cloudmonitor_request', 'CloudMonitor request latency', ['namespace', 'limiter'],
    buckets=(.1, .25, .5, .75, 1, 2.5, float('inf'))
)