#!/usr/bin/env python3

import time
import json
import csv
import sys
import re
import io
from datetime import datetime, timedelta
from zipfile import ZipFile

import pytz
import singer
from singer import utils
from bingads import AuthorizationData, OAuthWebAuthCodeGrant, ServiceClient
import suds
from suds.sudsobject import asdict
import stringcase
import requests
import arrow

from tap_bing_ads import reports

LOGGER = singer.get_logger()

REQUIRED_CONFIG_KEYS = [
    "start_date",
    "customer_id",
    "account_ids",
    "oauth_client_id",
    "oauth_client_secret",
    "refresh_token",
    "developer_token",
]

CONFIG = {}
STATE = {}

MAX_NUM_REPORT_POLLS = 10
REPORT_POLL_SLEEP = 5

SESSION = requests.Session()

ARRAY_TYPE_REGEX = r'ArrayOf([a-z]+)'

def create_sdk_client(service, account_id):
    LOGGER.info('Creating SOAP client with OAuth refresh credentials')

    authentication = OAuthWebAuthCodeGrant(
        CONFIG['oauth_client_id'],
        CONFIG['oauth_client_secret'],
        '') ## redirect URL not needed for refresh token

    authentication.request_oauth_tokens_by_refresh_token(CONFIG['refresh_token'])

    authorization_data = AuthorizationData(
        account_id=account_id,
        customer_id=CONFIG['customer_id'],
        developer_token=CONFIG['developer_token'],
        authentication=authentication)

    return ServiceClient(service, authorization_data)

def sobject_to_dict(obj):
    if not hasattr(obj, '__keylist__'):
        return obj

    out = {}
    for key, value in asdict(obj).items():
        if hasattr(value, '__keylist__'):
            out[key] = sobject_to_dict(value)
        elif isinstance(value, list):
            out[key] = []
            for item in value:
                out[key].append(sobject_to_dict(item))
        elif isinstance(value, datetime):
            out[key] = arrow.get(value).isoformat()
        else:
            out[key] = value
    return out

def xml_to_json_type(xml_type):
    if xml_type == 'boolean':
        return 'boolean'
    if xml_type in ['decimal', 'float', 'double']:
        return 'number'
    if xml_type == 'long':
        return 'integer'

    return 'string'

def get_json_schema(element):
    types = []
    _format = None
    enum = None

    if element.nillable:
        types.append('null')

    if element.root.name == 'simpleType':
        enum = list(map(lambda x: x.name, element.rawchildren[0].rawchildren))
        types.append('string')
    else:
        xml_type = element.type[0]

        _type = xml_to_json_type(xml_type)
        types.append(_type)

        if xml_type in ['dateTime', 'date']:
            _format = 'date-time'

    schema = {'type': types}

    if _format:
        schema['format'] = _format

    if enum:
        schema['enum'] = enum

    return schema

def get_array_type(array_type):
    xml_type = re.match(ARRAY_TYPE_REGEX, array_type).groups()[0]
    json_type = xml_to_json_type(xml_type)

    return {
        'type': ['null', 'array'],
        'items': {
            'type': json_type
        }
    }

def get_complex_type_elements(inherited_types, wsdl_type):
    ## inherited type
    if isinstance(wsdl_type.rawchildren[0].rawchildren[0], suds.xsd.sxbasic.Extension):
        abstract_base = wsdl_type.rawchildren[0].rawchildren[0].ref[0]
        if abstract_base not in inherited_types:
            inherited_types[abstract_base] = set()
        inherited_types[abstract_base].add(wsdl_type.name)

        elements = []
        for element_group in wsdl_type.rawchildren[0].rawchildren[0].rawchildren:
            for element in element_group:
                elements.append(element[0])
        return elements
    else:
        return wsdl_type.rawchildren[0].rawchildren

def wsdl_type_to_schema(inherited_types, wsdl_type):
    if wsdl_type.root.name == 'simpleType':
        return get_json_schema(wsdl_type)

    elements = get_complex_type_elements(inherited_types, wsdl_type)

    properties = {}
    for element in elements:
        if element.root.name == 'enumeration':
            properties[element.name] = get_json_schema(element)
        elif element.type is None and element.ref:
            properties[element.name] = element.ref[0] ## set to service type name for now
        elif element.type[1] != 'http://www.w3.org/2001/XMLSchema': ## not a built-in XML type
            _type = element.type[0]
            if 'ArrayOfstring' in _type:
                properties[element.name] = get_array_type(_type)
            else:
                properties[element.name] = _type ## set to service type name for now
        else:
            properties[element.name] = get_json_schema(element)

    return {
        'type': ['null', 'object'],
        'additionalProperties': False,
        'properties': properties
    }

