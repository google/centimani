"""Google Cloud function that uploads chunk of products to the Content API."""

# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# -*- coding: utf-8 -*-

import csv
import io
import json
import os
import re
import sys
import traceback
from typing import Any, Dict, List, Tuple

from flask import Response
from google.cloud import pubsub_v1
from google.cloud import secretmanager
from google.cloud import storage
from google.oauth2 import service_account
import google_auth_httplib2
from googleapiclient import discovery
from googleapiclient import http

CONTENT_API_SCOPE = 'https://www.googleapis.com/auth/content'
APPLICATION_NAME = 'mc_invoker'
SERVICE_NAME = 'content'
SERVICE_VERSION = 'v2.1'
MAX_PAGE_SIZE = 50

BUCKET_NAME = 'bucket_name'
PROJECT_ID = 'project_id'
DEPLOYMENT_NAME = 'deployment_name'
SOLUTION_PREFIX = 'solution_prefix'
REPORTING_TOPIC = 'reporting_topic'
CACHE_TTL_IN_HOURS = 'cache_ttl_in_hours'
FULL_PATH_TOPIC = 'full_path_topic'
MAX_ATTEMPTS = 'max_attempts'

PRODUCT_SINGLE_ATTR = {
    'id': 'offerId',
    'title': 'title',
    'description': 'description',
    'link': 'link',
    'image_link': 'imageLink',
    'content_language': 'contentLanguage',
    'target_country': 'targetCountry',
    'channel': 'channel',
    'expiration_date': 'expirationDate',
    'adult': 'adult',
    'kind': 'kind',
    'brand': 'brand',
    'color': 'color',
    'google_product_category': 'googleProductCategory',
    'gtin': 'gtin',
    'item_group_id': 'itemGroupId',
    'material': 'material',
    'mpn': 'mpn',
    'pattern': 'pattern',
    'sale_price_effective_date': 'salePriceEffectiveDate',
    'identifier_exists': 'identifierExists',
    'multipack': 'multipack',
    'custom_label_0': 'customLabel0',
    'custom_label_1': 'customLabel1',
    'custom_label_2': 'customLabel2',
    'custom_label_3': 'customLabel3',
    'custom_label_4': 'customLabel4',
    'is_bundle': 'isBundle',
    'mobile_link': 'mobileLink',
    'availability_date': 'availabilityDate',
    'shipping_label': 'shippingLabel',
    'display_ads_id': 'displayAdsId',
    'display_ads_title': 'displayAdsTitle',
    'display_ads_link': 'displayAdsLink',
    'display_ads_value': 'displayAdsValue',
    'sell_on_google_quantity': 'sellOnGoogleQuantity',
    'max_handling_time': 'maxHandlingTime',
    'min_handling_time': 'minHandlingTime',
    'age_group': 'ageGroup',
    'availability': 'availability',
    'condition': 'condition',
    'gender': 'gender',
    'size_system': 'sizeSystem',
    'size_type': 'sizeType',
    'source': 'source',
    'additional_size_type': 'additionalSizeType',
    'energy_efficiency_class': 'energyEfficiencyClass',
    'min_energy_efficiency_class': 'minEnergyEfficiencyClass',
    'max_energy_efficiency_class': 'maxEnergyEfficiencyClass',
    'tax_category': 'taxCategory',
    'transit_time_label': 'transitTimeLabel',
    'pickup_method': 'pickupMethod',
    'pickup_sla': 'pickupSla',
    'link_template': 'linkTemplate',
    'mobile_link_template': 'mobileLinkTemplate',
    'canonical_link': 'canonicalLink',
    'ads_grouping': 'adsGrouping',
    'adsRedirect': 'ads_redirect'
}
PRODUCT_ARRAY_ATTR = {
    'additional_image_link': 'additionalImageLinks',
    'size': 'sizes',
    'display_ads_similar_id': 'displayAdsSimilarIds',
    'promotion_id': 'promotionIds',
    'included_destination': 'includedDestinations',
    'excluded_destination': 'excludedDestinations',
    'ads_label': 'adsLabels',
    'product_type': 'productTypes',
    'shopping_ads_excluded_country': 'shoppingAdsExcludedCountries',
    'product_highlight': 'productHighlights'
}

