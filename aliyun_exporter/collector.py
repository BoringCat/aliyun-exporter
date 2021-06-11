import json
import logging
import time
import os

from datetime import datetime, timedelta
from prometheus_client.core import GaugeMetricFamily
from aliyunsdkcore.client import AcsClient
from aliyunsdkcms.request.v20190101 import DescribeMetricLastRequest
from aliyunsdkrds.request.v20140815 import DescribeDBInstancePerformanceRequest
from ratelimit import limits, sleep_and_retry

from concurrent.futures import ThreadPoolExecutor, as_completed

from aliyun_exporter.info_provider import InfoProvider
from aliyun_exporter.utils import try_or_else, requestHistogram

rds_performance = 'rds_performance'
special_namespaces = {
    rds_performance: lambda collector : RDSPerformanceCollector(collector),
}

class CollectorConfig(object):
    def __init__(self,
                 pool_size=None,
                 rate_limit=10,
                 rate_period=1,
                 credential=None,
                 metrics=None,
                 info_metrics=None,
                 protocol_type='http'
                 ):
        # if metrics is None:
        # raise Exception('Metrics config must be set.')

        self.credential = credential
        self.metrics = metrics
        self.pool_size = pool_size or rate_limit
        self.rate_limit = rate_limit
        self.rate_period = rate_period
        self.info_metrics = info_metrics
        self.protocol_type = protocol_type

        # ENV
        access_id = os.environ.get('ALIYUN_ACCESS_ID')
        access_secret = os.environ.get('ALIYUN_ACCESS_SECRET')
        region = os.environ.get('ALIYUN_REGION')
        protocol_type = os.environ.get('PROTOCOL_TYPE')
        if self.credential is None:
            self.credential = {}
        if access_id is not None and len(access_id) > 0:
            self.credential['access_key_id'] = access_id
        if access_secret is not None and len(access_secret) > 0:
            self.credential['access_key_secret'] = access_secret
        if region is not None and len(region) > 0:
            self.credential['region_ids'] = [ region ]
        if self.credential['access_key_id'] is None or \
                self.credential['access_key_secret'] is None:
            raise Exception('Credential is not fully configured.')
        if protocol_type is not None:
            self.protocol_type = protocol_type