def normalize_abstract_types(inherited_types, type_map):
    for base_type, types in inherited_types.items():
        if base_type in type_map:
            schemas = []
            for inherited_type in types:
                if inherited_type in type_map:
                    schemas.append(type_map[inherited_type])
            schemas.append(type_map[base_type])
            type_map[base_type] = {'oneOf': schemas}

def fill_in_nested_types(type_map, schema):
    for prop, descriptor in schema['properties'].items():
        if isinstance(descriptor, str) and descriptor in type_map:
            schema['properties'][prop] = type_map[descriptor]

def get_type_map(client):
    inherited_types = {}
    type_map = {}
    for type_tuple in client.soap_client.sd[0].types:
        _type = type_tuple[0]
        qname = _type.qname[1]
        if 'https://bingads.microsoft.com' not in qname and \
           'http://schemas.datacontract.org' not in qname:
            continue
        type_map[_type.name] = wsdl_type_to_schema(inherited_types, _type)

    normalize_abstract_types(inherited_types, type_map)

    for schema in type_map.values():
        if 'properties' in schema:
            fill_in_nested_types(type_map, schema)
        elif 'items' in schema and 'properties' in schema['items']:
            fill_in_nested_types(type_map, schema['items'])

    return type_map

def get_stream_def(stream_name, schema, metadata=None, pks=None, replication_key=None):
    stream_def = {
        'tap_stream_id': stream_name,
        'stream': stream_name,
        'schema': schema
    }

    if pks:
        stream_def['key_properties'] = pks

    if replication_key:
        stream_def['replication_key'] = replication_key
        stream_def['replication_method'] = 'INCREMENTAL'
    else:
        stream_def['replication_method'] = 'FULL_TABLE'

    if metadata:
        stream_def['metadata'] = metadata

    return stream_def

def get_core_schema(client, obj):
    type_map = get_type_map(client)
    return type_map[obj]

def discover_core_objects():
    core_object_streams = []

    LOGGER.info('Initializing CustomerManagementService client - Loading WSDL')
    client = ServiceClient('CustomerManagementService')

    account_schema = get_core_schema(client, 'AdvertiserAccount')
    core_object_streams.append(
        get_stream_def('accounts', account_schema, pks=['Id'], replication_key='LastModifiedTime'))

    LOGGER.info('Initializing CampaignManagementService client - Loading WSDL')
    client = ServiceClient('CampaignManagementService')

    campaign_schema = get_core_schema(client, 'Campaign')
    core_object_streams.append(get_stream_def('campaigns', campaign_schema, pks=['Id']))

    ad_group_schema = get_core_schema(client, 'AdGroup')
    core_object_streams.append(get_stream_def('ad_groups', ad_group_schema, pks=['Id']))

    ad_schema = get_core_schema(client, 'Ad')
    core_object_streams.append(get_stream_def('ads', ad_schema, pks=['Id']))

    return core_object_streams

def get_report_schema(report_colums):
    properties = {}
    for column in report_colums:
        if column in reports.REPORTING_FIELD_TYPES:
            _type = reports.REPORTING_FIELD_TYPES[column]
        else:
            _type = 'string'

        # TimePeriod's column name changes depending on aggregation level
        # This tap always uses daily aggregation
        if column == 'TimePeriod':
            column = 'GregorianDate'
            _type = 'datetime'

        if _type == 'datetime':
            col_schema = {'type': 'string', 'format': 'date-time'}
        else:
            col_schema = {'type': _type}

        properties[column] = col_schema

    properties['_sdc_report_datetime'] = {
        'type': 'string',
        'format': 'date-time'
    }

    return {
        'properties': properties,
        'additionalProperties': False
    }

def get_report_metadata(report_name):
    if report_name in reports.REPORT_SPECIFIC_REQUIRED_FIELDS:
        required_fields = (
            reports.REPORT_REQUIRED_FIELDS +
            reports.REPORT_REQUIRED_FIELDS[report_name])
    else:
        required_fields = reports.REPORT_REQUIRED_FIELDS

    return list(map(
        lambda field: {"metadata": {"inclusion": "automatic"}, "breadcrumb": ["properties", field]},
        required_fields))

def discover_reports():
    report_streams = []
    LOGGER.info('Initializing ReportingService client - Loading WSDL')
    client = ServiceClient('ReportingService')
    type_map = get_type_map(client)
    report_column_regex = r'^(?!ArrayOf)(.+Report)Column$'

    for type_name, type_schema in type_map.items():
        match = re.match(report_column_regex, type_name)
        if match and match.groups()[0] in reports.REPORT_WHITELIST:
            report_name = match.groups()[0]
            stream_name = stringcase.snakecase(report_name)
            report_schema = get_report_schema(type_schema['enum'])
            report_metadata = get_report_metadata(report_name)
            report_stream_def = get_stream_def(
                stream_name,
                report_schema,
                metadata=report_metadata)
            report_streams.append(report_stream_def)

    return report_streams