PRODUCT_OTHER_ATTR = {
    'price': 'price',
    'sale_price': 'salePrice',
    'shipping': 'shipping',
    'shipping_weight': 'shippingWeight'
}


def _count_partial_errors(response: str):
  """Counts the partial errors in the Content API response.

  Args:
      response: Content API upload response.

  Returns:
      An integer representing the total number of partial errors in the response
      failure error.
      A list containing the code, message and number of times that each unique
      error code was returned by the API for one of the products uploaded.
  """

  error_count = 0
  error_stats = {}
  error_array = []

  if response['kind'] == 'content#productsCustomBatchResponse':
    entries = response['entries']
    for entry in entries:
      errors = entry.get('errors')
      if errors:
        print('Errors for batch entry %d:' % entry['batchId'])
        print('A partial failure for batch entry '
              f'{entry["batchId"]} occurred. Error messages are shown below.')

        for error in errors['errors']:
          error_count += 1
          error_message = error['message']
          domain = error['domain']
          reason = error['reason']

          error_code = f'{error_message}_{domain}_{reason}'
          if error_code not in error_stats:
            error_stats[error_code] = {'count': 0}
            error_stats[error_code]['message'] = error_message
          error_stats[error_code]['count'] += 1

          print(f' Error message: {error["message"]}, '
                f'domain: {error["domain"]}, '
                f'reason: {error["reason"]}')

    for code_key in error_stats:
      error_array.append({
          'code': code_key,
          'message': error_stats[code_key]['message'],
          'count': error_stats[code_key]['count']
      })
  else:
    print('There was an error. Response: %s' % response)
    error_count += 1
    error_stats['major'] = {
        'count':
            1,
        'message':
            ('A major error ocurred as an invalid response was captured. '
             'Response: {response}')
    }

  return error_count, error_array


def _add_errors_to_input_data(data: Dict[str, Any],
                              num_errors: int) -> Dict[str, Any]:
  """Includes the error count to the input data.

  Args:
    data: The input data received in the trigger invocation.
    num_errors: The number of errors to add.

  Returns:
    The input data enriched with the num errors.
  """
  data['child']['num_errors'] = num_errors
  return data


def _read_csv_from_blob(bucket_name: str,
                        blob_name: str,
                        delimiter: str = '\t'):
  """Function to read a blob containing a CSV file and return it as an array.

  Args:
    bucket_name: The name of the source bucket.
    blob_name: The name of the file to move.
    delimiter: Optional, the CSV delimiter character (tab character if empty).

  Returns:
    A csv reader object to be used to iterate.
  """
  storage_client = storage.Client()
  print('Reading {} from {}'.format(blob_name, bucket_name))
  bucket = storage_client.get_bucket(bucket_name)
  blob = bucket.blob(blob_name)
  downloaded_blob = blob.download_as_string()
  decoded_blob = downloaded_blob.decode('utf-8')
  return csv.reader(io.StringIO(decoded_blob), delimiter=delimiter)


def _read_json_from_blob(bucket_name: str, blob_name: str):
  """Function to read a blob containing a json file and return it as a list.

  Args:
    bucket_name: The name of the source bucket.
    blob_name: The name of the file to move.

  Returns:
    A list of objects extracted from the file.
  """
  products = []
  storage_client = storage.Client()
  print('Reading {} from {}'.format(blob_name, bucket_name))
  bucket = storage_client.get_bucket(bucket_name)
  blob = bucket.blob(blob_name)
  downloaded_blob = blob.download_as_string()
  decoded_blob = downloaded_blob.decode('utf-8')
  lines = decoded_blob.split('\n')
  for line in lines:
    if line:
      # Remove BOM if present
      line = line.replace('\ufeff', '')
      product = json.loads(line)
      products.append(product)
  return products


