import json

from aliyunsdkcore.client import AcsClient
from cachetools import cached, TTLCache

import aliyunsdkecs.request.v20140526.DescribeInstancesRequest as DescribeECS
import aliyunsdkrds.request.v20140815.DescribeDBInstancesRequest as DescribeRDS
import aliyunsdkr_kvstore.request.v20150101.DescribeInstancesRequest as DescribeRedis
import aliyunsdkslb.request.v20140515.DescribeLoadBalancersRequest as DescribeSLB
import aliyunsdkdds.request.v20151201.DescribeDBInstancesRequest as Mongodb
from aliyunsdkcore.request import CommonRequest

from .utils import try_or_else, requestHistogram

ecsInfoHistogram = requestHistogram.labels('ecs_info', False)
rdsInfoHistogram = requestHistogram.labels('rds_info', False)
redisInfoHistogram = requestHistogram.labels('redis_info', False)
slbInfoHistogram = requestHistogram.labels('slb_info', False)
mongodbInfoHistogram = requestHistogram.labels('mongodb_info', False)
elasticsearchInfoHistogram = requestHistogram.labels('elasticsearch_info', False)
logstashInfoHistogram = requestHistogram.labels('logstash_info', False)

cache = TTLCache(maxsize=1000, ttl=3600)

class OpenAPIAddPageRequest(CommonRequest):
    def __init__(self, domain=None, version=None, action_name=None, uri_pattern=None, product=None,
                 location_endpoint_type='openAPI'):
        super().__init__(domain=domain, version=version, action_name=action_name, uri_pattern=uri_pattern,
                         product=product, location_endpoint_type=location_endpoint_type)
    def set_PageSize(self, PageSize):
        self.add_query_param('size', str(PageSize))
    def set_PageNumber(self,PageNumber):
        self.add_query_param('PageNumber', str(PageNumber))


'''
InfoProvider provides the information of cloud resources as metric.

The result from alibaba cloud API will be cached for an hour. 

Different resources should implement its own 'xxx_info' function. 

Different resource has different information structure, and most of
them are nested, for simplicity, we map the top-level attributes to the
labels of metric, and handle nested attribute specially. If a nested
attribute is not handled explicitly, it will be dropped.
'''
class InfoProvider():

    def __init__(self, client: AcsClient, protocol_type = 'http'):
        self.client = client
        assert protocol_type in ['http', 'https'], 'protocol_type must be "http" or "https"'
        self.protocol_type = protocol_type
        self.infos = set()

    def append_info(self, info_name):
        self.infos.add(info_name)

    def has(self, info_name):
        return info_name in self.infos

    @cached(cache)
    def get_metrics(self, resource: str) -> dict:
        return {
            'ecs': lambda : self.ecs_info(),
            'rds': lambda : self.rds_info(),
            'redis': lambda : self.redis_info(),
            'slb':lambda : self.slb_info(),
            'mongodb':lambda : self.mongodb_info(),
            'elasticsearch':lambda : self.elasticsearch_info(),
            'logstash':lambda : self.logstash_info(),
        }[resource]()

    @ecsInfoHistogram.time()
    def ecs_info(self) -> dict:
        req = DescribeECS.DescribeInstancesRequest()
        nested_handler = {
            'InnerIpAddress': lambda obj : try_or_else(lambda : obj['IpAddress'][0], ''),
            'PublicIpAddress': lambda obj : try_or_else(lambda : obj['IpAddress'][0], ''),
            'VpcAttributes': lambda obj : try_or_else(lambda : obj['PrivateIpAddress']['IpAddress'][0], ''),
        }
        return self.info_template(req, 'aliyun_meta_ecs', nested_handler=nested_handler)

    @rdsInfoHistogram.time()
    def rds_info(self) -> dict:
        req = DescribeRDS.DescribeDBInstancesRequest()
        return self.info_template(req, 'aliyun_meta_rds', to_list=lambda data: data['Items']['DBInstance'])

    @redisInfoHistogram.time()
    def redis_info(self) -> dict:
        req = DescribeRedis.DescribeInstancesRequest()
        return self.info_template(req, 'aliyun_meta_redis', to_list=lambda data: data['Instances']['KVStoreInstance'])

    @slbInfoHistogram.time()
    def slb_info(self) -> dict:
        req = DescribeSLB.DescribeLoadBalancersRequest()
        return self.info_template(req, 'aliyun_meta_slb', to_list=lambda data: data['LoadBalancers']['LoadBalancer'])

    @mongodbInfoHistogram.time()
    def mongodb_info(self) -> dict:
        req = Mongodb.DescribeDBInstancesRequest()
        return self.info_template(req, 'aliyun_meta_mongodb', to_list=lambda data: data['DBInstances']['DBInstance'])

    @elasticsearchInfoHistogram.time()
    def elasticsearch_info(self) -> dict:
        req = OpenAPIAddPageRequest()
        req.set_accept_format('json')
        req.set_method('GET')
        req.set_version('2017-06-13')
        req.add_header('Content-Type', 'application/json')
        req.set_uri_pattern('/openapi/instances')
        req.set_domain('elasticsearch.%s.aliyuncs.com' % self.client.get_region_id())
        body = '''{}'''
        req.set_content(body.encode('utf-8'))
        return self.info_template(req, 'aliyun_meta_elasticsearch', to_list=lambda data: data['Result'])

    @logstashInfoHistogram.time()
    def logstash_info(self) -> dict:
        req = OpenAPIAddPageRequest()
        req.set_accept_format('json')
        req.set_method('GET')
        req.set_version('2017-06-13')
        req.add_header('Content-Type', 'application/json')
        req.set_uri_pattern('/openapi/logstashes')
        req.set_domain('elasticsearch.%s.aliyuncs.com' % self.client.get_region_id())
        body = '''{}'''
        req.set_content(body.encode('utf-8'))
        return self.info_template(req, 'aliyun_meta_logstash_info', to_list=lambda data: data['Result'])

    '''
    Template method to retrieve resource information and transform to metric.
    '''
    def info_template(self,
                      req,
                      name,
                      desc='',
                      page_size=100,
                      page_num=1,
                      nested_handler=None,
                      to_list=(lambda data: data['Instances']['Instance'])) -> dict:
        infos = []
        label_keys = None
        for instance in self.pager_generator(req, page_size, page_num, to_list):
            if label_keys is None:
                label_keys = self.label_keys(instance, nested_handler)
            infos.append(dict(zip(label_keys, self.label_values(instance, label_keys, nested_handler))))
        return {'name': name, 'desc': desc, 'infos': infos, 'labels': label_keys}

    def pager_generator(self, req, page_size, page_num, to_list):
        req.set_PageSize(page_size)
        req.set_protocol_type(self.protocol_type)
        while True:
            req.set_PageNumber(page_num)
            try:
                resp = self.client.do_action_with_exception(req)
            except Exception as err:
                raise err
            data = json.loads(resp)
            instances = to_list(data)
            for instance in instances:
                yield instance
            if len(instances) < page_size:
                break
            page_num += 1

    def label_keys(self, instance, nested_handler=None):
        if nested_handler is None:
            nested_handler = {}
        return [k for k, v in instance.items()
                if k in nested_handler or isinstance(v, (str, int, float))]

    def label_values(self, instance, label_keys, nested_handler=None):
        if nested_handler is None:
            nested_handler = {}
        return map(lambda k: str(nested_handler[k](instance[k])) if k in nested_handler else try_or_else(lambda: str(instance[k]), ''),
                   label_keys)