def test_credentials(account_ids):
    if not account_ids:
        raise Exception('At least one id in account_ids is required to test authentication')

    create_sdk_client('CustomerManagementService', account_ids[0])

def do_discover(account_ids):
    LOGGER.info('Testing authentication')
    test_credentials(account_ids)

    LOGGER.info('Discovering core objects')
    core_object_streams = discover_core_objects()

    LOGGER.info('Discovering reports')
    report_streams = discover_reports()

    json.dump({'streams': core_object_streams + report_streams}, sys.stdout, indent=2)

## TODO: remove fields not selected?

def sync_accounts_stream(account_ids):
    accounts = []

    LOGGER.info('Initializing CustomerManagementService client - Loading WSDL')
    client = ServiceClient('CustomerManagementService')
    account_schema = get_core_schema(client, 'AdvertiserAccount')
    singer.write_schema('accounts', account_schema, ['Id'])

    for account_id in account_ids:
        client = create_sdk_client('CustomerManagementService', account_id)
        response = client.GetAccount(AccountId=account_id)
        accounts.append(sobject_to_dict(response))

    accounts_bookmark = singer.get_bookmark(STATE, 'accounts', 'last_record')
    if accounts_bookmark:
        accounts = list(
            filter(lambda x: x['LastModifiedTime'] >= accounts_bookmark,
                   accounts))

    max_accounts_last_modified = max([x['LastModifiedTime'] for x in accounts])

    singer.write_records('accounts', accounts)

    singer.write_bookmark(STATE, 'accounts', 'last_record', max_accounts_last_modified)
    singer.write_state(STATE)

def sync_campaigns(client, account_id, selected_streams):
    response = client.GetCampaignsByAccountId(AccountId=account_id)
    response_dict = sobject_to_dict(response)
    if 'Campaign' in response_dict:
        campaigns = response_dict['Campaign']

        if 'campaigns' in selected_streams:
            singer.write_schema('campaigns', get_core_schema(client, 'Campaign'), ['Id'])
            singer.write_records('campaigns', campaigns)

        return map(lambda x: x['Id'], campaigns)

def sync_ad_groups(client, account_id, campaign_ids, selected_streams):
    ad_group_ids = []
    for campaign_id in campaign_ids:
        response = client.GetAdGroupsByCampaignId(CampaignId=campaign_id)
        response_dict = sobject_to_dict(response)

        if 'AdGroup' in response_dict:
            ad_groups = sobject_to_dict(response)['AdGroup']

            if 'ad_groups' in selected_streams:
                LOGGER.info('Syncing AdGroups for Account: {}, Campaign: {}'.format(
                    account_id, campaign_id))
                singer.write_schema('ad_groups', get_core_schema(client, 'AdGroup'), ['Id'])
                singer.write_records('ad_groups', ad_groups)

            ad_group_ids.append(list(map(lambda x: x['Id'], ad_groups)))
    return ad_group_ids

def sync_ads(client, ad_group_ids):
    for ad_group_id in ad_group_ids:
        response = client.GetAdsByAdGroupId(
            AdGroupId=ad_group_id,
            AdTypes={
                'AdType': [
                    'AppInstall',
                    'DynamicSearch',
                    'ExpandedText',
                    'Product',
                    'Text',
                    'Image'
                ]
            })
        response_dict = sobject_to_dict(response)

        if 'Ad' in response_dict:
            singer.write_schema('ads', get_core_schema(client, 'Ad'), ['Id'])
            singer.write_records('ads', response_dict['Ad'])

def sync_core_objects(account_id, selected_streams):
    client = create_sdk_client('CampaignManagementService', account_id)

    LOGGER.info('Syncing Campaigns for Account: {}'.format(account_id))
    campaign_ids = sync_campaigns(client, account_id, selected_streams)

    if campaign_ids and ('ad_groups' in selected_streams or 'ads' in selected_streams):
        ad_group_ids = sync_ad_groups(client, account_id, campaign_ids, selected_streams)
        if 'ads' in selected_streams:
            LOGGER.info('Syncing Ads for Account: {}'.format(account_id))
            sync_ads(client, ad_group_ids)

def stream_report(stream_name, report_name, url):
    response = SESSION.get(url)

    if response.status_code != 200:
        raise Exception('Non-200 ({}) response downloading report: {}'.format(
            response.status_code, report_name))

    with ZipFile(io.BytesIO(response.content)) as zip_file:
        with zip_file.open(zip_file.namelist()[0]) as binary_file:
            with io.TextIOWrapper(binary_file) as csv_file:
                # handle control character at the start of the file
                header_line = next(csv_file)[1:]
                headers = header_line.replace('"', '').split(',')

                reader = csv.DictReader(csv_file, fieldnames=headers)

                for row in reader:
                    ## TODO: add _sdc_report_datetime
                    ## TODO: data type row
                    singer.write_record(stream_name, row)