def _mv_blob(bucket_name, blob_name, new_bucket_name, new_blob_name):
  """Function for moving files between directories or buckets in GCP.

  Args:
    bucket_name: The name of the source bucket.
    blob_name: The name of the file to move.
    new_bucket_name: The name of target bucket (can be equal the source one).
    new_blob_name: name of file in target bucket.

  Returns:
      None.
  """
  storage_client = storage.Client()
  source_bucket = storage_client.get_bucket(bucket_name)
  source_blob = source_bucket.blob(blob_name)
  destination_bucket = storage_client.get_bucket(new_bucket_name)

  # copy to new destination
  source_bucket.copy_blob(source_blob, destination_bucket, new_blob_name)
  # delete from source
  source_blob.delete()

  print(f'File moved from {blob_name} to {new_blob_name}')


def _mv_blob_if_last_try(task_retries, max_attempts, input_json, bucket_name):
  """Checks if it is the last attempt and moves the chunk to the failed folder.

  Args:
    task_retries: Retry number passed from Cloud Tasks.
    max_attempts: Max number of configured retries.
    input_json: Configuration information.
    bucket_name: Name of the GCS file storing the chunks.

  Returns:
    None.
  """

  if task_retries + 1 == max_attempts:
    datestamp = input_json['date']
    chunk_filename = input_json['child']['file_name']
    full_chunk_path = datestamp + '/slices_processing/' + chunk_filename
    new_file_name = full_chunk_path.replace('slices_processing/',
                                            'slices_failed/')
    _mv_blob(bucket_name, full_chunk_path, bucket_name, new_file_name)


def _upload_products(service: discovery.Resource, env_info: Dict[str, Any],
                     job_info: Dict[str, Any], products, task_retries: int,
                     full_chunk_path: str):
  """Loads a chunk of products from GCS and sends it to the Content API.

  Args:
    service: Initialized service of a Content API client.
    env_info: GCP configuration extracted from enviroment variables.
    job_info: Job configuration derived from the input_json object.
    products: Chunk of products prepared to be uploaded.
    task_retries: Number of retries performed in Cloud Tasks.
    full_chunk_path: Full path to the chunk being processed.

  Returns:
    The status of the operation: 200 represents success or a non-retryable
    error, 500 is a retryable error.
  """

  try:
    response = None

    if job_info['operation'] in ['insert', 'delete', 'direct']:
      response = _send_products_in_batch(service, job_info, products)
    else:
      raise Exception(f'Invalid operation found: {job_info["operation"]}.')
    if response:
      num_partial_errors, error_array = _count_partial_errors(response)
      pubsub_payload = _add_errors_to_input_data(job_info['input_json'],
                                                 num_partial_errors)
      if error_array:
        pubsub_payload['child']['errors'] = error_array
      _send_pubsub_message(env_info[PROJECT_ID], env_info[FULL_PATH_TOPIC],
                           pubsub_payload)
      # Move blob to /slices_processed after a successful execution
      new_file_name = full_chunk_path.replace('slices_processing/',
                                              'slices_processed/')
      _mv_blob(env_info[BUCKET_NAME], full_chunk_path, env_info[BUCKET_NAME],
               new_file_name)
    return 200
  # pylint: disable=broad-except
  except Exception:
    print('Unexpected error while uploading products:', sys.exc_info()[0])
    str_traceback = traceback.format_exc()
    print('Unexpected exception traceback follows:')
    print(str_traceback)

    input_json = job_info['input_json']
    pubsub_payload = _add_errors_to_input_data(input_json,
                                               input_json['child']['num_rows'])
    _send_pubsub_message(env_info[PROJECT_ID], env_info[REPORTING_TOPIC],
                         pubsub_payload)
    # If last try, move blob to /slices_failed
    _mv_blob_if_last_try(task_retries, env_info[MAX_ATTEMPTS], input_json,
                         env_info[BUCKET_NAME])
    return 500
  return 200