class AliyunCollector(object):
    def __init__(self, config: CollectorConfig):
        self.metrics = config.metrics or {}
        self.info_metrics = config.info_metrics
        self.client = None
        self.info_providers = {}
        for region_id in config.credential['region_ids']:
            client = AcsClient(
                ak=config.credential['access_key_id'],
                secret=config.credential['access_key_secret'],
                region_id=region_id
            )
            self.info_providers[region_id] = InfoProvider(client, config.protocol_type)
            if not self.client:
                self.client = client
        self.limiter = limits(calls=config.rate_limit, period=config.rate_period)
        self.special_collectors = dict()
        self.pool = ThreadPoolExecutor(max_workers=config.pool_size)
        for k, v in special_namespaces.items():
            if k in self.metrics:
                self.special_collectors[k] = v(self)

    def query_metric(self, namespace: str, metric: str, period: int):
        histogram = requestHistogram.labels(namespace, False)
        limithistogram = requestHistogram.labels(namespace, True)
        @limithistogram.time()  # 限速后的请求时间
        @sleep_and_retry        # 等待并重试
        @self.limiter           # 限速
        @histogram.time()       # 真实请求时间
        def _fetch_metric(req):
            return self.client.do_action_with_exception(req)
        req = DescribeMetricLastRequest.DescribeMetricLastRequest()
        req.set_Namespace(namespace)
        req.set_MetricName(metric)
        req.set_Period(period)
        # start_time = time.time()
        try:
            resp = _fetch_metric(req)
        except Exception as e:
            logging.error('Error request cloud monitor api', exc_info=e)
            return []
        data = json.loads(resp)
        if 'Datapoints' in data:
            points = json.loads(data['Datapoints'])
            return points
        else:
            logging.error('Error query metrics for {}_{}, the response body don not have Datapoints field, please check you permission or workload' .format(namespace, metric))
            return []

    def parse_label_keys(self, point):
        return [k for k in point if k not in ['timestamp', 'Maximum', 'Minimum', 'Average', 'Value', 'userId']]

    def format_metric_name(self, namespace, name):
        return 'aliyun_{}_{}'.format(namespace, name)

    def metric_generator(self, namespace, metric):
        if 'name' not in metric:
            raise Exception('name must be set in metric item.')
        name = metric['name']
        metric_name = metric['name']
        period = 60
        measure = 'Average'
        if 'rename' in metric:
            name = metric['rename']
        if 'period' in metric:
            period = metric['period']
        if 'measure' in metric:
            measure = metric['measure']

        try:
            points = self.query_metric(namespace, metric_name, period)
        except Exception as e:
            logging.error('Error query metrics for {}_{}'.format(namespace, metric_name), exc_info=e)
            return (metric_up_gauge(self.format_metric_name(namespace, name), False),)
        if len(points) < 1:
            return (metric_up_gauge(self.format_metric_name(namespace, name), False),)
        label_keys = self.parse_label_keys(points[0])
        gauge = GaugeMetricFamily(self.format_metric_name(namespace, name), '', labels=label_keys)
        for point in points:
            if measure not in point:
                raise KeyError('Measure %s is not in datapoint %s_%s. Which have keys: [%s]' % (measure, namespace, name, ', '.join(point.keys())))
            timestamp = point.get('timestamp', None)
            if isinstance(timestamp, (int, float)):
                timestamp = timestamp / 1000
            gauge.add_metric([try_or_else(lambda: str(point[k]), '') for k in label_keys], point[measure], timestamp=timestamp)
        return (gauge, metric_up_gauge(self.format_metric_name(namespace, name), True))

    def collect(self):
        futures = []
        info_futures = []
        for namespace in self.metrics:
            if namespace in special_namespaces:
                futures.append(self.pool.submit(self.special_collectors[namespace].collect))
                continue
            for metric in self.metrics[namespace]:
                futures.append(self.pool.submit(self.metric_generator, namespace, metric))
        if self.info_metrics != None:
            for resource in self.info_metrics:
                for info_provider in self.info_providers.values():
                    info_futures.append(self.pool.submit(info_provider.get_metrics, resource))
        for future in as_completed(futures):
            yield from future.result()
        infos = {}
        for future in as_completed(info_futures):
            d = future.result()
            if not d['labels']:
                continue
            i = infos.get(d['name'],GaugeMetricFamily(d['name'], d['desc'], labels=d['labels']))
            for info in d['infos']:
                i.add_metric(info, 1)
            infos[d['name']] = i
        yield from infos.values()


def metric_up_gauge(resource: str, succeeded=True):
    metric_name = resource + '_up'
    description = 'Did the {} fetch succeed.'.format(resource)
    return GaugeMetricFamily(metric_name, description, value=int(succeeded))


class RDSPerformanceCollector:

    def __init__(self, delegate: AliyunCollector):
        self.parent = delegate

    def collect(self):
        for id in [s.labels['DBInstanceId'] for s in self.parent.info_provider.get_metrics('rds').samples]:
            metrics = self.query_rds_performance_metrics(id)
            for metric in metrics:
                yield from self.parse_rds_performance(id, metric)

    def parse_rds_performance(self, id, value):
        value_format: str = value['ValueFormat']
        metric_name = value['Key']
        keys = ['value']
        if value_format is not None and '&' in value_format:
            keys = value_format.split('&')
        metric = value['Values']['PerformanceValue']
        if len(metric) < 1:
            return
        values = metric[0]['Value'].split('&')
        for k, v in zip(keys, values):
            gauge = GaugeMetricFamily(
                self.parent.format_metric_name(rds_performance, metric_name + '_' + k),
                '', labels=['instanceId'])
            gauge.add_metric([id], float(v))
            yield gauge

    def query_rds_performance_metrics(self, id):
        req = DescribeDBInstancePerformanceRequest.DescribeDBInstancePerformanceRequest()
        req.set_DBInstanceId(id)
        req.set_Key(','.join([metric['name'] for metric in self.parent.metrics[rds_performance]]))
        now = datetime.utcnow();
        now_str = now.replace(second=0, microsecond=0).strftime("%Y-%m-%dT%H:%MZ")
        one_minute_ago_str = (now - timedelta(minutes=1)).replace(second=0, microsecond=0).strftime("%Y-%m-%dT%H:%MZ")
        req.set_StartTime(one_minute_ago_str)
        req.set_EndTime(now_str)
        try:
            resp = self.parent.client.do_action_with_exception(req)
        except Exception as e:
            logging.error('Error request rds performance api', exc_info=e)
            return []
        data = json.loads(resp)
        return data['PerformanceKeys']['PerformanceKey']