def sync_report(client, account_id, report_stream): # account_id will be used pylint: disable=unused-argument
    report_name = stringcase.pascalcase(report_stream.stream)

    ## TODO: add date range to log
    LOGGER.info('Syncing report: {}'.format(report_name))

    report_request = client.factory.create('{}Request'.format(report_name))
    report_request.Format = 'Csv'
    report_request.Aggregation = 'Daily'
    report_request.Language = 'English'
    report_request.ExcludeReportHeader = True
    report_request.ExcludeReportFooter = True

    ## TODO: Columns - only user selected columns
    report_columns = client.factory.create('ArrayOf{}Column'.format(report_name))
    report_columns.KeywordPerformanceReportColumn.append([
        'TimePeriod',
        'AccountId',
        'CampaignId',
        'Keyword',
        'KeywordId',
        'DeviceType',
        'BidMatchType',
        'Clicks',
        'Impressions',
        'Ctr',
        'AverageCpc',
        'Spend',
        'QualityScore',
    ])
    report_request.Columns = report_columns

    ## TODO: use config start_date ?
    ## TODO: use config conversion_window

    #now = datetime.utcnow()
    ## TODO: remove
    ## !!!!!!!! hard coded for development
    end_datetime = datetime(2016, 7, 15)
    start_datetime = end_datetime - timedelta(days=30)

    start_date = client.factory.create('Date')
    start_date.Day = start_datetime.day
    start_date.Month = start_datetime.month
    start_date.Year = start_datetime.year

    end_date = client.factory.create('Date')
    end_date.Day = end_datetime.day
    end_date.Month = end_datetime.month
    end_date.Year = end_datetime.year

    report_time = client.factory.create('ReportTime')
    report_time.CustomDateRangeStart = start_date
    report_time.CustomDateRangeEnd = end_date
    report_time.PredefinedTime = None
    report_request.Time = report_time

    request_id = client.SubmitGenerateReport(report_request)

    for _ in range(0, MAX_NUM_REPORT_POLLS):
        response = client.PollGenerateReport(request_id)
        if response.Status == 'Error':
            raise Exception('Error running {} report'.format(report_name))
        if response.Status == 'Success':
            ## TODO: add date range to log
            LOGGER.info('No results for report: {}'.format(report_name))
            if response.ReportDownloadUrl:
                stream_report(report_stream.stream, report_name, response.ReportDownloadUrl)
            break
        time.sleep(REPORT_POLL_SLEEP)

def sync_reports(account_id, catalog):
    client = create_sdk_client('ReportingService', account_id)

    reports_to_sync = filter(lambda x: x.is_selected() and x.stream[-6:] == 'report',
                             catalog.streams)

    for report_stream in reports_to_sync:
        sync_report(client, account_id, report_stream)

def sync_account_data(account_id, catalog, selected_streams):
    LOGGER.info('Syncing core objects')
    sync_core_objects(account_id, selected_streams)

    LOGGER.info('Syncing reports')
    sync_reports(account_id, catalog)

def do_sync_all_accounts(account_ids, catalog):
    selected_streams = list(
        map(lambda x: x.stream,
            filter(lambda x: x.is_selected(),
                   catalog.streams)))

    ## TODO: write schemas - catalog is just for selection

    if 'accounts' in selected_streams:
        LOGGER.info('Syncing Accounts')
        sync_accounts_stream(account_ids)

    for account_id in account_ids:
        sync_account_data(account_id, catalog, selected_streams)

def main_impl():
    args = utils.parse_args(REQUIRED_CONFIG_KEYS)

    CONFIG.update(args.config)
    STATE.update(args.state)
    account_ids = CONFIG['account_ids'].split(",")

    if args.discover:
        do_discover(account_ids)
        LOGGER.info("Discovery complete")
    elif args.catalog:
        do_sync_all_accounts(account_ids, args.catalog)
        LOGGER.info("Sync Completed")
    else:
        LOGGER.info("No catalog was provided")

## TODO:
## - Account TimeZone? - Convert core objects timezone based on this?
## - Use Campaign.TimeZone for reporting timezone? Convert report timezones based on this?
## - record_counter metric each time rows are streamed
## - http_request_timer or generic Timer for each SDK call and
##      initalize a client (since it loads the remote WSDL)
## - job_timer while waiting on report jobs

def main():
    try:
        main_impl()
    except Exception as exc:
        LOGGER.critical(exc)
        raise exc

if __name__ == "__main__":
    main()