def _build_products(raw_products: List[List[str]], job_info: Dict[str, Any]):
  """Builds a list of products from the raw data provided in the CSV file.

  Args:
    raw_products: Products read from the CSV file.
    job_info: Job configuration derived from the input_json object.

  Returns:
    A list of products prepared to be uploaded.
  """
  fields = next(raw_products)
  fields = [x.strip() for x in fields]
  # Remove BOM if present
  fields[0] = fields[0].replace('\ufeff', '')
  products = []
  for raw_product in raw_products:
    raw_product = [x.strip() for x in raw_product]
    if (job_info['operation'] == 'delete'):
      product = _build_product_for_deletion(raw_product, fields, job_info)
    else:
      product = _build_product_for_insertion(raw_product, fields, job_info)
    if product:
      products.append(product)
  return products


def _build_product_for_insertion(raw_product: List[str], fields: List[str],
                                 job_info: Dict[str, Any]):
  """Builds a single product object from the raw data provided in the CSV file.

  Args:
    raw_product: Products read from the CSV file.
    fields: A list with the fields' names from the header of the CSV file.
    job_info: Job configuration derived from the input_json object.

  Returns:
    An individual product prepared to be uploaded.
  """
  product = {
      'channel': job_info['channel'],
      'targetCountry': job_info['target_country'],
      'contentLanguage': job_info['content_language']
  }

  for field, value in zip(fields, raw_product):
    if value:
      if field in PRODUCT_SINGLE_ATTR:
        product[PRODUCT_SINGLE_ATTR[field]] = value
      elif field in PRODUCT_ARRAY_ATTR:
        if PRODUCT_ARRAY_ATTR[field] not in product:
          product[PRODUCT_ARRAY_ATTR[field]] = []
        product[PRODUCT_ARRAY_ATTR[field]].append(value)
      else:
        # Check for special fields
        if field in ['price', 'sale_price']:
          values = value.split(sep=' ', maxsplit=1)
          currency = job_info['default_currency']
          if len(values) > 1:
            currency = values[1]
          product[PRODUCT_OTHER_ATTR[field]] = {
              'value': values[0],
              'currency': currency
          }
        elif field == 'shipping':
          multiple_shippings = value.split(sep=',')
          product[PRODUCT_OTHER_ATTR[field]] = []
          for shipping_info in multiple_shippings:
            splitted = shipping_info.split(sep=':')
            shipping = {'country': splitted[0]}
            if len(splitted) > 1 and splitted[1]:
              # Warning: We are hardcoding region, but it could also be
              # postal_code, location_id or location_group_name
              shipping['region'] = splitted[1]
            if len(splitted) > 2 and splitted[2]:
              shipping['service'] = splitted[2]
            if len(splitted) > 3 and splitted[3]:
              values = splitted[3].split(sep=' ', maxsplit=1)
              currency = job_info['default_currency']
              if len(values) > 1:
                currency = values[1]
              shipping['price'] = {'value': values[0], 'currency': currency}
            if len(splitted) > 4 and splitted[4]:
              shipping['minHandlingTime'] = splitted[4]
            if len(splitted) > 5 and splitted[5]:
              shipping['maxHandlingTime'] = splitted[5]
            if len(splitted) > 6 and splitted[6]:
              shipping['minTransitTime'] = splitted[6]
            if len(splitted) > 7 and splitted[7]:
              shipping['maxTransitTime'] = splitted[7]

            product[PRODUCT_OTHER_ATTR[field]].append(shipping)
        elif field == 'shipping_weight':
          values = value.split(sep=' ', maxsplit=1)
          unit = job_info['default_shipping_weight_unit']
          if len(values) > 1:
            unit = values[1]
          product[PRODUCT_OTHER_ATTR[field]] = {
              'value': values[0],
              'unit': unit
          }

  return product


def _build_product_for_deletion(raw_product: List[str], fields: List[str],
                                job_info: Dict[str, Any]):
  """Builds a single product object from the raw data provided in the CSV file.

  Args:
    raw_product: Products read from the CSV file.
    fields: A list with the fields' names from the header of the CSV file.
    job_info: Job configuration derived from the input_json object.

  Returns:
    An individual product prepared to be uploaded.
  """
  product = {}

  if 'product_id' in fields:
    product['productId'] = raw_product[fields.index('product_id')]
  elif 'productId' in fields:
    product['productId'] = raw_product[fields.index('productId')]
  elif 'id' in fields:
    product['productId'] = (f'{job_info["channel"]}'
                            f':{job_info["content_language"]}'
                            f':{job_info["target_country"]}'
                            f':{raw_product[fields.index("id")]}')
  else:
    print('Invalid product received for deletion. No product_id or id found.')

  return product


def _send_pubsub_message(project_id, reporting_topic, pubsub_payload):
  """Sends a pubsub message.

  Args:
    project_id: ID of the Google Cloud Project where the solution is deployed.
    reporting_topic: Pub/Sub topic to use in the message to be sent.
    pubsub_payload: Payload of the Pub/Sub message to be sent.

  Returns:
    None.
  """
  publisher = pubsub_v1.PublisherClient()
  topic_path_reporting = publisher.topic_path(project_id, reporting_topic)
  publisher.publish(
      topic_path_reporting, data=bytes(json.dumps(pubsub_payload),
                                       'utf-8')).result()


def _blob_exists(bucket_name, blob_name):
  """Checks if a blob exists in Google Cloud Storage.

  Args:
    bucket_name: Name of the bucket to read the blob from.
    blob_name: Name of the blob to check.

  Returns:
    Boolean indicating if the blob exists or not.
  """

  storage_client = storage.Client()
  bucket = storage_client.bucket(bucket_name)
  return storage.Blob(bucket=bucket, name=blob_name).exists(storage_client)


def _check_products_blob(datestamp, bucket_name, chunk_filename):
  """Checks if a blob exists either in the processing or processed directories.

  Args:
    datestamp: timestamp used to build the directory name
    bucket_name: Name of the bucket to read the blob from
    chunk_filename: Name of the chunk to check
  Returns:
    Full path to the blob or None if it cannot be found
  """
  processing_chunk_name = datestamp + '/slices_processing/' + chunk_filename
  if _blob_exists(bucket_name, processing_chunk_name):
    print('Found products blob in slices_processing folder')
    return processing_chunk_name
  else:
    processed_chunk_name = datestamp + '/slices_processed/' + chunk_filename
    print('Looking for {}'.format(processing_chunk_name))
    if _blob_exists(bucket_name, processed_chunk_name):
      print('Found products blob in slices_processed folder')
      return processed_chunk_name
    else:
      print('ERROR: Blob not found')
      return None


def _mc_invoker_worker(service: discovery.Resource, env_info: Dict[str, Any],
                       job_info: Dict[str, Any], task_retries: int):
  """Loads a chunk of products from GCS and sends it to the Content API.

  Args:
    service: Initialized service of a Content API client.
    env_info: GCP configuration extracted from enviroment variables.
    job_info: Job configuration derived from the input_json object.
    task_retries: Number of retries performed in Cloud Tasks.

  Returns:
    None.
  """

  input_json = job_info['input_json']
  datestamp = input_json['date']
  chunk_filename = input_json['child']['file_name']
  # Load filename from GCS
  # Load chunk file
  full_chunk_name = _check_products_blob(datestamp, env_info[BUCKET_NAME],
                                         chunk_filename)

  if full_chunk_name:
    allowed_file_check = re.match('.*\\.(csv|json)[-0-9]+$', full_chunk_name)
  if full_chunk_name and allowed_file_check:
    extension = allowed_file_check.group(1)
    if extension == 'csv':
      raw_products = _read_csv_from_blob(env_info[BUCKET_NAME], full_chunk_name)
      ready_products = _build_products(raw_products, job_info)
    else:
      ready_products = _read_json_from_blob(env_info[BUCKET_NAME],
                                            full_chunk_name)
    result = _upload_products(
        service,
        env_info,
        job_info,
        ready_products,
        task_retries,
        full_chunk_name,
    )
    return result
  else:
    input_json['child']['num_errors'] = input_json['child']['num_rows']
    _send_pubsub_message(env_info[PROJECT_ID], env_info[REPORTING_TOPIC],
                         input_json)
    return 200


def _get_max_attempts(config: Dict[str, Any]) -> int:
  """Retrieves the max attempts from the config.

  Args:
    config: a dictionary with the platform configuration.

  Returns:
    The number of attempts.
  """
  return config['queue_config']['retry_config']['max_attempts']


def _enhance_information_from_json(
    input_json: Dict[str, Any]) -> Tuple[str, str, str]:
  """Extracts additional information from the cloud function request payload.

  Args:
    input_json: The initial cloud function request payload in json format.

  Returns:
    A job configuration object.

  Raises:
    Exception: File name format is missing information.
  """

  # Proposed filename structure:
  # mc_<free-text-without-underscore>_<merchant_id>_<operation>_<crendential>_\
  #    <date>_<channel>-<language>-<country>-<weight_unit>-<currency>

  file_name = re.sub(r'.(csv|txt|tsv)$', '', input_json['parent']['file_name'])
  arr = file_name.split('_')
  if len(arr) < 6:
    raise Exception('File name format is missing information.')

  job_info = {
      'input_json': input_json,
      'merchant_id': arr[2].replace('-', ''),
      'operation': arr[3].lower(),
      'credentials_name': arr[4].lower(),
      'date': arr[5],
      'channel': 'online',
      'content_language': 'es',
      'target_country': 'ES',
      'default_shipping_weight_unit': 'kg',
      'default_currency': 'EUR',
  }

  extra_parameters = arr[6].split('-')
  extra_labels = [
      'channel', 'content_language', 'target_country',
      'default_shipping_weight_unit', 'default_currency'
  ]
  for i in range(len(extra_parameters)):
    if extra_parameters[i]:
      job_info[extra_labels[i]] = extra_parameters[i]

  return job_info


def _get_credentials(credentials_name, config):
  """Gets the credentials from the general configuration.

  Args:
    credentials_name: The credentials name in the general configuration.
    config: The general configuration.

  Returns:
    The credentials required to authenticate to the Content API.

  Raises:
    Exception: Credentials not found.
  """
  if credentials_name not in config['credentials']:
    raise Exception(f'Credentials for {credentials_name} not found.')

  print(f'Using {credentials_name} credentials to authenticate.')
  return config['credentials'][credentials_name]


def initialize_service(auth_config):
  """Initializes and returns a Content API service.

  Args:
    auth_config: The authorization information for the service account.

  Returns:
    A Resource object with methods for interacting with the Content API service.

  Raises:
    Exception: No access to any Merchant Center account.
  """
  credentials = service_account.Credentials.from_service_account_info(
      auth_config, scopes=[CONTENT_API_SCOPE])

  auth_http = google_auth_httplib2.AuthorizedHttp(
      credentials,
      http=http.set_user_agent(http.build_http(), APPLICATION_NAME))

  service = discovery.build(SERVICE_NAME, SERVICE_VERSION, http=auth_http)

  auth_info = service.accounts().authinfo().execute()
  account_ids = auth_info.get('accountIdentifiers')

  if not account_ids:
    raise Exception('The currently authenticated user does not have access to '
                    'any Merchant Center accounts.')

  return service


def _send_products_in_batch(service: discovery.Resource, job_info: Dict[str,
                                                                        Any],
                            products: List[Dict[str, Any]]):
  """Sends products using batches through the Content API.

  Args:
    service: Initialized service of a Content API client.
    job_info: Job configuration derived from the input_json object.
    products: Chunk of products prepared to be uploaded.

  Returns:
    The result of the insert operation.
  """
  merchant_id = job_info['merchant_id']

  batch = {'entries': []}
  counter = 0
  if job_info['operation'] == 'direct':
    batch['entries'] = products
  else:
    for product in products:
      counter += 1
      entry = {
          'batchId': counter,
          'merchantId': merchant_id,
          'method': job_info['operation'],
      }
      if job_info['operation'] == 'delete':
        entry['productId'] = product['productId']
      else:
        entry['product'] = product
      batch['entries'].append(entry)
  request = service.products().custombatch(body=batch)
  response = request.execute()

  return response


def _read_platform_config_from_secret(project_id: str,
                                      secret_id: str) -> Dict[str, Any]:
  """Gets the config for the platform.

  Args:
      project_id: the project name where the secret is defined
      secret_id: string representing the id of the secret with the config

  Returns:
      A dictionary containing the conifguration
  """
  # Create the Secret Manager client.
  client = secretmanager.SecretManagerServiceClient()
  # Build the resource name of the secret version.
  name = f'projects/{project_id}/secrets/{secret_id}/versions/latest'
  # Access the secret version.
  response = client.access_secret_version(request={'name': name})
  payload = response.payload.data.decode('UTF-8')
  data = json.loads(payload)

  return data


def mc_invoker(request):
  """Triggers the upload of a chunk of products to merchant center.

  Args:
    request (flask.Request): HTTP request object.

  Returns:
        The response text or any set of values that can be turned into a
        Response object using
        `make_response
        <http://flask.pocoo.org/docs/1.0/api/#flask.Flask.make_response>`.
  """
  required_elem = [
      'OUTPUT_GCS_BUCKET', 'DEFAULT_GCP_PROJECT', 'STORE_RESPONSE_STATS_TOPIC',
      'DEPLOYMENT_NAME', 'SOLUTION_PREFIX', 'CACHE_TTL_IN_HOURS'
  ]
  if not all(elem in os.environ for elem in required_elem):
    print('Cannot proceed, there are missing input values, '
          'please make sure you set all the environment variables correctly.')
    sys.exit(1)

  env_info = {
      BUCKET_NAME: os.environ['OUTPUT_GCS_BUCKET'],
      PROJECT_ID: os.environ['DEFAULT_GCP_PROJECT'],
      DEPLOYMENT_NAME: os.environ['DEPLOYMENT_NAME'],
      SOLUTION_PREFIX: os.environ['SOLUTION_PREFIX'],
      REPORTING_TOPIC: os.environ['STORE_RESPONSE_STATS_TOPIC'],
      CACHE_TTL_IN_HOURS: int(os.environ['CACHE_TTL_IN_HOURS'])
  }

  config = _read_platform_config_from_secret(
      env_info[PROJECT_ID],
      f'{env_info[DEPLOYMENT_NAME]}_{env_info[SOLUTION_PREFIX]}_mc_config')
  env_info[FULL_PATH_TOPIC] = (f'{env_info[DEPLOYMENT_NAME]}.'
                               f'{env_info[SOLUTION_PREFIX]}.'
                               f'{env_info[REPORTING_TOPIC]}')
  input_json = request.get_json(silent=True)

  task_retries = -1
  try:
    env_info[MAX_ATTEMPTS] = int(_get_max_attempts(config))
    if 'X-Cloudtasks-Taskretrycount' in request.headers:
      task_retries = int(request.headers.get('X-Cloudtasks-Taskretrycount'))
      print('Got {} task retries from Cloud Tasks'.format(task_retries))

    job_info = _enhance_information_from_json(input_json)

    auth_config = _get_credentials(job_info['credentials_name'], config)
    service = initialize_service(auth_config)

    result = _mc_invoker_worker(service, env_info, job_info, task_retries)

    return Response('', result)

  # pylint: disable=broad-except
  except Exception:
    print('ERROR: Unexpected exception raised during the process: ',
          sys.exc_info()[0])
    str_traceback = traceback.format_exc()
    print('Unexpected exception traceback follows:')
    print(str_traceback)

    pubsub_payload = _add_errors_to_input_data(input_json,
                                               input_json['child']['num_rows'])
    _send_pubsub_message(env_info[PROJECT_ID], env_info[FULL_PATH_TOPIC],
                         pubsub_payload)
    # If last try, move blob to /slices_failed
    _mv_blob_if_last_try(task_retries, env_info[MAX_ATTEMPTS], input_json,
                         env_info[BUCKET_NAME])
    return Response('', 500)


def _test_main() -> None:
  """Main function for testing using the command line.

  Args:
    None

  Returns:
    None
  """
  # Replace with your testing JSON
  # pylint: disable=line-too-long
  input_string = (
      ' {"date": "YYYYMMDD", '
      '"target_platform": "mc",'
      '"extra_parameters": ["", "", "", "", ""],'
      '"parent": {"cid": "XXXXXXXXXX", "file_name": "MC_MERCHANT-TEST_11111111_insert_default_YYYYMMDD_online-xx-XX-xx-XXX.csv",'
      '"file_path": "input",'
      '"file_date": "YYYYMMDD",'
      '"total_files": 100,'
      '"total_rows": 25000},'
      '"child": {"file_name": "MC_MERCHANT-TEST_11111111_insert_default_YYYYMMDD_online-xx-XX-xx-XXX.csv---3",'
      '"num_rows": 250}}')

  input_json = json.loads(input_string)

  if not all(elem in os.environ for elem in [
      'OUTPUT_GCS_BUCKET', 'DEFAULT_GCP_PROJECT', 'STORE_RESPONSE_STATS_TOPIC',
      'CACHE_TTL_IN_HOURS'
  ]):
    print('Cannot proceed, there are missing input values, '
          'please make sure you set all the environment variables correctly.')
    sys.exit(1)

  env_info = {
      BUCKET_NAME: os.environ['OUTPUT_GCS_BUCKET'],
      PROJECT_ID: os.environ['DEFAULT_GCP_PROJECT'],
      DEPLOYMENT_NAME: os.environ['DEPLOYMENT_NAME'],
      SOLUTION_PREFIX: os.environ['SOLUTION_PREFIX'],
      REPORTING_TOPIC: os.environ['STORE_RESPONSE_STATS_TOPIC'],
      CACHE_TTL_IN_HOURS: int(os.environ['CACHE_TTL_IN_HOURS'])
  }

  config = _read_platform_config_from_secret(
      env_info[PROJECT_ID],
      f'{env_info[DEPLOYMENT_NAME]}_{env_info[SOLUTION_PREFIX]}_mc_config')
  env_info[FULL_PATH_TOPIC] = (f'{env_info[DEPLOYMENT_NAME]}.'
                               f'{env_info[SOLUTION_PREFIX]}.'
                               f'{env_info[REPORTING_TOPIC]}')

  task_retries = -1

  try:
    job_info = _enhance_information_from_json(input_json)

    auth_config = _get_credentials(job_info['credentials_name'], config)
    service = initialize_service(auth_config)

    result = _mc_invoker_worker(service, env_info, job_info, task_retries)

    return Response('', result)
    # print('Test execution returned: {}'.format(result))
  except Exception:
    print('Unexpected exception raised during the process')
    raise


if __name__ == '__main__':
  _test_main()
